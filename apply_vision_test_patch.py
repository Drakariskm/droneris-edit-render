from pathlib import Path

MAIN = Path("app/main.py")
if not MAIN.exists():
    raise SystemExit("ERROR: app/main.py not found. Run this from the repository root.")

text = MAIN.read_text(encoding="utf-8")

IMPORT_LINE = "from app.vision import build_vision_sample_manifest, VisionSamplerError\n"
ANCHOR_IMPORT = "from fastapi.responses import FileResponse, JSONResponse\n"

if IMPORT_LINE not in text:
    if ANCHOR_IMPORT not in text:
        raise SystemExit("ERROR: expected FastAPI responses import not found; main.py layout differs.")
    text = text.replace(ANCHOR_IMPORT, ANCHOR_IMPORT + IMPORT_LINE, 1)

ENDPOINT = r'''

@app.post("/vision/test")
async def vision_test(video: UploadFile = File(...)) -> JSONResponse:
    # DRONERIS AI VISION DIRECTOR R2 — Phase 1 test only.
    # No OpenAI Vision request is made.
    cleanup_old_jobs()

    ext = Path(video.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v"}:
        raise HTTPException(status_code=415, detail="VIDEO_FORMAT_NOT_SUPPORTED")

    test_id = uuid.uuid4().hex
    test_dir = ROOT / f"vision_test_{test_id}"
    frames_dir = test_dir / "frames"
    source_path = test_dir / "source.mp4"
    test_dir.mkdir(parents=True, exist_ok=False)

    try:
        size = await save_upload(video, source_path)

        manifest = await asyncio.to_thread(
            build_vision_sample_manifest,
            source_path,
            frames_dir,
            frame_count=16,
        )

        frames = manifest.get("frames") or []
        if len(frames) != 16:
            raise HTTPException(
                status_code=500,
                detail=f"FRAME_SAMPLER_COUNT_FAIL:{len(frames)}",
            )

        safe_frames = []
        for frame in frames:
            p = Path(str(frame.get("path") or ""))
            safe_frames.append({
                "index": frame.get("index"),
                "timestampSec": frame.get("timestamp_s"),
                "file": p.name,
                "sizeBytes": p.stat().st_size if p.is_file() else 0,
            })

        video_meta = manifest.get("video") or {}

        return JSONResponse({
            "ok": True,
            "status": "FRAME_SAMPLER_PASS",
            "visionConnected": False,
            "frameCount": len(safe_frames),
            "source": {
                "name": safe_name(video.filename, "source.mp4"),
                "sizeBytes": size,
                "durationSec": video_meta.get("duration_s"),
                "width": video_meta.get("width"),
                "height": video_meta.get("height"),
                "fps": video_meta.get("fps"),
                "codec": video_meta.get("codec"),
            },
            "frames": safe_frames,
        })

    except HTTPException:
        raise
    except VisionSamplerError as e:
        raise HTTPException(
            status_code=422,
            detail=f"VISION_FRAME_SAMPLER_FAILED:{type(e).__name__}:{e}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"VISION_TEST_FAILED:{type(e).__name__}:{e}",
        )
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)
'''

ANCHOR_ENDPOINT = '\n\n@app.get("/api/jobs/{job_id}")\ndef get_job'
if '@app.post("/vision/test")' not in text:
    if ANCHOR_ENDPOINT not in text:
        raise SystemExit("ERROR: expected /api/jobs/{job_id} anchor not found; main.py layout differs.")
    text = text.replace(ANCHOR_ENDPOINT, ENDPOINT + ANCHOR_ENDPOINT, 1)

MAIN.write_text(text, encoding="utf-8")
print("PASS: app/main.py patched with POST /vision/test")
