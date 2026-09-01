"""
DRONERIS AI VISION DIRECTOR R2
Phase 1: deterministic FFmpeg frame sampler.
Phase 2: OpenAI Vision analysis of the 16 sampled frames.

Contract:
    MP4
      -> ffprobe metadata
      -> 16 evenly distributed JPEG frames
      -> OpenAI Vision structured analysis

Important:
- Existing sampler API is preserved.
- AI EDIT DIRECTOR R1 is not modified here.
- This module only analyzes; it does not change edit scenes.
"""

from __future__ import annotations

import base64
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


DRONERIS_VISION_VERSION = "DRONERIS_AI_VISION_DIRECTOR_R2_2026_09_01"
DRONERIS_VISION_SAMPLER_VERSION = "DRONERIS_AI_VISION_DIRECTOR_R2_FRAME_SAMPLER_2026_09_01"

DEFAULT_FRAME_COUNT = 16
DEFAULT_JPEG_QUALITY = 2
DEFAULT_SCALE_WIDTH = 1280
DEFAULT_IMAGE_DETAIL = "low"
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


class VisionSamplerError(RuntimeError):
    """Base error for the DRONERIS Vision frame sampler."""


class FFmpegNotFoundError(VisionSamplerError):
    """Raised when ffmpeg or ffprobe cannot be found."""


class VideoProbeError(VisionSamplerError):
    """Raised when video metadata cannot be read reliably."""


class FrameSamplingError(VisionSamplerError):
    """Raised when one or more requested frames cannot be extracted."""


class VisionAnalysisError(RuntimeError):
    """Raised when OpenAI Vision analysis cannot be completed reliably."""


@dataclass(frozen=True)
class VideoInfo:
    path: str
    duration_s: float
    width: int
    height: int
    fps: Optional[float]
    codec: Optional[str]
    container: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SampledFrame:
    index: int
    timestamp_s: float
    path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FrameSampleResult:
    version: str
    source: str
    frame_count: int
    video: VideoInfo
    frames: Sequence[SampledFrame]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "frameCount": self.frame_count,
            "video": self.video.to_dict(),
            "frames": [frame.to_dict() for frame in self.frames],
        }


def _which(binary: str) -> str:
    resolved = shutil.which(binary)
    if not resolved:
        raise FFmpegNotFoundError(
            f"{binary} was not found in PATH. Install FFmpeg and ensure "
            f"both ffmpeg and ffprobe are available."
        )
    return resolved


