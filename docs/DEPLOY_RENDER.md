# Deploying the API to Render

Backend only. The frontend stays on Vercel.

## Once

1. Commit `render.yaml` at the repository root and the updated `backend/Dockerfile`.
2. Render → **New** → **Blueprint** → pick this repo. It reads `render.yaml`
   and prompts for every value marked `sync: false`.
3. Paste the secrets from the table below.
4. First deploy takes ~4 minutes, most of it installing `ortools`.
5. Set `CORS_ORIGINS` to your Vercel URL, and point the frontend's
   `VITE_API_URL` at `https://disruptionops.onrender.com/api/v1`.

Migrations do **not** run on deploy, deliberately: a container that migrates on
boot will run `009` five times if Render restarts it five times. Run them from
your machine against `SUPABASE_DIRECT_CONNECTION_STRING`.

## Environment variables

Values marked **secret** go in Render's dashboard and never in the repo.
Everything under "has a default" can be skipped — the listed default applies.

### Required — the service will not work without these

| Variable | Secret | Notes |
|---|---|---|
| `INDRADHANU_ENV` | no | `production`. Anything else leaves docs and OpenAPI public. |
| `CORS_ORIGINS` | no | Comma-separated origins, each with scheme, no trailing slash. `https://indradhanu.vercel.app` |
| `SUPABASE_TRANSACTION_POOLER` | **yes** | The API DSN. Must be the pooler, not the direct string — see below. |
| `SUPABASE_URL` | no | `https://wfkzdevcfancytlvezxx.supabase.co` |
| `SUPABASE_ANON_PUBLIC_KEY` | no | Public by design; RLS is what protects the data. |
| `SUPABASE_SERVICE_ROLE_KEY` | **yes** | Bypasses RLS entirely. Never goes near the frontend. |
| `SUPABASE_JWT_KEY` | **yes** | Verifies the tokens the frontend sends. |

### Strongly recommended

| Variable | Secret | Without it |
|---|---|---|
| `SUPABASE_DIRECT_CONNECTION_STRING` | **yes** | Migrations and seeding cannot run from the service. The API is unaffected. |
| `MAPBOX_TOKEN` | **yes** | Routing falls back to public OSRM, then to straight lines. You lose street names in turn instructions — "left onto Karve Road" becomes "go 2.1 km north". |
| `GEMINI_API_KEY` | **yes** | Every model call falls back to deterministic rules. Prose gets plainer; **no number changes**. The service still starts. |
| `SARVAM_AI_API_KEY` | **yes** | The voice report button fails. Typed reports still work. |

### Optional

| Variable | Secret | Without it |
|---|---|---|
| `HF_API_TOKEN` | **yes** | The report classifier's keyword pass decides alone, with no model second opinion. |
| `VLM_URL`, `VLM_API_KEY`, `VLM_MODEL` | **yes** | Photos are accepted and not looked at. See `docs/VLM_CONTRACT.md`. |
| `NASA_FIRMS_KEY` | **yes** | Fire detections unavailable; other hazards unaffected. |
| `DATA_GOV_IN_KEY` | **yes** | That feed reports `down` in the status strip. |
| `GOOGLE_FLOOD_HUB_KEY` | **yes** | Flood forecast comes from GloFAS via Open-Meteo, which needs no key. |
| `AWS_REGION`, `AWS_API_KEY_BEDROCK_FOR_XAI`, `BEDROCK_MODEL_ID` | **yes** | Only read when `LLM_PROVIDER=bedrock`. |

### Has a default — set only to override

`LOG_LEVEL` (INFO) · `DB_POOL_MIN` (2) · `DB_POOL_MAX` (10) ·
`LLM_PROVIDER` (gemini) · `GEMINI_MODEL` (gemini-2.0-flash) ·
`OSRM_URL` · `OPEN_METEO_FORECAST_URL` · `OPEN_METEO_FLOOD_URL` ·
`OPEN_METEO_AIR_URL` · `FEED_TIMEOUT_SECONDS` (12) ·
`FEED_CACHE_TTL_SECONDS` (900)

### Never set on Render

**`DEV_AUTH_ROLE`.** It treats every unauthenticated request as a named role.
The settings validator refuses to start when it is set and `INDRADHANU_ENV` is
not `development`, so a deploy carrying it crash-loops with a clear message —
which is the intended behaviour, not a bug to work around. Your local `.env`
has `DEV_AUTH_ROLE=ward_officer`; do not copy the file wholesale into Render.

## Three things that will bite

**The pooler, not the direct connection.** Supabase's direct host resolves to
IPv6 only and Render's outbound network is IPv4. The direct string fails at
connect with `Network is unreachable`, which reads exactly like the database
being down. `settings.dsn` already prefers the pooler; just make sure the
pooler variable is the one you filled in. `session.py` already sets
`statement_cache_size=0`, which is what makes pgbouncer's transaction mode
usable at all.

**Cold starts on the free and starter plans.** Render spins the instance down
after inactivity, and the first request afterwards waits ~50s while the
container boots and opens its pool. Before a judged demo, hit `/health/live`
a minute beforehand. If the demo cannot tolerate it, that is the reason to pay
for the plan above.

**CORS failures are silent server-side.** A wrong `CORS_ORIGINS` produces
normal 200s in Render's logs while the browser discards every response. The
symptom is "the deployed site shows nothing" with a clean server log. Check the
browser console first, not the logs.

## Verifying

```bash
curl https://disruptionops.onrender.com/health/live     # {"status":"ok"} — process is up
curl https://disruptionops.onrender.com/health          # includes the database
curl "https://disruptionops.onrender.com/api/v1/status" # per-feed state, and which LLM engine answered
```

`/docs` is disabled when `INDRADHANU_ENV=production`. That is deliberate; flip
the variable to `staging` if you want the schema browsable on a deployed box.
