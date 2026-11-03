"""What a photo is allowed to say.

A vision model looking at a flood photo is genuinely useful and genuinely
dangerous, and the difference is entirely in what the server does with its
answer. So this module is mostly a contract, and the contract has one rule:

    **A photo is corroboration. It is never the claim.**

The report says what happened. The photo can agree with it, disagree with it, or
be of something else entirely, and each of those three outcomes changes the
*trust* in the report rather than replacing it. A model that can set a category
on its own can be shown a picture of a swimming pool and dispatch a boat; a
model that can only raise or lower confidence in something a person already
said cannot.

Concretely, what the model's output may and may not do:

  =========================  ==========================================
  May                        May not
  =========================  ==========================================
  raise or lower trust       set the category
  suggest a depth band       set the severity
  say "no water visible"     resolve or close an incident
  say people are present     commit a unit
  name visible landmarks     override what the reporter typed
  =========================  ==========================================

The model runs wherever is convenient — on the phone, on our server, on a hosted
endpoint — because none of those change what the answer is permitted to do. That
is the property that makes the placement decision an engineering one rather than
a safety one.

Everything below validates. A field that is missing, out of range, or not in the
allowed set is dropped rather than coerced, and a response that fails wholesale
leaves the report exactly as it would have been with no photo at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.taxonomy import cache as taxonomy

log = get_logger(__name__)

#: The prompt. Kept here rather than in the client so the on-device app, the
#: hosted endpoint and any future provider are all answering the same question.
#:
#: Written for a small instruction-tuned VLM: short, closed vocabularies, no
#: free text that matters, and an explicit escape hatch ("unclear") for every
#: field, because a 3B model asked to be certain will be certain and wrong.
PROMPT = """You are looking at a photograph attached to a disaster report.

Describe ONLY what is visible. Do not infer, do not guess what happened before
or after, and do not use knowledge about the place. If something is not clearly
visible, use "unclear".

Reply with JSON only, no other text, matching exactly this shape:

{
  "shows_hazard": true | false | "unclear",
  "hazard_visible": ["water_on_road","standing_water","fallen_tree",
                     "damaged_structure","downed_power_line","blocked_drain",
                     "fire","debris","crowd","none"],
  "water": {
    "present": true | false,
    "depth_band": "none" | "ankle" | "knee" | "waist" | "above_waist" | "unclear",
    "moving": true | false | "unclear",
    "reference": "what you judged the depth against, e.g. a car wheel"
  },
  "people": {
    "visible": 0,
    "in_water": 0,
    "apparently_trapped": true | false | "unclear"
  },
  "vehicles_visible": 0,
  "time_of_day": "day" | "night" | "unclear",
  "image_quality": "clear" | "blurry" | "dark" | "obstructed",
  "looks_staged_or_reused": true | false | "unclear",
  "caption": "one short factual sentence, max 20 words",
  "confidence": 0.0
}

Rules:
- "confidence" is your confidence in this whole answer, 0.0 to 1.0.
- Count people and vehicles you can actually see. Do not estimate crowds.
- Set "looks_staged_or_reused" true only for obvious signs: a screenshot, a
  watermark, a photo of a screen, or visible compression from repeated sharing.
