"""
DRONERIS AI VISION DIRECTOR R2
Phase 1: deterministic FFmpeg frame sampler.

Contract:
    MP4 -> ffprobe metadata -> 16 evenly distributed JPEG frames

OpenAI Vision is intentionally NOT connected in this phase.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


DRONERIS_VISION_VERSION = "DRONERIS_AI_VISION_DIRECTOR_R2_FRAME_SAMPLER_2026_09_01"
DEFAULT_FRAME_COUNT = 16
DEFAULT_JPEG_QUALITY = 2
DEFAULT_SCALE_WIDTH = 1280
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


class VisionSamplerError(RuntimeError):
    """Base error for the DRONERIS Vision frame sampler."""


class FFmpegNotFoundError(VisionSamplerError):
    """Raised when ffmpeg or ffprobe cannot be found."""


class VideoProbeError(VisionSamplerError):
    """Raised when video metadata cannot be read reliably."""


class FrameSamplingError(VisionSamplerError):
    """Raised when one or more requested frames cannot be extracted."""


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
    """
    Read stable metadata using ffprobe.

    Duration is resolved from format.duration first, then stream.duration.
    """
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

    duration_candidates = [
        format_info.get("duration"),
        video_stream.get("duration"),
    ]
    duration_s = None
    for candidate in duration_candidates:
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
    Return timestamps centered inside equal-duration bins.

    For N=16 the video is divided into 16 equal temporal segments and one
    frame is sampled from the center of each segment. This avoids depending
    on the possibly-black first frame or an incomplete last frame while still
    covering the complete clip uniformly.
    """
    if frame_count <= 0:
        raise ValueError("frame_count must be greater than zero")
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be a positive finite number")

    segment = duration_s / frame_count

    # For extremely short videos, stay a tiny distance away from exact EOF.
    eof_guard = min(0.001, duration_s * 0.001)
    last_safe = max(0.0, duration_s - eof_guard)

    timestamps = []
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
        # -2 preserves aspect ratio while keeping an even dimension.
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
    """
    Extract exactly `frame_count` evenly distributed JPEG frames.

    Default R2 contract:
        frame_count = 16
        scale_width = 1280
        format = JPEG

    If `output_dir` is omitted, a persistent temporary directory is created.
    """
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
        version=DRONERIS_VISION_VERSION,
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
    Convenience entry point for the future AI Vision Director.

    Returns JSON-serializable metadata and frame paths.
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


__all__ = [
    "DRONERIS_VISION_VERSION",
    "DEFAULT_FRAME_COUNT",
    "VisionSamplerError",
    "FFmpegNotFoundError",
    "VideoProbeError",
    "FrameSamplingError",
    "VideoInfo",
    "SampledFrame",
    "FrameSampleResult",
    "probe_video",
    "evenly_spaced_timestamps",
    "sample_frames",
    "build_vision_sample_manifest",
]
