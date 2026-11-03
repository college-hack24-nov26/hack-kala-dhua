"""The ingest bus (lanes, batching, idempotency, shedding, write-through) and
the batched IoT path, against fakes. No database, no Redis."""
from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager

sys.path.insert(0, "tests")
import _stubs  # noqa: E402

_stubs.install()


class Bus(unittest.TestCase):
    def setUp(self):
        from app.core import ingest
        from app.core.config import settings
        self.ingest, self.settings = ingest, settings
        ingest.reset_for_tests()
        self.saved = (dict(ingest._HANDLERS), settings.ingest_bulk_max, settings.ingest_critical_max,
                      settings.ingest_flush_ms)
        self.seen: list[list[dict]] = []

        async def batch(payloads):
            self.seen.append(payloads)
            return [{"ok": p.get("i")} for p in payloads]
        ingest._HANDLERS.clear()
        ingest._HANDLERS["telemetry"] = ("bulk", batch)
        ingest._HANDLERS["report"] = ("critical", batch)
        settings.ingest_flush_ms = 5

    def tearDown(self):
        h, b, c, f = self.saved
        self.ingest._HANDLERS.clear()
        self.ingest._HANDLERS.update(h)
        self.settings.ingest_bulk_max, self.settings.ingest_critical_max, self.settings.ingest_flush_ms = b, c, f
        self.ingest.reset_for_tests()

    def test_batches_and_tickets(self):
        ing = self.ingest

        async def flow():
            await ing.start(workers=1)
            got = [await ing.submit("telemetry", {"i": i}) for i in range(300)]
            for _ in range(100):
                if ing.stats.processed.get("bulk") == 300:
                    break
                await asyncio.sleep(0.02)
            t = await ing.ticket(got[7].ticket)
            await ing.stop()
            return got, t
        got, t = asyncio.run(flow())
        self.assertTrue(all(g.accepted for g in got))
        self.assertEqual(sum(len(b) for b in self.seen), 300)
        self.assertLess(len(self.seen), 300)                  # batched, not one call per item
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["result"], {"ok": 7})

    def test_idempotency_key_dedups_retries(self):
        ing = self.ingest

        async def flow():
            a = await ing.submit("report", {"i": 1}, idempotency_key="phone-1:abc")
            b = await ing.submit("report", {"i": 1}, idempotency_key="phone-1:abc")
            return a, b
        a, b = asyncio.run(flow())
        self.assertEqual(a.ticket, b.ticket)
        self.assertTrue(b.duplicate)

    def test_bulk_lane_sheds_when_full(self):
        ing = self.ingest
        self.settings.ingest_bulk_max = 5

        async def flow():
            return [await ing.submit("telemetry", {"i": i}) for i in range(8)]
        got = asyncio.run(flow())
        self.assertEqual(sum(g.accepted for g in got), 5)
        self.assertTrue(got[-1].shed)
        self.assertGreater(got[-1].retry_after, 0)

    def test_critical_lane_never_sheds_it_writes_through(self):
        ing = self.ingest
        self.settings.ingest_critical_max = 2

        async def flow():
            return [await ing.submit("report", {"i": i}) for i in range(4)]
        got = asyncio.run(flow())
        self.assertTrue(all(g.accepted for g in got))
        self.assertEqual(got[3].inline_result, {"ok": 3})
        self.assertEqual(ing.stats.written_through, 2)

    def test_critical_drains_before_bulk(self):
        ing = self.ingest

        async def flow():
            for i in range(5):
                await ing.submit("telemetry", {"i": f"t{i}"})
            await ing.submit("report", {"i": "sos"})
            return ing._drain_local(3)
        out = asyncio.run(flow())
        self.assertEqual(out[0]["payload"]["i"], "sos")


class FakeConn:
    def __init__(self, existing_dups=()):
        self.dups = set(existing_dups)
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetches = 0

    async def fetch(self, q, *a):
        self.fetches += 1
        if "insert into sensor_nodes" in q:
            ids = a[2]
            return [{"id": n, "lat": None, "lon": None, "ward_id": None, "last_seq": None,
                     "baseline": {}, "state": {}, "escalated": {}, "created": True} for n in ids]
        if "from sensor_readings" in q:
            return [{"node_id": n, "seq": s, "uptime_s": u}
                    for n, s, u in zip(*a) if (n, s, u) in self.dups]
        return []

    async def executemany(self, q, rows):
        self.executemany_calls.append((q, rows))


class BulkIoT(unittest.TestCase):
    def test_batch_is_constant_round_trips_and_in_order(self):
        from app.db import session
        from app.iot import service

        conn = FakeConn(existing_dups={("N1", 2, 20)})

        @asynccontextmanager
        async def tx():
            yield conn

        async def noop(*a, **k):
            return None
        saved = (session.transaction, service._touch_mesh_node, service._touch_gateway)
        session.transaction, service._touch_mesh_node, service._touch_gateway = tx, noop, noop
        try:
            obs = [{"node": "N1", "seq": s, "up": s * 10, "mq2": 100 + s, "tempC": 30} for s in (3, 1, 2)]
            obs += [{"node": "N2", "seq": 1, "up": 10, "mic": 40}, {"node": ""},
                    {"node": "N2", "seq": 1, "up": 10, "mic": 40}]           # in-batch duplicate
            res = asyncio.run(service.ingest_bulk(obs, gateway_id="gw", city_id="pune"))
        finally:
            session.transaction, service._touch_mesh_node, service._touch_gateway = saved
        self.assertEqual(res["accepted"], 3)                  # N1 s1, s3 and N2 once
        self.assertEqual(res["duplicates"], 2)                # one from the DB, one in the batch
        self.assertEqual(res["errors"], 1)
        self.assertEqual(len(res["results"]), 6)               # one per input, in input order
        self.assertTrue(res["results"][2]["duplicate"])
        self.assertEqual(conn.fetches, 2)                      # node upsert + dup lookup
        inserts = [rows for q, rows in conn.executemany_calls if "insert into sensor_readings" in q]
        self.assertEqual(len(inserts), 1)
        self.assertEqual([r[3] for r in inserts[0] if r[0] == "N1"], [1, 3])  # scored in seq order
        updates = [rows for q, rows in conn.executemany_calls if "update sensor_nodes" in q]
        self.assertEqual(sorted(u[0] for u in updates[0]), ["N1", "N2"])


class Lease(unittest.TestCase):
    def test_missing_table_means_sole_replica(self):
        from app.core import lease
        from app.db import session

        async def boom(*a, **k):
            raise RuntimeError('relation "service_leases" does not exist')
        saved = session.fetchrow
        session.fetchrow = boom
        try:
            ok, meta = asyncio.run(lease.hold("event_router", 15))
        finally:
            session.fetchrow = saved
        self.assertTrue(ok)
        self.assertEqual(meta, {})


if __name__ == "__main__":
    unittest.main()
