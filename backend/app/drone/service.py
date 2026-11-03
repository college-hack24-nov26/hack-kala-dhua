"""Drone frame localization, logged into the command centre.

The matching itself runs in a separate service (mapp-s map matcher, its own
Render deployment): it takes a camera frame, finds which reference tile of the
area it shows and returns that tile's latitude/longitude. In the field a drone
would send its own frames; for the demo a person uploads one.

Two ways in, one record:
  * the finder POSTs every result to /drone/localized (its webhook), so an
    upload on the finder's own page shows up here;
  * the console uploads through /drone/localize, which forwards the image to
    the finder and records what comes back.
A result arriving both ways (same request_id) is recorded once.
"""
from __future__ import annotations

import os
import time
import uuid
from collections import OrderedDict, deque
from typing import Any

import httpx

from app.core.logging import get_logger
from app.world import events as ev
from app.world.clock import WALL

log = get_logger(__name__)

FINDER_URL = os.environ.get("MAP_FINDER_URL", "https://mapp-s.onrender.com").rstrip("/")
KIND = "drone.localized"

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/reverse"
_places: dict[tuple[float, float], dict] = {}

_recent: deque[dict] = deque(maxlen=40)
_seen: OrderedDict[str, dict] = OrderedDict()


def recent(limit: int = 20) -> list[dict]:
    return list(_recent)[:limit]


def _summary(r: dict, source: str, drone: str | None, thumb: str | None) -> dict:
    coords = r.get("coordinates") or {}
    accepted = bool(r.get("accepted")) and coords.get("latitude") is not None
    return {
        "id": str(r.get("request_id") or uuid.uuid4()),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "drone": drone or "drone-1",
        "accepted": accepted,
        "lat": float(coords["latitude"]) if accepted else None,
        "lon": float(coords["longitude"]) if accepted else None,
        "tile": r.get("image"),
        "inliers": r.get("inliers"),
        "errorPx": round(float(r["error"]), 2) if isinstance(r.get("error"), (int, float)) else None,
        "processingMs": r.get("processing_ms"),
        "mode": r.get("mode"),
        "reason": None if accepted else (r.get("reason") or "No confident match"),
        "thumb": thumb if isinstance(thumb, str) and thumb.startswith("data:image/") and len(thumb) < 200_000 else None,
        "wardId": None,
        "place": None,
        "address": None,
    }


async def place_name(lat: float, lon: float) -> dict:
    """Mapbox reverse geocoding: coordinates -> "neighbourhood, locality, city"."""
    key = (round(lat, 4), round(lon, 4))
    if key in _places:
        return _places[key]
    from app.core.config import settings
    token = settings.mapbox_token or os.environ.get("MAPBOX_TOKEN", "")
    out: dict = {"place": None, "address": None}
    if not token:
        return out
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(GEOCODE_URL, params={
                "longitude": lon, "latitude": lat, "language": "en", "access_token": token})
        feats = r.json().get("features", []) if r.status_code == 200 else []
        by = {f["properties"].get("feature_type"): f["properties"] for f in feats}
        parts = [by.get(t, {}).get("name") for t in ("neighborhood", "locality", "place")]
        parts = [p for i, p in enumerate(parts) if p and p not in parts[:i]]
        out["place"] = ", ".join(parts) or (by.get("place") or {}).get("name")
        addr = by.get("address") or by.get("street")
        out["address"] = addr.get("full_address") or addr.get("name") if addr else None
    except Exception as exc:  # noqa: BLE001
        log.warning("drone_geocode_failed", error=str(exc)[:160])
    if out["place"]:
        _places[key] = out
    return out


