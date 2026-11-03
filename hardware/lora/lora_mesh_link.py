"""LoRa mesh link: joins two bitchat BLE-mesh areas over a pair of LoRa radios.

Bluetooth reaches tens of metres per hop. Two Unos with SX1278 radios (running
modem/modem.ino) carry the same IDX1 packets over hundreds of metres to
kilometres, so phones that cannot hear each other over Bluetooth still share
reports, SOS and the control room's alerts.

    phones (BLE) ── bitchat phone :8765 ── laptop A ── Uno+LoRa  ~~~~~~
                                                                       ~~~ LoRa
    phones (BLE) ── bitchat phone :8765 ── laptop B ── Uno+LoRa  ~~~~~~
                                               │
                                               └── internet ── Indradhanu API

Run one copy next to each Uno. Give it whatever that laptop has:

    --phone IP   a bitchat phone (fork with the VLM API on) on the same Wi-Fi
    --api URL --key KEY   internet + the API's MESH_GATEWAY_KEY

What it does with them:

    phone inbox  -> LoRa (and -> API when --api)
    LoRa         -> phone (into the BLE mesh) and -> API when --api
    API outbox   -> LoRa and -> phone          (alerts, dispatches to the offline area)
    keyboard     -> LoRa                       (type "sos <text>" or any text)

Only IDX1 packets are relayed by default (--all relays every mesh message).
Each message is sent twice over LoRa and de-duplicated everywhere by its
content, so nothing loops between the two sides.

    pip install pyserial

    # offline area, laptop A with a phone:
    python lora_mesh_link.py --phone 192.168.43.20 --name field-A --lat 18.5204 --lon 73.8567

    # command centre, laptop B with internet (phone optional):
    python lora_mesh_link.py --api https://disruptionops.onrender.com --key <MESH_GATEWAY_KEY> --name hq-B

    # no phones at all: on laptop A type   sos water entering the ground floor
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict

MAXPKT = 240
HDR = 8                        # b"M" + 4-char id + idx + total + b":"
CHUNK = MAXPKT - HDR
B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def log(tag: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [{tag}] {msg}", flush=True)


# ------------------------------------------------------------------ framing ---
def fragments(data: bytes) -> list[bytes]:
    """Split one message into LoRa frames: b'M' + id(4) + idx + total + b':' + chunk."""
    mid = uuid.uuid4().hex[:4].encode()
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    if len(parts) >= len(B36):
        raise ValueError("message too long for the link")
    return [b"M" + mid + B36[i].encode() + B36[len(parts)].encode() + b":" + p for i, p in enumerate(parts)]


class Reassembler:
    def __init__(self, ttl: float = 90.0):
        self.ttl = ttl
        self.parts: dict[bytes, dict] = {}

    def add(self, frame: bytes) -> bytes | None:
        now = time.monotonic()
        for k in [k for k, v in self.parts.items() if now - v["t"] > self.ttl]:
            del self.parts[k]
        if len(frame) < HDR or frame[:1] != b"M" or frame[7:8] != b":":
            return None
        mid, idx, total = frame[1:5], B36.find(chr(frame[5])), B36.find(chr(frame[6]))
        if idx < 0 or total <= 0 or idx >= total:
            return None
        e = self.parts.setdefault(mid, {"t": now, "n": total, "got": {}})
        e["got"][idx] = frame[HDR:]
        if len(e["got"]) == e["n"]:
            del self.parts[mid]
            return b"".join(e["got"][i] for i in range(e["n"]))
        return None


class Seen:
    """Content de-duplication across all paths (LoRa, phone, API)."""

    def __init__(self, keep: int = 2000):
        self.keep, self.d = keep, OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def key(text: str) -> str:
        t = text.strip()
        i = t.find("IDX1|")
        return hashlib.sha1((t[i:] if i >= 0 else t).encode()).hexdigest()

    def first(self, text: str) -> bool:
        k = self.key(text)
        with self.lock:
            if k in self.d:
                return False
            self.d[k] = True
            while len(self.d) > self.keep:
                self.d.popitem(last=False)
            return True


# -------------------------------------------------------------------- modem ---
class Modem:
    def __init__(self, port: str | None, baud: int = 115200):
        import serial
        from serial.tools import list_ports
        if not port:
            ports = list(list_ports.comports())
            cands = [p.device for p in ports if any(k in f"{p.description} {p.manufacturer} {p.hwid}".lower()
                                                    for k in ("arduino", "ch34", "wch", "usb-serial", "2341:", "1a86:"))]
            port = (cands or [p.device for p in ports] or [None])[0]
        if not port:
            sys.exit("[link] no Arduino found. Plug it in, close the Arduino Serial Monitor, or pass --port COM5")
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.resp: queue.Queue[str] = queue.Queue()
        self.frames: queue.Queue[tuple[int, float, bytes]] = queue.Queue()
        self.local: queue.Queue[str] = queue.Queue()      # this Uno's own sensor readings
        self.node_id: str | None = None                   # from the READY line (node.ino)
        self.asked_sensors = 0.0
        self.tx_lock = threading.Lock()
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        time.sleep(2.2)                       # the Uno resets when the port opens
        if not self.ready.wait(4):
            self.ping()

    def _read(self) -> None:
        buf = b""
        while True:
            try:
                buf += self.ser.read(512)
            except Exception as e:  # unplugged
                log("modem", f"serial error: {e}")
                time.sleep(1)
                continue
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                if line.startswith("R"):
                    try:
                        rssi, snr, hx = line[1:].split(",", 2)
                        self.frames.put((int(rssi), float(snr), bytes.fromhex(hx)))
                    except ValueError:
                        log("modem", f"garbled line: {line[:60]}")
                elif line.startswith("L") and "|" in line:
                    self.local.put(line[1:])
                elif line.startswith("#"):
                    info = line[1:].strip()
                    # Sensor inventory and event cues are for the bench, not the demo screen.
                    if info.startswith("READ"):
                        pass    # the named copy of an L line, for Serial Monitor only
                    elif info.startswith("SENSORS"):
                        if time.monotonic() - self.asked_sensors < 5:
                            log("node", info)
                    elif not info.startswith("EVENT"):
                        log("node", info)
                elif line.startswith("READY"):
                    parts = line.split()
                    if "node" in parts and parts.index("node") + 1 < len(parts):
                        self.node_id = parts[parts.index("node") + 1]
                    self.ready.set()
                    log("modem", f"{self.port}: {line}")
                elif line.startswith("!radio"):
                    log("modem", line)
                else:
                    self.resp.put(line)

    def _cmd(self, cmd: str, expect: str, timeout: float = 2.0) -> str:
        while not self.resp.empty():
            self.resp.get_nowait()
        self.ser.write((cmd + "\n").encode())
        try:
            r = self.resp.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no answer to {cmd[:1]}")
        if not r.startswith(expect):
            raise RuntimeError(f"modem said {r!r} to {cmd[:1]}")
        return r

    def ping(self) -> str:
        with self.tx_lock:
            r = self._cmd("P", "P")
            self.ready.set()
            return r

    def raw(self, cmd: str) -> None:
        """A one-line command with no reply to wait for (E…, I)."""
        with self.tx_lock:
            self.ser.write((cmd + "\n").encode())

    def send_frame(self, frame: bytes) -> None:
        with self.tx_lock:
            self._cmd("C", "+")
            for i in range(0, len(frame), 24):
                self._cmd("A" + frame[i:i + 24].hex(), "+")
            self._cmd("S", "D", timeout=6)


# --------------------------------------------------------------------- HTTP ---
def http(method: str, url: str, body: dict | None = None, headers: dict | None = None,
         timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "indradhanu-lora-link/1")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)}


def idx1(type_: str, body: dict, key: str = "", human: str = "") -> str:
    """Same format as backend/app/mesh/envelope.py and scripts/mesh_bridge.py."""
    clean = {k: (round(v, 5) if k in ("la", "lo") else v) for k, v in body.items() if v not in (None, "")}
    payload = json.dumps(clean, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(key.encode(), f"IDX1|{type_}|{payload}".encode(), hashlib.sha256).hexdigest()[:16] if key else "-"
    text = f"IDX1|{type_}|{payload}|{sig}"
    return f"{human.strip()} {text}" if human else text


# ------------------------------------------------------------ sensor readings ---
FIELDS = ["node", "seq", "up", "mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps", "vib_g",
          "mic", "piezo", "knocks", "tilt_sw", "pir", "sim"]
INTS = {"seq", "up", "mq2", "mq135", "mic", "piezo", "knocks", "tilt_sw", "pir"}
SIM_BITS = {1: "mq2", 2: "mq135", 4: "temp", 8: "imu", 0x10: "mic", 0x20: "piezo", 0x40: "tilt"}


def parse_reading(line: str) -> dict | None:
    """'RN01|12|60|180|...|sim' -> observation dict for POST /iot/observations."""
    parts = line.strip().split("|")
    if len(parts) < 5 or not parts[0]:
        return None
    obs: dict = {"raw": line.strip()[:200],
                 "received_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"}
    for i, name in enumerate(FIELDS):
        v = parts[i] if i < len(parts) else ""
        if v == "":
            obs[name] = None
        elif name == "node":
            obs[name] = v[:40]
        elif name == "sim":
            try:
                obs[name] = int(v, 16)
            except ValueError:
                obs[name] = None
        else:
            try:
                obs[name] = int(v) if name in INTS else float(v)
            except ValueError:
                obs[name] = None
    return obs


def sim_names(mask: int | None) -> str:
    if not mask:
        return "all real"
    names = [n for b, n in SIM_BITS.items() if mask & b]
    return "all mimicked" if len(names) == len(SIM_BITS) else "mimicked: " + ",".join(names)


# --------------------------------------------------------------------- link ---
class Link:
    def __init__(self, a: argparse.Namespace, modem):
        self.a, self.modem = a, modem
        self.seen = Seen()
        self.reasm = Reassembler()
        self.lora_q: queue.Queue[str] = queue.Queue()
        self.phone_q: queue.Queue[str] = queue.Queue()
        self.phone = f"http://{a.phone}:{a.phone_port}" if a.phone else None
        self.api = a.api.rstrip("/") + ("" if a.api.rstrip("/").endswith("/api/v1") else "/api/v1") if a.api else None
        self.gw = {"X-Mesh-Gateway-Key": a.key}
        self.stats = {"lora_in": 0, "lora_out": 0, "phone_in": 0, "phone_out": 0, "api_in": 0, "api_out": 0,
                      "readings_local": 0, "readings_remote": 0, "readings_posted": 0}
        self.locs: dict[str, tuple[float, float]] = {}
        for spec in a.loc or []:
            try:
                node, ll = spec.split("=")
                la, lo = (float(x) for x in ll.split(","))
                self.locs[node.strip()] = (la, lo)
            except ValueError:
                sys.exit(f"[link] bad --loc {spec!r}; use NODE=lat,lon e.g. RN01=18.5204,73.8567")
        self.readings: queue.Queue[dict] = queue.Queue()

    def relay_ok(self, text: str) -> bool:
        return self.a.all or "IDX1|" in text

    # ---- outputs
    def to_lora(self, text: str) -> None:
        self.lora_q.put(text)

    def to_phone(self, text: str) -> None:
        if self.phone:
            self.phone_q.put(text)

    def to_api(self, texts: list[str]) -> None:
        if not self.api:
            return
        texts = [t for t in texts if "IDX1|" in t]
        if not texts:
            return
        c, res = http("POST", f"{self.api}/mesh/inbound", {"gatewayId": self.a.name, "packets": texts}, self.gw)
        if c == 200:
            self.stats["api_out"] += len(texts)
            for t, r in zip(texts, res.get("results", [])):
                log("api", f"{r.get('outcome') or r.get('error') or r}: {t[-80:]}")
        else:
            log("api", f"refused ({c}): {str(res)[:160]}")

    # ---- workers
    def lora_tx(self) -> None:
        while True:
            text = self.lora_q.get()
            frames = fragments(text.encode("utf-8"))
            for rep in range(self.a.repeat):
                if rep:
                    time.sleep(random.uniform(1.2, 2.8))
                for f in frames:
                    try:
                        self.modem.send_frame(f)
                    except Exception as e:
                        log("lora", f"send failed: {e}")
                        time.sleep(1)
                    time.sleep(0.15)
            self.stats["lora_out"] += 1
            log("lora>", f"{len(frames)} frame(s): {text[:100]}")

    def lora_rx(self) -> None:
        while True:
            rssi, snr, frame = self.modem.frames.get()
            if frame[:1] == b"N":
                obs = parse_reading(frame[1:].decode("ascii", "replace"))
                if obs:
                    obs["rssi"], obs["snr"] = rssi, snr
                    self.stats["readings_remote"] += 1
                    log("<node", f"{obs['node']} #{obs.get('seq')} rssi {rssi} snr {snr}")
                    self.readings.put(obs)
                continue
            data = self.reasm.add(frame)
            if data is None:
                continue
            text = data.decode("utf-8", "replace")
            if not self.seen.first(text):
                continue
            self.stats["lora_in"] += 1
            log("<lora", f"rssi {rssi} snr {snr}: {text[:120]}")
            self.to_phone(text)
            self.to_api([text])

    def phone_tx(self) -> None:
        last = 0.0
        while True:
            text = self.phone_q.get()
            while True:
                wait = self.a.rate - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
                body = {"text": text}
                if self.a.channel:
                    body["channel"] = self.a.channel
                c, r = http("POST", f"{self.phone}/send/text", body)
                last = time.monotonic()
                if c == 200:
                    self.stats["phone_out"] += 1
                    log("phone>", text[:100])
                    break
                log("phone", f"send refused ({c}): {r.get('error') or r}; retrying")
                time.sleep(2)

    def phone_rx(self) -> None:
        since = 0
        c, st = http("GET", f"{self.phone}/status")
        log("phone", f"{self.phone}: {c} {str(st)[:120]}")
        if c == 200 and not st.get("api_enabled", True):
            log("phone", "turn on Settings → VLM API in bitchat")
        while True:
            c, box = http("GET", f"{self.phone}/inbox?since={since}")
            if c == 200:
                msgs = [m.get("text", "") for m in box.get("messages", [])]
                since = box.get("next", since)
                fresh = [t for t in msgs if t and self.relay_ok(t) and self.seen.first(t)]
                for t in fresh:
                    self.stats["phone_in"] += 1
                    log("<phone", t[:120])
                    self.to_lora(t)
                self.to_api(fresh)
            elif c == 0:
                log("phone", f"unreachable: {box.get('error')}")
                time.sleep(5)
            time.sleep(self.a.every)

    def api_rx(self) -> None:
        while True:
            c, out = http("GET", f"{self.api}/mesh/outbox?gateway_id={self.a.name}&limit=10", headers=self.gw)
            if c == 200:
                ids = []
                for m in out.get("messages", []):
                    text = m.get("text", "")
                    ids.append(m["id"])
                    if text and self.seen.first(text):
                        self.stats["api_in"] += 1
                        log("<api", f"{m.get('kind')}: {text[:100]}")
                        self.to_lora(text)
                        self.to_phone(text)
                if ids:
                    http("POST", f"{self.api}/mesh/outbox/ack", {"gatewayId": self.a.name, "ids": ids}, self.gw)
            elif c in (401, 403):
                log("api", f"key refused ({c}); outbox polling paused 60 s")
                time.sleep(60)
            time.sleep(self.a.every * 2)

    def local_readings(self) -> None:
        """This Uno's own readings (USB). It also broadcasts them over LoRa itself."""
        while True:
            obs = parse_reading(self.modem.local.get())
            if obs:
                self.stats["readings_local"] += 1
                if self.stats["readings_local"] % 6 == 1:
                    log("node", f"{obs['node']} #{obs.get('seq')} gas {obs.get('mq2')}/{obs.get('mq135')} "
                                f"temp {obs.get('temp_c')}")
                self.readings.put(obs)

    def post_readings(self) -> None:
        """Readings from both nodes -> POST /iot/observations (only with --api)."""
        while True:
            batch = [self.readings.get()]
            time.sleep(0.5)
            while not self.readings.empty() and len(batch) < 50:
                batch.append(self.readings.get_nowait())
            if not self.api:
                continue
            for o in batch:
                if o.get("node") in self.locs:
                    o["lat"], o["lon"] = self.locs[o["node"]]
            c, res = http("POST", f"{self.api}/iot/observations",
                          {"gatewayId": self.a.name, "observations": batch}, self.gw)
            if c == 200:
                self.stats["readings_posted"] += len(batch)
                for sc in res.get("scored", [])[-2:]:
                    if sc.get("escalated"):
                        log("api", f"{sc['node']} ESCALATED {sc['escalated']}")
            else:
                log("api", f"readings refused ({c}): {str(res)[:160]}")
                time.sleep(3)

    def api_cmds(self) -> None:
        """Event cues from the command centre: run them on this node or send them over LoRa."""
        while True:
            c, out = http("GET", f"{self.api}/iot/commands?gateway_id={self.a.name}", headers=self.gw)
            if c == 200:
                for cmd in out.get("commands", []):
                    node, ev = str(cmd.get("node", ""))[:8], str(cmd.get("event", ""))[:1]
                    if not node or ev not in "fct0":
                        continue
                    if node == getattr(self.modem, "node_id", None):
                        self.modem.raw("E" + ev)
                    else:
                        for _ in range(2):
                            try:
                                self.modem.send_frame(("E" + node + ev).encode())
                            except Exception as e:  # noqa: BLE001
                                log("lora", f"cue failed: {e}")
                            time.sleep(0.4)
            elif c == 404:
                time.sleep(60)  # API without the endpoint yet
            time.sleep(2)

    def keyboard(self) -> None:
        print("Type a message + Enter to send it over LoRa. 'sos <text>' = located report. "
              "'event fire|collapse|trapped|stop [node]' = sensor event. 'sensors', 'stats'.")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line == "stats":
                print(self.stats)
                continue
            if line == "sensors":
                self.modem.asked_sensors = time.monotonic()
                self.modem.raw("I")
                continue
            if line.lower().startswith("event"):
                words = line.split()[1:]
                arg = (words[0] if words else "").lower()
                target = words[1].upper() if len(words) > 1 else None
                code = {"fire": "f", "smoke": "f", "collapse": "c", "trapped": "t", "stop": "0", "": "0"}.get(arg)
                if not code:
                    print("event fire|collapse|trapped|stop [RN01]   (60 s; name a node to cue it over LoRa)")
                    continue
                if target and target != getattr(self.modem, "node_id", None):
                    self.modem.send_frame(("E" + target + code).encode())
                else:
                    self.modem.raw("E" + code)
                continue
            if line.lower().startswith("sos ") or line.lower().startswith("report "):
                note = line.split(" ", 1)[1]
                if self.a.lat is None or self.a.lon is None:
                    print("start with --lat and --lon to send located reports")
                    continue
                text = idx1("R", {"id": f"lora-{uuid.uuid4().hex[:10]}", "n": self.a.name, "la": self.a.lat,
                                  "lo": self.a.lon, "x": note[:200], "t": int(time.time())},
                            self.a.hmac, human=f"[SOS] {note[:80]}")
            else:
                text = line
            self.seen.first(text)
            self.to_lora(text)
            self.to_phone(text)
            self.to_api([text])

    def run(self) -> None:
        jobs = [self.lora_tx, self.lora_rx, self.post_readings]
        if hasattr(self.modem, "local"):
            jobs.append(self.local_readings)
        if self.phone:
            jobs += [self.phone_tx, self.phone_rx]
        if self.api:
            jobs += [self.api_rx, self.api_cmds]
        for j in jobs:
            threading.Thread(target=j, daemon=True).start()
        log("link", f"{self.a.name}: LoRa on {getattr(self.modem, 'port', '?')}"
                    f"{' · phone ' + self.phone if self.phone else ''}{' · API ' + self.api if self.api else ''}")
        if sys.stdin and sys.stdin.isatty() or self.a.keyboard:
            threading.Thread(target=self.keyboard, daemon=True).start()
        try:
            while True:
                time.sleep(60)
                log("stats", str(self.stats))
        except KeyboardInterrupt:
            print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="COM5 / /dev/ttyUSB0 (auto-detected if omitted)")
    ap.add_argument("--name", default=f"lora-{uuid.getnode() % 10000}", help="this end's name (shows on the console)")
    ap.add_argument("--phone", default=os.getenv("BITCHAT_IP"), help="bitchat phone IP on this Wi-Fi")
    ap.add_argument("--phone-port", type=int, default=8765)
    ap.add_argument("--channel", default=None, help="bitchat channel to post into (default: the phone's)")
    ap.add_argument("--api", default=os.getenv("INDRADHANU_API"), help="e.g. https://disruptionops.onrender.com")
    ap.add_argument("--key", default=os.getenv("MESH_GATEWAY_KEY", ""), help="the API's MESH_GATEWAY_KEY")
    ap.add_argument("--hmac", default=os.getenv("MESH_HMAC_KEY", ""), help="MESH_HMAC_KEY, to sign 'sos' reports typed here")
    ap.add_argument("--lat", type=float, default=None, help="where this end is, for 'sos' reports")
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--loc", action="append", default=[], metavar="NODE=lat,lon",
                    help="where a sensor node is (repeat per node), e.g. RN01=18.5204,73.8567")
    ap.add_argument("--all", action="store_true", help="relay every mesh message, not only IDX1 packets")
    ap.add_argument("--repeat", type=int, default=2, help="send each message this many times over LoRa")
    ap.add_argument("--every", type=float, default=3.0, help="seconds between phone polls")
    ap.add_argument("--rate", type=float, default=5.5, help="seconds between sends to the phone (bitchat limit)")
    ap.add_argument("--keyboard", action="store_true", help="read messages from stdin even when not a terminal")
    a = ap.parse_args()
    if a.api and not a.key:
        ap.error("--api needs --key (MESH_GATEWAY_KEY)")
    Link(a, Modem(a.port)).run()


if __name__ == "__main__":
    main()
