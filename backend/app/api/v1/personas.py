"""The citizen and field surfaces.

Separate from the operations console on purpose, and not only for tidiness. A
ward officer has no use for a marker they can walk around, and a resident must
never be handed the whole fleet's position. The three interfaces answer three
different questions and they should not share an endpoint:

  * **citizen**: what is happening where I am, where should I go, and how do I
    tell someone. Scoped to their surroundings.
  * **field**: what am I assigned to, and how do I tell the control room that my
    vehicle has a puncture. Scoped to their agency.
  * **admin**: everything, and that is `demo.py`.

Scoping is enforced here and again in row level security, so this is the
convenient layer rather than the one holding the line.
"""

from __future__ import annotations

import json

import secrets
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import Field

from app.core.errors import BadRequest, Conflict, NotFound, UpstreamUnavailable
from app.core.logging import get_logger
from app.core import cache
from app.core.security import CurrentPrincipal, StaffPrincipal
from app.db import session as db
from app.db.repositories import queries as q
from app.guidance import router as guidance
from app.incidents import intake, parse, speech, vision, vision_client
from app.schemas.domain import Camel, CategoryId, LngLat
from app.taxonomy import UnknownTaxonomyValue
from app.taxonomy import cache as taxonomy
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)
router = APIRouter(tags=["personas"])

def _nearest_ward_or_refuse(loc) -> None:
    """Reports from outside every ward go to the nearest covered ward.

    A person a few kilometres past the boundary is still reporting something the
    nearest ward's officers need to hear about; refusing it helped nobody. Only a
    deployment with no wards at all refuses.
    """
    if loc.ward is None:
        raise BadRequest(loc.note or "No wards are configured for this deployment yet.")


def _filed_to(loc) -> dict:
    """Where the report was filed, said plainly when that was not where the person is."""
    return {
        "wardId": loc.ward.id,
        "wardName": loc.ward.name,
        "outsideArea": not loc.inside,
        "distanceKm": loc.distance_km,
        "filedNote": (
            None if loc.inside else
            f"You are about {loc.distance_km:g} km outside the covered area, so this was "
            f"sent to the nearest covered ward, {loc.ward.name}."
        ),
    }


# Camera and VLM detections are for the command centre. An incident that only
# cameras have reported stays off the resident and crew views until an operator
# confirms it (any status past 'reported') or a person reports it too. Used as
# `and not <CAMERA_ONLY>` against an `incidents i` row.
CAMERA_ONLY = """(
    i.status = 'reported'
    and exists (select 1 from citizen_reports cr where cr.incident_id = i.id)
    and not exists (
        select 1 from citizen_reports cr
         where cr.incident_id = i.id
           and coalesce(cr.reporter_key, '') not like 'device:sensor:%'
           and coalesce(cr.reporter_name, '') not like 'Sensor %'))"""


