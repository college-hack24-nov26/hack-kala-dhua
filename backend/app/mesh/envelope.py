"""IDX1: the packet that crosses the offline mesh.

One line of text, because bitchat carries text reliably and fragments
everything else. Human-readable in front, machine-readable after the marker, so
a person reading the bitchat channel sees what happened and a program reading
the same message can act on it:

    FLOOD ALERT Baner: go to Balewadi School (1.2 km N) IDX1|A|{...}|3f9a1c0e2b7d4a55

    IDX1 | type | compact JSON | signature

Types
-----
  inbound (to the control room)
    R  report         a person's report from the citizen app's mesh mode
    S  sensor         a camera node / sensor detection
    F  field status   a crew's status (road blocked, unit down, on scene)
    H  heartbeat      a node saying it is alive, and where
    K  ack            "I received outbound message <id>"
  outbound (from the control room)
    A  alert          public advisory for an area: centre + radius, or ward
    D  dispatch       a crew's new job
    C  cancel         a crew's job was cancelled, and what to do instead
    B  block          a road is closed here (so offline guidance avoids it)

Keys are one or two letters to keep packets small (BLE fragments at a few
hundred bytes; every fragment is a chance to lose the message):

    id  packet id (sender-unique)     t   unix seconds when it happened
    n   node / device id              k   kind (category, detector, status)
    la  latitude, 5 dp (~1 m)         lo  longitude, 5 dp
    r   radius in metres              w   ward id
    c   confidence 0..1               v   1 if a VLM agreed
    x   free text (short)             u   unit id
    i   incident id (short)           h   hops, if a relay counts them
    s   severity 1..5                 m   minutes (hold, ETA)

Signature: first 16 hex of HMAC-SHA256(key, "IDX1|<type>|<json>"). Integrity
from the sender to us; bitchat's own Noise encryption protects each hop.
Without a key, packets are accepted and marked unsigned, which the trust model
scores down (`SOURCE_CREDIBILITY["mesh_unsigned"]`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

MARKER = "IDX1|"
INBOUND = {"R", "S", "F", "H", "K"}
OUTBOUND = {"A", "D", "C", "B"}
TYPES = INBOUND | OUTBOUND
#: Refuse anything longer; a real packet is well under this.
MAX_PACKET = 1200

_PACKET = re.compile(r"IDX1\|([A-Z])\|(\{.*\})\|([0-9a-f]{16}|-)\s*$", re.S)


class BadPacket(ValueError):
    """Not an IDX1 packet, or not one we accept."""


@dataclass(slots=True)
class Packet:
    type: str
    body: dict[str, Any]
    signed: bool
    verified: bool
    human: str = ""

    @property
    def id(self) -> str:
        return str(self.body.get("id") or "")

    @property
    def location(self) -> tuple[float, float] | None:
        try:
            return (float(self.body["lo"]), float(self.body["la"]))
        except (KeyError, TypeError, ValueError):
            return None


def _sign(key: str, type_: str, payload: str) -> str:
    msg = f"{MARKER}{type_}|{payload}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()[:16]


def _compact(body: dict[str, Any]) -> str:
    clean: dict[str, Any] = {}
    for k, v in body.items():
        if v is None or v == "":
            continue
        if k in ("la", "lo") and isinstance(v, (int, float)):
            v = round(float(v), 5)
        clean[k] = v
    return json.dumps(clean, separators=(",", ":"), ensure_ascii=False)


def encode(type_: str, body: dict[str, Any], *, key: str = "", human: str = "") -> str:
    if type_ not in TYPES:
        raise BadPacket(f"unknown type {type_!r}")
    payload = _compact(body)
    sig = _sign(key, type_, payload) if key else "-"
    text = f"{MARKER}{type_}|{payload}|{sig}"
    if human:
        text = f"{human.strip()[:240]} {text}"
    if len(text) > MAX_PACKET:
        raise BadPacket("packet too long for the mesh")
    return text


def decode(text: str, *, key: str = "", require_signature: bool = False) -> Packet:
    """Parse one message. Raises BadPacket if it is not ours or is tampered."""
    if not text or MARKER not in text or len(text) > MAX_PACKET:
        raise BadPacket("no IDX1 marker")
    start = text.index(MARKER)
    human = text[:start].strip()
    m = _PACKET.match(text[start:].strip())
    if not m:
        raise BadPacket("malformed IDX1 packet")
    type_, payload, sig = m.group(1), m.group(2), m.group(3)
    if type_ not in TYPES:
        raise BadPacket(f"unknown type {type_!r}")
    try:
        body = json.loads(payload)
    except ValueError as exc:
        raise BadPacket("body is not JSON") from exc
    if not isinstance(body, dict):
        raise BadPacket("body is not an object")

    signed = sig != "-"
    verified = False
    if key and signed:
        verified = hmac.compare_digest(sig, _sign(key, type_, payload))
        if not verified:
            raise BadPacket("signature does not match")
    if require_signature and not verified:
        raise BadPacket("unsigned packet refused")
    return Packet(type=type_, body=body, signed=signed, verified=verified, human=human)


def find_packets(text: str) -> list[str]:
    """A bitchat inbox line may carry the packet after a nickname or prefix."""
    return [text[m.start():] for m in re.finditer(re.escape(MARKER), text or "")][:1]
