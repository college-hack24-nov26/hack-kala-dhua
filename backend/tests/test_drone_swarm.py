"""The drone swarm listener against a stand-in feed that follows the spec."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["DRONE_SWARM_ANCHOR"] = "18.5196,73.8567"

from app.drone import swarm as sw  # noqa: E402

SURVIVORS = [[-8.0, 6.0, 0.4], [10.0, -4.0, 6.5], [3.0, 9.0, 0.3]]


def drone_rows(t):
    return [[-13 + i + t, -11 + t, 10.0, 0, 0, 0.3826834, 0.9238795,
             "EXPLORE", 0.95, 1] for i in range(12)]


async def feed(ws, run):
    await ws.send(json.dumps({"type": "init", "scene": {
        "survivors": SURVIVORS, "placements": [], "launch_pad": [-13, -11], "manifest": {},
        "drones": 12, "ground": [16.25, 12.75], "mode": "AI (jev)"},
        "log": ["SENSOR: warm-up", "AI: D1 -> Jev: sector 3 p=0.51"]}))
    t = 0.2 if run else 30.0
    for k in range(30):
        t += 1 / 12
        found = [0] if k >= 5 else []
        found += [1] if k >= 10 else []
        delivered = [0] if k >= 20 else []
        if k == 5:
            await ws.send(json.dumps({"type": "log", "message": "SENSOR: D4 located site 1; broadcast to peer agents for auction"}))
        if k == 8:
            await ws.send(json.dumps({"type": "log", "message": "AI: D5 -> Jev: sector 20 p=0.67; bid 0 p=0.83"}))
        if k == 12:
            await ws.send(json.dumps({"type": "log", "message": 'AGENT: {"time":40.38,"event":"obstacle_detour","drone":2}'}))
        await ws.send(json.dumps({"type": "frame", "t": t, "phase": "AGENTIC SEARCH / RESCUE | AI ACTIVE",
                                  "found": found, "delivered": delivered, "drones": drone_rows(t),
                                  "payloads": {"0": [-8, 6, 0.2]} if delivered else {}}))
        await asyncio.sleep(0.01)
    await ws.send(json.dumps({"type": "log", "message": "MISSION COMPLETE: restarting scenario"}))


class FakeConn:
    async def fetchval(self, *a):
        return "block-1"

    async def execute(self, *a):
        return "OK"


class Tx:
    async def __aenter__(self):
        return FakeConn()

    async def __aexit__(self, *a):
        return False


class SwarmFeed(unittest.IsolatedAsyncioTestCase):
    async def test_runs_end_to_end(self):
        from websockets.asyncio.server import serve

        runs = {"n": 0}

        async def handler(ws):
            n = runs["n"]
            runs["n"] += 1
            await feed(ws, n)       # then the server drops the socket, as on restart

        rec = {"intake": [], "events": [], "replan": [], "mesh": [], "blocks": 0}

        async def receive(**kw):
            rec["intake"].append(kw)
            return SimpleNamespace(created_incident=len(rec["intake"]) <= 2, linked=len(rec["intake"]) > 2,
                                   incident_id=f"inc-{len(rec['intake'])}", report_id="r")

        async def append(**kw):
            rec["events"].append(kw["kind"])

        async def snap(lng, lat):
            return SimpleNamespace(lng=lng, lat=lat, street="Laxmi Road", moved_m=20, on_road=True)

        async def fetchval(*a):
            return None                     # no active block nearby

        async def executemany(q, rows):
            rec["mesh"].append(len(rows))

        sw.swarm.anchor = None
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            sw.swarm.url = f"ws://127.0.0.1:{port}"
            with mock.patch("app.incidents.intake.receive", receive), \
                 mock.patch("app.mesh.service.ward_at", mock.AsyncMock(return_value="w-12")), \
                 mock.patch("app.mesh.service._nudge_planner", lambda why: rec["replan"].append(why)), \
                 mock.patch("app.agents.commander.nudge", lambda *a, **k: True), \
                 mock.patch("app.world.events.append", append), \
                 mock.patch("app.solver.routing.snap_to_road", snap), \
                 mock.patch("app.db.session.fetchval", fetchval), \
                 mock.patch("app.db.session.fetch", mock.AsyncMock(return_value=[])), \
                 mock.patch("app.db.session.execute", mock.AsyncMock()), \
                 mock.patch("app.db.session.transaction", lambda: Tx()), \
                 mock.patch("app.db.session.pool", lambda: SimpleNamespace(executemany=executemany)), \
                 mock.patch.object(sw, "_load", mock.AsyncMock()):
                await sw.start()
                for _ in range(80):
                    await asyncio.sleep(0.1)
                    if runs["n"] >= 2 and sw.swarm.run >= 2 and len(sw.swarm.delivered) == 1:
                        break
                await asyncio.sleep(0.3)
                status = sw.status()
                await sw.stop()

        self.assertEqual(sw.swarm.anchor, (18.5196, 73.8567))           # from the env
        self.assertGreaterEqual(status["run"], 2)                        # restart detected
        self.assertEqual(len(status["drones"]), 12)
        # site 1 and 2 filed per run; second run links (dedup) rather than duplicates
        sites = sorted({(k["note"].split("site ")[1][0]) for k in rec["intake"]})
        self.assertEqual(sites, ["1", "2"])
        self.assertTrue(all(k["source"] == "sensor" and k["category"] == "person_stranded"
                            for k in rec["intake"]))
        self.assertIn("Drone D4", rec["intake"][0]["note"])               # credited from the log
        self.assertIn("drone.survivor_located", rec["events"])
        self.assertIn("drone.payload_delivered", rec["events"])
        self.assertIn("road.blocked", rec["events"])                      # detour over a street
        self.assertTrue(any("survivor" in r for r in rec["replan"]))
        self.assertTrue(any("obstruction" in r for r in rec["replan"]))
        self.assertTrue(rec["mesh"] and rec["mesh"][0] == 12)
        self.assertEqual(status["decisions"][0]["sector"], 20)
        self.assertEqual(status["decisions"][0]["bids"], [{"site": 1, "p": 0.83}])
        # survivor 1 sits ~64 m west, ~48 m north of the anchor at scale 8
        s1 = status["survivors"][0]
        self.assertAlmostEqual((s1["lat"] - 18.5196) * 111320, 48, delta=1)
        self.assertLess(s1["lng"], 73.8567)

    def test_heading_and_geo(self):
        sw.swarm.anchor, sw.swarm.scale = (18.5, 73.8), 8.0
        self.assertEqual(sw._heading_deg(0, 0, 0, 1), 90.0)      # facing +x = east
        lng, lat = sw.to_geo(0, 10)
        self.assertAlmostEqual((lat - 18.5) * 111320, 80, delta=0.5)
        self.assertAlmostEqual(lng, 73.8, places=6)


if __name__ == "__main__":
    unittest.main()
