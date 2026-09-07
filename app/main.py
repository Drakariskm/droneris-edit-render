from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from openai import OpenAI
from app.director import improve_first_cut_with_ai
from app.vision import build_vision_sample_manifest, analyze_sampled_frames_with_openai
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

APP_VERSION = "DRONERIS_RENDER_BACKEND_R1.2.0_MULTI_SOURCE_M1_FREE_SAFE"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
ROOT = Path(os.environ.get("DRONERIS_JOB_ROOT", "/tmp/droneris_render_jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
TTL_SECONDS = int(os.environ.get("DRONERIS_JOB_TTL_SECONDS", "21600"))  # 6 h
MAX_UPLOAD_BYTES = int(os.environ.get("DRONERIS_MAX_UPLOAD_BYTES", str(2 * 1024**3)))  # per-file guard
MAX_JOB_UPLOAD_BYTES = int(os.environ.get("DRONERIS_MAX_JOB_UPLOAD_BYTES", str(2 * 1024**3)))  # total job guard
MAX_SOURCE_COUNT = max(1, min(10, int(os.environ.get("DRONERIS_MAX_SOURCE_COUNT", "10"))))
VISION_JOB_FRAME_BUDGET = max(16, min(64, int(os.environ.get("DRONERIS_VISION_JOB_FRAME_BUDGET", "32"))))

# Render Free safety profile: keep FFmpeg memory/CPU bounded.
RENDER_WIDTH = int(os.environ.get("DRONERIS_RENDER_WIDTH", "1280"))
RENDER_HEIGHT = int(os.environ.get("DRONERIS_RENDER_HEIGHT", "720"))
RENDER_FPS = int(os.environ.get("DRONERIS_RENDER_FPS", "30"))
FFMPEG_THREADS = max(1, int(os.environ.get("DRONERIS_FFMPEG_THREADS", "1")))
FFMPEG_PRESET = os.environ.get("DRONERIS_FFMPEG_PRESET", "ultrafast")
FFMPEG_CRF = os.environ.get("DRONERIS_FFMPEG_CRF", "24")

origins_raw = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://edit.droneris.tech,http://localhost:4173,http://127.0.0.1:4173",
)
ALLOWED_ORIGINS = [x.strip() for x in origins_raw.split(",") if x.strip()]

app = FastAPI(title="DRONERIS Render Backend", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

RENDER_SEMAPHORE = asyncio.Semaphore(max(1, int(os.environ.get("DRONERIS_MAX_PARALLEL_RENDERS", "1"))))


def run_cmd(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def ffprobe_json(path: Path) -> dict[str, Any]:
    proc = run_cmd([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path)
    ], timeout=60)
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = None
    for candidate in (data.get("format", {}).get("duration"), v.get("duration")):
        try:
            if candidate is not None:
                duration = float(candidate)
                break
        except Exception:
            pass
    fps = None
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate")
    if rate and isinstance(rate, str) and "/" in rate:
        try:
            a, b = rate.split("/", 1)
            fps = float(a) / float(b) if float(b) else None
        except Exception:
            pass
    return {
        "durationSec": duration,
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": fps,
        "codec": v.get("codec_name"),
        "sizeBytes": int(data.get("format", {}).get("size") or 0),
    }


def safe_name(name: str | None, fallback: str) -> str:
    name = Path(name or fallback).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:180] or fallback


async def save_upload(upload: UploadFile, dest: Path) -> int:
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="UPLOAD_TOO_LARGE")
            out.write(chunk)
    await upload.close()
    return total


def state_path(job_dir: Path) -> Path:
    return job_dir / "state.json"


def load_state(job_dir: Path) -> dict[str, Any]:
    p = state_path(job_dir)
    if not p.exists():
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return json.loads(p.read_text("utf-8"))


def write_state(job_dir: Path, **patch: Any) -> dict[str, Any]:
    p = state_path(job_dir)
    state: dict[str, Any] = {}
    if p.exists():
        try:
            state = json.loads(p.read_text("utf-8"))
        except Exception:
            state = {}
    state.update(patch)
    state["updatedAt"] = time.time()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)
    return state


def cleanup_old_jobs() -> None:
    now = time.time()
    if not ROOT.exists():
        return
    for child in ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = state_path(child).stat().st_mtime if state_path(child).exists() else child.stat().st_mtime
            if now - mtime > TTL_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


