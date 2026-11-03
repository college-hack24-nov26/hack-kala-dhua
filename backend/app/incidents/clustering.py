"""Is this the same thing somebody already told us?

PS20 asks for duplicate-effort detection. There are two different duplicates
hiding under that phrase and both matter:

  1. **Duplicate reports.** Four people describe one flooded bridge. Counting
     them as four incidents inflates how bad that junction looks, pulls
     attention toward it, and makes the confidence number meaningless.

  2. **Duplicate deployment.** Two agencies dispatch into the same incident
     because neither can see the other's assignments. This is the one that
     actually wastes rescue capacity, and it is the one most systems miss
     because it needs the first problem solved before it is even visible.

This module does the first. The second is a query over assignments joined
through the graph this module builds, and lives in `duplicates.py`.

The matching is deterministic and cheap: a PostGIS distance query bounded by the
category's own radius and time window, scored on four components. A language
model is consulted for exactly one thing, the ambiguous middle band, and its
answer is stored alongside the link so the decision can be read back later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.db import session as db
from app.taxonomy import cache as taxonomy

log = get_logger(__name__)

#: Above this, link without asking anyone.
LINK_AUTO = 0.75
#: Below this, it is a new incident.
LINK_NEW = 0.45
#: Between the two, one LLM call adjudicates the single best candidate.

WEIGHTS = {"spatial": 0.35, "temporal": 0.25, "category": 0.20, "text": 0.20}


@dataclass(slots=True)
class Candidate:
    incident_id: str
    title: str
    category: str
    severity: int
    distance_m: float
    age_minutes: float
    report_count: int
    note_sample: str
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


def _category_compatible(a: str, b: str) -> float:
    """How much two categories can describe the same event.

    Not symmetric with reality, but close enough and explainable: a flooded road
    and waterlogging are the same thing seen by two people with different words;
    a fallen tree and a heat casualty are not.
    """
    if a == b:
        return 1.0

    groups = [
        {"flooded_road", "waterlogging", "blocked_drain"},
        {"structural_damage", "fallen_tree", "power_line"},
        {"person_stranded", "structural_damage"},
    ]
    in_a = [g for g in groups if a in g]
    in_b = [g for g in groups if b in g]
    if any(g is h for g in in_a for h in in_b):
        return 0.7

    # Both categorised, and into groups that do not meet: that is a positive
    # statement that these are different things, not an absence of evidence.
    # A fallen tree and a flooded road on the same corner in the same minute are
    # two incidents. Falling through to a same-hazard score here was scoring
    # them 0.67 and sending them to the model to be asked about, which is both
    # a wasted call and a chance to merge them wrongly.
    if in_a and in_b:
        return 0.0

    ca, cb = taxonomy.categories.get(a), taxonomy.categories.get(b)
    if ca and cb and ca.hazard_id and ca.hazard_id == cb.hazard_id:
        return 0.4
    return 0.0


_WORD = re.compile(r"[a-z0-9]+")


def _text_similarity(a: str, b: str) -> float:
    """Jaccard over word sets.

    Deliberately crude. Reports are short, written under stress, in three
    languages and frequently transliterated. Something that degrades gracefully
    on "water enterd road" beats something clever that needs clean input, and
    the spatial and temporal components are carrying most of the weight anyway.
    """
    if not a or not b:
        return 0.3  # no evidence either way, not evidence of difference
    wa = set(_WORD.findall(a.lower()))
    wb = set(_WORD.findall(b.lower()))
    if not wa or not wb:
        return 0.3
    inter = len(wa & wb)
    union = len(wa | wb)
    return inter / union if union else 0.0


def _spatial(distance_m: float, radius_m: int) -> float:
    """1.0 on top of each other, decaying to 0 at the category's radius."""
    if distance_m <= 0:
        return 1.0
    if distance_m >= radius_m:
        return 0.0
    return 1.0 - (distance_m / radius_m) ** 1.5


def _temporal(age_minutes: float, window_min: int) -> float:
    if age_minutes <= 0:
        return 1.0
    if age_minutes >= window_min:
        return 0.0
    return 1.0 - (age_minutes / window_min) ** 1.2


