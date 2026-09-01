from __future__ import annotations

import json
from typing import Any

DIRECTOR_VERSION = "DRONERIS_AI_EDIT_DIRECTOR_R2_VISION_AWARE_2026_09_01"
R1_FALLBACK_VERSION = "DRONERIS_AI_EDIT_DIRECTOR_R1_2026_09_01"

DRONERIS_DIRECTOR_PROMPT = """
You are DRONERIS AI EDIT DIRECTOR R2.

You refine a deterministic First Cut for professional cinematic drone footage
used for real-estate and property presentation.

You may receive AI Vision evidence sampled uniformly across the source video.
When valid Vision evidence is present, use it as the primary visual-quality
signal. Candidate scenes remain the structural editing grammar.

IMPORTANT
- Never invent footage, objects, visual defects, POIs, or mission facts.
- Preserve chronological order.
- Do not overlap scenes.
- Do not modify mission, Core, flight, or safety data.
- Quality is more important than clip count.
- Avoid repetitive and mechanical editing.
- Prefer visually strong, unobstructed frames where the subject is clearly visible.
- Do not choose HERO merely from timeline position.
- Strong or GOOD composition, useful subject occupancy, low obstruction, and high
  hero/movement potential should influence selection.
- HIGH obstruction or recommendedUse NONE is a strong negative signal.
- Digital zoom is secondary; do not use zoom to rescue a fundamentally weak shot.
- If Vision is not available, behave like the previous metadata-only Director.

VISION-AWARE SOURCE WINDOWS
- Every candidate has an allowedStart and allowedEnd.
- You may reposition a scene only INSIDE its allowed window.
- You may never use source time outside its allowed window.
- This provides limited local movement toward better Vision evidence without
  allowing arbitrary source-region invention.
- You may disable a weak candidate.
- Keep at least 2 enabled scenes.

PREFERRED CLIP DURATIONS
REVEAL: 5-7 seconds
HERO: 6-9 seconds
PRIMARY MOVEMENT: 8-12 seconds
DETAIL / POI: 4-7 seconds
SECONDARY MOVEMENT: 6-9 seconds
FINAL HERO: 6-9 seconds
EXIT: 4-7 seconds

DIGITAL ZOOM
REVEAL: 1.00-1.03
HERO: 1.00-1.12
PRIMARY MOVEMENT: 1.00-1.05
DETAIL / POI: 1.00-1.15
SECONDARY MOVEMENT: 1.00-1.05
FINAL HERO: 1.00-1.10
EXIT: 1.00-1.03

Never exceed 1.15.

Return JSON only in this exact shape:

{
  "directorScore": 0,
  "scenes": [
    {
      "id": 1,
      "enabled": true,
      "start": 0.0,
      "end": 6.0,
      "zoomFactor": 1.0,
      "speed": 1.0,
      "reason": "short factual explanation"
    }
  ]
}
""".strip()


def _valid_vision(vision_analysis: Any) -> bool:
    return (
        isinstance(vision_analysis, dict)
        and vision_analysis.get("status") == "VISION_ANALYSIS_PASS"
        and vision_analysis.get("visionConnected") is True
        and isinstance(vision_analysis.get("frames"), list)
        and len(vision_analysis.get("frames") or []) >= 2
    )


def _vision_payload(vision_analysis: dict[str, Any]) -> dict[str, Any]:
    frames = []
    for f in vision_analysis.get("frames") or []:
        if not isinstance(f, dict):
            continue
        frames.append({
            "index": f.get("index"),
            "timestampSec": f.get("timestampSec"),
            "subjectVisible": f.get("subjectVisible"),
            "subjectOccupancy": f.get("subjectOccupancy"),
            "composition": f.get("composition"),
            "obstruction": f.get("obstruction"),
            "heroPotential": f.get("heroPotential"),
            "detailPotential": f.get("detailPotential"),
            "movementQuality": f.get("movementQuality"),
            "recommendedZoom": f.get("recommendedZoom"),
            "recommendedUse": f.get("recommendedUse"),
            "reason": f.get("reason"),
        })
    return {
        "visionScore": vision_analysis.get("visionScore"),
        "summary": vision_analysis.get("summary"),
        "frames": frames,
    }


def _sample_interval(vision_analysis: dict[str, Any], duration: float) -> float:
    timestamps = []
    for f in vision_analysis.get("frames") or []:
        try:
            timestamps.append(float(f.get("timestampSec")))
        except (TypeError, ValueError):
            pass
    timestamps = sorted(set(timestamps))
    gaps = [
        timestamps[i] - timestamps[i - 1]
        for i in range(1, len(timestamps))
        if timestamps[i] > timestamps[i - 1]
    ]
    if gaps:
        return max(1.0, min(12.0, sum(gaps) / len(gaps)))
    return max(1.0, min(12.0, float(duration) / 16.0))


