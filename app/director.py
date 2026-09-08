from __future__ import annotations

import json
from typing import Any

DIRECTOR_VERSION = "DRONERIS_AI_EDIT_DIRECTOR_R4_STRENGTH_AWARE_2026_09_08"
R1_FALLBACK_VERSION = "DRONERIS_AI_EDIT_DIRECTOR_R1_2026_09_01"

DRONERIS_DIRECTOR_PROMPT = """
You are DRONERIS AI EDIT DIRECTOR R4.

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

TRANSITION DIRECTION
Choose the transition AFTER each enabled scene. The final enabled scene must use CUT.
CUT is the professional default. Use a visible transition only when it improves continuity, pacing, or a deliberate change of section.
Do NOT put a visible transition on every join. For a normal 12-18 clip film, usually 2-4 visible transitions are enough.

Allowed transitionOut values:
- CUT — default for most joins, especially continuous motion and energetic pacing.
- CROSSFADE — restrained soft continuity between compatible calm/HERO shots.
- DISSOLVE — deliberate visual/time/section change; use sparingly.
- DIP_BLACK — major chapter/location break only; normally zero or one per film.

Transition duration guidance:
- CUT: 0.0 seconds
- CROSSFADE: 0.35-0.80 seconds
- DISSOLVE: 0.40-0.90 seconds
- DIP_BLACK: 0.40-0.80 seconds

Transition selection rules:
- Prefer CUT when unsure.
- Do not use a visible transition to hide a weak edit; choose better scene timing instead.
- Avoid repeated decorative transitions.
- Similar direction/motion with good continuity usually wants CUT.
- Calm HERO to calm HERO may justify CROSSFADE.
- Strong semantic/temporal section change may justify DISSOLVE.
- DIP_BLACK must be rare and intentional.
- Style should influence restraint, not override visual evidence.

AI EDIT STRENGTH
The user selects exactly one editStrength: LOW, MEDIUM, or HIGH.
This controls editorial intervention, not source quality or mission safety.

LOW / restrained:
- Prefer longer, calmer shots and preserve natural camera movement.
- Make fewer timing, speed, and zoom interventions.
- Keep visible transitions rare; CUT should dominate.
- Do not create artificial energy.

MEDIUM / balanced:
- Balanced cinematic pacing with clear visual variety.
- Moderate timing refinements and restrained speed/zoom corrections.
- Use visible transitions selectively.

HIGH / dynamic:
- Prefer shorter, more varied shots while preserving continuity and strong HERO moments.
- Be more willing to refine timing and use speed/zoom within the supplied hard limits.
- Pacing may be more energetic, but never use effects mechanically.
- CUT remains the default even at HIGH strength.

The user payload contains the exact hard limits for the selected strength. Those limits override generic guidance above.

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
      "transitionOut": "CUT",
      "transitionDuration": 0.0,
      "transitionReason": "short factual explanation",
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


def _normalize_transition(name: Any) -> str:
    key = str(name or "CUT").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "FADE": "CROSSFADE",
        "CROSS_FADE": "CROSSFADE",
        "DIPBLACK": "DIP_BLACK",
        "FADE_BLACK": "DIP_BLACK",
    }
    key = aliases.get(key, key)
    return key if key in {"CUT", "CROSSFADE", "DISSOLVE", "DIP_BLACK"} else "CUT"


def _transition_duration(kind: str, value: Any) -> float:
    if kind == "CUT":
        return 0.0
    ranges = {
        "CROSSFADE": (0.35, 0.80, 0.55),
        "DISSOLVE": (0.40, 0.90, 0.60),
        "DIP_BLACK": (0.40, 0.80, 0.55),
    }
    lo, hi, default = ranges[kind]
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return round(max(lo, min(hi, v)), 3)


EDIT_STRENGTH_POLICY: dict[str, dict[str, Any]] = {
    "low": {
        "label": "LOW",
        "minSpeed": 0.95, "maxSpeed": 1.10, "maxZoomFactor": 1.08,
        "visibleRatio": 0.15, "maxVisible": 2, "maxDipBlack": 0,
    },
    "medium": {
        "label": "MEDIUM",
        "minSpeed": 0.85, "maxSpeed": 1.25, "maxZoomFactor": 1.12,
        "visibleRatio": 0.30, "maxVisible": 4, "maxDipBlack": 1,
    },
    "high": {
        "label": "HIGH",
        "minSpeed": 0.75, "maxSpeed": 1.35, "maxZoomFactor": 1.15,
        "visibleRatio": 0.35, "maxVisible": 5, "maxDipBlack": 1,
    },
}


def _normalize_edit_strength(value: Any) -> str:
    key = str(value or "medium").strip().lower()
    aliases = {
        "blago": "low", "slabo": "low", "light": "low",
        "srednje": "medium", "normal": "medium", "balanced": "medium",
        "jako": "high", "strong": "high", "dynamic": "high",
    }
    key = aliases.get(key, key)
    return key if key in EDIT_STRENGTH_POLICY else "medium"


def _strength_policy(value: Any) -> dict[str, Any]:
    return dict(EDIT_STRENGTH_POLICY[_normalize_edit_strength(value)])


def _minimum_enabled(candidate_count: int, edit_strength: str) -> int:
    count = max(2, int(candidate_count))
    strength = _normalize_edit_strength(edit_strength)
    keep_margin = {"low": 2, "medium": 2, "high": 1}[strength]
    return max(2, count - keep_margin)


def _apply_transition_guard(scenes: list[dict[str, Any]], edit_strength: str = "medium") -> None:
    enabled_indexes = [i for i, s in enumerate(scenes) if s.get("enabled", True)]
    if not enabled_indexes:
        return

    # Final enabled scene never transitions to another clip.
    last_enabled = enabled_indexes[-1]
    scenes[last_enabled]["transitionOut"] = "CUT"
    scenes[last_enabled]["transitionDuration"] = 0.0

    policy = _strength_policy(edit_strength)
    joins = max(0, len(enabled_indexes) - 1)
    ratio_budget = max(1, round(joins * float(policy["visibleRatio"]))) if joins else 0
    max_visible = min(int(policy["maxVisible"]), ratio_budget) if joins else 0
    visible_seen = 0
    dip_black_seen = 0
    max_dip_black = int(policy["maxDipBlack"])

    for idx in enabled_indexes[:-1]:
        scene = scenes[idx]
        kind = _normalize_transition(scene.get("transitionOut"))
        if kind == "DIP_BLACK":
            if dip_black_seen >= max_dip_black:
                kind = "CUT"
            else:
                dip_black_seen += 1
        if kind != "CUT":
            if visible_seen >= max_visible:
                kind = "CUT"
            else:
                visible_seen += 1
        scene["transitionOut"] = kind
        scene["transitionDuration"] = _transition_duration(kind, scene.get("transitionDuration"))

def improve_first_cut_with_ai(
    *,
    openai_client: Any,
    model: str,
    duration: float,
    scenes: list[dict[str, Any]],
    source_type: str = "REAL_FLIGHT",
    style: str = "clean_real_estate",
    vision_analysis: dict[str, Any] | None = None,
    edit_strength: str = "medium",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:

    if openai_client is None:
        return scenes, {
            "enabled": False,
            "mode": "DETERMINISTIC_FALLBACK",
            "reason": "OPENAI_API_KEY_NOT_CONFIGURED",
            "directorVersion": DIRECTOR_VERSION,
        }

    vision_enabled = _valid_vision(vision_analysis)
    edit_strength = _normalize_edit_strength(edit_strength)
    strength_policy = _strength_policy(edit_strength)
    minimum_enabled = _minimum_enabled(len(scenes), edit_strength)
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
        "editStrength": strength_policy["label"],
        "candidateScenes": candidates,
        "constraints": {
            "visionAware": vision_enabled,
            "preserveChronology": True,
            "maxZoomFactor": strength_policy["maxZoomFactor"],
            "minSpeed": strength_policy["minSpeed"],
            "maxSpeed": strength_policy["maxSpeed"],
            "minimumEnabledScenes": minimum_enabled,
            "localShiftLimitSec": round(local_shift, 3),
            "allowedTransitions": ["CUT", "CROSSFADE", "DISSOLVE", "DIP_BLACK"],
            "cutIsDefault": True,
            "maxVisibleTransitionRatio": strength_policy["visibleRatio"],
            "maxVisibleTransitions": strength_policy["maxVisible"],
            "maxDipBlack": strength_policy["maxDipBlack"],
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

            zoom = max(1.0, min(float(strength_policy["maxZoomFactor"]), float(ai_scene.get("zoomFactor", 1.0))))
            speed = max(float(strength_policy["minSpeed"]), min(float(strength_policy["maxSpeed"]), float(ai_scene.get("speed", 1.0))))

            updated = dict(original)
            updated["start"] = round(start, 3)
            updated["end"] = round(end, 3)
            updated["enabled"] = bool(ai_scene.get("enabled", True))
            updated["zoomFactor"] = round(zoom, 3)
            updated["speed"] = round(speed, 3)
            transition_kind = _normalize_transition(ai_scene.get("transitionOut", "CUT"))
            updated["transitionOut"] = transition_kind
            updated["transitionDuration"] = _transition_duration(transition_kind, ai_scene.get("transitionDuration", 0.0))
            updated["transitionReason"] = str(ai_scene.get("transitionReason", ""))[:240]
            updated["directorReason"] = str(ai_scene.get("reason", ""))[:240]
            updated["revision"] = "AI_VISION_DIRECTOR" if vision_enabled else "AI_DIRECTOR"

            final_scenes.append(updated)
            used_ids.add(scene_id)
            last_end = end

        _apply_transition_guard(final_scenes, edit_strength)

        enabled_count = sum(1 for scene in final_scenes if scene.get("enabled", True))
        if enabled_count < minimum_enabled:
            raise ValueError(f"AI_PLAN_TOO_SPARSE_FOR_{edit_strength.upper()}:{enabled_count}<{minimum_enabled}")

        return final_scenes, {
            "enabled": True,
            "mode": "AI_VISION_DIRECTOR_PROMPT" if vision_enabled else "AI_DIRECTOR_PROMPT",
            "model": model,
            "directorScore": result.get("directorScore"),
            "visionAware": vision_enabled,
            "visionScore": vision_analysis.get("visionScore") if vision_enabled and vision_analysis else None,
            "localShiftLimitSec": round(local_shift, 3) if vision_enabled else 0.0,
            "directorVersion": DIRECTOR_VERSION,
            "editStrength": edit_strength,
            "strengthPolicy": strength_policy,
            "minimumEnabledScenes": minimum_enabled,
            "fallbackVersion": R1_FALLBACK_VERSION,
        }

    except Exception as exc:
        return scenes, {
            "enabled": True,
            "mode": "DETERMINISTIC_FALLBACK_AFTER_AI_ERROR",
            "model": model,
            "visionAware": vision_enabled,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "editStrength": edit_strength,
            "strengthPolicy": strength_policy,
            "minimumEnabledScenes": minimum_enabled,
            "directorVersion": DIRECTOR_VERSION,
            "fallbackVersion": R1_FALLBACK_VERSION,
        }
