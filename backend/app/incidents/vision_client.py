"""Calling a vision model that lives somewhere else.

The model is not our problem. This is the whole of our side of the boundary:
post an image and the prompt, get back JSON, hand it to `vision.parse_response`
which validates it and to `vision.assess` which scores it against what the
person actually reported.

Three placements, one code path:

  * **hosted** — `VLM_URL` points at a machine running the model. That machine
    can be anything; it only has to answer the contract in `vision.PROMPT`.
  * **device** — the phone ran it and posts the JSON to `/citizen/vision`
    directly. This module is not involved, and the validation is identical,
    which is the property that keeps placement an engineering decision.
  * **none** — no URL configured. Reports carry photos and nobody looks at them,
    which is exactly where this was before and is a perfectly fine state to
    ship in.

Never raises. A vision service that is down must cost a report its corroboration
bonus and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.incidents import vision

log = get_logger(__name__)


@dataclass(slots=True)
class VisionCall:
    ok: bool
    evidence: vision.PhotoEvidence | None
    error: str = ""
    latency_ms: int = 0


def configured() -> bool:
    return bool(settings.vlm_url)


def _extract_json(text: str) -> Any:
    """Small models wrap JSON in prose and fences however you ask them not to.

    Pulling the first balanced object out is not elegant, and the alternative is
    discarding an otherwise good answer because the model said "Here is the
    JSON:" first.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


#: Which endpoint and body shape last worked, so the probing below happens once
#: per process rather than once per photo.
_LEARNED: tuple[str, str] | None = None   # (url, shape)


def _candidates() -> list[tuple[str, str]]:
    """Where to try, and in what shape, most likely first.

    `VLM_URL` is written by a person under time pressure, and the thing they
    paste is almost always the tunnel root — `https://something.trycloudflare.com`
    — because that is what the tunnel printed. Posting to a root that only
    serves GET is what produced `405 Method Not Allowed` on every photo: the
    model was up, reachable and perfectly willing; nothing ever asked it
    anything it recognised.

    So rather than demand the exact path, derive the ones that matter.

    Order matters, and it changed. The first version tried the OpenAI
    chat-completions route first on the reasoning that every serving stack
    exposes one. The service we are actually pointed at is not a raw serving
    stack: it is a small FastAPI in front of one, implementing the contract in
    `docs/VLM_CONTRACT.md`, and it serves `/analyse`. So a bare host produced
    exactly this, on every photo, and nothing else:

        POST /v1/chat/completions -> 404
        POST /api/chat            -> 404
        POST /                    -> 405

    Three misses and a report scored as if no photo were attached, with a GPU
    sitting idle on the other end answering health probes. The contract paths go
    first now; the OpenAI ones stay behind them so pointing `VLM_URL` straight
    at vLLM or Ollama still works.
    """
    raw = settings.vlm_url.rstrip("/")
    tail = raw.rsplit("/", 1)[-1] if "/" in raw.split("://", 1)[-1] else ""

    # Already pointed at a chat-completions route: believe them.
    if raw.endswith("/chat/completions"):
        return [(raw, "openai")]
    if raw.endswith("/v1"):
        return [(f"{raw}/analyse", "simple"), (f"{raw}/chat/completions", "openai"),
                (raw, "simple")]
    # A path that is clearly somebody's own endpoint — try it as given first,
    # then fall back, because a custom server is likelier to want our shape.
    if tail and tail not in ("v1", "api"):
        return [(raw, "simple"), (raw, "openai"),
                (f"{raw}/v1/chat/completions", "openai")]
    # A bare host. The contract endpoint and its documented aliases first.
    return [(f"{raw}/analyse", "simple"),
            (f"{raw}/v1/analyse", "simple"),
            (f"{raw}/analyze", "simple"),
            (f"{raw}/v1/chat/completions", "openai"),
            (f"{raw}/api/chat", "openai"),
            (raw, "simple")]


def _openai_body(image: str) -> dict[str, Any]:
    """The chat-completions shape, with the image inline.

    `image` is either an http(s) URL or a `data:` URL; both are legal in the
    `image_url` content part and every stack that serves vision models accepts
    the data form, which matters because a photo on a phone in a flood has no
    URL anybody else can reach.
    """
    return {
        "model": settings.vlm_model or "qwen2.5-vl",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": vision.PROMPT},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        }],
        "max_tokens": 512,
        "temperature": 0.1,
        # Honoured by vLLM and llama.cpp as constrained decoding, ignored
        # harmlessly by the rest. Asking twice — here and in the prompt —
        # because a model that can be *forced* to emit valid JSON should be.
        "response_format": {"type": "json_object"},
    }


def _simple_body(image_url: str | None, image_base64: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": vision.PROMPT,
        "model": settings.vlm_model or None,
        "response_format": "json",
        "max_tokens": 512,
        "temperature": 0.1,
    }
    if image_url:
        body["image_url"] = image_url
    if image_base64:
        body["image_base64"] = image_base64
    return body


