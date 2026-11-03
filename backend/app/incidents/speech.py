"""Reports spoken, not typed.

Typing is the wrong input for this. Somebody standing in water, on a phone, in
the dark, with one hand free, in Marathi, is not going to fill in a form — and
the fastest report is the one that needed the least from the person making it.
So: hold a button, say what you see, let go.

Sarvam's Saarika is the right model for it and the reason is narrow. It is
trained on Indian languages specifically, including code-mixed speech, which is
what people in Pune actually speak: a Marathi sentence with English nouns in it.
A general multilingual recogniser transcribes that badly, and the failure is not
random — it drops exactly the loan words ("underpass", "society", "terrace")
that carry the location.

Two modes, and which one is used is a real decision:

  * **transcribe** keeps the original language. The parser's keyword vocabulary
    is already trilingual, so "पाणी साचले आहे" matches Marathi terms directly
    and an officer reading the report afterwards sees what was actually said.
  * **translate** returns English. Used only as a fallback when the language is
    one the keyword vocabulary does not cover, because translation loses the
    idiom the parser leans on.

A service failure raises `SpeechError` with a sentence a person can read; silence
returns None. Either way the person keeps the text box they already had.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

STT_URL = "https://api.sarvam.ai/speech-to-text"
STT_TRANSLATE_URL = "https://api.sarvam.ai/speech-to-text-translate"

#: Languages the keyword parser has vocabulary for. Anything else is translated
#: to English first, because a transcript in a language nothing downstream reads
#: is worse than a translated one.
PARSEABLE = {"hi-IN", "mr-IN", "en-IN"}

#: What the API accepts. Larger than this and the upload is the slow part.
MAX_AUDIO_BYTES = 8 * 1024 * 1024

LANGUAGE_NAMES = {
    "en-IN": "English", "hi-IN": "Hindi", "mr-IN": "Marathi",
    "bn-IN": "Bengali", "gu-IN": "Gujarati", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "od-IN": "Odia", "pa-IN": "Punjabi",
    "ta-IN": "Tamil", "te-IN": "Telugu",
}


def configured() -> bool:
    return bool(_key())


def _key() -> str:
    return (settings.sarvam_api_key or settings.sarvam_ai_api_key or "").strip()


@dataclass(slots=True)
class Transcript:
    text: str
    language: str
    #: True when the text was translated rather than transcribed.
    translated: bool
    model: str
    latency_ms: int = 0
    #: Anything the service said that is worth showing the person.
    notes: list[str] = field(default_factory=list)

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES.get(self.language, self.language or "unknown")

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def decode_audio(b64: str) -> bytes:
    """Decode a base64 payload, refusing anything implausible.

    Size is checked before decoding as well as after: a client that sends
    thirty megabytes of base64 should be told so, not have it materialised in
    this process first.
    """
    if len(b64) > MAX_AUDIO_BYTES * 2:
        raise ValueError("That recording is too large. Keep it under about thirty seconds.")
    if "," in b64[:64] and b64.lstrip().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("That audio could not be decoded.") from exc
    if not raw:
        raise ValueError("The recording was empty.")
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError("That recording is too large. Keep it under about thirty seconds.")
    return raw


#: Extension Sarvam should see for each container we may be sent. The service reads the
#: format from the upload, and a browser's `audio/webm;codecs=opus` is not a file name.
_EXTENSIONS = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/flac": "flac",
}


def normalise_type(content_type: str) -> tuple[str, str]:
    """`audio/webm;codecs=opus` → (`audio/webm`, `report.webm`)."""
    base = (content_type or "").split(";", 1)[0].strip().lower() or "audio/wav"
    return base, f"report.{_EXTENSIONS.get(base, 'wav')}"


class SpeechError(Exception):
    """The speech service refused or failed. `message` is safe to show a person."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


async def transcribe(
    audio: bytes,
    *,
    filename: str | None = None,
    content_type: str = "audio/wav",
    language: str = "unknown",
) -> Transcript | None:
    """Speech to text, keeping the original language where we can parse it.

    `language="unknown"` asks Saarika to detect it, which is the right default:
    a person reporting a flood should not have to pick their own language off a
    list first.

    Returns None when nothing was said. Raises [SpeechError] when the service
    itself failed, so the caller can say *why* instead of "nothing heard".
    """
    key = _key()
    if not key:
        return None

    import time

    content_type, default_name = normalise_type(content_type)
    filename = filename or default_name
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=max(30.0, settings.feed_timeout_seconds * 2)) as client:
            response = await client.post(
                STT_URL,
                headers={"api-subscription-key": key},
                files={"file": (filename, audio, content_type)},
                data={
                    "model": settings.sarvam_stt_model,
                    "language_code": language or "unknown",
                },
            )
    except httpx.HTTPError as exc:
        log.warning("sarvam_stt_unreachable", error=str(exc)[:200])
        raise SpeechError("The speech service could not be reached. Try again, or type the report.") from exc

    if response.status_code >= 400:
        detail = _error_text(response)
        log.warning("sarvam_stt_refused", status=response.status_code, detail=detail[:300],
                    content_type=content_type, size=len(audio))
        if response.status_code in (401, 403):
            raise SpeechError("Speech is set up with a key the speech service does not accept.",
                              status=response.status_code)
        if response.status_code == 429:
            raise SpeechError("The speech service is busy. Try again in a moment.", status=429)
        raise SpeechError(
            f"The speech service could not read that recording ({detail or response.status_code}).",
            status=response.status_code,
        )
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise SpeechError("The speech service sent an answer that could not be read.") from exc

    latency = int((time.perf_counter() - started) * 1000)
    text = str((payload or {}).get("transcript") or "").strip()
    detected = str((payload or {}).get("language_code") or language or "").strip()

    notes: list[str] = []
    if detected and detected not in PARSEABLE and detected != "unknown":
        # The transcript is in a language the keyword vocabulary does not cover.
        # Rather than hand the parser something it will certainly fall through
        # on, ask for English and say that is what happened.
        english = await _translate(audio, filename=filename, content_type=content_type)
        if english and english.strip():
            notes.append(
                f"Spoken in {LANGUAGE_NAMES.get(detected, detected)} and "
                "translated to English, because the keyword vocabulary does not "
                "cover that language yet."
            )
            return Transcript(
                text=english.strip(), language=detected, translated=True,
                model=settings.sarvam_stt_model, latency_ms=latency, notes=notes,
            )

    if not text:
        return None
    return Transcript(
        text=text, language=detected or "unknown", translated=False,
        model=settings.sarvam_stt_model, latency_ms=latency, notes=notes,
    )


def _error_text(response: httpx.Response) -> str:
    """The service's own words for a refusal, from whichever shape it used."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:200]
        return str(body.get("message") or body.get("detail") or err or "")[:200]
    return str(body)[:200]


async def _translate(
    audio: bytes, *, filename: str, content_type: str
) -> str | None:
    key = _key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=settings.feed_timeout_seconds * 2) as client:
            response = await client.post(
                STT_TRANSLATE_URL,
                headers={"api-subscription-key": key},
                files={"file": (filename, audio, content_type)},
                data={"model": settings.sarvam_stt_translate_model},
            )
            response.raise_for_status()
            return str((response.json() or {}).get("transcript") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("sarvam_translate_unavailable", error=str(exc)[:200])
        return None
