"""Photos behind feed items: what a citizen's phone or a camera node actually saw.

Small images only (<= 400 KB after decoding; phones and cameras send a
thumbnail). Stored in `feed_media` (migration 033) and returned inline as data
URLs by `/mesh/detail`, so the console can show the picture next to the VLM's
reading of it without a separate signed-URL round trip. LoRa packets never
carry images; a camera that also has HTTPS sends one with its detection.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)
MAX_BYTES = 400_000


def _decode(b64: str) -> tuple[bytes, str] | None:
    mime = "image/jpeg"
    if b64.startswith("data:"):
        head, _, b64 = b64.partition(",")
        mime = head[5:].split(";")[0] or mime
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > MAX_BYTES:
        return None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    elif not mime.startswith("image/"):
        return None
    return raw, mime


async def store(image_base64: str | None, *, source: str, caption: str | None = None,
                report_id: str | None = None, event_ref: str | None = None,
                photo_token: str | None = None, city_id: str = "pune") -> int | None:
    if not image_base64:
        return None
    dec = _decode(image_base64)
    if dec is None:
        return None
    raw, mime = dec
    try:
        return await db.fetchval(
            "insert into feed_media (city_id, source, mime, bytes, caption, report_id, event_ref, photo_token) "
            "values ($1,$2,$3,$4,$5,$6::uuid,$7,$8) returning id",
            city_id, source, mime, raw, (caption or "")[:500] or None, report_id, event_ref, photo_token)
    except Exception as exc:  # noqa: BLE001 - a lost thumbnail never loses the report
        log.warning("media_store_failed", error=str(exc)[:200])
        return None


async def attach_token(photo_token: str | None, report_id: str) -> None:
    if not photo_token:
        return
    try:
        await db.execute("update feed_media set report_id = $2::uuid where photo_token = $1 and report_id is null",
                         photo_token, report_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_attach_failed", error=str(exc)[:200])


async def for_item(report_id: str | None = None, event_ref: str | None = None) -> list[dict[str, Any]]:
    if not report_id and not event_ref:
        return []
    try:
        rows = await db.fetch(
            "select id, source, mime, bytes, caption, created_at from feed_media "
            "where ($1::uuid is not null and report_id = $1::uuid) or ($2::text is not null and event_ref = $2) "
            "order by id limit 6", report_id, event_ref)
    except Exception:  # noqa: BLE001 - table not there yet (migration 033)
        return []
    return [{"id": r["id"], "source": r["source"], "caption": r["caption"], "at": r["created_at"].isoformat(),
             "dataUrl": f"data:{r['mime']};base64,{base64.b64encode(bytes(r['bytes'])).decode()}"} for r in rows]