def improve_first_cut_with_ai(
    *,
    openai_client: Any,
    model: str,
    duration: float,
    scenes: list[dict[str, Any]],
    source_type: str = "REAL_FLIGHT",
    style: str = "clean_real_estate",
    vision_analysis: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:

    if openai_client is None:
        return scenes, {
            "enabled": False,
            "mode": "DETERMINISTIC_FALLBACK",
            "reason": "OPENAI_API_KEY_NOT_CONFIGURED",
            "directorVersion": DIRECTOR_VERSION,
        }

    vision_enabled = _valid_vision(vision_analysis)
    local_shift = _sample_interval(vision_analysis, duration) if vision_enabled else 0.0

    candidates = []
    candidate_bounds: dict[int, tuple[float, float]] = {}

    for scene in scenes:
        scene_id = int(scene["id"])
        original_start = float(scene["start"])
        original_end = float(scene["end"])

        if vision_enabled:
            allowed_start = max(0.0, original_start - local_shift)
            allowed_end = min(float(duration), original_end + local_shift)
        else:
            allowed_start = original_start
            allowed_end = original_end

        candidate_bounds[scene_id] = (allowed_start, allowed_end)
        candidates.append({
            "id": scene_id,
            "type": str(scene["type"]),
            "label": str(scene["label"]),
            "start": original_start,
            "end": original_end,
            "duration": round(original_end - original_start, 3),
            "allowedStart": round(allowed_start, 3),
            "allowedEnd": round(allowed_end, 3),
        })

    payload: dict[str, Any] = {
        "sourceDurationSec": round(float(duration), 3),
        "sourceType": str(source_type or "REAL_FLIGHT"),
        "style": str(style or "clean_real_estate"),
        "candidateScenes": candidates,
        "constraints": {
            "visionAware": vision_enabled,
            "preserveChronology": True,
            "maxZoomFactor": 1.15,
            "minSpeed": 0.75,
            "maxSpeed": 1.35,
            "minimumEnabledScenes": 2,
            "localShiftLimitSec": round(local_shift, 3),
        },
    }

    if vision_enabled and vision_analysis is not None:
        payload["vision"] = _vision_payload(vision_analysis)

    try:
        response = openai_client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": DRONERIS_DIRECTOR_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )

        result = json.loads(response.output_text.strip())
        ai_scenes = result.get("scenes")

        if not isinstance(ai_scenes, list):
            raise ValueError("AI_SCENES_INVALID")

        original_by_id = {int(scene["id"]): scene for scene in scenes}
        final_scenes = []
        used_ids = set()
        last_end = -1.0

        for ai_scene in ai_scenes:
            scene_id = int(ai_scene.get("id", -1))
            if scene_id in used_ids or scene_id not in original_by_id:
                continue

            original = original_by_id[scene_id]
            allowed_start, allowed_end = candidate_bounds[scene_id]
            original_start = float(original["start"])
            original_end = float(original["end"])

            requested_start = float(ai_scene.get("start", original_start))
            requested_end = float(ai_scene.get("end", original_end))

            start = max(allowed_start, min(requested_start, allowed_end))
            end = min(allowed_end, max(requested_end, allowed_start))

            if start < last_end:
                start = last_end

            if end - start < 1.0:
                continue

            zoom = max(1.0, min(1.15, float(ai_scene.get("zoomFactor", 1.0))))
            speed = max(0.75, min(1.35, float(ai_scene.get("speed", 1.0))))

            updated = dict(original)
            updated["start"] = round(start, 3)
            updated["end"] = round(end, 3)
            updated["enabled"] = bool(ai_scene.get("enabled", True))
            updated["zoomFactor"] = round(zoom, 3)
            updated["speed"] = round(speed, 3)
            updated["directorReason"] = str(ai_scene.get("reason", ""))[:240]
            updated["revision"] = "AI_VISION_DIRECTOR" if vision_enabled else "AI_DIRECTOR"

            final_scenes.append(updated)
            used_ids.add(scene_id)
            last_end = end

        enabled_count = sum(1 for scene in final_scenes if scene.get("enabled", True))
        if enabled_count < 2:
            raise ValueError("AI_PLAN_TOO_SPARSE")

        return final_scenes, {
            "enabled": True,
            "mode": "AI_VISION_DIRECTOR_PROMPT" if vision_enabled else "AI_DIRECTOR_PROMPT",
            "model": model,
            "directorScore": result.get("directorScore"),
            "visionAware": vision_enabled,
            "visionScore": vision_analysis.get("visionScore") if vision_enabled and vision_analysis else None,
            "localShiftLimitSec": round(local_shift, 3) if vision_enabled else 0.0,
            "directorVersion": DIRECTOR_VERSION,
            "fallbackVersion": R1_FALLBACK_VERSION,
        }

    except Exception as exc:
        return scenes, {
            "enabled": True,
            "mode": "DETERMINISTIC_FALLBACK_AFTER_AI_ERROR",
            "model": model,
            "visionAware": vision_enabled,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "directorVersion": DIRECTOR_VERSION,
            "fallbackVersion": R1_FALLBACK_VERSION,
        }
