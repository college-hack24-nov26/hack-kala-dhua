"""Memory for the Copilot and the Commander, kept in Supabase (migration 020).

Two kinds, because they answer different questions:

* **Conversation** (`copilot_turns`) — what was just asked and answered in this
  session, and which ids the answer was about. This is what lets "apply the
  second one", "cancel it", "and Baner?" mean something.
* **Long-term** (`agent_memory`) — standing orders, facts about the city,
  after-action lessons, episode summaries. Recalled by relevance to the
  question, by ward, and always when it is a standing order.

The rule that keeps memory safe to use: **memory carries words and ids, never
numbers.** A memory may say "the Commissioner asked that Boat 2 stay in
Kothrud"; it may not say "Boat 2 is four minutes away". Every figure in an
answer still comes from a tool call made at answer time, and the narrator is
told so.

Everything here is best-effort. If the migration has not been applied, or the
database is slow, the Copilot answers exactly as it did before, without memory,
and says `memory: unavailable` in its note.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)

SCOPES = ("standing_order", "fact", "lesson", "episode", "preference")

#: After a failure, do not try again for this long. A missing table should cost
#: one warning, not one per question.
_BACKOFF_S = 60.0
_down_until = 0.0


def available() -> bool:
    return time.monotonic() >= _down_until


def _mark_down(exc: Exception) -> None:
    global _down_until
    _down_until = time.monotonic() + _BACKOFF_S
    log.info("memory_unavailable", error=type(exc).__name__, detail=str(exc)[:160])


# ------------------------------------------------------------------ writes ---
async def remember(
    *,
    scope: str,
    content: str,
    created_by: str,
    city_id: str = "pune",
    data: dict | None = None,
    ward_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    importance: int = 3,
    source: str = "copilot",
    expires_in_minutes: int | None = None,
) -> str | None:
    if scope not in SCOPES or not available():
        return None
    content = " ".join((content or "").split())[:2000]
    if len(content) < 3:
        return None
    try:
        return await db.fetchval(
            """
            insert into agent_memory
              (city_id, scope, content, data, ward_id, subject_type, subject_id,
               importance, source, created_by, expires_at)
            values ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10,
                    case when $11::int is null then null
                         else now() + make_interval(mins => $11::int) end)
            returning id::text
            """,
            city_id, scope, content, json.dumps(data or {}), ward_id,
            subject_type, subject_id, max(1, min(5, int(importance))), source,
            created_by, expires_in_minutes,
        )
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return None


async def forget(*, text: str | None = None, memory_id: str | None = None,
                 actor: str, city_id: str = "pune") -> int:
    """Withdraw memories. Soft: the row stays with who withdrew it and when."""
    if not available():
        return 0
    try:
        if memory_id:
            status = await db.execute(
                """
                update agent_memory set active = false, withdrawn_by = $2,
                       withdrawn_at = now()
                 where id = $1::uuid and active
                """,
                memory_id, actor,
            )
        else:
            status = await db.execute(
                """
                update agent_memory set active = false, withdrawn_by = $3,
                       withdrawn_at = now()
                 where city_id = $1 and active
                   and tsv @@ websearch_to_tsquery('simple', $2)
                """,
                city_id, text or "", actor,
            )
        return int(str(status).split()[-1] or 0)
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return 0


async def record_turn(
    session_id: str | None, role: str, text: str, *, actor: str,
    city_id: str = "pune", intent: str | None = None, args: dict | None = None,
    refs: dict | None = None,
) -> None:
    if not session_id or not available():
        return
    try:
        await db.execute(
            """
            insert into copilot_turns
              (session_id, city_id, actor, role, text, intent, args, refs)
            values ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)
            """,
            session_id[:80], city_id, actor, role, (text or "")[:4000], intent,
            json.dumps(args or {}, default=str), json.dumps(refs or {}, default=str),
        )
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)


# ------------------------------------------------------------------- reads ---
async def recent_turns(session_id: str | None, limit: int = 6) -> list[dict]:
    if not session_id or not available():
        return []
    try:
        rows = await db.fetch(
            """
            select role, text, intent, args, refs, created_at
              from copilot_turns
             where session_id = $1
               and created_at > now() - interval '12 hours'
             order by created_at desc
             limit $2
            """,
            session_id[:80], limit,
        )
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return []
    out = []
    for r in reversed(rows):
        out.append({
            "role": r["role"], "text": r["text"], "intent": r["intent"],
            "args": _json(r["args"]), "refs": _json(r["refs"]),
        })
    return out


async def recall(query: str, *, city_id: str = "pune", ward_id: str | None = None,
                 limit: int = 6) -> list[dict]:
    if not available():
        return []
    try:
        rows = await db.fetch(
            "select * from recall_memory($1, $2, $3, $4)",
            city_id, _search_terms(query), ward_id, limit,
        )
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return []
    return [
        {"id": str(r["id"]), "scope": r["scope"], "content": r["content"],
         "data": _json(r["data"]), "wardId": r["ward_id"],
         "importance": r["importance"], "by": r["created_by"],
         "at": r["created_at"].isoformat()}
        for r in rows
    ]


async def list_memory(*, city_id: str = "pune", scope: str | None = None,
                      limit: int = 30) -> list[dict]:
    if not available():
        return []
    try:
        rows = await db.fetch(
            """
            select id::text, scope, content, ward_id, importance, created_by,
                   created_at, expires_at
              from agent_memory
             where city_id = $1 and active
               and (expires_at is null or expires_at > now())
               and ($2::text is null or scope = $2)
             order by (scope = 'standing_order') desc, created_at desc
             limit $3
            """,
            city_id, scope, limit,
        )
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return []
    return [
        {"id": r["id"], "scope": r["scope"], "content": r["content"],
         "ward_id": r["ward_id"], "importance": r["importance"],
         "by": r["created_by"], "at": r["created_at"].isoformat(),
         "expires": r["expires_at"].isoformat() if r["expires_at"] else None}
        for r in rows
    ]


# ------------------------------------------------------------------ shaping ---
def _json(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}


_STOP = {
    "what", "is", "the", "a", "an", "to", "of", "in", "on", "and", "or", "for",
    "me", "we", "our", "it", "that", "this", "do", "does", "did", "are", "be",
    "why", "how", "which", "who", "show", "tell", "give", "about", "with",
    "can", "should", "would", "now", "please", "there", "any", "i", "you",
}


def _search_terms(question: str) -> str:
    """Turn a question into an OR query over its content words.

    `websearch_to_tsquery` ANDs bare words, so "why is Kothrud above Aundh"
    would only match a memory mentioning all five. OR over content words is
    what recall actually wants.
    """
    words = [w.strip(".,?!:;\"'()").lower() for w in (question or "").split()]
    words = [w for w in words if len(w) > 2 and w not in _STOP]
    return " or ".join(dict.fromkeys(words))[:400]


def refs_from_blocks(blocks: list[dict]) -> dict[str, list[str]]:
    """The ids an answer was about, so a follow-up can point at them."""
    refs: dict[str, list[str]] = {
        "resource_ids": [], "incident_ids": [], "strategy_ids": [], "ward_ids": [],
    }

    def add(key: str, value: Any) -> None:
        if isinstance(value, str) and value and value not in refs[key]:
            refs[key].append(value)

    for b in blocks:
        for a in b.get("actions") or []:
            p = a.get("params") or {}
            add("resource_ids", p.get("resource_id"))
            add("incident_ids", p.get("incident_id"))
            add("ward_ids", p.get("ward_id") or a.get("wardId"))
        for s in b.get("strategies") or []:
            add("strategy_ids", s.get("id"))
        for r in (b.get("rows") or [])[:12]:
            if isinstance(r, dict):
                add("resource_ids", r.get("resource_id") or r.get("resourceId"))
                add("incident_ids", r.get("incident_id") or r.get("incidentId"))
                add("ward_ids", r.get("ward_id") or r.get("wardId"))
        for key in ("resourceId", "incidentId"):
            if b.get(key):
                add("resource_ids" if key == "resourceId" else "incident_ids", b[key])
    return {k: v[:12] for k, v in refs.items() if v}


def context_for_prompt(turns: list[dict], memories: list[dict],
                       limit: int = 1400) -> str:
    """Compact, labelled context for the router and narrator prompts."""
    parts: list[str] = []
    if turns:
        convo = []
        for t in turns[-6:]:
            who = "Q" if t["role"] == "user" else "A"
            line = f"{who}: {t['text'][:220]}"
            if t.get("refs"):
                line += f"  [refs {json.dumps(t['refs'])[:200]}]"
            convo.append(line)
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(convo))
    if memories:
        parts.append("MEMORY (words only, not numbers):\n" + "\n".join(
            f"- [{m['scope']}] {m['content'][:200]} (by {m['by']})" for m in memories[:6]
        ))
    return "\n\n".join(parts)[:limit]
