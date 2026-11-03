"""Bridge between a bitchat phone and the Indradhanu API. Standard library only.

Two modes.

bridge (default) — run on a laptop that is on the same Wi-Fi as a phone running
the bitchat fork with the VLM API on. Every few seconds it:

    1. reads GET  http://<phone>:8765/inbox?since=N      (what the mesh heard)
    2. posts POST <api>/api/v1/mesh/inbound              (to the control room)
    3. reads GET  <api>/api/v1/mesh/outbox               (alerts, dispatches…)
    4. posts POST http://<phone>:8765/send/text           (into the mesh)
       respecting the phone's 5-second send window, which answers 400
       "Rate limited" to anything sent inside it
    5. posts POST <api>/api/v1/mesh/outbox/ack

   Use this when the gateway phone has no internet of its own but the laptop
   does, or when you do not want to rebuild the app with gateway mode.

sim — no phone at all. Sends packets straight to the API with a per-hop delay
and loss rate, so the full path (packet -> verify -> intake -> incident ->
re-plan -> outbox) can be shown on a stage with one laptop:

    python scripts/mesh_bridge.py sim --api https://api.example --key $MESH_GATEWAY_KEY \
        --hmac $MESH_HMAC_KEY --report flooded_road 18.5204 73.8567 "Water knee deep" --hops 3

Environment variables INDRADHANU_API, MESH_GATEWAY_KEY, MESH_HMAC_KEY and
BITCHAT_IP are read when the flags are not given.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid

UA = "indradhanu-mesh-bridge/1"


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)}


# ------------------------------------------------------------------- packets --
def sign(key: str, type_: str, payload: str) -> str:
    return hmac.new(key.encode(), f"IDX1|{type_}|{payload}".encode(),
                    hashlib.sha256).hexdigest()[:16]


def packet(type_: str, body: dict, key: str = "", human: str = "") -> str:
    """Same format as app/mesh/envelope.py. Kept in step by tests."""
    clean = {k: (round(v, 5) if k in ("la", "lo") else v)
             for k, v in body.items() if v not in (None, "")}
    payload = json.dumps(clean, separators=(",", ":"), ensure_ascii=False)
    sig = sign(key, type_, payload) if key else "-"
    text = f"IDX1|{type_}|{payload}|{sig}"
    return f"{human.strip()} {text}" if human else text


# -------------------------------------------------------------------- bridge --
def bridge(args: argparse.Namespace) -> None:
    phone = f"http://{args.phone}:{args.port}"
    api = args.api.rstrip("/")
    gw = {"X-Mesh-Gateway-Key": args.key}
    since = 0
    last_send = 0.0

    code, status = http("GET", f"{phone}/status")
    print(f"[bridge] phone {phone}: {code} {status}")
    if code != 200 or not status.get("api_enabled"):
        print("[bridge] enable the VLM API in bitchat settings first", file=sys.stderr)

    while True:
        # 1-2: mesh -> control room
        code, box = http("GET", f"{phone}/inbox?since={since}")
        if code == 200 and box.get("messages"):
            texts = [m["text"] for m in box["messages"]]
            c, res = http("POST", f"{api}/api/v1/mesh/inbound",
                          {"gatewayId": args.gateway_id, "packets": texts}, gw)
            if c == 200:
                since = box.get("next", since)
                for t, r in zip(texts, res.get("results", [])):
                    print(f"[in ] {r.get('outcome') or r.get('error')}: {t[:90]}")
            else:
                print(f"[in ] API refused ({c}): {res}")
        elif code == 0:
            print(f"[in ] phone unreachable: {box.get('error')}")

        # 3-5: control room -> mesh
        c, out = http("GET", f"{api}/api/v1/mesh/outbox?gateway_id={args.gateway_id}&limit=10",
                      headers=gw)
        acked = []
        for m in out.get("messages", []) if c == 200 else []:
            wait = args.rate - (time.monotonic() - last_send)
            if wait > 0:
                time.sleep(wait)
            body = {"text": m["text"]}
            if m.get("channel"):
                body["channel"] = m["channel"]
            sc, sr = http("POST", f"{phone}/send/text", body)
            last_send = time.monotonic()
            if sc == 200:
                acked.append(m["id"])
                print(f"[out] {m['kind']} p{m['priority']}: {m['text'][:90]}")
            else:
                print(f"[out] phone refused ({sc}): {sr.get('error')}; will be re-offered")
        if acked:
            http("POST", f"{api}/api/v1/mesh/outbox/ack",
                 {"gatewayId": args.gateway_id, "ids": acked}, gw)
        time.sleep(args.every)


# ----------------------------------------------------------------------- sim --
def sim(args: argparse.Namespace) -> None:
    api = args.api.rstrip("/")
    gw = {"X-Mesh-Gateway-Key": args.key}
    kind, lat, lon, note = args.report
    body = {"id": f"sim-{uuid.uuid4().hex[:10]}", "n": args.node, "k": kind,
            "la": float(lat), "lo": float(lon), "x": note, "t": int(time.time())}
    type_ = "S" if args.sensor else "R"
    if args.sensor:
        body.update({"c": args.confidence, "f": "3/5", "v": 1})
    text = packet(type_, body, args.hmac, human=f"[{kind.upper()}] {note}")
    print(f"[sim] {text}")
    for hop in range(1, args.hops + 1):
        time.sleep(random.uniform(0.3, 1.2))
        if random.random() < args.loss:
            print(f"[sim] hop {hop}: lost, retrying from the previous phone")
            time.sleep(1.0)
        print(f"[sim] hop {hop}/{args.hops}")
    code, res = http("POST", f"{api}/api/v1/mesh/inbound",
                     {"gatewayId": "sim-gateway", "packets": [text]}, gw)
    print(f"[sim] API {code}: {json.dumps(res)}")
    # A second gateway hearing the same packet: must be a duplicate, not a report.
    code, res = http("POST", f"{api}/api/v1/mesh/inbound",
                     {"gatewayId": "sim-gateway-2", "packets": [text]}, gw)
    print(f"[sim] second gateway {code}: {json.dumps(res)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="bridge", choices=["bridge", "sim"])
    ap.add_argument("--api", default=os.getenv("INDRADHANU_API", "http://localhost:8000"))
    ap.add_argument("--key", default=os.getenv("MESH_GATEWAY_KEY", ""))
    ap.add_argument("--hmac", default=os.getenv("MESH_HMAC_KEY", ""))
    ap.add_argument("--phone", default=os.getenv("BITCHAT_IP", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gateway-id", default=f"laptop-{uuid.getnode() % 100000}")
    ap.add_argument("--every", type=float, default=3.0)
    ap.add_argument("--rate", type=float, default=5.5, help="seconds between phone sends")
    ap.add_argument("--report", nargs=4, metavar=("KIND", "LAT", "LON", "NOTE"),
                    default=["flooded_road", "18.5204", "73.8567", "Water knee deep on the road"])
    ap.add_argument("--sensor", action="store_true", help="send as a camera detection (S)")
    ap.add_argument("--confidence", type=float, default=0.78)
    ap.add_argument("--node", default="sim-phone-1")
    ap.add_argument("--hops", type=int, default=3)
    ap.add_argument("--loss", type=float, default=0.2)
    args = ap.parse_args()
    if not args.key:
        ap.error("--key or MESH_GATEWAY_KEY is required")
    (sim if args.mode == "sim" else bridge)(args)


if __name__ == "__main__":
    main()