def _run(
    args: Sequence[str],
    *,
    timeout_s: int = 60,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise VisionSamplerError(
            f"Command timed out after {timeout_s}s: {' '.join(args[:3])} ..."
        ) from exc
    except OSError as exc:
        raise VisionSamplerError(f"Unable to execute command: {exc}") from exc


def _parse_fraction(value: Optional[str]) -> Optional[float]:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        return float(value)
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def probe_video(video_path: Union[str, Path]) -> VideoInfo:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise VideoProbeError(f"Video file does not exist: {path}")

    ffprobe = _which("ffprobe")

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries",
        "format=duration,format_name:"
        "stream=index,codec_type,codec_name,width,height,duration,"
        "avg_frame_rate,r_frame_rate",
        "-of", "json",
        str(path),
    ]
    proc = _run(cmd, timeout_s=30)
    if proc.returncode != 0:
        raise VideoProbeError(
            f"ffprobe failed for {path.name}: {proc.stderr.strip() or 'unknown error'}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VideoProbeError("ffprobe returned invalid JSON.") from exc

    streams = payload.get("streams") or []
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise VideoProbeError("No video stream found.")

    format_info = payload.get("format") or {}

    duration_s = None
    for candidate in (format_info.get("duration"), video_stream.get("duration")):
        try:
            value = float(candidate)
            if math.isfinite(value) and value > 0:
                duration_s = value
                break
        except (TypeError, ValueError):
            continue

    if duration_s is None:
        raise VideoProbeError("Unable to determine a positive video duration.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise VideoProbeError("Unable to determine valid video dimensions.")

    fps = (
        _parse_fraction(video_stream.get("avg_frame_rate"))
        or _parse_fraction(video_stream.get("r_frame_rate"))
    )

    return VideoInfo(
        path=str(path),
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        codec=video_stream.get("codec_name"),
        container=format_info.get("format_name"),
    )


def evenly_spaced_timestamps(
    duration_s: float,
    frame_count: int = DEFAULT_FRAME_COUNT,
) -> List[float]:
    """
    Divide the full duration into N equal bins and sample the center of each bin.
    """
    if frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be a positive finite number")

    segment = duration_s / frame_count
    eof_guard = min(0.001, duration_s * 0.001)
    last_safe = max(0.0, duration_s - eof_guard)

    timestamps: List[float] = []
    for i in range(frame_count):
        ts = (i + 0.5) * segment
        ts = min(max(0.0, ts), last_safe)
        timestamps.append(round(ts, 6))

    return timestamps


def _extract_frame(
    video_path: Path,
    timestamp_s: float,
    output_path: Path,
    *,
    scale_width: Optional[int],
    jpeg_quality: int,
    timeout_s: int,
) -> None:
    ffmpeg = _which("ffmpeg")

    vf: List[str] = []
    if scale_width:
        vf.append(f"scale={int(scale_width)}:-2")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{timestamp_s:.6f}",
        "-i", str(video_path),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-an",
        "-sn",
    ]

    if vf:
        cmd += ["-vf", ",".join(vf)]

    cmd += [
        "-q:v", str(int(jpeg_quality)),
        "-y",
        str(output_path),
    ]

    proc = _run(cmd, timeout_s=timeout_s)

    if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        raise FrameSamplingError(
            f"Failed to extract frame at {timestamp_s:.3f}s: "
            f"{proc.stderr.strip() or 'ffmpeg produced no frame'}"
        )


def sample_frames(
    video_path: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    *,
    frame_count: int = DEFAULT_FRAME_COUNT,
    scale_width: Optional[int] = DEFAULT_SCALE_WIDTH,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    overwrite: bool = True,
    timeout_per_frame_s: int = 30,
) -> FrameSampleResult:
    video = probe_video(video_path)
    source = Path(video.path)

    if frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")
    if jpeg_quality < 1 or jpeg_quality > 31:
        raise ValueError("jpeg_quality must be between 1 and 31")
    if scale_width is not None and scale_width <= 0:
        raise ValueError("scale_width must be positive or None")

    if output_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="droneris_vision_frames_"))
    else:
        out_dir = Path(output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

    timestamps = evenly_spaced_timestamps(video.duration_s, frame_count)
    frames: List[SampledFrame] = []

    for index, timestamp_s in enumerate(timestamps, start=1):
        output_path = out_dir / f"frame_{index:02d}_{timestamp_s:010.3f}s.jpg"

        if output_path.exists() and not overwrite:
            if output_path.stat().st_size == 0:
                raise FrameSamplingError(f"Existing frame is empty: {output_path}")
        else:
            _extract_frame(
                source,
                timestamp_s,
                output_path,
                scale_width=scale_width,
                jpeg_quality=jpeg_quality,
                timeout_s=timeout_per_frame_s,
            )

        frames.append(
            SampledFrame(
                index=index,
                timestamp_s=timestamp_s,
                path=str(output_path),
            )
        )

    if len(frames) != frame_count:
        raise FrameSamplingError(
            f"Expected {frame_count} frames but produced {len(frames)}."
        )

    return FrameSampleResult(
        version=DRONERIS_VISION_SAMPLER_VERSION,
        source=str(source),
        frame_count=frame_count,
        video=video,
        frames=tuple(frames),
    )


def build_vision_sample_manifest(
    video_path: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    *,
    frame_count: int = DEFAULT_FRAME_COUNT,
) -> Dict[str, Any]:
    """
    Existing Phase 1 entry point. Preserved for backward compatibility.
    No OpenAI request is made here.
    """
    result = sample_frames(
        video_path=video_path,
        output_dir=output_dir,
        frame_count=frame_count,
    )
    payload = result.to_dict()
    payload["visionConnected"] = False
    payload["status"] = "FRAME_SAMPLER_PASS"
    return payload


def _image_to_data_url(path: Union[str, Path]) -> str:
    p = Path(path)
    if not p.is_file() or p.stat().st_size <= 0:
        raise VisionAnalysisError(f"Vision frame is missing or empty: {p}")
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise VisionAnalysisError("OpenAI Vision returned empty output.")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Defensive fallback for a model response wrapped in a markdown code fence.
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise VisionAnalysisError("OpenAI Vision output was not valid JSON.")


def _clamp_number(value: Any, low: float, high: float, default: float) -> float:
    try:
        x = float(value)
        if not math.isfinite(x):
            return default
        return max(low, min(high, x))
    except (TypeError, ValueError):
        return default


def _normalize_vision_result(
    result: Dict[str, Any],
    *,
    expected_frames: Sequence[Dict[str, Any]],
    model: str,
) -> Dict[str, Any]:
    by_index: Dict[int, Dict[str, Any]] = {}
    for item in result.get("frames") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = item

    normalized_frames: List[Dict[str, Any]] = []

    for expected in expected_frames:
        idx = int(expected["index"])
        ts = float(expected["timestampSec"])
        item = by_index.get(idx, {})

        recommended_use = str(item.get("recommendedUse") or "NONE").upper().strip()
        if recommended_use not in {
            "REVEAL",
            "HERO",
            "PRIMARY_MOVEMENT",
            "DETAIL",
            "SECONDARY_MOVEMENT",
            "FINAL_HERO",
            "EXIT",
            "CONTEXT",
            "NONE",
        }:
            recommended_use = "NONE"

        composition = str(item.get("composition") or "UNKNOWN").upper().strip()
        if composition not in {"STRONG", "GOOD", "FAIR", "WEAK", "UNKNOWN"}:
            composition = "UNKNOWN"

        obstruction = str(item.get("obstruction") or "UNKNOWN").upper().strip()
        if obstruction not in {"NONE", "LOW", "MEDIUM", "HIGH", "UNKNOWN"}:
            obstruction = "UNKNOWN"

        normalized_frames.append({
            "index": idx,
            "timestampSec": round(ts, 3),
            "subjectVisible": bool(item.get("subjectVisible", False)),
            "subjectOccupancy": round(
                _clamp_number(item.get("subjectOccupancy"), 0.0, 1.0, 0.0),
                3,
            ),
            "composition": composition,
            "obstruction": obstruction,
            "heroPotential": int(round(
                _clamp_number(item.get("heroPotential"), 0.0, 100.0, 0.0)
            )),
            "detailPotential": int(round(
                _clamp_number(item.get("detailPotential"), 0.0, 100.0, 0.0)
            )),
            "movementQuality": int(round(
                _clamp_number(item.get("movementQuality"), 0.0, 100.0, 50.0)
            )),
            "recommendedZoom": round(
                _clamp_number(item.get("recommendedZoom"), 1.0, 1.35, 1.0),
                2,
            ),
            "recommendedUse": recommended_use,
            "reason": str(item.get("reason") or "")[:220],
        })

    visible = [f for f in normalized_frames if f["subjectVisible"]]
    vision_score = int(round(_clamp_number(result.get("visionScore"), 0.0, 100.0, 0.0)))
    if vision_score == 0 and normalized_frames:
        raw_scores = [
            (f["heroPotential"] * 0.40)
            + (f["movementQuality"] * 0.35)
            + ((100 if f["composition"] in {"STRONG", "GOOD"} else 50) * 0.25)
            for f in normalized_frames
        ]
        vision_score = int(round(sum(raw_scores) / len(raw_scores)))

    return {
        "enabled": True,
        "status": "VISION_ANALYSIS_PASS",
        "visionConnected": True,
        "version": DRONERIS_VISION_VERSION,
        "model": model,
        "visionScore": max(0, min(100, vision_score)),
        "subjectVisibleFrameCount": len(visible),
        "frameCount": len(normalized_frames),
        "summary": str(result.get("summary") or "")[:700],
        "frames": normalized_frames,
    }


def analyze_sampled_frames_with_openai(
    *,
    openai_client: Any,
    model: str,
    sample_manifest: Dict[str, Any],
    source_type: str = "REAL_FLIGHT",
    style: str = "clean_real_estate",
    image_detail: str = DEFAULT_IMAGE_DETAIL,
) -> Dict[str, Any]:
    """
    Analyze the already-sampled 16 JPEG frames with OpenAI Vision.

    This function DOES NOT modify edit scenes.
    It returns structured visual evidence for a later Director step.
    """
    if openai_client is None:
        return {
            "enabled": False,
            "status": "VISION_NOT_CONFIGURED",
            "visionConnected": False,
            "version": DRONERIS_VISION_VERSION,
            "frameCount": 0,
        }

    raw_frames = sample_manifest.get("frames") or []
    if len(raw_frames) != DEFAULT_FRAME_COUNT:
        raise VisionAnalysisError(
            f"Expected {DEFAULT_FRAME_COUNT} sampled frames, got {len(raw_frames)}."
        )

    prepared_frames: List[Dict[str, Any]] = []
    content: List[Dict[str, Any]] = []

    prompt = f"""
You are DRONERIS AI VISION DIRECTOR R2.

Analyze 16 uniformly sampled frames from one drone real-estate video.

SOURCE TYPE: {source_type}
EDIT STYLE: {style}

The images are ordered chronologically from frame 1 to frame 16.

Your task is visual analysis only. Do not invent motion that cannot be inferred from adjacent sampled frames.
Evaluate what is visibly present in each frame and how useful it is for film editing.

For every frame return:
- index: 1..16
- subjectVisible: true/false
- subjectOccupancy: approximate fraction of frame height occupied by the principal property/subject, 0.0..1.0
- composition: STRONG | GOOD | FAIR | WEAK | UNKNOWN
- obstruction: NONE | LOW | MEDIUM | HIGH | UNKNOWN
- heroPotential: 0..100
- detailPotential: 0..100
- movementQuality: 0..100
  This may use adjacent-frame visual progression, but do not claim exact optical-flow measurement.
- recommendedZoom: 1.00..1.35
- recommendedUse:
  REVEAL | HERO | PRIMARY_MOVEMENT | DETAIL | SECONDARY_MOVEMENT |
  FINAL_HERO | EXIT | CONTEXT | NONE
- reason: one concise sentence

Also return:
- visionScore: 0..100 overall visual usefulness
- summary: concise visual assessment of the footage

Rules:
- Prefer conservative judgments.
- If the property is too small, lower heroPotential and suggest modest zoom.
- If trees/objects obscure the property, reflect that in obstruction.
- Do not classify a frame as HERO merely because of its position in the video.
- Do not assume a building is visible unless it is actually visible.
- Do not alter timestamps or create edit cuts.
- Return JSON only.

Exact JSON shape:
{{
  "visionScore": 0,
  "summary": "",
  "frames": [
    {{
      "index": 1,
      "subjectVisible": true,
      "subjectOccupancy": 0.0,
      "composition": "GOOD",
      "obstruction": "NONE",
      "heroPotential": 0,
      "detailPotential": 0,
      "movementQuality": 0,
      "recommendedZoom": 1.0,
      "recommendedUse": "NONE",
      "reason": ""
    }}
  ]
}}
""".strip()

    content.append({"type": "input_text", "text": prompt})

    for frame in raw_frames:
        try:
            idx = int(frame.get("index"))
            ts = float(frame.get("timestamp_s"))
            path = str(frame.get("path") or "")
        except (TypeError, ValueError) as exc:
            raise VisionAnalysisError("Invalid sampled frame metadata.") from exc

        prepared_frames.append({
            "index": idx,
            "timestampSec": ts,
            "path": path,
        })

        content.append({
            "type": "input_text",
            "text": f"FRAME {idx:02d} — timestamp {ts:.3f}s",
        })
        content.append({
            "type": "input_image",
            "image_url": _image_to_data_url(path),
            "detail": image_detail,
        })

    try:
        response = openai_client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
        )
    except Exception as exc:
        raise VisionAnalysisError(
            f"OpenAI Vision request failed: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        output_text = response.output_text
    except Exception as exc:
        raise VisionAnalysisError("OpenAI response did not expose output_text.") from exc

    parsed = _extract_json_object(output_text)

    return _normalize_vision_result(
        parsed,
        expected_frames=prepared_frames,
        model=model,
    )


def sample_and_analyze_with_openai(
    *,
    video_path: Union[str, Path],
    output_dir: Union[str, Path],
    openai_client: Any,
    model: str,
    frame_count: int = DEFAULT_FRAME_COUNT,
    source_type: str = "REAL_FLIGHT",
    style: str = "clean_real_estate",
    image_detail: str = DEFAULT_IMAGE_DETAIL,
) -> Dict[str, Any]:
    """
    Convenience function for the complete R2 visual-analysis pipeline.

    MP4 -> 16 frames -> OpenAI Vision -> structured JSON

    Still does NOT modify AI EDIT DIRECTOR scenes.
    """
    manifest = build_vision_sample_manifest(
        video_path=video_path,
        output_dir=output_dir,
        frame_count=frame_count,
    )

    vision = analyze_sampled_frames_with_openai(
        openai_client=openai_client,
        model=model,
        sample_manifest=manifest,
        source_type=source_type,
        style=style,
        image_detail=image_detail,
    )

    return {
        "sampler": manifest,
        "vision": vision,
    }


__all__ = [
    "DRONERIS_VISION_VERSION",
    "DRONERIS_VISION_SAMPLER_VERSION",
    "DEFAULT_FRAME_COUNT",
    "VisionSamplerError",
    "FFmpegNotFoundError",
    "VideoProbeError",
    "FrameSamplingError",
    "VisionAnalysisError",
    "VideoInfo",
    "SampledFrame",
    "FrameSampleResult",
    "probe_video",
    "evenly_spaced_timestamps",
    "sample_frames",
    "build_vision_sample_manifest",
    "analyze_sampled_frames_with_openai",
    "sample_and_analyze_with_openai",
]
