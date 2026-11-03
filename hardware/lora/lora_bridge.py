"""LoRa gateway -> Indradhanu API bridge.  Runs on the command-centre laptop.

Deliberately dumb: read a line from the gateway Arduino's USB serial, split it
into fields, POST it. No scoring, no "human detected", no thresholds - the API
is the only place that decides what a reading means, so the console, the agent
and the history all see the same thing.

    pip install pyserial requests

    # real hardware (port is found automatically; or pass --port COM5)
    python lora_bridge.py --api https://disruptionops.onrender.com --key <MESH_GATEWAY_KEY> \
        --loc RN01=18.5204,73.8567

    # no hardware yet: three fake nodes so the analytics page has data
    python lora_bridge.py --api ... --key ... --simulate

Node locations: an Arduino has no GPS, so tell the bridge where each node is
(--loc NODE=lat,lon, repeatable). It is sent with every reading; the API keeps
the last one, so you only need it on the first run or when you move the node.

If the API cannot be reached, readings are kept (in memory and in
lora_bridge_backlog.jsonl) and sent when it comes back. Nothing is dropped
because the internet blinked.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

FIELDS = ["node", "seq", "up", "mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps",
          "vib_g", "mic", "piezo", "knocks", "tilt_sw", "pir"]
INTS = {"seq", "up", "mq2", "mq135", "mic", "piezo", "knocks", "tilt_sw", "pir"}
BACKLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lora_bridge_backlog.jsonl")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_rx(line: str) -> dict | None:
    """'RX|RN01|184|...|-67|9.5' -> dict. Returns None for anything else."""
    parts = line.strip().split("|")
    if len(parts) < 4 or parts[0] != "RX":
        return None
    payload, rssi, snr = parts[1:-2], parts[-2], parts[-1]
    if len(payload) < 3:
        return None
    obs: dict = {"raw": "|".join(payload), "received_at": now_iso()}
    for i, name in enumerate(FIELDS):
        v = payload[i] if i < len(payload) else ""
        if v == "":
            obs[name] = None
            continue
        if name == "node":
            obs[name] = v[:40]
            continue
        try:
            obs[name] = int(v) if name in INTS else float(v)
        except ValueError:
            obs[name] = None
    if obs.get("pir") == -1:
        obs["pir"] = None
    try:
        obs["rssi"] = int(rssi)
        obs["snr"] = float(snr)
    except ValueError:
        obs["rssi"] = obs["snr"] = None
    return obs


class Poster(threading.Thread):
    """Sends readings in small batches; keeps them on disk while the API is away."""

    def __init__(self, api: str, key: str, gateway: str, city: str, locs: dict):
        super().__init__(daemon=True)
        self.url = api.rstrip("/")
        if not self.url.endswith("/api/v1"):
            self.url += "/api/v1"
        self.url += "/iot/observations"
        self.key, self.gateway, self.city, self.locs = key, gateway, city, locs
        self.q: deque = deque(maxlen=20000)
        self.lock = threading.Lock()
        self.sent = 0
        self.failing = False
        self._load_backlog()

    def _load_backlog(self) -> None:
        if os.path.exists(BACKLOG):
            with open(BACKLOG, encoding="utf-8") as f:
                for line in f:
                    try:
                        self.q.append(json.loads(line))
                    except Exception:
                        pass
            os.remove(BACKLOG)
            if self.q:
                print(f"[bridge] {len(self.q)} readings from last time will be sent first")

    def put(self, obs: dict) -> None:
        loc = self.locs.get(obs.get("node"))
        if loc:
            obs["lat"], obs["lon"] = loc
        with self.lock:
            self.q.append(obs)

    def save_backlog(self) -> None:
        with self.lock:
            items = list(self.q)
        if items:
            with open(BACKLOG, "w", encoding="utf-8") as f:
                for o in items:
                    f.write(json.dumps(o) + "\n")
            print(f"[bridge] saved {len(items)} unsent readings to {BACKLOG}")

    def run(self) -> None:
        backoff = 1.0
        while True:
            with self.lock:
                batch = [self.q[i] for i in range(min(50, len(self.q)))]
            if not batch:
                time.sleep(0.2)
                continue
            try:
                r = requests.post(
                    self.url, timeout=15,
                    headers={"X-Mesh-Gateway-Key": self.key},
                    json={"gatewayId": self.gateway, "cityId": self.city, "observations": batch},
                )
                if r.status_code == 403:
                    print(f"[bridge] API refused the key (403): {r.text[:200]}")
                    time.sleep(10)
                    continue
                if r.status_code >= 400 and r.status_code < 500:
                    print(f"[bridge] API rejected the batch ({r.status_code}): {r.text[:300]} - dropping it")
                else:
                    r.raise_for_status()
                    self._report(r.json())
                with self.lock:
                    for _ in batch:
                        if self.q:
                            self.q.popleft()
                self.sent += len(batch)
                if self.failing:
                    print("[bridge] API reachable again")
                self.failing, backoff = False, 1.0
            except Exception as e:
                if not self.failing:
                    print(f"[bridge] API unreachable ({type(e).__name__}); keeping readings, will retry")
                self.failing = True
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def _report(resp: dict) -> None:
        for s in resp.get("scored", [])[-3:]:
            esc = f"  -> ESCALATED {s['escalated']}" if s.get("escalated") else ""
            print(f"  {s.get('node')}: human {s.get('human', 0):.0%}  structural "
                  f"{s.get('structural', 0):.0%}  environment {s.get('environmental', 0):.0%}{esc}")


def find_port() -> str | None:
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    for p in ports:
        text = f"{p.description} {p.manufacturer} {p.hwid}".lower()
        if any(k in text for k in ("arduino", "ch340", "ch34", "wch", "usb-serial", "2341:", "1a86:")):
            return p.device
    return ports[0].device if len(ports) == 1 else None


def run_serial(port: str | None, baud: int, poster: Poster) -> None:
    import serial
    port = port or find_port()
    if not port:
        sys.exit("[bridge] no gateway Arduino found. Plug it in, close Arduino IDE's Serial Monitor, "
                 "or pass --port COM5")
    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                print(f"[bridge] listening on {port} @ {baud}")
                while True:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("RX|"):
                        obs = parse_rx(line)
                        if obs:
                            poster.put(obs)
                            print(f"[rx] {obs['node']} #{obs.get('seq')} rssi {obs.get('rssi')}  "
                                  f"queued {len(poster.q)}  sent {poster.sent}")
                    elif line.startswith("GW|"):
                        print(f"[gw] {line}")
                    elif line.startswith("#"):
                        print(f"[gw] {line[1:].strip()}")
        except Exception as e:  # unplugged, port busy...
            print(f"[bridge] serial problem on {port}: {e} - retrying in 3 s")
            time.sleep(3)


def run_simulate(poster: Poster, period: float) -> None:
    """Fake gateway lines, parsed by the same code as real ones. Node ids start
    with SIM- so the console can label them as simulated."""
    nodes = {"SIM-01": (18.5204, 73.8567), "SIM-02": (18.5236, 73.8615), "SIM-03": (18.5172, 73.8531)}
    for n, loc in nodes.items():
        poster.locs.setdefault(n, loc)
    seq = {n: 0 for n in nodes}
    t0 = time.time()
    print("[bridge] SIMULATING SIM-01..03. SIM-03 develops a survivor-under-rubble scene after ~60 s.")
    while True:
        el = time.time() - t0
        for n in nodes:
            seq[n] += 1
            hot = n == "SIM-03" and el > 60
            mq2 = 180 + random.randint(-8, 8) + (int(min(el - 60, 120) * 3) if hot else 0)
            mq135 = 260 + random.randint(-10, 10) + (int(min(el - 60, 120) * 2) if hot else 0)
            temp = 29 + random.random() + (min(el - 60, 120) / 20 if hot else 0)
            tilt = 1.0 + random.random() * 0.3 + (min(el - 60, 90) / 9 if hot else 0)
            gyro = random.random() * 2 + (random.random() * 25 if hot and random.random() < 0.3 else 0)
            vib = 0.004 + random.random() * 0.004 + (0.05 if hot and random.random() < 0.3 else 0)
            mic = 18 + random.randint(0, 10) + (random.randint(80, 220) if hot and random.random() < 0.5 else 0)
            knocks = (random.choice([0, 2, 3, 4]) if hot else 0)
            piezo = 8 + random.randint(0, 6) + (knocks * 60 if knocks else 0)
            tsw = 1 if (hot and el > 120) else 0
            line = (f"RX|{n}|{seq[n]}|{int(el) + 200}|{mq2}|{mq135}|{temp:.1f}|{tilt:.1f}|{gyro:.1f}"
                    f"|{vib:.3f}|{mic}|{piezo}|{knocks}|{tsw}|-1|{-60 - random.randint(0, 30)}|{8 + random.random():.1f}")
            obs = parse_rx(line)
            if obs:
                poster.put(obs)
        print(f"[sim] t={el:4.0f}s queued {len(poster.q)} sent {poster.sent}")
        time.sleep(period)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("INDRADHANU_API", "https://disruptionops.onrender.com"))
    ap.add_argument("--key", default=os.environ.get("MESH_GATEWAY_KEY", ""))
    ap.add_argument("--port", default=None, help="COM5 / /dev/ttyUSB0 (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--gateway", default="lora-gw-01", help="name this gateway shows under on the console")
    ap.add_argument("--city", default="pune")
    ap.add_argument("--loc", action="append", default=[], metavar="NODE=lat,lon")
    ap.add_argument("--simulate", action="store_true", help="no hardware: generate three fake nodes")
    ap.add_argument("--period", type=float, default=2.0, help="seconds between simulated readings")
    a = ap.parse_args()

    if not a.key:
        sys.exit("[bridge] --key (or env MESH_GATEWAY_KEY) is required: it is the API's MESH_GATEWAY_KEY")
    locs = {}
    for spec in a.loc:
        try:
            node, ll = spec.split("=")
            lat, lon = (float(x) for x in ll.split(","))
            if not (math.isfinite(lat) and math.isfinite(lon)):
                raise ValueError
            locs[node.strip()] = (lat, lon)
        except ValueError:
            sys.exit(f"[bridge] bad --loc {spec!r}; use NODE=lat,lon e.g. RN01=18.5204,73.8567")

    poster = Poster(a.api, a.key, a.gateway, a.city, locs)
    poster.start()
    print(f"[bridge] posting to {poster.url}")
    try:
        if a.simulate:
            run_simulate(poster, a.period)
        else:
            run_serial(a.port, a.baud, poster)
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
        poster.save_backlog()


if __name__ == "__main__":
    main()
