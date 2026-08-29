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
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

APP_VERSION = "DRONERIS_RENDER_BACKEND_R1.1.0_FREE_SAFE"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
ROOT = Path(os.environ.get("DRONERIS_JOB_ROOT", "/tmp/droneris_render_jobs"))
ROOT.mkdir(parents=True, exist_ok=True)
TTL_SECONDS = int(os.environ.get("DRONERIS_JOB_TTL_SECONDS", "21600"))  # 6 h
MAX_UPLOAD_BYTES = int(os.environ.get("DRONERIS_MAX_UPLOAD_BYTES", str(2 * 1024**3)))  # 2 GiB app guard

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


def do_render(job_dir: Path, plan: dict[str, Any], music_path: Path | None) -> None:
    try:
        write_state(job_dir, renderStatus="PROCESSING", renderProgress=2, error=None)
        source = job_dir / "source.mp4"
        if not source.exists():
            raise RuntimeError("SOURCE_VIDEO_MISSING")
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
            print(f"[DRONERIS] job={job_dir.name} scene={idx}/{count} start", flush=True)
            render_scene(source, scene, clip)
            print(f"[DRONERIS] job={job_dir.name} scene={idx}/{count} done", flush=True)
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
    video: UploadFile = File(...),
    kmz: UploadFile | None = File(None),
    srt: UploadFile | None = File(None),
    manifest: UploadFile | None = File(None),
    source_type: str = Form("REAL_FLIGHT"),
    mission_id: str = Form(""),
    style: str = Form("clean_real_estate"),
) -> JSONResponse:
    cleanup_old_jobs()
    ext = Path(video.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v"}:
        raise HTTPException(status_code=415, detail="VIDEO_FORMAT_NOT_SUPPORTED")
    job_id = uuid.uuid4().hex
    job_dir = ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source_path = job_dir / "source.mp4"
    write_state(job_dir, jobId=job_id, status="UPLOADING", renderStatus="NOT_STARTED", createdAt=time.time())
    try:
        size = await save_upload(video, source_path)
        meta = ffprobe_json(source_path)
        if not meta.get("durationSec"):
            raise HTTPException(status_code=422, detail="VIDEO_DURATION_UNAVAILABLE")
        extras: dict[str, Any] = {}
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
        scenes = build_first_cut(float(meta["durationSec"]))
        state = write_state(
            job_dir,
            status="READY",
            source={"name": safe_name(video.filename, "source.mp4"), "sourceType": source_type, "sizeBytes": size, **meta},
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
            "missionId": mission_id,
            "extras": extras,
            "scenes": scenes,
            "warnings": ["R1_DIRECTOR_BASELINE_SERVER_SIDE", "AI_VISION_NOT_CONNECTED_YET"],
        })
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except subprocess.CalledProcessError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"MEDIA_PROBE_FAILED:{e.stderr[-300:] if e.stderr else ''}")
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"JOB_CREATE_FAILED:{type(e).__name__}:{e}")


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
    return JSONResponse({"ok": True, "jobId": job_id, "renderStatus": "QUEUED"}, status_code=202)


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
    return JSONResponse(status_code=500, content={"ok": False, "detail": f"UNHANDLED:{type(exc).__name__}"})
