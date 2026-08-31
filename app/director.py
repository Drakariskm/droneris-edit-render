from __future__ import annotations

import json
from typing import Any

DIRECTOR_VERSION = "DRONERIS_AI_EDIT_DIRECTOR_R1_2026_09_01"

DRONERIS_DIRECTOR_PROMPT = """
You are DRONERIS AI EDIT DIRECTOR.

You refine a deterministic First Cut for professional cinematic drone footage
used for real-estate and property presentation.

IMPORTANT
- You do NOT have raw video vision at this stage.
- Use only the supplied metadata and candidate scenes.
- Do not invent footage, objects, visual defects, POIs, or mission facts.
- Do not create new source regions.
- You may only trim INSIDE supplied candidate scene boundaries.
- Preserve chronological order.
- Do not overlap scenes.
- Do not modify mission, Core, flight, or safety data.
- Quality is more important than clip count.
- Avoid repetitive and mechanical editing.

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
HERO: 1.04-1.12
PRIMARY MOVEMENT: 1.00-1.05
DETAIL / POI: 1.05-1.15
SECONDARY MOVEMENT: 1.00-1.05
FINAL HERO: 1.04-1.10
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


def improve_first_cut_with_ai(
    *,
    openai_client: Any,
    model: str,
    duration: float,
    scenes: list[dict[str, Any]],
    source_type: str = "REAL_FLIGHT",
    style: str = "clean_real_estate",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:

    if openai_client is None:
        return scenes, {
            "enabled": False,
            "mode": "DETERMINISTIC_FALLBACK",
            "reason": "OPENAI_API_KEY_NOT_CONFIGURED",
            "directorVersion": DIRECTOR_VERSION,
        }

    candidates = []
    for scene in scenes:
        candidates.append({
            "id": int(scene["id"]),
            "type": str(scene["type"]),
            "label": str(scene["label"]),
            "start": float(scene["start"]),
            "end": float(scene["end"]),
            "duration": round(float(scene["end"]) - float(scene["start"]), 3),
        })

    payload = {
        "sourceDurationSec": round(float(duration), 3),
        "sourceType": str(source_type or "REAL_FLIGHT"),
        "style": str(style or "clean_real_estate"),
        "candidateScenes": candidates,
        "constraints": {
            "metadataOnly": True,
            "preserveChronology": True,
            "maxZoomFactor": 1.15,
            "minSpeed": 0.75,
            "maxSpeed": 1.35,
        },
    }

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
            original_start = float(original["start"])
            original_end = float(original["end"])

            start = max(original_start, float(ai_scene.get("start", original_start)))
            end = min(original_end, float(ai_scene.get("end", original_end)))

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
            updated["revision"] = "AI_DIRECTOR"

            final_scenes.append(updated)
            used_ids.add(scene_id)
            last_end = end

        enabled_count = sum(1 for scene in final_scenes if scene.get("enabled", True))
        if enabled_count < 2:
            raise ValueError("AI_PLAN_TOO_SPARSE")

        return final_scenes, {
            "enabled": True,
            "mode": "AI_DIRECTOR_PROMPT",
            "model": model,
            "directorScore": result.get("directorScore"),
            "directorVersion": DIRECTOR_VERSION,
        }

    except Exception as exc:
        return scenes, {
            "enabled": True,
            "mode": "DETERMINISTIC_FALLBACK_AFTER_AI_ERROR",
            "model": model,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "directorVersion": DIRECTOR_VERSION,
        }
