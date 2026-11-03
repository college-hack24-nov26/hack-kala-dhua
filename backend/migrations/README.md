# Migrations

Applied to the Supabase project `Indradhanu` (`wfkzdevcfancytlvezxx`) in this
order. They are recorded here so the schema is reproducible from the repository
rather than only existing in one hosted database.

| File | What it does |
|---|---|
| `001_portability_taxonomy_as_data.sql` | cities, hazard types, capabilities, resource kinds, lifeline kinds, incident categories, and the capability mappings |
| `002_convert_taxonomy_enums_to_fks.sql` | converts the taxonomy enum columns to text foreign keys and drops the old enums |
| `003_cities_agencies_events_simruns.sql` | city scoping, agencies, simulation runs, the append-only event log, RLS |

Later migrations, all applied to the same project:

| File | What it does |
|---|---|
| `004`–`019` | indexes, streets and routes, relief logistics, copilot params and delegations, reporter outcomes, tenancy, shelters, removal of the unused mesh column, RLS hardening, reporter key, photo evidence, return to service, fire category, keywords as data |
| `020_agent_memory.sql` | `copilot_turns`, `agent_memory`, `recall_memory()`; optional `embedding vector(768)` (applied 26 Sep 2026) |
| `021_operations_and_overrides.sql` | `cancelled` added to `task_status` **and** `assignment_status` (the latter was already being written by the executor), `operator_overrides`, cancel/hold/pin under clause pol-2 (applied 26 Sep 2026) |
| `022_mesh_transport.sql` | `mesh_messages`, `mesh_outbox`, `mesh_nodes`, `mesh_state` (applied 26 Sep 2026) |
| `023_help_requests_get_a_responder.sql` | `field_assessment` capability; unknown reports get one responder; plain calls for help classify as person_stranded (applied 27 Sep 2026) |
| `024_severity_reading_and_agent_memory_scopes.sql` | report `assessed_severity`; `agent_memory.namespace`; `agent_memory_ledger` (applied 27 Sep 2026) |
| `025_control_state.sql` | `control_state` (emergency stop flag) (applied 27 Sep 2026) |
| `026_ghaziabad_region.sql` | Ghaziabad (IPEC, Sahibabad) region: 14 wards, 45 units, 22 lifelines, 3 agencies; re-homes reports filed there (applied 27 Sep 2026) |

Run 001 to 003 in order against a fresh database. They are written to be safe to
re-run: every create is `if not exists` and every seed is `on conflict do nothing`.
002 is the exception, since dropping a type is not idempotent; it is written to
run once against the schema 001 leaves behind.

## The one thing to understand before editing these

Taxonomy is data, lifecycle is type. Which hazards, incident categories,
resource kinds and agencies exist lives in tables, because a new city or a new
disaster must not require a migration and a redeploy. Status columns
(`incident_status`, `assignment_status`, `task_status`, `decision_status`,
`resource_status`, `app_role`) stay as Postgres enums, because application code
branches on them and a value outside the set really would be a bug.