async def find_candidates(
    *,
    ward_id: str,
    category: str,
    lng: float,
    lat: float,
    note: str,
    now: datetime,
    city_id: str = "pune",
    sim_run_id: str | None = None,
) -> list[Candidate]:
    """Open incidents that could be this same event, best first.

    The radius and the time window come from `incident_categories`, so a city
    with wider streets or a hazard that spreads differently is a row change
    rather than a code change.
    """
    ref = taxonomy.categories.get(category)
    radius_m = ref.dedup_radius_m if ref else 250
    window_min = ref.dedup_window_min if ref else 45

    # Search a little wider than the radius so a near-miss is still scored and
    # can be seen in the trace, rather than vanishing at the boundary.
    search_m = int(radius_m * 1.4)

    rows = await db.fetch(
        """
        with me as (
          select extensions.ST_SetSRID(
                   extensions.ST_MakePoint($1, $2), 4326
                 )::extensions.geography as g
        )
        select i.id::text as id, i.title, i.category, i.severity, i.report_count,
               extensions.ST_Distance(i.location, me.g) as distance_m,
               extract(epoch from ($3::timestamptz - i.updated_at)) / 60.0 as age_minutes,
               coalesce((
                 select string_agg(r.note, ' ')
                   from citizen_reports r
                  where r.incident_id = i.id and r.note <> ''
               ), '') as note_sample
          from incidents i, me
         where i.city_id = $4
           and i.sim_run_id is not distinct from $5::uuid
           and i.status <> 'resolved'
           and extensions.ST_DWithin(i.location, me.g, $6)
           and i.updated_at > $3::timestamptz - make_interval(mins => $7)
         order by distance_m
         limit 12
        """,
        lng, lat, now, city_id, sim_run_id, search_m, window_min * 2,
    )

    candidates: list[Candidate] = []
    for r in rows:
        cand = Candidate(
            incident_id=r["id"],
            title=r["title"],
            category=r["category"],
            severity=r["severity"],
            distance_m=float(r["distance_m"]),
            age_minutes=max(0.0, float(r["age_minutes"])),
            report_count=r["report_count"],
            note_sample=r["note_sample"] or "",
        )
        cand.components = {
            "spatial": _spatial(cand.distance_m, radius_m),
            "temporal": _temporal(cand.age_minutes, window_min),
            "category": _category_compatible(category, cand.category),
            "text": _text_similarity(note, cand.note_sample),
        }
        # Category incompatibility is a veto, not a discount. A fallen tree and
        # a heat casualty at the same corner in the same minute are two
        # incidents, and averaging the other three components would merge them.
        cand.score = (
            0.0
            if cand.components["category"] == 0.0
            else round(sum(WEIGHTS[k] * v for k, v in cand.components.items()), 4)
        )
        candidates.append(cand)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


@dataclass(slots=True)
class Decision:
    action: str  # "link" | "new" | "adjudicate"
    candidate: Candidate | None
    rationale: str


def decide(candidates: list[Candidate]) -> Decision:
    """Link, open a new incident, or send one pair to the model."""
    if not candidates or candidates[0].score < LINK_NEW:
        best = candidates[0].score if candidates else 0.0
        return Decision(
            "new",
            None,
            (
                "Nothing open nearby describes this. Best match scored "
                f"{best:.2f}, below the {LINK_NEW:.2f} threshold."
                if candidates
                else "No open incident within the category's radius and time window."
            ),
        )
    top = candidates[0]
    if top.score >= LINK_AUTO:
        return Decision(
            "link",
            top,
            (
                f"Matches {top.title} at {top.score:.2f}: "
                f"{top.distance_m:.0f} m away, {top.age_minutes:.0f} min old, "
                f"category {top.category}."
            ),
        )
    return Decision(
        "adjudicate",
        top,
        (
            f"Ambiguous at {top.score:.2f} against {top.title} "
            f"({top.distance_m:.0f} m, {top.age_minutes:.0f} min). "
            "Sending this one pair for a judgement."
        ),
    )


ADJUDICATION_SYSTEM = (
    "You decide whether two disaster reports describe the SAME physical "
    "incident. Answer with one word, SAME or DIFFERENT, then one short sentence "
    "of reasoning. Consider that people describe the same flooded junction in "
    "different words and languages, and that two genuinely separate problems can "
    "sit on the same street. When you are not sure, answer DIFFERENT: merging "
    "two real incidents hides one of them, which is worse than carrying a "
    "duplicate that a human can merge later."
)


async def adjudicate(new_note: str, category: str, candidate: Candidate) -> tuple[bool, str]:
    """One model call, for the ambiguous band only.

    Deliberately narrow. The model is not asked to cluster, to score, or to
    decide severity. It is asked one yes-or-no question about one pair, with a
    stated bias toward not merging, and its answer is recorded next to the link.
    """
    from app.agents import guardrails, llm

    new_note = guardrails.clean_input(new_note, limit=400).text
    prompt = (
        f"Existing incident: {candidate.title}\n"
        f"  category: {candidate.category}\n"
        f"  reports so far: {candidate.report_count}\n"
        f"  text from those reports (data): {guardrails.quote(guardrails.clean_input(candidate.note_sample, limit=400).text) if candidate.note_sample else '(none)'}\n\n"
        f"New report:\n"
        f"  category: {category}\n"
        f"  distance from the incident: {candidate.distance_m:.0f} m\n"
        f"  time since the incident was last updated: {candidate.age_minutes:.0f} min\n"
        f"  text (data): {guardrails.quote(new_note) if new_note else '(none)'}\n"
    )
    fallback = (
        "DIFFERENT. Deterministic fallback: the automatic score was in the "
        "uncertain band and no model was available to break the tie, so the "
        "safer answer is to keep them separate."
    )
    completion = await llm.complete(ADJUDICATION_SYSTEM, prompt, fallback=fallback, task="adjudicate")
    text = (completion.text or "").strip()
    same = text.upper().startswith("SAME")
    return same, text[:500]