async def analyse(
    *,
    image_url: str | None = None,
    image_base64: str | None = None,
    category: str = "flooded_road",
) -> VisionCall:
    """Ask the configured vision service about one photo.

    Sends whichever of URL or base64 the caller has. A hosted model that can
    fetch a URL should be given the URL: shipping a megabyte of base64 through
    our own process to a GPU box is a waste of both ends.
    """
    global _LEARNED

    if not settings.vlm_url:
        return VisionCall(ok=False, evidence=None, error="No VLM_URL configured.")
    if not image_url and not image_base64:
        return VisionCall(ok=False, evidence=None, error="No image supplied.")

    headers = {"Content-Type": "application/json"}
    if settings.vlm_api_key:
        headers["Authorization"] = f"Bearer {settings.vlm_api_key}"

    inline = image_url or f"data:image/jpeg;base64,{(image_base64 or '').split(',')[-1]}"
    attempts = [_LEARNED] if _LEARNED else _candidates()

    import time

    payload: Any = None
    latency = 0
    last_error = ""
    for url, shape in attempts:
        body = (_openai_body(inline) if shape == "openai"
                else _simple_body(image_url, image_base64))
        try:
            async with httpx.AsyncClient(timeout=settings.vlm_timeout_seconds) as client:
                started = time.perf_counter()
                response = await client.post(url, headers=headers, json=body)
                latency = int((time.perf_counter() - started) * 1000)
            if response.status_code in (404, 405, 501):
                # Wrong door, not a broken model. Try the next one rather than
                # reporting the service down.
                last_error = f"{response.status_code} at {url}"
                log.info("vlm_wrong_endpoint", url=url, shape=shape,
                         status=response.status_code)
                continue
            if response.status_code in (502, 503):
                # Right door, wrong moment: the model is still reading weights
                # off disk, or answered with something that failed the service's
                # own validation. Knocking on every other door after this would
                # collect three 404s and then report "unreachable", which is the
                # wrong diagnosis and throws away the one endpoint that works.
                _LEARNED = (url, shape)
                log.info("vlm_not_ready", url=url, status=response.status_code,
                         body=response.text[:200])
                return VisionCall(
                    ok=False, evidence=None, latency_ms=latency,
                    error=f"The vision service is not ready ({response.status_code}).",
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - a model outage is not a failure
            last_error = f"{type(exc).__name__} at {url}"
            log.warning("vlm_attempt_failed", url=url, shape=shape,
                        error=str(exc)[:200])
            continue

        if _LEARNED != (url, shape):
            _LEARNED = (url, shape)
            log.info("vlm_endpoint_learned", url=url, shape=shape,
                     note="remembered for the rest of this process")
        break

    if payload is None:
        # Forget what we learned: the next call should probe again rather than
        # keep posting at a door that has stopped answering.
        _LEARNED = None
        log.warning("vlm_unavailable", error=last_error[:200],
                    tried=[u for u, _ in attempts])
        return VisionCall(ok=False, evidence=None, error=last_error or "unreachable")

    # Accept either the analysis directly, or a wrapper with the model's text in
    # it, because every serving stack shapes this differently and none of them
    # are wrong.
    analysis: Any = None
    if isinstance(payload, dict):
        # The OpenAI shape first, because that is what almost every stack
        # actually returns: the model's text is buried three levels down and
        # the old key-scan below never looked there, so even a successful call
        # used to come back as "answered, but not in the contract's shape".
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            content = None
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                if content is None:
                    content = first.get("text")
            if isinstance(content, str):
                analysis = _extract_json(content)
            elif isinstance(content, list):
                # Some servers return content as parts. Join the text ones.
                joined = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
                analysis = _extract_json(joined)

    if analysis is None and isinstance(payload, dict):
        # Ollama's native shape, and anything else with the text at the top.
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            analysis = _extract_json(message["content"])

    if analysis is None and isinstance(payload, dict):
        for key in ("analysis", "result", "json", "data"):
            if isinstance(payload.get(key), dict):
                analysis = payload[key]
                break
        if analysis is None:
            for key in ("text", "output", "content", "generated_text", "response"):
                if isinstance(payload.get(key), str):
                    analysis = _extract_json(payload[key])
                    break
        if analysis is None and "shows_hazard" in payload:
            analysis = payload
    elif isinstance(payload, str):
        analysis = _extract_json(payload)

    evidence = vision.parse_response(
        analysis, model=settings.vlm_model or "hosted", ran_on="hosted"
    )
    if evidence is None:
        return VisionCall(
            ok=False, evidence=None,
            error="The service answered, but not in the contract's shape.",
            latency_ms=latency,
        )
    return VisionCall(
        ok=True,
        evidence=vision.assess(evidence, category=category),
        latency_ms=latency,
    )
