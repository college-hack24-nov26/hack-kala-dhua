# Production hardening: guardrails, token cost, 20k req/s

Last updated 2026-10-10. Everything here is in the code and covered by tests
(`backend/tests/test_guardrails_and_cost.py`, `backend/tests/test_ingest_scale.py`;
144 tests pass). Numbers are measured unless marked *estimate*.

---

## 1. Guardrails, enforced in the agent graph

Before: the graph had state contracts and one plan check; the Copilot question,
resident `/ask`, report parser and duplicate adjudicator sent public text to the
model raw; the Incident Commander's prompt *asked* it to simulate before moving
units and to use only real ids, but nothing checked; and an officer could
approve plan A while `dispatch` re-solved and wrote plan B.

Now there are six layers, and no call path can skip one.

| Layer | Where | Rule ids |
|---|---|---|
| **Graph entry** | new first node `guard_in` | `graph.kill_switch`, `graph.run_storm` (>20 cycles/min/city coalesced), `graph.trigger_injection` |
| **Plan invariants** | `validate` | duplicate unit, bad coverage (refuse); mass re-task, coverage drop, ETA > 90 min (officer) |
| **Policy gate** | `policy_gate` | `plan.strands_incident`: pulling the last unit off an S3+ incident needs an officer |
| **Dispatch** | `dispatch` | `plan.dispatch_drift`: re-preview after approval; if the solve now moves a unit the officer did not approve, nothing is written. `plan.post_commit_audit` flags auto-written plans that should have needed an officer |
| **Agent actions** | Commander loop | allow-listed action keys; every id must have come back from a tool; a move/cancel must be simulated first; tool results scrubbed of instruction-like text (indirect injection from citizen reports) |
| **Model gateway** | `llm.complete` (every call) | PII redacted before text leaves for a third-party model; prompt size cap; output PII redaction; answers that echo injected instructions discarded; narration/resident answers whose numbers are not in their data discarded (`output.ungrounded_number`) |

Every firing is a `Trip`: counted per rule, kept for the console
(`GET /api/v1/status/guardrails`), stored on the run (`run.guardrails`), and
written to the event log as `guardrail.tripped`.

```
START → guard_in ─(paused / storm)→ held → END
          └→ triage → … → optimise → validate → policy_gate → (officer) → dispatch[drift check, audit] → observe
```

## 2. Token cost at scale

`app/agents/llm_cost.py`, applied inside the gateway so callers only name a `task`.

| Technique | What it does here |
|---|---|
| Deterministic first | keyword rules → zero-shot classifier → model only in the ambiguous band (already the design; now measured per task as `avoidedRate`) |
| Response cache | per task TTL; key = task + model tier + guarded prompt, or a semantic key (resident Q&A: ward facts + normalised question). Fallback answers are never cached |
| Single-flight | identical in-flight calls wait for one answer (50 identical reports → 1 call, tested) |
| Model tiering | `route`, `classify`, `severity`, `adjudicate`, `citizen_ask` → small model (`GEMINI_MODEL_SMALL`); `narrate`, `commander` → main model |
| Output caps | per task: a category is 16 tokens, severity 160, Commander 400 (was uncapped on Gemini) |
| Prefix caching | system prompt sent as `system_instruction` (Gemini) / with a `cachePoint` (Bedrock, opt-in) so the provider reuses the static prefix |
| No thinking on small tasks | `thinking_budget=0` for 2.5-Flash small-tier calls |
| Token budget | per minute and per day; low priority sheds at 60 %, normal 85 %, life-safety keeps 100 % |
| Bulkhead | ≤ 32 model calls in flight; low priority degrades at 75 % full instead of queueing in front of life-safety work |
| Async client | Gemini via `client.aio` (no thread-pool ceiling) |
| Accounting | tokens in/out/provider-cached, latency, cost (only for models with a configured price) at `GET /api/v1/status/llm/usage` |

Measured (`backend/scripts/bench_llm_cache.py`, 400 ms fake model): **10,000 resident
questions over 5 s across 30 wards** → 150 model calls (1.5 %), 7,821 served
from cache, 2,029 answered deterministically while the bulkhead was full;
63k tokens vs 4.2M naive (≈ $0.008 vs $0.54 at gemini-2.5-flash-lite list
prices — Google's pricing page, 2026-10). With `REDIS_URL` the cache and budget
are shared across replicas.

## 3. Throughput: 20k req/s

### What changed

| Change | Why |
|---|---|
| Pure-ASGI request-context and rate-limit middleware | `BaseHTTPMiddleware` added a task and two streams per request |
| Sampled access logs (`ACCESS_LOG_SAMPLE`) | errors, 4xx/5xx and slow requests always logged |
| Hot routes registered first | Starlette scans ~250 routes linearly; route matching was 75 % of CPU on the ingest door |
| **Ingest bus** (`app/core/ingest.py`) | accept → 202 + ticket; workers drain batches. Lanes: critical (reports; never shed, written through when full), standard, bulk (telemetry; 503 + Retry-After when full). Idempotency keys. Memory or Redis Streams with consumer groups and re-claim |
| Batched IoT path (`ingest_bulk`) | 5 fixed round trips per batch instead of ~5 per reading; SQL validated with `EXPLAIN` on the live schema, dup lookup uses `sensor_readings_node_time_idx` |
| Leader lease for the event router (migration 028, applied) | N replicas, one re-planner; takeover resumes from the stored cursor |
| Global rate limits / LLM cache / budget via Redis | optional; falls back to per-replica if Redis is absent or down |
| Separate worker process (`python -m app.worker`) | scale draining on queue depth, independently of the API |
| uvloop + httptools + backlog 4096, `WEB_CONCURRENCY` | explicit in the Dockerfile |

New endpoints: `POST /api/v1/ingest/telemetry`, `POST /api/v1/ingest/reports`,
`GET /api/v1/ingest/tickets/{id}`, `GET /api/v1/status/ingest`,
`GET /api/v1/status/llm/usage`, `GET /api/v1/status/guardrails`.
The existing synchronous doors are unchanged.

### Measured (one uvicorn worker, 2-vCPU container shared with the load generator `wrk`)

| Path | Before | After |
|---|---|---|
| `GET /health/live` (full middleware stack) | 942 req/s | **6,419 req/s** (p50 ≈ 20 ms at 128 connections) |
| `POST /ingest/telemetry` (validate + auth + enqueue + batch drain) | — (sync path did ~5 DB round trips per reading) | **2,683 req/s**, drain kept up (avg batch 126) |

### Capacity math for 20,000 req/s (*estimate from the above*)

* Telemetry door at ~2.7k req/s per vCPU → **8 API vCPUs** for 20k single-reading
  requests; gateways that batch 20 readings per request need **1**.
  The k8s manifest runs 4–40 one-vCPU pods with HPA at 60 % CPU (headroom 2×).
* Database: 20k readings/s arrive as ~40 batches/s of 500 → ~200 statements/s.
  That is within a mid-size Postgres; it is **not** within a Supabase
  micro/free instance. Before a real 20k test: a larger compute add-on (or a
  dedicated telemetry store), time-partitioned `sensor_readings`, and a read
  replica for the analytics screens.
* LLM: request volume does not translate into model volume (§2); the budget
  caps spend regardless of traffic.

To run the real test: `deploy/k8s/indradhanu.yaml` + `k6 run deploy/loadtest/k6-ingest.js -e RATE=20000`.