# =========================================================== citizen =========
class GuideIn(Camel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    intent: Literal[
        "shelter", "hospital", "safety", "ambulance",
        "food", "water", "medical_supplies",
    ] = "safety"
    #: Free text: "my father is bleeding", "chest pain". Used to pick a facility
    #: that can actually treat it, never to invent one.
    condition: str | None = None
    city_id: str = "pune"


@router.post("/citizen/guide")
async def citizen_guide(body: GuideIn, _: CurrentPrincipal) -> dict:
    """Where should I go, and why?

    Runs on facts: which facilities have room, which are near open incidents,
    which roads have been reported blocked. Returns the decision, the reasoning,
    the two runners-up it rejected and what it does not know. Sometimes the
    answer is "stay where you are", and that is a real answer rather than a
    failure to produce one.
    """
    g = await guidance.guide(
        lng=body.lng, lat=body.lat, intent=body.intent,
        condition=body.condition, city_id=body.city_id,
    )
    payload = {
        "intent": g.intent,
        "headline": g.headline,
        "shouldMove": g.should_move,
        "reasoning": g.reasoning,
        "warnings": g.warnings,
        "destination": asdict(g.destination) if g.destination else None,
        "alternatives": [asdict(a) for a in g.alternatives],
        "route": g.route,
        "routeKm": g.route_km,
        "routeMinutes": g.route_minutes,
        "routeEngine": g.route_engine,
        "routeSteps": g.route_steps,
        "hazardsConsidered": g.avoided_blocks,
        "exposedPoints": g.exposed_points,
    }
    # The control room sees what the public was told. An operator coordinating
    # against advice they do not know was issued is the coordination failure
    # this whole system exists to stop, and it would be an odd one to build in.
    from app.demo import runner as demo_runner

    if g.route:
        demo_runner.state.citizen_route = {
            "path": g.route,
            "headline": g.headline,
            "destination": g.destination.name if g.destination else None,
            "km": g.route_km,
            "minutes": g.route_minutes,
            "engine": g.route_engine,
            "steps": g.route_steps,
            "from": [body.lng, body.lat],
        }
    return payload


class CitizenReportIn(Camel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    #: Free text. The category is worked out from it rather than demanded.
    text: str = ""
    #: Optional override when someone does choose from the list.
    category: CategoryId | None = None
    photo_url: str | None = None
    #: Returned by `/citizen/vision/analyse`. Says "a photo was looked at, and
    #: the server remembers what it showed" without the client being able to say
    #: what it showed. The image itself never has to leave the phone.
    photo_token: str | None = None
    #: A stable per-install id the app keeps in local storage. Not identity —
    #: it is never linked to a person and survives nothing but a reinstall —
    #: but it is what separates one phone from another.
    #:
    #: Without it every anonymous report shared the device id `citizen-anon`,
    #: which meant the anti-spam components read the whole city's anonymous
    #: traffic as one very busy reporter: `recent_from_source` climbed with
    #: total volume and quietly pushed everybody's trust score down during
    #: exactly the surge the scoring exists to survive.
    device_id: str | None = Field(default=None, max_length=64)
    city_id: str = "pune"


@router.post("/citizen/report")
async def citizen_report(body: CitizenReportIn, principal: CurrentPrincipal) -> dict:
    """File a report by typing a sentence.

    Nobody picks from a dropdown standing in water. The text is classified by
    keyword first, in English, Hindi and Marathi, and only goes to a model when
    that is not confident. What it was read as, and from which words, comes back
    so the person can correct it.
    """
    loc = await q.locate_ward(body.lng, body.lat, body.city_id)
    _nearest_ward_or_refuse(loc)

    parsed = (
        parse.Parsed(category=body.category, confidence=1.0, method="chosen", note=body.text)
        if body.category
        else await parse.parse_with_model(body.text)
    )

    # A photo assessed a moment ago, redeemed here. Unknown or expired tokens
    # are ignored rather than refused: a report that arrives without the photo
    # evidence is a slightly less trusted report, and losing the report over a
    # bookkeeping detail would be the wrong trade in a flood.
    evidence = (
        cache.photo_evidence.peek(body.photo_token) if body.photo_token else None
    )

    try:
        result = await intake.receive(
            ward_id=loc.ward.id,
            category=parsed.category,
            location=(body.lng, body.lat),
            note=body.text,
            photo_url=body.photo_url or ("device://photo" if evidence else None),
            photo_agreement=evidence.agreement if evidence else None,
            photo_evidence=vision.as_dict(evidence) if evidence else None,
            source="app",
            reporter_id=principal.user_id,
            reporter_name=principal.full_name or "Resident",
            device_id=(
                f"citizen-{principal.user_id}" if principal.user_id
                else f"device-{body.device_id}" if body.device_id
                else "citizen-anon"
            ),
            city_id=body.city_id,
            clock=clocks.WALL,
            classification_confidence=parsed.confidence, classification_method=parsed.method,
        )
    except UnknownTaxonomyValue as exc:
        raise NotFound(str(exc)) from exc

    if body.photo_token and result.report_id:
        from app import media
        await media.attach_token(body.photo_token, result.report_id)

    # The control room hears about it.
    #
    # This was missing, and it was not cosmetic. The spoken path below already
    # beat and marked the world dirty; the typed path — which is the one almost
    # everybody uses — wrote its rows and told nothing. So a resident could file
    # a report, watch it land in the database, and see no beat on the console and
    # no re-plan, because `dirty` is what trips the re-allocation trigger. A
    # report that changes nothing is not a report, it is a log line.
    from app.demo import runner as demo_runner

    demo_runner.state.beat(
        "you",
        f"{principal.full_name or 'A resident'} reported: “{body.text[:70]}”"
        + (" (new incident)" if result.created_incident
           else " (merged into an open incident)" if result.linked
           else " (held: an officer will read it)"),
        incidentId=result.incident_id, reportId=result.report_id,
        wardId=loc.ward.id, trust=result.trust.score,
    )
    demo_runner.state.dirty = True

    return {
        "reportId": result.report_id,
        "incidentId": result.incident_id,
        "createdIncident": result.created_incident,
        "linked": result.linked,
        # Not classified: stored and shown to an officer, but no incident yet.
        "held": not result.created_incident and not result.linked,
        **_filed_to(loc),
        "readAs": parsed.category,
        "readAsLabel": (taxonomy.categories[parsed.category].display_name
                        if parsed.category in taxonomy.categories else parsed.category),
        "readHow": parsed.explanation,
        "readConfidence": parsed.confidence,
        "urgencyBoost": parsed.urgency_boost,
        "trust": result.trust.score,
        "trustStatus": result.trust.status,
        "trustReasons": result.trust.reasons,
        "linkScore": result.link_score,
        "summary": result.summary,
        # What the photo contributed, shown back rather than applied silently.
        # A person who attached a photo and saw the report trusted *less* for it
        # deserves to be told why, and an officer reading the queue needs the
        # same sentence.
        "photo": vision.as_dict(evidence) if evidence else None,
    }


class ArrivedIn(Camel):
    """I got there."""

    lifeline_id: str
    #: How many people walked in together. One phone, a family.
    party_size: int = Field(default=1, ge=1, le=20)


@router.post("/citizen/arrived")
async def citizen_arrived(body: ArrivedIn, principal: CurrentPrincipal) -> dict:
    """A resident reached a shelter, and the shelter now holds one more person.

    Worth being exact about what this is. The occupancy model in the demo
    runner estimates a crowd from the severity of the ward — useful, and a
    guess. This is not a guess: it is one account, at one facility, saying it
    arrived. Both write the same `occupancy` column, because the building does
    not care which of them a head came from, but the count of arrivals the
    system actually witnessed is kept separately so the console can distinguish
    evidence from estimate.

    Arriving also *consumes*. Before this a person could be routed to a relief
    centre, counted at the door, and take nothing off its shelves, because stock
    only ever moved on a model rate. A shelter whose occupancy rises while its
    supplies do not is a shelter that will read as coping right up to the moment
    it is not.
    """
    from app.demo import runner as demo_runner

    try:
        result = await demo_runner.arrive(
            lifeline_id=body.lifeline_id,
            party_size=body.party_size,
            reporter_id=principal.user_id,
            reporter_name=principal.full_name or "A resident",
        )
    except ValueError as exc:
        raise NotFound(str(exc)) from exc

    # The citizen view is cached for three seconds against its own poll; a
    # facility that just took someone in should not keep reporting the old
    # figure to the person standing in it.
    cache.citizen_state.clear()
    return result


class VisionIn(Camel):
    """A photo analysis, from wherever the model ran.

    `ranOn` is recorded rather than trusted. A result from the phone and a
    result from our own endpoint are subject to exactly the same validation and
    the same ceiling on what they may change, which is what makes running the
    model on the device a deployment choice rather than a security one.
    """

    report_id: str | None = None
    #: The model's raw JSON, exactly as it answered. Validated here.
    analysis: dict
    model: str = "unknown"
    ran_on: Literal["device", "server", "hosted"] = "device"
    #: What the reporter said it was, so the photo can be checked against it.
    category: CategoryId = "flooded_road"


class VoiceReportIn(Camel):
    """A spoken report. Base64 audio in, a filed report out."""

    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    #: Base64 of a short recording. `data:` prefixes are tolerated.
    audio_base64: str
    content_type: str = "audio/webm"
    #: "unknown" asks the recogniser to detect it, which is the right default:
    #: nobody reporting a flood should have to pick their language off a list.
    language: str = "unknown"
    #: When false, the transcript comes back and nothing is filed, so the person
    #: can read what was heard before it becomes a report.
    file_it: bool = True
    #: Same per-install id as the typed route. See `CitizenReportIn.device_id`.
    device_id: str | None = Field(default=None, max_length=64)
    city_id: str = "pune"


@router.post("/citizen/report/voice")
async def citizen_voice_report(
    body: VoiceReportIn, principal: CurrentPrincipal
) -> dict:
    """Hold a button, say what you see.

    Typing is the wrong input for this. Somebody standing in water, on a phone,
    in the dark, with one hand free is not going to fill in a form, and the
    fastest report is the one that asked least of the person making it.

    The transcript then goes through exactly the same door as a typed report:
    the same parser, the same trust scoring, the same clustering. Speech is an
    input method, not a second pipeline, so nothing downstream has to know or
    care that this one was spoken.
    """
    if not speech.configured():
        raise UpstreamUnavailable(
            "Speech reporting is not set up on this server (SARVAM_API_KEY). "
            "Type the report instead."
        )
    try:
        audio = speech.decode_audio(body.audio_base64)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc

    try:
        heard = await speech.transcribe(
            audio, content_type=body.content_type, language=body.language
        )
    except speech.SpeechError as exc:
        # The service failed, which is not the person's fault: say so, and say it
        # as something other than "bad request".
        raise UpstreamUnavailable(exc.message) from exc
    if heard is None or not heard.usable:
        raise BadRequest(
            "Nothing could be made out in that recording. Try again somewhere "
            "quieter, or type it."
        )

    parsed = await parse.parse_with_model(heard.text)
    payload: dict = {
        "heard": heard.text,
        "language": heard.language,
        "languageName": heard.language_name,
        "translated": heard.translated,
        "latencyMs": heard.latency_ms,
        "notes": heard.notes,
        "readAs": parsed.category,
        "readAsLabel": (taxonomy.categories[parsed.category].display_name
                        if parsed.category in taxonomy.categories else parsed.category),
        "readHow": parsed.explanation,
        "readConfidence": parsed.confidence,
        "filed": False,
    }
    if not body.file_it:
        # The person reads what was heard first. A misheard report that gets
        # filed anyway is worse than a slow one.
        return payload

    loc = await q.locate_ward(body.lng, body.lat, body.city_id)
    _nearest_ward_or_refuse(loc)

    try:
        result = await intake.receive(
            ward_id=loc.ward.id, category=parsed.category,
            location=(body.lng, body.lat), note=heard.text,
            source="app", reporter_id=principal.user_id,
            reporter_name=principal.full_name or "Resident",
            device_id=(
                f"citizen-{principal.user_id}" if principal.user_id
                else f"device-{body.device_id}" if body.device_id
                else "citizen-anon"
            ),
            city_id=body.city_id, clock=clocks.WALL,
            classification_confidence=parsed.confidence, classification_method=parsed.method,
        )
    except UnknownTaxonomyValue as exc:
        raise NotFound(str(exc)) from exc

    await ev.append(
        clock=clocks.WALL, kind="report.spoken",
        actor=f"agent:speech:{heard.model}",
        subject_type="report", subject_id=result.report_id,
        ward_id=loc.ward.id,
        payload={"language": heard.language, "translated": heard.translated,
                 "latency_ms": heard.latency_ms, "chars": len(heard.text)},
    )

    from app.demo import runner as demo_runner

    demo_runner.state.beat(
        "you",
        f"Spoken report in {heard.language_name}: “{heard.text[:60]}”",
        incidentId=result.incident_id, wardId=loc.ward.id,
    )
    demo_runner.state.dirty = True

    payload.update({
        "filed": True,
        "reportId": result.report_id,
        "incidentId": result.incident_id,
        "createdIncident": result.created_incident,
        "linked": result.linked,
        **_filed_to(loc),
        "trust": result.trust.score,
        "trustStatus": result.trust.status,
        "trustReasons": result.trust.reasons,
        "linkScore": result.link_score,
        "summary": result.summary,
    })
    return payload


class VisionAnalyseIn(Camel):
    """Ask the configured service to look at a photo for us."""

    image_url: str | None = None
    image_base64: str | None = None
    category: CategoryId = "flooded_road"
    report_id: str | None = None


@router.post("/citizen/vision/analyse")
async def citizen_vision_analyse(
    body: VisionAnalyseIn, _: CurrentPrincipal
) -> dict:
    """Run the hosted model, if one is configured, and return the assessment.

    The app calls this when it did *not* run the model itself. The result goes
    through exactly the same validation as one that came from a phone, and is
    subject to exactly the same ceiling on what it may change.
    """
    if not vision_client.configured():
        raise BadRequest(
            "No vision service is configured. Set VLM_URL, or run the model on "
            "the device and post the result to /citizen/vision."
        )
    call = await vision_client.analyse(
        image_url=body.image_url,
        image_base64=body.image_base64,
        category=body.category,
    )
    if not call.ok or call.evidence is None:
        raise BadRequest(
            f"The vision service did not return a usable answer ({call.error}). "
            "The report stands exactly as it would have with no photo."
        )
    if body.report_id:
        await _record_photo_evidence(body.report_id, call.evidence)

    # Hold the assessment against a token so the report that follows can be
    # *scored* on it rather than annotated with it afterwards. Writing the
    # agreement over a report whose trust score is already computed changes a
    # JSON column and nothing else — the photo could not lower the score of a
    # report it contradicted, which is most of the reason to look at photos.
    token = secrets.token_urlsafe(18)
    await cache.photo_evidence.get_or_set(
        token, cache.PHOTO_TOKEN_TTL, lambda: _hold(call.evidence)
    )
    # Keep the picture (a thumbnail) so the console's live feed can show what
    # the VLM looked at; it is tied to the report when the token is redeemed.
    from app import media
    await media.store(body.image_base64, source="citizen", caption=call.evidence.caption,
                      report_id=body.report_id, photo_token=token)
    return {
        "accepted": True,
        "latencyMs": call.latency_ms,
        "photoToken": token,
        "evidence": vision.as_dict(call.evidence),
    }


async def _hold(evidence: vision.PhotoEvidence) -> vision.PhotoEvidence:
    """`TTLCache` computes values asynchronously; this one is already computed."""
    return evidence


async def _record_photo_evidence(
    report_id: str, evidence: vision.PhotoEvidence
) -> None:
    """Write the agreement where the trust model will read it, and log it."""
    await db.execute(
        """
        update citizen_reports
           set trust_breakdown = coalesce(trust_breakdown, '{}'::jsonb)
                                 || jsonb_build_object('photo_agreement', $2::numeric)
         where id = $1::uuid
        """,
        report_id, evidence.agreement,
    )
    await ev.append(
        clock=clocks.WALL, kind="report.photo_assessed",
        actor=f"agent:vision:{evidence.ran_on}",
        subject_type="report", subject_id=report_id,
        payload={
            "agreement": evidence.agreement,
            "hazards": evidence.hazards,
            "depth_band": evidence.depth_band,
            "model": evidence.model,
            "ran_on": evidence.ran_on,
            "life_safety_signal": evidence.life_safety_signal,
        },
    )


@router.get("/citizen/vision-contract")
async def vision_contract(_: CurrentPrincipal) -> dict:
    """The prompt and the accepted shape, served to the client.

    The app does not hardcode the prompt. Serving it means a change to what we
    ask the model can ship without an app release, which matters when the app is
    a 3 GB download somebody installed before a flood and will not update during
    one.
    """
    return {
        "prompt": vision.PROMPT,
        "hazards": sorted(vision.HAZARDS),
        "depthBands": list(vision.DEPTHS),
        "imageQuality": list(vision.QUALITY),
        "categoriesCovered": vision.known_categories(),
        "maxImageEdge": 1024,
        "hostedAvailable": vision_client.configured(),
        "notes": [
            "A photo is corroboration, never the claim. It can raise or lower "
            "trust in what the reporter typed; it cannot set the category or "
            "the severity, and it cannot commit a unit.",
            "Send the analysis, not the image, when the model ran on the "
            "device. The photo stays on the phone unless the reporter chooses "
            "to upload it.",
        ],
    }


@router.post("/citizen/vision")
async def citizen_vision(body: VisionIn, _: CurrentPrincipal) -> dict:
    """Take a photo analysis and say what it changed.

    Returns the assessment whether or not a report id was supplied, so the app
    can show the person what the photo will contribute *before* they file, which
    is the difference between a system that analyses you and one that shows its
    working.
    """
    evidence = vision.parse_response(
        body.analysis, model=body.model, ran_on=body.ran_on
    )
    if evidence is None:
        raise BadRequest(
            "That analysis could not be read. It is ignored rather than guessed "
            "at, so the report stands exactly as it would have with no photo."
        )
    evidence = vision.assess(evidence, category=body.category)

    if body.report_id:
        await _record_photo_evidence(body.report_id, evidence)

    return {
        "accepted": True,
        "evidence": vision.as_dict(evidence),
        "effect": (
            "Raises confidence in the report."
            if evidence.agreement > 0.15
            else "Contradicts the report, so it is held for a human."
            if evidence.agreement < -0.15
            else "Neither supports nor undermines the report."
        ),
    }


def _eta_remaining(path, kind, router_min, progress, uid):
    from app.nav import eta
    return eta.remaining(path, kind, router_min, progress, uid)


@router.get("/citizen/state")
async def citizen_state(
    _: CurrentPrincipal,
    lng: float = Query(...), lat: float = Query(...),
    city_id: str = Query(default="pune"),
    radius_km: float = Query(default=4.0, ge=0.5, le=25.0),
) -> dict:
    """What a resident needs, and nothing else.

    Not the fleet. A resident is shown the units actually coming to something
    near them, because that is reassurance, and nothing else, because the live
    position of every ambulance in the city is not theirs to have.

    Cached for three seconds against a four-second poll, which is a smaller
    claim than it sounds and the correct one: a lone viewer still reads fresh
    almost every time, while a street full of people standing together collapses
    to one set of queries instead of a hundred. The saving is in the herd, not
    in the timer — see `app/core/cache.py` for why the key is a rounded position
    rather than the ward it falls in.
    """
    return await cache.citizen_state.get_or_set(
        cache.position_key(lng, lat, city_id, round(radius_km, 2)),
        3.0,
        lambda: _citizen_state(lng, lat, city_id, radius_km),
    )


async def _citizen_state(
    lng: float, lat: float, city_id: str, radius_km: float
) -> dict:
    loc = await q.locate_ward(lng, lat, city_id)
    metres = radius_km * 1000

    nearby, alerts, facilities, units, blocks = await _citizen_scope(
        lng, lat, city_id, metres
    )

    return {
        "ward": (
            {"id": loc.ward.id, "name": loc.ward.name,
             "population": loc.ward.population} if loc.ward else None
        ),
        "inside": loc.inside,
        "note": loc.note,
        "risk": await _ward_risk(loc.ward.id) if loc.ward else None,
        "incidents": nearby,
        "alerts": alerts,
        "facilities": facilities,
        "unitsNearby": units,
        "roadBlocks": blocks,
        "categories": [
            {"id": c.id, "label": c.display_name, "lifeSafety": c.life_safety}
            for c in taxonomy.categories.values()
        ],
        # What this deployment can actually do, so the app can offer it or not.
        #
        # Voice and photo analysis are both optional upstream services. Without
        # a key the endpoints correctly refuse, and the app was finding that out
        # the only way left to it: by offering a microphone, recording somebody
        # standing in water, uploading the audio, and showing them a 400. A
        # button that cannot work should say so before it is pressed, not after.
        "capabilities": {
            "voice": speech.configured(),
            "vision": vision_client.configured(),
        },
    }


async def _ward_risk(ward_id: str) -> dict | None:
    """One row per ward, rewritten by the hazard agent on its own cadence — so
    this one really is ward-shaped, and is keyed the way the plan intended."""
    return await cache.ward_risk.get_or_set(
        ward_id, 5.0, lambda: _ward_risk_uncached(ward_id)
    )


async def _ward_risk_uncached(ward_id: str) -> dict | None:
    r = await db.fetchrow(
        """
        select score, severity, lead_time_hours, population_at_risk, drivers
          from ward_risks where ward_id = $1 order by created_at desc limit 1
        """,
        ward_id,
    )
    if r is None:
        return None
    return {
        "score": float(r["score"]), "severity": r["severity"],
        "leadTimeHours": float(r["lead_time_hours"]),
        "populationAtRisk": r["population_at_risk"],
        "drivers": r["drivers"] or [],
    }


async def _citizen_scope(lng: float, lat: float, city_id: str, metres: float):
    incidents = [
        {"id": r["id"], "title": r["title"], "category": r["category"],
         "severity": r["severity"], "status": r["status"],
         "reportCount": r["report_count"],
         "location": [float(r["lng"]), float(r["lat"])],
         "distanceM": round(float(r["m"]))}
        for r in await db.fetch(
            """
            with me as (select extensions.ST_SetSRID(
                          extensions.ST_MakePoint($1,$2),4326)::extensions.geography g)
            select i.id::text, i.title, i.category, i.severity, i.status::text status,
                   i.report_count,
                   extensions.ST_X(i.location::extensions.geometry) lng,
                   extensions.ST_Y(i.location::extensions.geometry) lat,
                   extensions.ST_Distance(i.location, me.g) m
              from incidents i, me
             where i.city_id = $3 and i.status <> 'resolved'
               and extensions.ST_DWithin(i.location, me.g, $4)
               and not """ + CAMERA_ONLY + """
             order by m limit 40
            """,
            lng, lat, city_id, metres,
        )
    ]
    # Every alert the control room issued for this person's region, not only
    # the ones beside them: the ones near them first (those route them), the
    # rest after, marked as elsewhere. Pune and Ghaziabad never mix.
    alerts = [
        {"id": r["id"], "headline": r["headline"], "action": r["action"],
         "severity": r["severity"], "issuedAt": r["issued_at"].isoformat(),
         "safeLocation": r["safe_location"], "near": bool(r["near"]), "ward": r["ward"],
         "distanceKm": round(float(r["m"]) / 1000, 1)}
        for r in await db.fetch(
            """
            with me as (select extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography g)
            select a.id::text, a.headline, a.action, a.severity, a.issued_at,
                   a.safe_location, w.name ward,
                   extensions.ST_Distance(w.centroid, me.g) m,
                   extensions.ST_DWithin(w.centroid, me.g, $4 * 3) near
              from alerts a
              join wards w on w.id = a.ward_id, me
             where w.city_id = $1
               and ((w.id like 'w-gzb%') = ($2 >= 75.5))
             order by near desc, a.issued_at desc limit 25
            """,
            city_id, lng, lat, metres,
        )
    ]
    facilities = [
        {"id": r["id"], "name": r["name"], "kind": r["kind"],
         "location": [float(r["lng"]), float(r["lat"])],
         "capacity": r["capacity"], "occupancy": r["occupancy"],
         "kindLabel": r["kind_label"],
         "status": r["status"], "specialities": list(r["specialities"] or []),
         "supplies": r["supplies"] or {},
         "distanceM": round(float(r["m"]))}
        for r in await db.fetch(
            """
            with me as (select extensions.ST_SetSRID(
                          extensions.ST_MakePoint($1,$2),4326)::extensions.geography g)
            select l.id, l.name, l.kind, l.capacity, l.occupancy, l.status,
                   l.specialities, l.supplies, k.display_name kind_label,
                   extensions.ST_X(l.location::extensions.geometry) lng,
                   extensions.ST_Y(l.location::extensions.geometry) lat,
                   extensions.ST_Distance(l.location, me.g) m
              from lifelines l
              join lifeline_kinds k on k.id = l.kind, me
             where l.city_id = $3 and k.serves_public
             order by m limit 16
            """,
            lng, lat, city_id,
        )
    ]
    units = [
        {"id": r["id"], "kind": r["kind"], "label": r["label"],
         "status": r["status"], "location": [float(r["lng"]), float(r["lat"])],
         "etaMinutes": r["eta_minutes"]}
        for r in await db.fetch(
            """
            with me as (select extensions.ST_SetSRID(
                          extensions.ST_MakePoint($1,$2),4326)::extensions.geography g)
            select r.id, r.kind, r.label, r.status::text status, a.eta_minutes,
                   extensions.ST_X(r.location::extensions.geometry) lng,
                   extensions.ST_Y(r.location::extensions.geometry) lat
              from resources r
              join assignments a on a.resource_id = r.id
                   and a.status in ('proposed','approved','en_route','on_site')
              join incidents i on i.id = a.incident_id, me
             where r.city_id = $3
               and extensions.ST_DWithin(i.location, me.g, $4)
             limit 20
            """,
            lng, lat, city_id, metres,
        )
    ]
    # Roads crews have declared impassable. A resident is shown these because
    # they are the single most actionable thing in the whole snapshot: not "your
    # ward is at severity four", but "do not take that street".
    blocks = [
        {"id": r["id"], "reason": r["reason"], "reportedBy": r["reported_by"],
         "radiusM": r["radius_m"],
         "location": [float(r["lng"]), float(r["lat"])]}
        for r in await db.fetch(
            """
            with me as (select extensions.ST_SetSRID(
                          extensions.ST_MakePoint($1,$2),4326)::extensions.geography g)
            select b.id::text, b.reason, b.reported_by, b.radius_m,
                   extensions.ST_X(b.location::extensions.geometry) lng,
                   extensions.ST_Y(b.location::extensions.geometry) lat
              from road_blocks b, me
             where b.city_id = $3 and b.active
               and extensions.ST_DWithin(b.location, me.g, $4)
             order by extensions.ST_Distance(b.location, me.g)
             limit 30
            """,
            lng, lat, city_id, metres,
        )
    ]
    return incidents, alerts, facilities, units, blocks


# ============================================================= field =========
class FieldStatusIn(Camel):
    subject_type: Literal["resource", "lifeline"] = "resource"
    subject_id: str
    status_kind: str
    note: str = ""
    lng: float | None = None
    lat: float | None = None
    #: For a hospital or shelter declaring how full it is.
    occupancy: int | None = None


@router.get("/field/status-kinds")
async def field_status_kinds(_: CurrentPrincipal) -> list[dict]:
    rows = await db.fetch(
        "select id, label, makes_offline, severity, applies_to from field_status_kinds order by applies_to, id"
    )
    return [
        {"id": r["id"], "label": r["label"], "makesOffline": r["makes_offline"],
         "severity": r["severity"], "appliesTo": r["applies_to"]}
        for r in rows
    ]


@router.post("/field/status")
async def field_status(body: FieldStatusIn, principal: CurrentPrincipal) -> dict:
    """A crew tells the control room what is actually true.

    This is not a log entry. A puncture takes the vehicle out of the fleet, a
    hospital declaring itself full stops being a destination the guidance agent
    will send anyone to, and a blocked route becomes a point the router treats
    as impassable. Each of those changes the next plan, which is the difference
    between a status board and a system.
    """
    kind = await db.fetchrow(
        "select id, label, makes_offline, severity, applies_to from field_status_kinds where id = $1",
        body.status_kind,
    )
    if kind is None:
        raise NotFound(f"Unknown status {body.status_kind!r}.")
    if kind["applies_to"] != body.subject_type:
        raise Conflict(f"{kind['label']!r} applies to a {kind['applies_to']}, not a {body.subject_type}.")

    reporter = principal.full_name or principal.operator or "Field"
    effects: list[str] = []

    async with db.transaction() as conn:
        await conn.execute(
            """
            insert into field_reports
              (subject_type, subject_id, status_kind, note, reported_by, location)
            values ($1,$2,$3,$4,$5,
                    case when $6::float8 is null then null else
                      extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography
                    end)
            """,
            body.subject_type, body.subject_id, body.status_kind, body.note,
            reporter, body.lng, body.lat,
        )

        if body.subject_type == "resource":
            if kind["makes_offline"]:
                await conn.execute(
                    """
                    update resources
                       set status = 'offline', unavailable_reason = $2,
                           status_note = $3, last_reported_at = now()
                     where id = $1
                    """,
                    body.subject_id, kind["label"], body.note,
                )
                # Anything it was doing is now nobody's job, so it goes back into
                # the pool of unmet need rather than sitting assigned to a
                # vehicle that is not moving.
                released = await conn.fetchval(
                    """
                    update assignments set status = 'complete'
                     where resource_id = $1
                       and status in ('proposed','approved','en_route','on_site')
                    returning incident_id::text
                    """,
                    body.subject_id,
                )
                effects.append(f"{body.subject_id} is out of service: {kind['label'].lower()}.")
                if released:
                    effects.append("Its task was released and will be re-planned.")
            elif body.status_kind == "back_in_service":
                # The verb that did not exist. `makes_offline` took a vehicle
                # out and nothing put it back, so a puncture removed a truck
                # from the fleet for the rest of the event and the console could
                # count the loss without offering anything to do about it.
                #
                # No assignment is touched. Its task was released when it went
                # offline and belongs to whoever the next plan gives it to;
                # re-attaching it here would hand a crew a job the solver has
                # already placed somewhere else.
                await conn.execute(
                    """
                    update resources
                       set status = 'available', unavailable_reason = null,
                           status_note = $2, last_reported_at = now()
                     where id = $1
                    """,
                    body.subject_id, body.note,
                )
                effects.append(
                    f"{body.subject_id} is back in the fleet and will be "
                    "considered by the next plan."
                )
            elif body.status_kind == "task_complete":
                await conn.execute(
                    """
                    update assignments set status = 'complete'
                     where resource_id = $1 and status in ('en_route','on_site')
                    """,
                    body.subject_id,
                )
                await conn.execute(
                    # `unavailable_reason` is cleared here too. It used to
                    # survive: a unit that had been offline and then closed a
                    # task came back as available still carrying "Puncture",
                    # and every screen reading that column showed a reason for
                    # an unavailability that had ended. Wrong data rather than
                    # stale, and read by somebody deciding who to send.
                    "update resources set status = 'available', "
                    "unavailable_reason = null, status_note = $2, "
                    "last_reported_at = now() where id = $1",
                    body.subject_id, body.note,
                )
                effects.append("Task closed, unit back in the pool.")
            else:
                await conn.execute(
                    "update resources set status_note = $2, last_reported_at = now() where id = $1",
                    body.subject_id, f"{kind['label']}. {body.note}".strip(),
                )
                effects.append(f"Noted: {kind['label'].lower()}.")

            if body.status_kind == "route_blocked" and body.lng is not None:
                await conn.execute(
                    """
                    insert into road_blocks (location, reason, reported_by)
                    values (extensions.ST_SetSRID(
                              extensions.ST_MakePoint($1,$2),4326)::extensions.geography,
                            $3, $4)
                    """,
                    body.lng, body.lat, body.note or "Reported impassable by a crew", reporter,
                )
                effects.append(
                    "The point is now treated as impassable when routing anyone, "
                    "including residents."
                )

        else:  # lifeline
            status = {"beds_full": "full", "shelter_full": "full",
                      "beds_limited": "limited", "reopened": "open"}.get(
                          body.status_kind, "limited")
            await conn.execute(
                """
                update lifelines
                   set status = $2,
                       occupancy = coalesce($3, occupancy),
                       last_reported_at = now()
                 where id = $1
                """,
                body.subject_id, status, body.occupancy,
            )
            effects.append(
                f"{body.subject_id} is now {status}."
                + (" Guidance will stop sending people there."
                   if status == "full" else "")
            )

    await ev.append(
        clock=clocks.WALL, kind=f"field.{body.status_kind}",
        actor=f"field:{reporter}", subject_type=body.subject_type,
        subject_id=body.subject_id,
        payload={"status": body.status_kind, "label": kind["label"],
                 "note": body.note, "effects": effects},
    )

    from app.api.v1 import demo as demo_api
    from app.demo import runner as demo_runner

    demo_runner.state.beat(
        "field", f"{reporter}: {kind['label']}. " + " ".join(effects),
        subjectId=body.subject_id, subjectType=body.subject_type,
    )
    demo_runner.state.dirty = True
    # A crew pressing "full" or "puncture" has just made the console's cached
    # facility and road-block reads wrong. Drop them rather than let the console
    # show a stale shelter as open for the next two seconds.
    demo_api.invalidate_slow()

    return {"ok": True, "statusKind": body.status_kind, "label": kind["label"],
            "effects": effects}


class FieldHazardIn(Camel):
    """A crew reporting what is in front of them, rather than about a vehicle.

    `/field/status` answers "my truck has a puncture" and "this shelter is
    full" — it is always *about* a subject already in the database. There was no
    way for a crew standing in front of a collapsed wall to say so, which is an
    odd gap in a system whose whole argument is that the picture is assembled
    from whoever can see it. The most reliable reporters in the city could see
    things and had nowhere to put them.
    """

    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    #: Free text, classified the same way a resident's sentence is.
    text: str = ""
    #: Set when the crew picks from the list instead of typing.
    category: CategoryId | None = None
    photo_url: str | None = None
    photo_token: str | None = None
    city_id: str = "pune"


@router.post("/field/report", status_code=201)
async def field_report(body: FieldHazardIn, principal: CurrentPrincipal) -> dict:
    """A crew files a hazard at their own position.

    The same door as everything else — `intake.receive`, one transaction,
    trust scored before clustering — with one field different: `source="field"`,
    which the trust model credits at 0.95 against an anonymous app report's
    0.62. A trained crew standing in front of the thing is the best evidence
    this system gets, so such a report will usually clear the auto-confirm floor
    on its own and become a dispatchable incident immediately.

    That is the point of the separate route, and it is worth being explicit
    about: it is not a shortcut around the scoring, it *is* the scoring. The
    same report typed by an anonymous phone would sit at `needs_corroboration`
    until somebody else saw it too, and both of those are the correct answer for
    who sent them.
    """
    loc = await q.locate_ward(body.lng, body.lat, body.city_id)
    _nearest_ward_or_refuse(loc)

    parsed = (
        parse.Parsed(category=body.category, confidence=1.0, method="chosen", note=body.text)
        if body.category
        else await parse.parse_with_model(body.text)
    )
    evidence = (
        cache.photo_evidence.peek(body.photo_token) if body.photo_token else None
    )

    try:
        result = await intake.receive(
            ward_id=loc.ward.id,
            category=parsed.category,
            location=(body.lng, body.lat),
            note=body.text,
            photo_url=body.photo_url or ("device://photo" if evidence else None),
            photo_agreement=evidence.agreement if evidence else None,
            photo_evidence=vision.as_dict(evidence) if evidence else None,
            source="field",
            reporter_id=principal.user_id,
            reporter_name=principal.full_name or principal.operator or "Field crew",
            device_id=f"field-{principal.user_id or principal.operator or 'unknown'}",
            city_id=body.city_id,
            clock=clocks.WALL,
            classification_confidence=parsed.confidence, classification_method=parsed.method,
        )
    except UnknownTaxonomyValue as exc:
        raise NotFound(str(exc)) from exc

    # The control room hears about it, and the world is marked dirty so the
    # allocator re-solves. A hazard a crew reported that changes nobody's plan
    # is a log line, not a report.
    from app.demo import runner as demo_runner

    demo_runner.state.beat(
        "field",
        f"{principal.full_name or 'A crew'} reported {parsed.category.replace('_', ' ')} "
        f"in {loc.ward.name}"
        + (" (new incident)" if result.created_incident
           else " (merged into an open incident)" if result.linked
           else " (held: an officer will read it)"),
        incidentId=result.incident_id, reportId=result.report_id,
        wardId=loc.ward.id, trust=result.trust.score,
    )
    demo_runner.state.dirty = True

    return {
        "reportId": result.report_id,
        "incidentId": result.incident_id,
        "createdIncident": result.created_incident,
        "linked": result.linked,
        **_filed_to(loc),
        "readAs": parsed.category,
        "readAsLabel": (taxonomy.categories[parsed.category].display_name
                        if parsed.category in taxonomy.categories else parsed.category),
        "readHow": parsed.explanation,
        "trust": result.trust.score,
        "trustStatus": result.trust.status,
        "trustReasons": result.trust.reasons,
        "summary": result.summary,
        "photo": vision.as_dict(evidence) if evidence else None,
    }


@router.get("/field/state")
async def field_state(
    principal: CurrentPrincipal,
    operator: str | None = Query(default=None),
    city_id: str = Query(default="pune"),
) -> dict:
    """A crew sees their own agency's units and tasks, and nothing else.

    A field operator has no need for the whole city's fleet, and the row level
    security policy on `field_tasks` refuses it independently of this filter.
    """
    scope = operator if principal.is_staff and operator else principal.operator
    args: list = [city_id]
    where = "r.city_id = $1"
    if scope:
        args.append(scope)
        where += f" and r.operator = ${len(args)}"

    units = [
        {"id": r["id"], "kind": r["kind"], "label": r["label"],
         "operator": r["operator"], "status": r["status"],
         "statusNote": r["status_note"], "unavailableReason": r["unavailable_reason"],
         "location": [float(r["lng"]), float(r["lat"])],
         "capacity": r["capacity"], "assignedTo": r["incident_title"],
         "incidentId": r["incident_id"], "etaMinutes": r["eta_minutes"],
         "incidentLocation": (
             [float(r["ilng"]), float(r["ilat"])] if r["ilng"] is not None else None
         ),
         # The crew gets the road, and the turns, not a bearing. A driver told
         # to head 2.1 km north-east through a flooded city has been told
         # nothing; "left onto Karve Road" is an instruction.
         "route": r["path"] or [],
         "routeEngine": r["route_engine"],
         "steps": r["steps"] or [],
         "distanceKm": float(r["distance_km"]) if r["distance_km"] is not None else None,
         "progress": float(r["progress"] or 0),
         # The response-time model's view of this trip (P50 to plan with, P90 to
         # promise), from the route and the flood risk along it; None when the
         # model has nothing to say (no road route, not a road unit).
         "etaModel": _eta_remaining(r["path"], r["kind"], r["eta_minutes"], r["progress"], r["id"])}
        for r in await db.fetch(
            f"""
            select r.id, r.kind, r.label, r.operator, r.status::text status,
                   r.status_note, r.unavailable_reason, r.capacity,
                   extensions.ST_X(r.location::extensions.geometry) lng,
                   extensions.ST_Y(r.location::extensions.geometry) lat,
                   a.incident_id::text incident_id, a.eta_minutes,
                   a.route_engine, a.steps, a.distance_km, a.progress,
                   extensions.ST_AsGeoJSON(a.route)::json -> 'coordinates' as path,
                   i.title incident_title,
                   extensions.ST_X(i.location::extensions.geometry) ilng,
                   extensions.ST_Y(i.location::extensions.geometry) ilat
              from resources r
              left join lateral (
                select * from assignments x where x.resource_id = r.id
                   and x.status in ('proposed','approved','en_route','on_site')
                   and x.sim_run_id is null
                 order by x.created_at desc limit 1) a on true
              left join incidents i on i.id = a.incident_id
             where {where}
             order by r.kind, r.label
            """,
            *args,
        )
    ]

    tasks = await q.list_field_tasks(scope)
    facilities = [
        {"id": r["id"], "name": r["name"], "kind": r["kind"], "status": r["status"],
         "kindLabel": r["kind_label"] or r["kind"].replace("_", " ").title(),
         "capacity": r["capacity"], "occupancy": r["occupancy"],
         "supplies": r["supplies"] or {},
         "location": [float(r["lng"]), float(r["lat"])]}
        for r in await db.fetch(
            """
            select l.id, l.name, l.kind, l.status, l.capacity, l.occupancy,
                   l.supplies, k.display_name kind_label,
                   extensions.ST_X(l.location::extensions.geometry) lng,
                   extensions.ST_Y(l.location::extensions.geometry) lat
              from lifelines l
              left join lifeline_kinds k on k.id = l.kind
             where l.city_id = $1
             order by l.kind, l.name
            """,
            city_id,
        )
    ]
    recent = [
        {"subjectId": r["subject_id"], "statusKind": r["status_kind"],
         "note": r["note"], "reportedBy": r["reported_by"],
         "at": r["created_at"].isoformat()}
        for r in await db.fetch(
            "select subject_id, status_kind, note, reported_by, created_at "
            "from field_reports order by created_at desc limit 20"
        )
    ]

    # What this crew reported, and what happened to it.
    #
    # A crew could file a hazard and then had exactly one chance to learn its
    # fate: the card that appeared for a few seconds after pressing send. Reload
    # the page, or drive to the next street, and "did anyone act on that?" was
    # answered by faith. `recent` above is the *status* feed — punctures,
    # shelters full — and carries none of this.
    #
    # Scoped by `device_id`, which `/field/report` sets to
    # `field-<user or operator>`, rather than by name: two crews from the same
    # operator should not read each other's reports as their own, and a name is
    # not an identity.
    #
    # The join carries the consequence, not just the status: which incident it
    # landed on, how many other reports are on that incident, how many units are
    # on the way, and whether it has been closed. That chain is the answer to the
    # question actually being asked.
    my_reports = [
        {"id": r["id"], "text": r["note"], "readAs": r["classified_as"] or r["category"],
         "trust": float(r["trust_score"]) if r["trust_score"] is not None else None,
         "status": r["verification_status"], "outcome": r["outcome"],
         "at": r["created_at"].isoformat(),
         "incidentId": r["incident_id"], "incidentTitle": r["incident_title"],
         "incidentStatus": r["incident_status"], "incidentSeverity": r["incident_severity"],
         "reportCount": r["report_count"], "unitsOnIt": r["units_on_it"],
         "etaMinutes": r["eta_minutes"]}
        for r in await db.fetch(
            """
            select c.id::text, c.note, c.category, c.classified_as, c.trust_score,
                   c.verification_status, c.outcome, c.created_at,
                   c.incident_id::text incident_id,
                   i.title incident_title, i.status::text incident_status,
                   i.severity incident_severity,
                   coalesce(rc.n, 0)::int report_count,
                   coalesce(ac.n, 0)::int units_on_it,
                   ac.eta_minutes
              from citizen_reports c
              left join incidents i on i.id = c.incident_id
              left join lateral (
                select count(*) n from citizen_reports x
                 where x.incident_id = c.incident_id) rc on true
              left join lateral (
                select count(*) n, min(a.eta_minutes) eta_minutes
                  from assignments a
                 where a.incident_id = c.incident_id
                   and a.status in ('proposed','approved','en_route','on_site')
                   and a.sim_run_id is null) ac on true
             where c.source = 'field'
               and ($2::text is null or c.device_id = $2)
               and c.city_id = $1
             order by c.created_at desc
             limit 20
            """,
            city_id,
            f"field-{principal.user_id or principal.operator or 'unknown'}"
            if not principal.is_staff or not operator
            else None,
        )
    ]

    # Every open incident in the city, not only the ones this crew is assigned
    # to. A crew that has just reported a collapsed wall needs to see it land on
    # their own map — otherwise "did that go through?" is answered by faith —
    # and a crew driving past somebody else's open incident should know it is
    # there rather than discover it at the junction.
    #
    # `report_count` is on purpose: it is what separates "one person said this"
    # from "eleven people said this", which is the difference between a marker
    # a driver treats as a rumour and one they route around.
    incidents = [
        {"id": r["id"], "title": r["title"], "category": r["category"],
         "severity": r["severity"], "status": r["status"],
         "wardId": r["ward_id"], "reportCount": r["report_count"],
         "verification": r["verification"],
         "location": [float(r["lng"]), float(r["lat"])]}
        for r in await db.fetch(
            """
            select i.id::text, i.title, i.category, i.severity, i.status::text status,
                   i.ward_id,
                   extensions.ST_X(i.location::extensions.geometry) lng,
                   extensions.ST_Y(i.location::extensions.geometry) lat,
                   count(c.id)::int report_count,
                   -- The strongest verification any of its reports carries.
                   -- One confirmed report in a cluster of ten unconfirmed ones
                   -- is still a confirmed incident.
                   case max(case c.verification_status
                              when 'auto_confirmed' then 3
                              when 'needs_corroboration' then 2
                              when 'quarantined' then 1 else 0 end)
                     when 3 then 'confirmed'
                     when 2 then 'unconfirmed'
                     when 1 then 'held'
                     else 'unconfirmed' end verification
              from incidents i
              left join citizen_reports c on c.incident_id = i.id
             where i.city_id = $1
               and i.sim_run_id is null
               -- The four live states. `incident_status` is an enum, so a
               -- name that is not in it is a hard 22P02 at query time rather
               -- than an empty result — 'triaged' and 'assigned' are not
               -- members of it, whatever the domain vocabulary suggests.
               and i.status in ('reported', 'confirmed', 'dispatched', 'in_progress')
               and not """ + CAMERA_ONLY + """
             group by i.id
             order by i.severity desc, i.created_at desc
             limit 200
            """,
            city_id,
        )
    ]

    return {"operator": scope, "units": units,
            "tasks": [t.model_dump(by_alias=True) for t in tasks],
            "facilities": facilities, "recent": recent,
            "myReports": my_reports,
            "incidents": incidents}
