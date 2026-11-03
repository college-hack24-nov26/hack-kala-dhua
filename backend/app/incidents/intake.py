"""Report intake.

One door. Everything that claims something is happening comes through here: a
resident on the citizen portal, a field operator on a phone, an agency feed, a
sensor or gauge, and the simulator.
They differ in one field, `source`, which feeds the trust score. Nothing else
downstream can tell them apart, and that is the property the whole simulation
design rests on: a report a human types during a running simulation is processed
by this exact function and can therefore change the outcome.

The order matters and is not arbitrary:

    isolate text -> trust -> cluster -> incident -> needs -> events

Text is isolated before anything reads it, because report text is untrusted
input that later reaches a language model. Trust is scored before clustering so
a burst of fabricated reports cannot manufacture its own corroboration. Needs
are computed from the incident, not the report, because four reports of one
flooded road need one pump between them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.db import session as db
from app.incidents import clustering, trust
from app.taxonomy import cache as taxonomy
from app.world import events as ev
from app.world.clock import WALL, Clock

log = get_logger(__name__)

#: The holding category (migration 018). A report filed under it is never
#: turned into an incident automatically; see `receive` and `classify_held`.
UNCLASSIFIED = "unknown_report"


# --------------------------------------------------------- input isolation ---
#: Phrases that are trying to talk to the agent rather than describe the world.
#: This is not a content filter and does not reject the report; it flags it and
#: feeds the anomaly component. The real defence is structural: the agent is
#: handed a typed Evidence object and told the quoted text is data.
_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?previous|disregard\s+(the\s+)?above|system\s*prompt|"
    r"you\s+are\s+now|act\s+as\s+|new\s+instructions?|send\s+(all|every)\s+"
    r"(ambulance|unit|boat|resource)|</?(system|assistant|user)>)",
    re.I,
)


@dataclass(slots=True)
class Evidence:
    """What an agent is allowed to see of a report.

    The free text survives as `quoted_text` and is never spliced into a prompt
    as though it were an instruction. Everything an agent acts on is a typed
    field beside it.
    """

    category: str
    ward_id: str
    location: tuple[float, float]
    quoted_text: str
    has_photo: bool
    source: str
    injection_suspected: bool


def isolate(note: str, *, category: str, ward_id: str, location: tuple[float, float],
            has_photo: bool, source: str) -> Evidence:
    suspicious = bool(_INJECTION.search(note or ""))
    if suspicious:
        log.warning("report_injection_suspected", ward_id=ward_id, source=source)
    return Evidence(
        category=category,
        ward_id=ward_id,
        location=location,
        quoted_text=(note or "")[:2000],
        has_photo=has_photo,
        source=source,
        injection_suspected=suspicious,
    )


# ------------------------------------------------------------------ result ---
@dataclass(slots=True)
class IntakeResult:
    report_id: str
    incident_id: str | None
    created_incident: bool
    linked: bool
    trust: trust.Trust
    link_score: float
    link_rationale: str
    decided_by: str
    injection_suspected: bool
    event_id: int

    @property
    def summary(self) -> str:
        if self.created_incident:
            return f"Opened a new incident. {trust.explain(self.trust)}"
        if self.linked:
            return (
                f"Merged into an existing incident at {self.link_score:.2f} "
                f"({self.decided_by}). {trust.explain(self.trust)}"
            )
        return f"Held without an incident. {trust.explain(self.trust)}"


# ----------------------------------------------------------------- helpers ---
#: Reporter keys of authenticated machine sources (gateway key / integration token):
#: the drone swarm, edge-AI cameras, LoRa field nodes.
MACHINE_KEY_PREFIXES = ("device:sensor:",)


def is_machine_source(source: str, reporter_key: str | None) -> bool:
    return source in ("sensor", "field", "agency") and bool(reporter_key) and reporter_key.startswith(MACHINE_KEY_PREFIXES)


async def _trust_inputs(
    *,
    conn: Any,
    source: str,
    reporter_id: str | None,
    reporter_key: str | None,
    device_id: str | None,
    ward_id: str,
    category: str,
    lng: float,
    lat: float,
    note: str,
    has_photo: bool,
    now: datetime,
    corroborations: int,
    photo_agreement: float | None = None,
) -> trust.TrustInputs:
    # Keyed on `reporter_key`, not on the account id. Almost nobody reporting a
    # flood is signed in, so keying on `reporter_id` meant the lookup returned
    # nothing on every real report and every reporter scored a flat 0.5 forever
    # — a learning component that could not learn.
    reliability = None
    if reporter_key:
        reliability = await conn.fetchval(
            "select reliability from reporter_reliability where reporter_key = $1",
            reporter_key,
        )

    risk = await conn.fetchval(
        """
        select wr.score
          from ward_risks wr
          join hazard_runs hr on hr.id = wr.run_id
         where wr.ward_id = $1
         order by hr.started_at desc
         limit 1
        """,
        ward_id,
    )

    # Category plausibility: does this category belong to a hazard that anything
    # has actually scored recently? An unknown category is treated as plausible
    # rather than penalised, because a new category is a configuration change,
    # not a lie.
    ref = taxonomy.categories.get(category)
    plausible = True
    if ref and ref.hazard_id and risk is not None:
        active = await conn.fetchval(
            """
            select hazard from hazard_runs
             where started_at > $1::timestamptz - interval '12 hours'
             order by started_at desc limit 1
            """,
            now,
        )
        plausible = active is None or active == ref.hazard_id

    recent = 0
    near_dupe = 0
    speed = None
    # Burst, near-duplicate-text and implied-speed checks catch one person (or one
    # phone) faking many reports. An authenticated machine source is not one
    # person: a drone swarm or a camera network is many sensors behind one key,
    # legitimately reporting several places within minutes, and the planner
    # itself makes it re-file the same need. Those three rules made every drone
    # report look like spam (trust ~14%, quarantined). Machine sources keep every
    # other component, and their reliability is learned from outcomes as usual.
    machine = is_machine_source(source, reporter_key)
    if (device_id or reporter_id) and not machine:
        recent = await conn.fetchval(
            """
            select count(*) from citizen_reports
             where created_at > $1::timestamptz - interval '15 minutes'
               and (($2::text is not null and device_id = $2)
                 or ($3::uuid is not null and reporter_id = $3::uuid))
            """,
            now, device_id, reporter_id,
        ) or 0
        if note.strip():
            near_dupe = await conn.fetchval(
                """
                select count(*) from citizen_reports
                 where created_at > $1::timestamptz - interval '60 minutes'
                   and (($2::text is not null and device_id = $2)
                     or ($3::uuid is not null and reporter_id = $3::uuid))
                   and lower(btrim(note)) = lower(btrim($4))
                """,
                now, device_id, reporter_id, note,
            ) or 0
        prev = await conn.fetchrow(
            """
            select extensions.ST_Distance(
                     location,
                     extensions.ST_SetSRID(extensions.ST_MakePoint($4,$5),4326)::extensions.geography
                   ) / 1000.0 as km,
                   extract(epoch from ($1::timestamptz - created_at)) / 3600.0 as hours
              from citizen_reports
             where (($2::text is not null and device_id = $2)
                 or ($3::uuid is not null and reporter_id = $3::uuid))
               and created_at < $1::timestamptz
             order by created_at desc limit 1
            """,
            now, device_id, reporter_id, lng, lat,
        )
        if prev and prev["hours"] and float(prev["hours"]) > 0:
            hours = float(prev["hours"])
            if hours < 1.0:  # only meaningful over a short gap
                speed = float(prev["km"]) / hours

    return trust.TrustInputs(
        source=source,
        reporter_id=reporter_id,
        reporter_reliability=float(reliability) if reliability is not None else None,
        ward_risk=float(risk) if risk is not None else None,
        category_plausible=plausible,
        corroborations=corroborations,
        has_photo=has_photo,
        recent_from_source=int(recent),
        near_duplicate_text=int(near_dupe),
        implied_speed_kmh=speed,
        # What the photo said, if anything looked at it. `has_photo` only knows
        # that a file was attached; this knows whether the file agreed with the
        # sentence, which is the difference between "they sent something" and
        # "the picture shows knee-deep water exactly as they typed".
        photo_agreement=photo_agreement,
    )


def _title(category: str, ward_name: str) -> str:
    ref = taxonomy.categories.get(category)
    label = ref.display_name if ref else category.replace("_", " ").capitalize()
    return f"{label} — {ward_name}"


async def _recompute_incident(conn: Any, incident_id: str, clock: Clock) -> dict:
    """Report count, confidence and severity, from the reports themselves.

    Confidence is the aggregate trust of the reports that support the incident,
    lifted by independent corroboration. Severity starts from the category's
    base and rises with corroboration and the worst individual report, capped at
    5. None of it comes from a model.
    """
    row = await conn.fetchrow(
        """
        select count(*)::int                         as n,
               count(distinct coalesce(r.reporter_id::text, r.device_id, r.id::text))::int as independent,
               avg(coalesce(r.trust_score, 0.5))     as avg_trust,
               max(coalesce(r.trust_score, 0.5))     as max_trust,
               min(r.created_at)                     as first_at,
               -- The worst reading of any report trusted enough to act on.
               max(r.assessed_severity) filter (
                 where coalesce(r.trust_score, 0.5) >= $2)  as assessed
          from citizen_reports r
         where r.incident_id = $1::uuid
           and r.verification_status <> 'rejected'
        """,
        incident_id, trust.QUARANTINE,
    )
    n = row["n"] or 0
    independent = row["independent"] or 0
    avg_trust = float(row["avg_trust"] or 0.5)

    cat = await conn.fetchval("select category from incidents where id = $1::uuid", incident_id)
    ref = taxonomy.categories.get(cat)
    # Confidence is the fused belief over every node that sees this incident
    # (reports of every kind, cameras, drones, crews), not the average of report
    # scores: see app/incidents/fusion.py.
    try:
        from app.incidents import fusion
        async with conn.transaction():          # savepoint: a failed query here never aborts the intake
            _, items, ctx = await fusion.gather(conn, incident_id)
        belief = fusion.fuse(items, category=cat or "", life_safety=bool(ref and ref.life_safety),
                             can_fly=ctx.get("can_fly", False), cameras_near=ctx.get("cameras_near", False))
        confidence = belief.p
        avg_trust = belief.p
    except Exception as exc:  # noqa: BLE001 - never lose a report over the belief calculation
        log.warning("fusion_failed", incident=incident_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        corroboration_lift = 1.0 - 1.0 / (1.0 + 0.8 * max(0, independent - 1))
        confidence = min(0.99, avg_trust + (1.0 - avg_trust) * corroboration_lift)
    base = ref.base_severity if ref else 3
    rule = min(5, base + (1 if independent >= 3 else 0) + (1 if independent >= 6 else 0))
    # The text's own reading (bounded in severity.py) can lower an unclassified
    # "wassup" to 2 or raise "he is unconscious" to 5; corroboration can only
    # raise it further.
    assessed = row["assessed"]
    floor = rule if (independent >= 3 or getattr(ref, "life_safety", False)) else rule - 1
    severity = rule if assessed is None else max(1, min(5, max(int(assessed), floor)))

    await conn.execute(
        """
        update incidents
           set report_count = $2,
               confidence   = $3,
               severity     = $4,
               trust_score  = $5,
               first_reported_at = coalesce(first_reported_at, $6),
               updated_at   = $7
         where id = $1::uuid
        """,
        incident_id, n, round(confidence, 4), severity, round(avg_trust, 4),
        row["first_at"], clock.now(),
    )
    return {"report_count": n, "independent": independent,
            "confidence": round(confidence, 4), "severity": severity}


async def _assess_severity(note: str, *, category: str, source: str, simulated: bool):
    from app.core.config import settings
    from app.incidents import parse as parser
    from app.incidents import severity as sev

    ref = taxonomy.categories.get(category)
    base = ref.base_severity if ref else 3
    try:
        boost = parser.parse(note).urgency_boost if note else 0
    except Exception:  # noqa: BLE001
        boost = 0
    use_models = bool(getattr(settings, "severity_models_enabled", True)) and not simulated \
        and source not in ("sim", "field", "agency")
    return await sev.assess(note or "", base=base, urgency_boost=boost, use_models=use_models,
                            life_safety=bool(getattr(ref, "life_safety", False)))


#: What a report's words add to its category's needs: a fire with people
#: trapped needs a rescue team and an ambulance as well as a tender, and a
#: crowd or blocked junction needs police to keep the scene reachable.
#: English, Hindi and Marathi stems; only capabilities that exist are added.
EXTRA_NEEDS: tuple[tuple[tuple[str, ...], dict[str, int]], ...] = (
    (("trapped", "stuck inside", "people inside", "inside the building", "children inside", "फंसे", "फसे",
      "अडकले", "अडकलेले"), {"search_rescue": 1, "medical_transport": 1}),
    (("injured", "injury", "burn", "bleeding", "unconscious", "casualt", "घायल", "जखमी"), {"medical_transport": 1}),
    (("crowd", "traffic", "jam", "gathered", "भीड़", "गर्दी", "वाहतूक"), {"traffic_control": 1}),
)


def needs_from_text(note: str) -> dict[str, int]:
    t = (note or "").lower()
    out: dict[str, int] = {}
    for words, adds in EXTRA_NEEDS:
        if any(w in t for w in words):
            for cap, q in adds.items():
                out[cap] = max(out.get(cap, 0), q)
    return out


async def _write_needs(conn: Any, incident_id: str, category: str, clock: Clock, note: str = "") -> dict[str, int]:
    """What this incident requires, from the category's capability needs.

    Requirements are per incident, not per report. Four people reporting one
    flooded road need one pump between them, and that is the entire reason
    clustering has to happen before this does.

    The report's own words can add needs (`EXTRA_NEEDS`): one incident then
    gets a team of different units, e.g. fire tender + ambulance + police +
    rescue team for a fire with people trapped. Needs only grow from words;
    nothing a report says removes a need its category has.
    """
    ref = taxonomy.categories.get(category)
    needs = dict(ref.needs) if ref else {}
    for cap, q in needs_from_text(note).items():
        needs[cap] = max(needs.get(cap, 0), q)
    if needs:
        known = {r["id"] for r in await conn.fetch("select id from capabilities where id = any($1::text[])", list(needs))}
        needs = {c: q for c, q in needs.items() if c in known}
    if not needs:
        return {}
    await conn.executemany(
        """
        insert into incident_needs (incident_id, capability_id, required, updated_at)
        values ($1::uuid, $2, $3, $4)
        on conflict (incident_id, capability_id)
        do update set required = greatest(incident_needs.required, excluded.required), updated_at = excluded.updated_at
        """,
        [(incident_id, cap, qty, clock.now()) for cap, qty in needs.items()],
    )
    return needs


async def _attach(conn: Any, *, decision: Any, report_id: str, category: str, note: str,
                  ward_id: str, ward_name: str, city_id: str, lng: float, lat: float,
                  occurred: datetime, now: datetime, clock: Clock, trust_score: float,
                  caused_by: int) -> tuple[str, bool, float, str, str]:
    """Link a stored report to the incident it belongs to, or open one.

    Shared by `receive` and `classify_held`, so a report an officer classifies
    later goes through exactly the clustering, needs and events an automatically
    classified one does.
    """
    cat_ref = taxonomy.categories.get(category)
    decided_by = "auto"
    rationale = decision.rationale
    target: str | None = None
    link_score = decision.candidate.score if decision.candidate else 0.0

    if decision.action == "adjudicate" and decision.candidate is not None:
        same, answer = await clustering.adjudicate(note, category, decision.candidate)
        decided_by = "llm"
        rationale = f"{decision.rationale} Model: {answer}"
        target = decision.candidate.incident_id if same else None
    elif decision.action == "link" and decision.candidate is not None:
        target = decision.candidate.incident_id

    created = False
    if target is None:
        hazard = (cat_ref.hazard_id if cat_ref and cat_ref.hazard_id else "flood")
        inc = await conn.fetchrow(
            """
            insert into incidents
              (title, category, hazard, ward_id, city_id, location, severity,
               status, report_count, confidence, trust_score, first_reported_at,
               sim_run_id, created_at, updated_at)
            values ($1,$2,$3,$4,$5,
                    extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography,
                    $8,'reported',1,$9,$9,$10,$11::uuid,$12,$12)
            returning id::text
            """,
            _title(category, ward_name), category, hazard, ward_id, city_id,
            lng, lat, (cat_ref.base_severity if cat_ref else 3), trust_score,
            occurred, clock.sim_run_id, now,
        )
        target = inc["id"]
        created = True

    await conn.execute(
        "update citizen_reports set incident_id = $2::uuid where id = $1::uuid",
        report_id, target,
    )
    await conn.execute(
        """
        insert into report_links
          (report_id, incident_id, link_score, components, decided_by, rationale, created_at)
        values ($1::uuid,$2::uuid,$3,$4,$5,$6,$7)
        on conflict (report_id, incident_id) do nothing
        """,
        report_id, target, link_score,
        decision.candidate.components if decision.candidate else {},
        decided_by, rationale, now,
    )

    stats = await _recompute_incident(conn, target, clock)
    needs = await _write_needs(conn, target, category, clock, note)

    if created:
        await ev.append(
            clock=clock, kind=ev.Kind.INCIDENT_OPENED, actor=ev.agent("triage"),
            subject_type="incident", subject_id=target, city_id=city_id, ward_id=ward_id,
            payload={"category": category, "severity": stats["severity"],
                     "needs": needs, "rationale": rationale},
            caused_by=caused_by, conn=conn,
        )
    else:
        await ev.append(
            clock=clock, kind=ev.Kind.REPORT_LINKED, actor=ev.agent("triage"),
            subject_type="incident", subject_id=target, city_id=city_id, ward_id=ward_id,
            payload={
                "report_id": report_id, "link_score": link_score,
                "decided_by": decided_by, "rationale": rationale,
                "report_count": stats["report_count"],
                "independent_reporters": stats["independent"],
                "confidence": stats["confidence"],
                # This is the number the console shows as "merged, not
                # dispatched twice".
                "duplicates_absorbed": stats["report_count"] - 1,
            },
            caused_by=caused_by, conn=conn,
        )

    return target, created, link_score, decided_by, rationale


# -------------------------------------------------------------- the intake ---
async def receive(
    *,
    ward_id: str,
    category: str,
    location: tuple[float, float],
    note: str = "",
    photo_url: str | None = None,
    #: How well an attached photo agreed with what was typed, -1..1, from
    #: `vision.assess`. None when there was no photo, or nothing looked at it.
    #:
    #: This parameter is why every typed citizen report returned 500 for three
    #: hours: the route had already been taught to redeem a photo-evidence token
    #: and pass the agreement figure, and `receive` had never been taught to
    #: accept it, so every call died on `TypeError: unexpected keyword argument`
    #: before it touched the database. The `/reports` route, which does not send
    #: it, kept working — which is exactly why the pipeline looked healthy.
    photo_agreement: float | None = None,
    #: The full validated reading from the vision model, as
    #: `vision.as_dict(evidence)`. Stored with the report rather than used and
    #: dropped: the number below is what the scorer consumed, and this is the
    #: evidence for it — which an officer has to be able to check before acting
    #: on a trust score, and which is the only durable record of what the city's
    #: photographs actually showed.
    photo_evidence: dict[str, Any] | None = None,
    source: str = "app",
    reporter_id: str | None = None,
    #: Who to credit this report to for reliability purposes. 'user:<uuid>' for
    #: somebody signed in, 'device:<uuid>' for a phone that has reported before,
    #: None when there is genuinely nothing to attribute it to. Deliberately not
    #: `reporter_id`, which is a foreign key to auth.users and therefore can only
    #: ever describe an account — which is exactly why reliability never learned.
    reporter_key: str | None = None,
    reporter_name: str = "Anonymous",
    device_id: str | None = None,
    occurred_at: datetime | None = None,
    city_id: str = "pune",
    clock: Clock = WALL,
    #: How sure the classifier was of `category` and which tier decided it
    #: (keyword / classifier / model / chosen / detector). None means the caller
    #: named the category itself (a staff form, a feed, a mapped detector).
    classification_confidence: float | None = None,
    classification_method: str | None = None,
) -> IntakeResult:
    """Take one report all the way to an incident, or decline to.

    Everything happens in one transaction, including the events, so a report
    that fails halfway leaves no incident claiming reports that were never
    written and no event describing something that did not happen.
    """
    lng, lat = location
    now = clock.now()
    occurred = occurred_at or now

    # Derived here rather than at each of the eight call sites, so there is one
    # rule and it cannot drift: an account when there is one, otherwise the
    # device. Doing it per-caller is how you end up with 'device:abc' in the
    # citizen route and 'device:device-abc' in the simulator, two keys for one
    # phone, and a reliability history that silently splits in half.
    if reporter_key is None:
        reporter_key = (
            f"user:{reporter_id}" if reporter_id
            else f"device:{device_id}" if device_id
            else None
        )

    if category not in taxonomy.categories:
        from app.taxonomy import UnknownTaxonomyValue

        raise UnknownTaxonomyValue("incident category", category, list(taxonomy.categories))

    ev_obj = isolate(
        note, category=category, ward_id=ward_id, location=(lng, lat),
        has_photo=bool(photo_url), source=source,
    )

    # How bad is it, from the words: keywords, then a classifier, then the LLM,
    # all bounded (see app/incidents/severity.py). Outside the transaction: a
    # slow model must not hold row locks. Simulated reports skip the models.
    severity_read = await _assess_severity(note, category=category, source=source,
                                           simulated=clock.sim_run_id is not None)

    async with db.transaction() as conn:
        ward_name = await conn.fetchval("select name from wards where id = $1", ward_id) or ward_id

        # Cluster first, so corroboration can be counted before trust is scored.
        candidates = await clustering.find_candidates(
            ward_id=ward_id, category=category, lng=lng, lat=lat, note=note,
            now=occurred, city_id=city_id, sim_run_id=clock.sim_run_id,
        )
        decision = clustering.decide(candidates)

        corroborations = 0
        if decision.candidate is not None:
            # Independent means a different person or device. Counting a
            # reporter's own repeated reports as corroboration is precisely the
            # hole a report flood walks through.
            corroborations = await conn.fetchval(
                """
                select count(distinct coalesce(reporter_id::text, device_id, id::text))
                  from citizen_reports
                 where incident_id = $1::uuid
                   and ($2::text is null or reporter_id::text is distinct from $2::text)
                   and ($3::text is null or device_id is distinct from $3::text)
                """,
                decision.candidate.incident_id, reporter_id, device_id,
            ) or 0

        t_inputs = await _trust_inputs(
            conn=conn, source=source, reporter_id=reporter_id,
            reporter_key=reporter_key, device_id=device_id,
            ward_id=ward_id, category=category, lng=lng, lat=lat, note=note,
            has_photo=bool(photo_url), now=occurred, corroborations=int(corroborations),
            photo_agreement=photo_agreement,
        )
        if ev_obj.injection_suspected:
            t_inputs.near_duplicate_text += 1  # feeds the anomaly component

        cat_ref = taxonomy.categories.get(category)
        t = trust.score(t_inputs, life_safety=bool(cat_ref and cat_ref.life_safety))
        if ev_obj.injection_suspected:
            t.reasons.append(
                "The report text contains instruction-like phrasing. It is "
                "treated as data and flagged, never executed."
            )

        # Nothing could tell what this describes. It is stored, shown in the
        # inbox as held and counted, but it opens no incident and joins none:
        # an incident carries a category, the category carries needs, and the
        # needs move vehicles. "Not yet classified" was opening incidents and
        # putting them on the wall, which is a guess presented as a fact.
        unclassified = category == UNCLASSIFIED
        class_conf = (0.0 if unclassified
                      else round(float(classification_confidence), 3) if classification_confidence is not None
                      else 1.0)

        report_row = await conn.fetchrow(
            """
            insert into citizen_reports
              (ward_id, city_id, category, location, note, photo_path, classified_as,
               reporter_id, reporter_name, source, device_id, occurred_at,
               trust_score, trust_breakdown, verification_status, classification_confidence,
               sim_run_id, created_at, reporter_key, photo_evidence, photo_agreement,
               assessed_severity, severity_assessment)
            values ($1,$2,$3,
                    extensions.ST_SetSRID(extensions.ST_MakePoint($4,$5),4326)::extensions.geography,
                    $6,$7,$3,$8::uuid,$9,$10,$11,$12,$13,$14,$15,$16,$17::uuid,$18,$19,
                    $20::jsonb,$21,$22,$23::jsonb)
            returning id::text, created_at
            """,
            ward_id, city_id, category, lng, lat, note, photo_url,
            reporter_id, reporter_name, source, device_id, occurred,
            t.score, {"components": t.components, "reasons": t.reasons,
                      "classification": {"method": classification_method or "given",
                                         "confidence": class_conf}},
            "pending" if unclassified else t.status, class_conf, clock.sim_run_id, now, reporter_key,
            json.dumps(photo_evidence) if photo_evidence else None, photo_agreement,
            severity_read.severity, severity_read.as_dict(),
        )
        report_id = report_row["id"]

        received = await ev.append(
            clock=clock, kind=ev.Kind.REPORT_RECEIVED, actor=ev.citizen(reporter_id),
            subject_type="report", subject_id=report_id, city_id=city_id, ward_id=ward_id,
            payload={
                "source": source, "category": category, "trust": t.score,
                "verification": t.status,
                "injection_suspected": ev_obj.injection_suspected,
                "occurred_at": occurred.isoformat(),
            },
            conn=conn,
        )

        if unclassified:
            reason = ("Held for a person to classify: no keyword, classifier or "
                      "model could tell what this describes, so it opens no incident.")
            await ev.append(
                clock=clock, kind=ev.Kind.REPORT_HELD, actor=ev.agent("triage"),
                subject_type="report", subject_id=report_id, city_id=city_id, ward_id=ward_id,
                payload={"trust": t.score, "reason": reason, "source": source,
                         "method": classification_method or "given"},
                caused_by=received.id, conn=conn,
            )
            return IntakeResult(
                report_id=report_id, incident_id=None, created_incident=False, linked=False,
                trust=t, link_score=0.0, link_rationale=reason,
                decided_by="auto", injection_suspected=ev_obj.injection_suspected,
                event_id=received.id,
            )

        # A quarantined report is recorded and visible, but does not get to open
        # an incident or join one. It has not been called false; it has been
        # stopped from spending a vehicle.
        if t.status == "quarantined":
            await ev.append(
                clock=clock, kind=ev.Kind.REPORT_REJECTED, actor=ev.agent("triage"),
                subject_type="report", subject_id=report_id, city_id=city_id, ward_id=ward_id,
                payload={"trust": t.score, "reasons": t.reasons},
                caused_by=received.id, conn=conn,
            )
            return IntakeResult(
                report_id=report_id, incident_id=None, created_incident=False, linked=False,
                trust=t, link_score=0.0, link_rationale="Held before clustering.",
                decided_by="auto", injection_suspected=ev_obj.injection_suspected,
                event_id=received.id,
            )

        target, created, link_score, decided_by, rationale = await _attach(
            conn, decision=decision, report_id=report_id, category=category, note=note,
            ward_id=ward_id, ward_name=ward_name, city_id=city_id, lng=lng, lat=lat,
            occurred=occurred, now=now, clock=clock, trust_score=t.score, caused_by=received.id,
        )

        return IntakeResult(
            report_id=report_id, incident_id=target, created_incident=created,
            linked=not created, trust=t, link_score=link_score, link_rationale=rationale,
            decided_by=decided_by, injection_suspected=ev_obj.injection_suspected,
            event_id=received.id,
        )


async def classify_held(report_id: str, category: str, *, officer: str, clock: Clock = WALL) -> IntakeResult:
    """A person reads a held report and says what it is; it then goes through
    the same clustering, incident and needs path as any classified report.

    Only an unclassified report can be classified this way, and only into a
    category a report can be (not a system-only one such as an evacuation).
    """
    from app.incidents import parse

    if category not in taxonomy.categories or category in parse.SYSTEM_ONLY:
        from app.taxonomy import UnknownTaxonomyValue

        raise UnknownTaxonomyValue("incident category", category, parse.reportable())

    row = await db.fetchrow(
        """
        select id::text, ward_id, city_id, note, category, incident_id::text incident_id,
               extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat,
               coalesce(occurred_at, created_at) occurred, trust_score, trust_breakdown,
               reporter_id::text reporter_id
          from citizen_reports where id = $1::uuid
        """,
        report_id,
    )
    if row is None:
        raise LookupError("No such report.")
    if row["category"] != UNCLASSIFIED or row["incident_id"]:
        raise ValueError("Only a held, unclassified report can be classified here.")

    now = clock.now()
    lng, lat, note = float(row["lng"]), float(row["lat"]), row["note"] or ""
    candidates = await clustering.find_candidates(
        ward_id=row["ward_id"], category=category, lng=lng, lat=lat, note=note,
        now=row["occurred"], city_id=row["city_id"], sim_run_id=clock.sim_run_id,
    )
    decision = clustering.decide(candidates)
    score = float(row["trust_score"] or 0.5)
    # An officer has read it: it may act, but it still needs corroboration
    # unless the scorer had already trusted it fully.
    ref = taxonomy.categories.get(category)
    status = ("auto_confirmed" if score >= trust.AUTO_CONFIRM or (ref and ref.life_safety)
              else "needs_corroboration")

    async with db.transaction() as conn:
        ward_name = await conn.fetchval("select name from wards where id = $1", row["ward_id"]) or row["ward_id"]
        await conn.execute(
            """
            update citizen_reports
               set category = $2, classified_as = $2, classification_confidence = 1.0,
                   verification_status = $3,
                   trust_breakdown = coalesce(trust_breakdown, '{}'::jsonb)
                                     || jsonb_build_object('classification',
                                          jsonb_build_object('method', 'officer', 'confidence', 1.0, 'by', $4::text))
             where id = $1::uuid
            """,
            report_id, category, status, officer,
        )
        classified = await ev.append(
            clock=clock, kind=ev.Kind.REPORT_CLASSIFIED, actor=ev.officer(officer),
            subject_type="report", subject_id=report_id, city_id=row["city_id"], ward_id=row["ward_id"],
            payload={"category": category, "from": UNCLASSIFIED,
                     "reason": f"Classified by {officer} after reading the report."},
            conn=conn,
        )
        target, created, link_score, decided_by, rationale = await _attach(
            conn, decision=decision, report_id=report_id, category=category, note=note,
            ward_id=row["ward_id"], ward_name=ward_name, city_id=row["city_id"], lng=lng, lat=lat,
            occurred=row["occurred"], now=now, clock=clock, trust_score=score, caused_by=classified.id,
        )

    t = trust.Trust(score=score, status=status, components={}, reasons=[f"Classified by {officer}."])
    return IntakeResult(
        report_id=report_id, incident_id=target, created_incident=created,
        linked=not created, trust=t, link_score=link_score, link_rationale=rationale,
        decided_by=decided_by, injection_suspected=False, event_id=classified.id,
    )
