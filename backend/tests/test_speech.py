"""Speech-to-text failures are reported as what they are, not as "nothing heard"."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.incidents import speech


def _run(handler, monkeypatch, *, content_type="audio/webm;codecs=opus"):
    real = httpx.AsyncClient
    seen: dict = {}

    def client(**kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(lambda r: handler(r, seen)), **kw)

    monkeypatch.setattr(speech.httpx, "AsyncClient", client)
    monkeypatch.setattr(speech.settings, "sarvam_api_key", "test-key")
    result = asyncio.run(speech.transcribe(b"RIFF....WAVE", content_type=content_type))
    return result, seen


def test_normalises_browser_content_types():
    assert speech.normalise_type("audio/webm;codecs=opus") == ("audio/webm", "report.webm")
    assert speech.normalise_type("audio/mp4") == ("audio/mp4", "report.m4a")
    assert speech.normalise_type("audio/wav") == ("audio/wav", "report.wav")
    assert speech.normalise_type("") == ("audio/wav", "report.wav")


def test_sends_a_clean_type_and_file_name(monkeypatch):
    def handler(request, seen):
        seen["body"] = request.content
        return httpx.Response(200, json={"transcript": "water on the road", "language_code": "en-IN"})

    heard, seen = _run(handler, monkeypatch)
    assert heard is not None and heard.text == "water on the road"
    assert b'filename="report.webm"' in seen["body"]
    assert b"Content-Type: audio/webm\r\n" in seen["body"]
    assert b"codecs" not in seen["body"]


def test_service_refusal_raises_with_its_reason(monkeypatch):
    def handler(request, seen):
        return httpx.Response(400, json={"error": {"message": "Unsupported audio format"}})

    with pytest.raises(speech.SpeechError) as err:
        _run(handler, monkeypatch)
    assert "Unsupported audio format" in err.value.message
    assert err.value.status == 400


def test_bad_key_is_named(monkeypatch):
    def handler(request, seen):
        return httpx.Response(403, json={"error": {"message": "invalid key"}})

    with pytest.raises(speech.SpeechError) as err:
        _run(handler, monkeypatch)
    assert "key" in err.value.message


def test_silence_is_none_not_an_error(monkeypatch):
    def handler(request, seen):
        return httpx.Response(200, json={"transcript": "", "language_code": "unknown"})

    heard, _ = _run(handler, monkeypatch)
    assert heard is None