def parse_kmz_summary(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"readOnly": True, "name": path.name, "waypointCount": None}
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            target = next((n for n in names if n.lower().endswith("waylines.wpml")), None)
            if target is None:
                target = next((n for n in names if n.lower().endswith("template.kml")), None)
            if not target:
                result["warning"] = "KMZ_NO_WAYLINE_DOCUMENT"
                return result
            text = z.read(target).decode("utf-8", errors="ignore")
            result["waypointCount"] = len(re.findall(r"<Placemark(?:\s|>)", text, flags=re.I))
            result["document"] = target
    except Exception as e:
        result["warning"] = f"KMZ_READ_FAIL:{type(e).__name__}"
    return result


def allocate_vision_frame_counts(source_count: int) -> list[int]:
    """Bound total Vision work for multi-source jobs while preserving 16-frame single-source R2."""
    source_count = max(1, min(MAX_SOURCE_COUNT, int(source_count)))
    if source_count == 1:
        return [16]

    # At least two frames/source when possible; never exceed 16/source.
    budget = max(source_count * 2, VISION_JOB_FRAME_BUDGET)
    budget = min(budget, source_count * 16)
    base = max(2, min(16, budget // source_count))
    counts = [base] * source_count
    remaining = max(0, budget - base * source_count)
    i = 0
    while remaining > 0 and any(x < 16 for x in counts):
        if counts[i] < 16:
            counts[i] += 1
            remaining -= 1
        i = (i + 1) % source_count
    return counts


def scene_vision_quality(scene: dict[str, Any], vision: dict[str, Any] | None) -> float:
    """Score a local source scene from Vision evidence without inventing content."""
    base = float(scene.get("score") or 50.0)
    if not isinstance(vision, dict) or vision.get("status") != "VISION_ANALYSIS_PASS":
        return base

    frames = [f for f in (vision.get("frames") or []) if isinstance(f, dict)]
    if not frames:
        return base

    start = float(scene.get("start") or 0.0)
    end = float(scene.get("end") or start)
    mid = (start + end) / 2.0
    inside = []
    for f in frames:
        try:
            ts = float(f.get("timestampSec"))
        except (TypeError, ValueError):
            continue
        if start <= ts <= end:
            inside.append((abs(ts - mid), f))

    if inside:
        evidence = [f for _, f in sorted(inside, key=lambda x: x[0])[:3]]
    else:
        timed = []
        for f in frames:
            try:
                ts = float(f.get("timestampSec"))
            except (TypeError, ValueError):
                continue
            timed.append((abs(ts - mid), f))
        evidence = [f for _, f in sorted(timed, key=lambda x: x[0])[:2]]

    if not evidence:
        return base

    typ = str(scene.get("type") or "").upper()
    values = []
    for f in evidence:
        hero = float(f.get("heroPotential") or 0.0)
        detail = float(f.get("detailPotential") or 0.0)
        movement = float(f.get("movementQuality") or 50.0)
        composition = str(f.get("composition") or "UNKNOWN").upper()
        obstruction = str(f.get("obstruction") or "UNKNOWN").upper()
        comp_score = {"STRONG": 100.0, "GOOD": 85.0, "FAIR": 65.0, "WEAK": 35.0}.get(composition, 50.0)
        obstruction_penalty = {"NONE": 0.0, "LOW": 5.0, "MEDIUM": 18.0, "HIGH": 40.0}.get(obstruction, 10.0)

        if "HERO" in typ:
            visual = 0.55 * hero + 0.30 * comp_score + 0.15 * movement
        elif "DETAIL" in typ or "POI" in typ:
            visual = 0.50 * detail + 0.30 * comp_score + 0.20 * hero
        else:
            visual = 0.50 * movement + 0.30 * comp_score + 0.20 * hero
        values.append(max(0.0, visual - obstruction_penalty))

    visual_avg = sum(values) / len(values)
    source_score = float(vision.get("visionScore") or 0.0)
    return max(0.0, min(100.0, 0.25 * base + 0.60 * visual_avg + 0.15 * source_score))


def build_multisource_shared_cut(source_results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one editorial grammar scene per role from a shared pool of all sources."""
    role_order = [
        "REVEAL",
        "HERO",
        "PRIMARY MOVEMENT",
        "DETAIL / POI",
        "SECONDARY MOVEMENT",
        "FINAL HERO",
        "EXIT",
    ]
    pool: list[dict[str, Any]] = []
    for source in source_results:
        source_id = str(source["sourceId"])
        source_name = str(source.get("name") or source_id)
        vision = source.get("vision")
        for local_scene in source.get("scenes") or []:
            candidate = dict(local_scene)
            candidate["sourceId"] = source_id
            candidate["sourceName"] = source_name
            candidate["sourceSceneId"] = local_scene.get("id")
            candidate["selectionQuality"] = round(scene_vision_quality(local_scene, vision), 2)
            pool.append(candidate)

    if not pool:
        raise RuntimeError("MULTI_SOURCE_SHARED_POOL_EMPTY")

    selected: list[dict[str, Any]] = []
    source_use: dict[str, int] = {}
    last_source = ""

    for role in role_order:
        candidates = [c for c in pool if str(c.get("type") or "").upper() == role]
        if not candidates:
            continue

        def rank(c: dict[str, Any]) -> float:
            sid = str(c.get("sourceId") or "")
            diversity_bonus = 7.0 if source_use.get(sid, 0) == 0 else max(0.0, 3.0 - source_use.get(sid, 0))
            repeat_penalty = 4.0 if sid == last_source else 0.0
            return float(c.get("selectionQuality") or 0.0) + diversity_bonus - repeat_penalty

        chosen = max(candidates, key=rank)
        chosen = dict(chosen)
        chosen["id"] = len(selected) + 1
        chosen["revision"] = "MULTI_SOURCE_SHARED_POOL"
        chosen["directorReason"] = (
            f"Shared-pool {role} selected from {chosen.get('sourceId')} "
            f"(quality {float(chosen.get('selectionQuality') or 0):.1f})."
        )
        selected.append(chosen)
        sid = str(chosen.get("sourceId") or "")
        source_use[sid] = source_use.get(sid, 0) + 1
        last_source = sid

    if len(selected) < 2:
        raise RuntimeError("MULTI_SOURCE_SHARED_POOL_TOO_SPARSE")

    return selected, {
        "enabled": True,
        "mode": "VISION_SCORED_SHARED_POOL_R1",
        "sourceCount": len(source_results),
        "candidateCount": len(pool),
        "selectedCount": len(selected),
        "selectedBySource": source_use,
        "visionFrameBudget": VISION_JOB_FRAME_BUDGET,
    }


def build_first_cut(duration: float) -> list[dict[str, Any]]:
    # Deterministic R1 Director baseline. It deliberately does NOT alter the source mission/Core.
    duration = max(1.0, float(duration))
    target = min(75.0, max(10.0, duration * 0.90)) if duration < 83.34 else 75.0
    labels = [
        ("Reveal", "REVEAL", 0.075, 8.0, 92),
        ("Hero front", "HERO", 0.210, 10.0, 96),
        ("Primary movement", "PRIMARY MOVEMENT", 0.370, 13.0, 91),
        ("Detail / POI", "DETAIL / POI", 0.515, 9.0, 88),
        ("Secondary movement", "SECONDARY MOVEMENT", 0.670, 12.0, 90),
        ("Final hero", "FINAL HERO", 0.830, 11.0, 95),
        ("Exit / pull-away", "EXIT", 0.945, 12.0, 89),
    ]
    scale = target / 75.0
    scenes: list[dict[str, Any]] = []
    for i, (label, typ, center_ratio, base_len, score) in enumerate(labels, 1):
        length = max(0.6, base_len * scale)
        center = duration * center_ratio
        start = max(0.0, min(duration - length, center - length / 2))
        end = min(duration, start + length)
        scenes.append({
            "id": i,
            "label": label,
            "type": typ,
            "start": round(start, 3),
            "end": round(end, 3),
            "score": score,
            "enabled": True,
            "speed": 1.0,
            "revision": "AI",
            "corrections": [],
        })
    return scenes


def zoom_factor(scene: dict[str, Any]) -> float:
    factor = 1.0
    for c in scene.get("corrections") or []:
        if str(c.get("action", "")).lower() != "zoom":
            continue
        level = str(c.get("level", "SREDNJE")).upper()
        factor = max(factor, {"BLAGO": 1.06, "SREDNJE": 1.10, "JAKO": 1.15}.get(level, 1.10))
    return factor


def render_scene(source: Path, scene: dict[str, Any], output: Path) -> None:
    start = max(0.0, float(scene.get("start", 0)))
    end = max(start + 0.05, float(scene.get("end", start + 1)))
    duration = max(0.05, end - start)
    speed = min(2.0, max(0.5, float(scene.get("speed", 1.0) or 1.0)))
    zoom = zoom_factor(scene)

    w, h = RENDER_WIDTH, RENDER_HEIGHT
    filters = [
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=fast_bilinear",
        f"crop={w}:{h}",
    ]
    if zoom > 1.0001:
        zw = int(round(w * zoom / 2) * 2)
        zh = int(round(h * zoom / 2) * 2)
        filters += [f"scale={zw}:{zh}:flags=fast_bilinear", f"crop={w}:{h}"]
    if abs(speed - 1.0) > 1e-3:
        filters.append(f"setpts=PTS/{speed:.6f}")
    filters.append(f"fps={RENDER_FPS}")

    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-threads", str(FFMPEG_THREADS),
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        "-filter_threads", "1", "-filter_complex_threads", "1",
        "-map", "0:v:0", "-an", "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", str(FFMPEG_CRF),
        "-threads", str(FFMPEG_THREADS),
        "-x264-params", f"threads={FFMPEG_THREADS}:lookahead_threads=1:sliced_threads=0",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]
    run_cmd(args, timeout=1800)


def assemble_clips(clips: list[Path], output: Path, work_dir: Path) -> None:
    concat_file = work_dir / "concat.txt"
    concat_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in clips) + "\n", "utf-8")
    run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", "-movflags", "+faststart", str(output)
    ], timeout=1800)


def add_music(video: Path, music: Path, output: Path) -> None:
    meta = ffprobe_json(video)
    dur = float(meta.get("durationSec") or 0)
    if dur <= 0:
        raise RuntimeError("FINAL_DURATION_UNKNOWN")
    fade_out_start = max(0.0, dur - 2.0)
    af = f"atrim=0:{dur:.3f},afade=t=in:st=0:d=1.0,afade=t=out:st={fade_out_start:.3f}:d=2.0,volume=0.78"
    run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-stream_loop", "-1", "-i", str(music),
        "-filter_complex", f"[1:a]{af}[music]",
        "-map", "0:v:0", "-map", "[music]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", str(output)
    ], timeout=1800)


def resolve_scene_source(job_dir: Path, state: dict[str, Any], scene: dict[str, Any]) -> Path:
    sources = state.get("sources") or []
    if sources:
        source_id = str(scene.get("sourceId") or "").strip()
        if len(sources) > 1 and not source_id:
            raise RuntimeError("SCENE_SOURCE_ID_REQUIRED_FOR_MULTI_SOURCE")
        if not source_id:
            source_id = str((state.get("source") or {}).get("sourceId") or sources[0].get("sourceId") or "SRC_01")

        source_meta = next((x for x in sources if str(x.get("sourceId")) == source_id), None)
        if source_meta is None:
            raise RuntimeError(f"SCENE_SOURCE_ID_UNKNOWN:{source_id}")
        storage_name = Path(str(source_meta.get("storageName") or "")).name
        source_path = job_dir / "sources" / storage_name
        if not source_path.exists():
            raise RuntimeError(f"SOURCE_VIDEO_MISSING:{source_id}")
        return source_path

    # Backward compatibility for jobs created by R1.1.3 and older.
    legacy = job_dir / "source.mp4"
    if not legacy.exists():
        raise RuntimeError("SOURCE_VIDEO_MISSING")
    return legacy


def do_render(job_dir: Path, plan: dict[str, Any], music_path: Path | None) -> None:
    try:
        write_state(job_dir, renderStatus="PROCESSING", renderProgress=2, error=None)
        state = load_state(job_dir)
        print(f"[DRONERIS] render start job={job_dir.name} profile={RENDER_WIDTH}x{RENDER_HEIGHT}@{RENDER_FPS} threads={FFMPEG_THREADS}", flush=True)
        scenes = [s for s in (plan.get("scenes") or []) if s.get("enabled", True)]
        if not scenes:
            raise RuntimeError("NO_ENABLED_SCENES")
        work = job_dir / "render_work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        clips: list[Path] = []
        count = len(scenes)
        for idx, scene in enumerate(scenes, 1):
            clip = work / f"clip_{idx:03d}.mp4"
            source = resolve_scene_source(job_dir, state, scene)
            source_id = str(scene.get("sourceId") or "LEGACY")
            print(f"[DRONERIS] job={job_dir.name} scene={idx}/{count} source={source_id} start", flush=True)
            render_scene(source, scene, clip)
            print(f"[DRONERIS] job={job_dir.name} scene={idx}/{count} source={source_id} done", flush=True)
            clips.append(clip)
            write_state(job_dir, renderProgress=int(5 + 70 * idx / count))
        assembled = work / "assembled.mp4"
        assemble_clips(clips, assembled, work)
        write_state(job_dir, renderProgress=82)
        final = job_dir / "DRONERIS_FINAL.mp4"
        if music_path and music_path.exists():
            add_music(assembled, music_path, final)
        else:
            shutil.copy2(assembled, final)
        meta = ffprobe_json(final)
        print(f"[DRONERIS] render complete job={job_dir.name}", flush=True)
        write_state(
            job_dir,
            renderStatus="COMPLETED",
            renderProgress=100,
            final={"name": final.name, "durationSec": meta.get("durationSec"), "sizeBytes": final.stat().st_size},
        )
    except Exception as e:
        print(f"[DRONERIS] render failed job={job_dir.name} error={type(e).__name__}:{e}", flush=True)
        write_state(job_dir, renderStatus="FAILED", renderProgress=0, error=f"{type(e).__name__}: {e}")


async def render_worker(job_dir: Path, plan: dict[str, Any], music_path: Path | None) -> None:
    async with RENDER_SEMAPHORE:
        await asyncio.to_thread(do_render, job_dir, plan, music_path)


@app.on_event("startup")
def on_startup() -> None:
    cleanup_old_jobs()


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "DRONERIS Render Backend",
        "version": APP_VERSION,
        "status": "ONLINE",
        "coreIsolation": "READ_ONLY_NO_MISSION_WRITEBACK",
        "renderProfile": {
            "width": RENDER_WIDTH, "height": RENDER_HEIGHT, "fps": RENDER_FPS,
            "ffmpegThreads": FFMPEG_THREADS, "preset": FFMPEG_PRESET, "crf": FFMPEG_CRF,
        },
        "features": {
            "multiSource": True,
            "maxSources": MAX_SOURCE_COUNT,
            "sceneSourceId": True,
            "musicUpload": True,
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        ffmpeg = run_cmd(["ffmpeg", "-version"], timeout=10).stdout.splitlines()[0]
        ffprobe = run_cmd(["ffprobe", "-version"], timeout=10).stdout.splitlines()[0]
        ok = True
    except Exception as e:
        ffmpeg = ffprobe = f"ERROR:{e}"
        ok = False
    return {"ok": ok, "version": APP_VERSION, "ffmpeg": ffmpeg, "ffprobe": ffprobe}


@app.post("/api/jobs")
async def create_job(
    video: UploadFile | None = File(None),
    videos: list[UploadFile] | None = File(None),
    kmz: UploadFile | None = File(None),
    srt: UploadFile | None = File(None),
    manifest: UploadFile | None = File(None),
    source_type: str = Form("REAL_FLIGHT"),
    mission_id: str = Form(""),
    style: str = Form("clean_real_estate"),
) -> JSONResponse:
    cleanup_old_jobs()

    uploads: list[UploadFile] = []
    if video is not None and video.filename:
        uploads.append(video)
    for item in videos or []:
        if item is not None and item.filename:
            uploads.append(item)

    if not uploads:
        raise HTTPException(status_code=422, detail="VIDEO_REQUIRED")
    if len(uploads) > MAX_SOURCE_COUNT:
        raise HTTPException(status_code=422, detail=f"TOO_MANY_VIDEO_SOURCES_MAX_{MAX_SOURCE_COUNT}")

    for upload in uploads:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in {".mp4", ".mov", ".m4v"}:
            raise HTTPException(status_code=415, detail=f"VIDEO_FORMAT_NOT_SUPPORTED:{safe_name(upload.filename, 'video')}")

    job_id = uuid.uuid4().hex
    job_dir = ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    sources_dir = job_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    write_state(
        job_dir,
        jobId=job_id,
        status="UPLOADING",
        renderStatus="NOT_STARTED",
        createdAt=time.time(),
    )

    try:
        source_metas: list[dict[str, Any]] = []
        total_upload_bytes = 0

        for index, upload in enumerate(uploads, 1):
            source_id = f"SRC_{index:02d}"
            original_name = safe_name(upload.filename, f"source_{index:02d}.mp4")
            ext = Path(original_name).suffix.lower() or ".mp4"
            storage_name = f"{source_id}{ext}"
            source_path = sources_dir / storage_name

            size = await save_upload(upload, source_path)
            total_upload_bytes += size
            if total_upload_bytes > MAX_JOB_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="JOB_UPLOAD_TOO_LARGE")

            meta = ffprobe_json(source_path)
            if not meta.get("durationSec"):
                raise HTTPException(status_code=422, detail=f"VIDEO_DURATION_UNAVAILABLE:{source_id}")

            source_metas.append({
                "sourceId": source_id,
                "name": original_name,
                "storageName": storage_name,
                "sourceType": source_type,
                "primary": index == 1,
                "sizeBytes": size,
                **meta,
            })

        extras: dict[str, Any] = {}
        warnings = ["R1_DIRECTOR_BASELINE_SERVER_SIDE"]

        if kmz is not None and kmz.filename:
            kp = job_dir / safe_name(kmz.filename, "mission.kmz")
            await save_upload(kmz, kp)
            extras["kmz"] = parse_kmz_summary(kp)

        if srt is not None and srt.filename:
            sp = job_dir / safe_name(srt.filename, "telemetry.srt")
            await save_upload(srt, sp)
            extras["srt"] = {"name": sp.name, "readOnly": True}

        if manifest is not None and manifest.filename:
            mp = job_dir / safe_name(manifest.filename, "manifest.json")
            await save_upload(manifest, mp)
            extras["manifest"] = {"name": mp.name, "readOnly": True}

        # Preserve the exact single-source R1.1.3 Vision + Director contract.
        if len(source_metas) == 1:
            meta = source_metas[0]
            source_path = sources_dir / str(meta["storageName"])
            warnings.append("AI_VISION_NOT_CONNECTED_YET")

            try:
                vision_frames_dir = job_dir / "vision_frames"
                vision_manifest = await asyncio.to_thread(
                    build_vision_sample_manifest,
                    source_path,
                    vision_frames_dir,
                    frame_count=16,
                )

                sampled_frames = vision_manifest.get("frames") or []
                safe_frames: list[dict[str, Any]] = []
                for frame in sampled_frames:
                    frame_path = Path(str(frame.get("path") or ""))
                    safe_frames.append({
                        "index": frame.get("index"),
                        "timestampSec": frame.get("timestamp_s"),
                        "file": frame_path.name,
                        "sizeBytes": frame_path.stat().st_size if frame_path.is_file() else 0,
                    })

                sampler_ok = (
                    vision_manifest.get("status") == "FRAME_SAMPLER_PASS"
                    and len(safe_frames) == 16
                    and all(int(f.get("sizeBytes") or 0) > 0 for f in safe_frames)
                )
                extras["aiVisionSampler"] = {
                    "enabled": True,
                    "status": "FRAME_SAMPLER_PASS" if sampler_ok else "FRAME_SAMPLER_INCOMPLETE",
                    "version": vision_manifest.get("version"),
                    "frameCount": len(safe_frames),
                    "visionConnected": False,
                    "frames": safe_frames,
                }

                if sampler_ok:
                    warnings.append("VISION_FRAME_SAMPLER_PASS")
                    try:
                        vision_analysis = await asyncio.to_thread(
                            analyze_sampled_frames_with_openai,
                            openai_client=openai_client,
                            model=OPENAI_MODEL,
                            sample_manifest=vision_manifest,
                            source_type=source_type,
                            style=style,
                        )
                        extras["aiVision"] = vision_analysis
                        if vision_analysis.get("status") == "VISION_ANALYSIS_PASS":
                            warnings = [w for w in warnings if w != "AI_VISION_NOT_CONNECTED_YET"]
                            warnings.append("VISION_ANALYSIS_PASS")
                        else:
                            warnings.append(str(vision_analysis.get("status") or "VISION_ANALYSIS_WARNING"))
                    except Exception as vision_error:
                        extras["aiVision"] = {
                            "enabled": False,
                            "status": "VISION_ANALYSIS_WARNING",
                            "visionConnected": False,
                            "warning": f"{type(vision_error).__name__}:{vision_error}",
                        }
                        warnings.append("VISION_ANALYSIS_WARNING")
                else:
                    warnings.append("VISION_FRAME_SAMPLER_INCOMPLETE")
            except Exception as e:
                extras["aiVisionSampler"] = {
                    "enabled": False,
                    "status": "FRAME_SAMPLER_WARNING",
                    "frameCount": 0,
                    "visionConnected": False,
                    "warning": f"{type(e).__name__}:{e}",
                }
                warnings.append("VISION_FRAME_SAMPLER_WARNING")

            scenes = build_first_cut(float(meta["durationSec"]))
            scenes, ai_director = improve_first_cut_with_ai(
                openai_client=openai_client,
                model=OPENAI_MODEL,
                duration=float(meta["durationSec"]),
                scenes=scenes,
                source_type=source_type,
                style=style,
                vision_analysis=extras.get("aiVision"),
            )
            for scene in scenes:
                scene["sourceId"] = str(meta["sourceId"])
            extras["aiDirector"] = ai_director

        else:
            # Multi-source M1: bounded per-source Vision evidence -> one shared shot pool.
            frame_counts = allocate_vision_frame_counts(len(source_metas))
            multi_results: list[dict[str, Any]] = []
            source_vision_extras: list[dict[str, Any]] = []

            for source_meta, frame_count in zip(source_metas, frame_counts):
                source_id = str(source_meta["sourceId"])
                source_path = sources_dir / str(source_meta["storageName"])
                vision_manifest: dict[str, Any] | None = None
                vision_analysis: dict[str, Any] | None = None
                source_warning: str | None = None

                try:
                    vision_frames_dir = job_dir / "vision_frames" / source_id
                    vision_manifest = await asyncio.to_thread(
                        build_vision_sample_manifest,
                        source_path,
                        vision_frames_dir,
                        frame_count=frame_count,
                    )
                    try:
                        vision_analysis = await asyncio.to_thread(
                            analyze_sampled_frames_with_openai,
                            openai_client=openai_client,
                            model=OPENAI_MODEL,
                            sample_manifest=vision_manifest,
                            source_type=source_type,
                            style=style,
                        )
                    except Exception as vision_error:
                        source_warning = f"VISION_ANALYSIS_WARNING:{type(vision_error).__name__}:{vision_error}"
                except Exception as sampler_error:
                    source_warning = f"FRAME_SAMPLER_WARNING:{type(sampler_error).__name__}:{sampler_error}"

                local_scenes = build_first_cut(float(source_meta["durationSec"]))
                for scene in local_scenes:
                    scene["sourceId"] = source_id

                multi_results.append({
                    "sourceId": source_id,
                    "name": source_meta.get("name"),
                    "scenes": local_scenes,
                    "vision": vision_analysis,
                })
                source_vision_extras.append({
                    "sourceId": source_id,
                    "frameCount": frame_count,
                    "samplerStatus": (vision_manifest or {}).get("status") if vision_manifest else "WARNING",
                    "visionStatus": (vision_analysis or {}).get("status") if vision_analysis else "VISION_NOT_AVAILABLE",
                    "visionScore": (vision_analysis or {}).get("visionScore") if vision_analysis else None,
                    "warning": source_warning,
                })

            scenes, multi_director = build_multisource_shared_cut(multi_results)
            extras["multiSource"] = multi_director
            extras["multiSourceVision"] = source_vision_extras
            warnings.append("MULTI_SOURCE_SHARED_POOL_PASS")
            if any(x.get("visionStatus") == "VISION_ANALYSIS_PASS" for x in source_vision_extras):
                warnings.append("MULTI_SOURCE_VISION_PARTIAL_OR_FULL_PASS")
            else:
                warnings.append("MULTI_SOURCE_VISION_FALLBACK")

        primary_source = dict(source_metas[0])
        state = write_state(
            job_dir,
            status="READY",
            source=primary_source,
            sources=source_metas,
            sourceCount=len(source_metas),
            totalUploadBytes=total_upload_bytes,
            missionId=mission_id,
            style=style,
            extras=extras,
            scenes=scenes,
            coreIsolation="READ_ONLY_NO_MISSION_WRITEBACK",
        )

        return JSONResponse({
            "ok": True,
            "jobId": job_id,
            "status": state["status"],
            "source": state["source"],
            "sources": source_metas,
            "sourceCount": len(source_metas),
            "missionId": mission_id,
            "extras": extras,
            "scenes": scenes,
            "warnings": warnings,
        })

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except subprocess.CalledProcessError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail=f"MEDIA_PROBE_FAILED:{e.stderr[-300:] if e.stderr else ''}",
        )
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"JOB_CREATE_FAILED:{type(e).__name__}:{e}",
        )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(status_code=400, detail="INVALID_JOB_ID")
    job_dir = ROOT / job_id
    return load_state(job_dir)


@app.post("/api/jobs/{job_id}/render")
async def start_render(
    job_id: str,
    background_tasks: BackgroundTasks,
    plan: str = Form(...),
    music: UploadFile | None = File(None),
) -> JSONResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(status_code=400, detail="INVALID_JOB_ID")

    job_dir = ROOT / job_id
    state = load_state(job_dir)

    if state.get("renderStatus") in {"QUEUED", "PROCESSING"}:
        raise HTTPException(status_code=409, detail="RENDER_ALREADY_RUNNING")

    try:
        plan_obj = json.loads(plan)
    except Exception:
        raise HTTPException(status_code=400, detail="INVALID_EDIT_PLAN_JSON")

    if not isinstance(plan_obj, dict) or not isinstance(plan_obj.get("scenes"), list):
        raise HTTPException(status_code=400, detail="EDIT_PLAN_SCENES_REQUIRED")

    music_path: Path | None = None
    if music is not None and music.filename:
        music_path = job_dir / safe_name(music.filename, "music.mp3")
        await save_upload(music, music_path)

    write_state(job_dir, renderStatus="QUEUED", renderProgress=1, error=None)
    background_tasks.add_task(render_worker, job_dir, plan_obj, music_path)

    return JSONResponse(
        {"ok": True, "jobId": job_id, "renderStatus": "QUEUED"},
        status_code=202,
    )


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(status_code=400, detail="INVALID_JOB_ID")

    job_dir = ROOT / job_id
    state = load_state(job_dir)

    if state.get("renderStatus") != "COMPLETED":
        raise HTTPException(status_code=409, detail="RENDER_NOT_COMPLETED")

    final = job_dir / "DRONERIS_FINAL.mp4"

    if not final.exists():
        raise HTTPException(status_code=404, detail="FINAL_VIDEO_MISSING")

    return FileResponse(
        final,
        media_type="video/mp4",
        filename=f"{state.get('missionId') or 'DRONERIS'}_FINAL.mp4",
        headers={"Cache-Control": "private, no-store"},
    )


@app.post("/api/ai/pilot")
async def ai_pilot(request: Request) -> JSONResponse:
    if openai_client is None:
        raise HTTPException(status_code=503, detail="OPENAI_NOT_CONFIGURED")

    body = await request.json()

    question = str(body.get("question") or "").strip()
    selected_shot = body.get("selectedShot")
    timeline = body.get("timeline") or []

    if not question:
        raise HTTPException(status_code=400, detail="QUESTION_REQUIRED")

    system_prompt = """
You are DRONERIS AI PILOT, an AI film-editing assistant for drone real-estate videos.

Your job is to interpret the user's editing request and return a concise editing recommendation.

Important:
- Never modify the original source video.
- Prefer simple edit actions.
- Supported actions are:
  SHORTEN
  EXTEND
  SPEED
  ZOOM
  NONE
- If a selected shot exists, assume the request refers to that shot unless the user clearly refers to the whole film.
- Keep the response concise.
- Return JSON only.
"""

    user_payload = {
        "question": question,
        "selectedShot": selected_shot,
        "timeline": timeline,
    }

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        )

        text = response.output_text.strip()

        try:
            result = json.loads(text)
        except Exception:
            result = {
                "reply": text,
                "action": "NONE",
            }

        return JSONResponse({
            "ok": True,
            "result": result,
        })

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"OPENAI_PILOT_FAILED:{type(e).__name__}:{e}",
        )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": f"UNHANDLED:{type(exc).__name__}"},
    )