async def record(result: dict, *, source: str, drone: str | None = None,
                 thumb: str | None = None, city_id: str = "pune") -> dict:
    entry = _summary(result, source, drone, thumb)
    if entry["id"] in _seen:
        return _seen[entry["id"]]
    _seen[entry["id"]] = entry
    while len(_seen) > 200:
        _seen.popitem(last=False)

    if entry["accepted"]:
        entry.update(await place_name(entry["lat"], entry["lon"]))
        try:
            from app.mesh import service as mesh
            entry["wardId"] = await mesh.ward_at(entry["lon"], entry["lat"], city_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("drone_ward_lookup_failed", error=str(exc)[:160])
    if entry["accepted"]:
        entry["swarmTask"] = await _task_swarm(entry)
    _recent.appendleft(entry)

    where = f" — {entry['place']}" if entry.get("place") else ""
    text = (f"{entry['drone']} localized its camera frame: {entry['lat']:.5f}, {entry['lon']:.5f}{where} "
            f"({entry['inliers']} feature matches, {entry['processingMs']} ms)"
            + (" -> swarm assess drone requested" if (entry.get("swarmTask") or {}).get("ok") else "")
            if entry["accepted"] else
            f"{entry['drone']} frame could not be placed: {entry['reason']}")
    try:
        await ev.append(
            clock=WALL, kind=KIND, actor=ev.feed("drone"), subject_type="drone_frame",
            subject_id=entry["id"], city_id=city_id, ward_id=entry["wardId"],
            payload={k: v for k, v in entry.items() if k != "thumb"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("drone_event_failed", error=str(exc)[:160])
    try:
        from app.demo import runner
        runner.state.beat("drone", text, wardId=entry["wardId"], lat=entry["lat"],
                          lng=entry["lon"], droneFrame=entry["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("drone_beat_failed", error=str(exc)[:160])
    log.info("drone_localized", id=entry["id"], accepted=entry["accepted"], source=source)
    return entry


async def _task_swarm(entry: dict) -> dict | None:
    """Map matcher -> drone swarm: send an assess drone to where the frame was placed.

    Only when the swarm link is up and the point lies inside the swarm's map
    (SWARM_LOCALIZE_TASK=0 turns it off). The result is kept on the entry and
    shows in the Drone command log, so both panels tell the same story.
    """
    from app.drone import swarm_link
    if not swarm_link._on("SWARM_LOCALIZE_TASK", "1"):
        return None
    link = swarm_link.link()
    if link is None:
        return {"ok": False, "reason": "drone swarm link is off"}
    where = entry.get("place") or f"{entry['lat']:.5f}, {entry['lon']:.5f}"
    try:
        out = await link.task({"action": "new_task", "kind": "assess", "lat": entry["lat"], "lng": entry["lon"],
                               "category": "flood", "people": 1, "priority": 3,
                               "note": f"Verify map-matched frame from {entry['drone']} at {where}"[:120]},
                              by=f"map-matcher:{entry['drone']}"[:40])
    except ValueError as exc:  # outside the swarm's map / bad input
        return {"ok": False, "reason": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 - link down, timeout
        log.warning("drone_swarm_task_failed", error=str(exc)[:160])
        return {"ok": False, "reason": f"swarm did not answer: {str(exc)[:120]}"}
    res = out.get("result") or {}
    link._log("map_match", f"Map matcher placed {entry['drone']}'s frame at {where}; assess drone requested"
              + ("" if out.get("ok") else f" (swarm said: {out.get('error') or res.get('reason') or 'declined'})"),
              incident_id=res.get("incident_id"), frm="disruptionops", level="info" if out.get("ok") else "warn")
    return {"ok": bool(out.get("ok")), "incidentId": res.get("incident_id"), "requestId": res.get("request_id"),
            "reason": None if out.get("ok") else (out.get("error") or res.get("reason"))}


async def localize(data: bytes, filename: str, content_type: str, *, drone: str | None,
                   city_id: str = "pune") -> dict:
    """Send a frame to the finder and record the answer."""
    import base64
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
        resp = await client.post(
            f"{FINDER_URL}/api/v1/localize",
            params={"include_images": "false"},
            files={"query": (filename or "frame.jpg", data, content_type or "image/jpeg")},
            headers={"X-Indradhanu-Source": "console"},
        )
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:200]}
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code, "error": body.get("error") or "Localizer failed",
                "retryAfter": resp.headers.get("Retry-After")}
    thumb = None
    if len(data) < 150_000:
        thumb = f"data:{content_type or 'image/jpeg'};base64," + base64.b64encode(data).decode()
    entry = await record(body, source="console", drone=drone, thumb=thumb, city_id=city_id)
    return {"ok": True, "entry": entry}