- Never output any field not listed above."""

HAZARDS = {
    "water_on_road", "standing_water", "fallen_tree", "damaged_structure",
    "downed_power_line", "blocked_drain", "fire", "debris", "crowd", "none",
}
DEPTHS = ("none", "ankle", "knee", "waist", "above_waist", "unclear")
QUALITY = ("clear", "blurry", "dark", "obstructed")

#: Which visible hazards corroborate which reported category. Not a mapping from
#: photo to category — a photo never picks the category — but a test of whether
#: the picture is consistent with what the person wrote.
CORROBORATES: dict[str, set[str]] = {
    "flooded_road": {"water_on_road", "standing_water", "debris"},
    "waterlogging": {"standing_water", "water_on_road"},
    "person_stranded": {"water_on_road", "standing_water", "crowd", "debris"},
    "fallen_tree": {"fallen_tree", "debris"},
    "power_line": {"downed_power_line"},
    "structural_damage": {"damaged_structure", "debris"},
    "blocked_drain": {"blocked_drain", "standing_water"},
    "heat_casualty": {"crowd"},
}

#: Depth bands, as a severity contribution. The photo may nudge; the reported
#: category still sets the floor.
DEPTH_WEIGHT = {
    "none": 0.0, "ankle": 0.2, "knee": 0.5, "waist": 0.8,
    "above_waist": 1.0, "unclear": 0.0,
}


def _tri(value: Any) -> bool | None:
    """true / false / unclear, where unclear is an answer and not a failure."""
    if value is True or value is False:
        return value
    return None


def _count(value: Any, cap: int = 500) -> int:
    try:
        return max(0, min(cap, int(value)))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class PhotoEvidence:
    """A validated photo analysis, in the only terms the server accepts."""

    shows_hazard: bool | None
    hazards: list[str]
    water_present: bool
    depth_band: str
    water_moving: bool | None
    depth_reference: str
    people_visible: int
    people_in_water: int
    apparently_trapped: bool | None
    vehicles_visible: int
    time_of_day: str
    image_quality: str
    looks_staged: bool | None
    caption: str
    confidence: float
    model: str
    ran_on: str
    #: Filled by `assess`, not by the model.
    agreement: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def depth_weight(self) -> float:
        return DEPTH_WEIGHT.get(self.depth_band, 0.0)

    @property
    def life_safety_signal(self) -> bool:
        """Does the picture itself show somebody at risk?

        Only ever used to raise a report's urgency for a human to look at
        sooner. It cannot lower one: a photo that shows nobody is not evidence
        that nobody is there, and treating it as such is how you deprioritise a
        rescue because the camera was pointed at the water.
        """
        return bool(
            self.people_in_water > 0
            or self.apparently_trapped is True
            or (self.people_visible > 0 and self.depth_weight >= 0.5)
        )


def parse_response(
    payload: Any, *, model: str = "unknown", ran_on: str = "server"
) -> PhotoEvidence | None:
    """Validate a model's JSON. Returns None if it is not usable at all.

    Deliberately forgiving about extra keys and unforgiving about wrong values:
    a model that invents a hazard name has that name dropped, not mapped to the
    nearest thing we recognise. Silent coercion is how an "unclear" becomes a
    dispatch.
    """
    if not isinstance(payload, dict):
        return None

    water = payload.get("water") if isinstance(payload.get("water"), dict) else {}
    people = payload.get("people") if isinstance(payload.get("people"), dict) else {}

    hazards = [
        h for h in (payload.get("hazard_visible") or [])
        if isinstance(h, str) and h in HAZARDS and h != "none"
    ]

    depth = water.get("depth_band")
    depth = depth if depth in DEPTHS else "unclear"

    quality = payload.get("image_quality")
    quality = quality if quality in QUALITY else "clear"

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    caption = str(payload.get("caption") or "")[:200]

    return PhotoEvidence(
        shows_hazard=_tri(payload.get("shows_hazard")),
        hazards=hazards,
        water_present=bool(water.get("present")),
        depth_band=depth,
        water_moving=_tri(water.get("moving")),
        depth_reference=str(water.get("reference") or "")[:120],
        people_visible=_count(people.get("visible")),
        people_in_water=_count(people.get("in_water")),
        apparently_trapped=_tri(people.get("apparently_trapped")),
        vehicles_visible=_count(payload.get("vehicles_visible")),
        time_of_day=str(payload.get("time_of_day") or "unclear")[:10],
        image_quality=quality,
        looks_staged=_tri(payload.get("looks_staged_or_reused")),
        caption=caption,
        confidence=confidence,
        model=str(model)[:80],
        ran_on=str(ran_on)[:20],
    )


def assess(evidence: PhotoEvidence, *, category: str) -> PhotoEvidence:
    """Score the photo against what the person actually reported.

    Three outcomes, and the middle one is the interesting one:

      * **agrees** — the picture shows what was described. Trust goes up, and it
        goes up more for a clear daytime photo than a dark blurry one.
      * **says nothing** — a photo of a wall, an unclear image, a model that was
        not confident. Trust is unchanged. This is the common case and it must
        be cheap, because a system that penalises bad photos teaches people to
        stop sending them.
      * **contradicts** — the report says flooded road, the photo shows a dry
        street in daylight at high confidence. Trust goes *down*, and the
        contradiction is recorded so a human can look, because this is also
        exactly what a fabricated report looks like.
    """
    notes: list[str] = []
    expected = CORROBORATES.get(category, set())
    overlap = expected & set(evidence.hazards)

    # A photo nobody can read is evidence of nothing, in either direction.
    legible = evidence.image_quality == "clear" and evidence.confidence >= 0.45
    if not legible:
        evidence.agreement = 0.0
        notes.append(
            f"Photo was {evidence.image_quality} and the model was "
            f"{evidence.confidence:.0%} confident, so it neither supports nor "
            "undermines the report."
        )
        evidence.notes = notes
        return evidence

    if overlap:
        strength = min(1.0, 0.45 + 0.25 * len(overlap))
        evidence.agreement = round(strength * evidence.confidence, 3)
        notes.append(
            "Photo shows "
            + ", ".join(h.replace("_", " ") for h in sorted(overlap))
            + f", which is consistent with a {category.replace('_', ' ')} report."
        )
    elif (
        evidence.shows_hazard is False
        and evidence.confidence >= 0.7
        # Cross-checked, not taken at its word. `shows_hazard` is the least
        # reliable field the 3B returns — it answers `false` on a plainly
        # flooded street often enough that the people running it asked us not
        # to let a bare `false` cost a report anything. A model that says "no
        # hazard" while also listing hazards, or while saying there is water in
        # the picture, has contradicted itself, and the summary field is the
        # one to disbelieve. Only a photo that is empty by every measure counts
        # as evidence against the report.
        and not evidence.hazards
        and not evidence.water_present
    ):
        # A confident, legible "nothing is wrong here" against a report that
        # something is.
        evidence.agreement = round(-0.6 * evidence.confidence, 3)
        notes.append(
            "Photo shows no hazard at all, which contradicts the report. Held "
            "for a human rather than acted on either way."
        )
    else:
        evidence.agreement = 0.0
        notes.append(
            "Photo does not clearly show what was reported, so it was not "
            "counted as corroboration."
        )

    if evidence.water_present and evidence.depth_band not in ("none", "unclear"):
        notes.append(
            f"Water judged {evidence.depth_band.replace('_', ' ')} deep"
            + (f" against {evidence.depth_reference}" if evidence.depth_reference else "")
            + "."
        )
    if evidence.life_safety_signal:
        notes.append(
            "People visible in or beside the water. Raised for a human to look "
            "at sooner; it does not commit a unit on its own."
        )
    if evidence.looks_staged is True:
        evidence.agreement = min(evidence.agreement, -0.3)
        notes.append(
            "Image looks like a screenshot or a re-shared photo rather than "
            "something taken now."
        )

    evidence.notes = notes
    return evidence


def as_dict(e: PhotoEvidence) -> dict[str, Any]:
    return {
        "showsHazard": e.shows_hazard,
        "hazards": e.hazards,
        "water": {
            "present": e.water_present,
            "depthBand": e.depth_band,
            "moving": e.water_moving,
            "reference": e.depth_reference,
        },
        "people": {
            "visible": e.people_visible,
            "inWater": e.people_in_water,
            "apparentlyTrapped": e.apparently_trapped,
        },
        "vehiclesVisible": e.vehicles_visible,
        "timeOfDay": e.time_of_day,
        "imageQuality": e.image_quality,
        "looksStaged": e.looks_staged,
        "caption": e.caption,
        "confidence": e.confidence,
        "model": e.model,
        "ranOn": e.ran_on,
        "agreement": e.agreement,
        "lifeSafetySignal": e.life_safety_signal,
        "notes": e.notes,
    }


def known_categories() -> list[str]:
    """Categories this corroboration table covers, for the client to check."""
    return sorted(set(CORROBORATES) & set(taxonomy.categories))
