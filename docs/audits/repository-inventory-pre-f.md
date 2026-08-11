# Repository inventory before Phase F

Snapshot: local baseline commit `b0fb820` / tag
`local/phase-a-e-accepted-20260811`. The focused Phase A–E suite passed 243
tests and the full baseline passed 1,094 tests with 5 skips.

This document records the pre-cleanup structure. The decision labels describe
the Phase F disposition, not whether the content was useful when it was
created.

## Root inventory

| Path | Actual responsibility | Decision |
| --- | --- | --- |
| `.agents/` | Local repository-curation skill metadata | KEEP (hidden development infrastructure) |
| `HealthClaw_paper/` | MinerU extraction, source PDF, duplicated Markdown/JSON/images | MERGE a concise citation/lessons note into `docs/references/`; DELETE extraction artifacts |
| `docs/audits/agent-architecture/` | Frozen Agent plan, runtime cleanup plan, review/build log, acceptance manifest | MOVE to `docs/audits/agent-architecture/` |
| `backend/` | Deployable FastAPI composition entry point; also contains the obsolete `legacy_main.py` monolith | KEEP `main.py`; DELETE the legacy executable |
| `backend_engineering/` | Historical backend plan and review log | MOVE to `docs/audits/backend-engineering/` |
| `benchmarks/` | Offline longitudinal-memory evaluation harness, excluded from the wheel | MOVE to `tests/benchmarks/` |
| `cold_start/` | Historical frozen design and review log | MOVE to `docs/audits/cold-start/` |
| `docker/` | API and worker container definitions | KEEP |
| `docs/` | Two current documents without an index or coherent hierarchy | MERGE into the new docs hierarchy |
| `frontend/` | Next.js product UI and BFF | KEEP |
| `healthclaw_memory_governance/` | Historical design/review plus current implementation note | MOVE plan/log to audits; MOVE implementation note to architecture |
| `plug_and_play/` | Perceptor/device design, API contract and acceptance notes | MOVE current integration docs to architecture/development and historical plan/log to audits |
| `docs/audits/product-information-architecture/` | Historical product IA plan/review | MOVE to audits; summarize current IA under `docs/product/` |
| `docs/audits/product-positioning/` | Historical product-positioning plan/review | MOVE to audits; summarize current positioning under `docs/product/` |
| `reference_client/` | Shipped external Sleep API v1 example client | KEEP |
| `requirements/` | Hash-locked runtime and development dependencies | KEEP |
| `simulated_backend_closure/` | Superseded backend plan and review history | MOVE to `docs/audits/simulated-backend-closure/` |
| `simulation_verifier/` | Test-only oracle loader/contracts/fixtures | MERGE into `tests/support/simulation_verifier/` and `tests/fixtures/` |
| `docs/audits/skills-design/` | Historical Skill plan/review | MOVE to `docs/audits/skills-design/` |
| `docs/product/habit-profile/` | Mixed historical plan/log, current product docs, evidence templates and simulation fixtures | SPLIT current docs to `docs/product/habit-profile/`, history to audits, fixtures to `tests/fixtures/` |
| `sleepagent/` | Production Python package | KEEP, but reorganize namespaces |
| `sleepagent_simulated_acceptance_materials/` | Test-only synthetic acceptance bundle | MOVE to `tests/fixtures/acceptance-materials/` |
| `tests/` | 107 flat test modules, support code and one migration-named fixture | REORGANIZE into `unit/`, `integration/`, `architecture/`, `e2e/`, `fixtures/`, `support/`, and `benchmarks/` |
| root ZIP files | Generated historical/simulated evidence archives | DELETE; canonical source fixtures remain reproducible |

## Production Python namespaces

| Namespace | Responsibility | Decision |
| --- | --- | --- |
| `sleepagent.backend_*` | Backend settings, composition, persistence and runtime services | KEEP |
| `sleepagent.integrations.perceptor` | Vendor-specific signed push/pull integration and acceptance | KEEP |
| `sleepagent.product_api` | Product-facing API contracts/router/service | KEEP; MERGE the diagnostic router beneath it |
| `sleepagent.product_device` | Device-facing product schemas, projections and data quality | KEEP; MERGE Radar provider/quality code here where device-specific |
| `sleepagent.radar_agent` | Product Runtime plus persistence, API, model gateway, questionnaire, knowledge, reports, replay and device code | SPLIT by real ownership; DELETE the historical namespace |
| `sleepagent.simulation` | Runtime-safe synthetic scenario contracts/generator | KEEP; MERGE replay catalog here |
| `sleepagent.sleep_api` | Independent public Sleep API v1 application | KEEP |
| `sleepagent.sleep_domain` | Canonical sleep observations, episodes, lifecycle, authority and adapters | KEEP; REVIEW development-era compatibility components |

## `radar_agent` responsibility map

Pre-cleanup the namespace contains 86 Python modules and 44,000+ lines.

| Subpackage | Files | Real responsibility | Decision |
| --- | ---: | --- | --- |
| `product_agent/` | 54 | The sole four-Agent Product Runtime, its Episode lifecycle, tools, services, policies, contracts and registry | RENAME/MOVE to `sleepagent.product_runtime` |
| `api/` | 3 | Development diagnostic HTTP adapter for Product Episode tasks | MOVE to `sleepagent.product_api.diagnostics` |
| `runtime/` | 4 | Diagnostic task envelope/service/trace around the Product Runtime | MOVE to `sleepagent.product_runtime.task_runtime`; remove historical branches |
| `persistence/` | 8 + SQL | Shared Product, sleep-domain, API and backend persistence | MOVE to `sleepagent.persistence` |
| `provider/` | 2 | Radar provider/replay contracts | MOVE to `sleepagent.product_device.provider` |
| `quality.py` | 1 | Legacy-schema Radar night-quality aggregation used by the Product tool | MOVE to `sleepagent.product_device.night_quality` |
| `llm/` | 3 | External model routing/fault adapter | MOVE to `sleepagent.integrations.llm` |
| `questionnaire/` | 4 | Habit-profile questionnaire contracts and selection service | MOVE to `sleepagent.product_runtime.questionnaire` |
| `rag/` | 3 | Reviewed knowledge seed and citation grounding | RENAME/MOVE to `sleepagent.product_runtime.knowledge` |
| `reports/` | 3 | Role projection/report rendering | MOVE to `sleepagent.product_runtime.reports` |
| `confirmation/` | 2 | Deterministic HITL confirmation matrix | MOVE to `sleepagent.product_runtime.confirmation` |
| `schemas/` | 2 | Diagnostic task/evidence/report schemas and device conversion functions | MOVE to `sleepagent.product_runtime.schemas` |
| `replay/` | 3 + JSON | Deterministic simulation scenarios and goldens | MOVE to `sleepagent.simulation.replay` |
| `cli/` | 2 | Product diagnostic/demo CLI | MOVE to `sleepagent.product_runtime.cli` and keep the console command contract |
| `boundary.py`, root `__init__.py` | 2 | Assertion that the historical namespace itself is canonical | DELETE; replace with current architecture invariants |

The frozen 25-symbol public surface is
`sleepagent.radar_agent.product_agent.__all__`; it moves unchanged to
`sleepagent.product_runtime.__all__`. `ProductEpisodeRunner` and
`ProductEpisodeRuntime` remain the only facade and lifecycle owner.

## Tests

The pre-cleanup suite has 107 test files, 962 explicitly declared test
functions (1,094 collected cases), and 5 expected skips. Only 14 files carry a
`unit` marker, three carry a `postgres` marker, and one carries an
`asgi_lifespan` marker, so the flat layout—not the markers—is the main source
of ambiguity.

Migration/historical debt:

- 12 `test_phase3a_*`, `test_phase3b_*`, and `test_phase3c_*` files;
- `test_historical_runtime_api.py` and
  `test_historical_runtime_compatibility.py`, which exist only for retired
  `legacy_fixed`/`dynamic_goal` database records;
- `test_legacy_authority_migration.py`, which tests a one-time development
  database importer/cutover;
- `fixtures/phase3a_capability_goldens.json`.

Decision: preserve roster, retired-runtime absence, HDS authority, composition
root, Tool ownership, dependency direction, safety, permission, HITL,
persistence, device, API and frontend invariants under current behavior names.
Delete only tests whose sole subject is unsupported historical execution/data
compatibility.

## Plans, reviews, acceptance and references

The root contains ten plan families: Agent architecture, backend engineering,
cold start, plug-and-play, product information architecture, product
positioning, Skill design, habit profile, memory governance, and the
superseded simulated-backend closure. Each has a `PLAN.md` and almost all have
a `PLAN-REVIEW-LOG.md`; Agent architecture also holds the Phase A–E cleanup
plan, acceptance manifest and long build transcript. All are MOVE items under
`docs/audits/`, explicitly historical rather than newcomer entry points.

Current operational product/device notes are separated from those audit
records. The HealthClaw directory is REVIEW/DELETE except for a concise
reference explaining the citation, adopted memory ideas and rejected
assumptions. Its PDF, MinerU JSON/layout, duplicate Markdown and extracted
images have no runtime, test, legal-retention or package dependency.

## Legacy and compatibility code

| Item | Evidence | Decision |
| --- | --- | --- |
| `backend/legacy_main.py` | Second executable FastAPI composition used by tests/dev diagnostics; production uses `backend.main` | DELETE and construct diagnostic routers only in tests |
| `persistence/history.py` | SELECT-only decoder for retired fixed/dynamic task rows | DELETE; no deployed database exists |
| historical branches in task service/API/CLI/trace/store | Reachable only when a task has a non-`product_episode` runtime kind | DELETE |
| dynamic-runtime tables from migration 002 | Only historical reader/tests query most tables | DELETE from canonical baseline when unused by current persistence |
| sleep-domain legacy webhook importer and authority cutover controller | One-time migration of pre-production SQLite/provider state | REVIEW separately from Agent runtime; retain only if current adapters still require the conversion boundary |
| `radar_compat.py` | Current Perceptor/product-device conversion path still imports it | KEEP behavior, RENAME when its remaining inputs are current adapter contracts |

## Migration inventory and disposition

There are 25 SQL files (6,713 lines). The release runner explicitly labels
001–020 a legacy release and uses 021 to attest their checksums before applying
022–025. No production database upgrade contract exists, so the sequence is
development history and will be squashed into one current baseline. The
reasons each file existed are:

1. initial Radar subjects/tasks/events/evidence/reports/audit tables;
2. fixed/dynamic runtime metadata, invocation, plan, checkpoint and budget tables;
3. habit-profile state and commits;
4. persistent question suppression;
5. questionnaire episode/selection/cooldown state;
6. Product Agent state;
7. longitudinal-memory governance;
8. longitudinal authority compare-and-swap;
9. human-decision governance;
10. canonical sleep-domain foundation;
11. adapter registry/deployment control;
12. device-binding promotion/audit;
13. signed Perceptor push-ingestion state;
14. pull reconciliation and observation facts/conflicts;
15. NightEpisode lifecycle, membership and publication;
16. deterministic quality/risk/alert/follow-up fast path;
17. Product Agent bridge and role views;
18. public Sleep API v1 projections;
19. Sleep API event polling;
20. legacy-to-canonical authority cutover;
21. checksum migration ledger v2;
22. backend namespace, identity, grants and governance epochs;
23. durable backend command/outbox/work/delivery protocol;
24. UUIDv7 Episode contract and wake-date reconciliation;
25. encryption-key metadata, retention work and crypto-shred receipts.

The canonical baseline must preserve the final clean-database schema used by
current code, remove tables that only support retired runtimes, install one
ledger identity, and pass clean-create, idempotent upgrade, schema-equivalence
and full regression checks.

## Production imports of the historical namespace

There are 96 production Python files with 367 static references to
`sleepagent.radar_agent.*`. All 86 files inside the namespace refer to sibling
modules through that prefix. External dependants are exactly:

- `backend/legacy_main.py`;
- `sleepagent/backend_persistence.py`, `backend_runtime.py`, and
  `worker_runtime.py`;
- `sleepagent/integrations/perceptor/push_runtime.py`;
- `sleepagent/product_device/api.py` and `product_device/llm.py`;
- `sleepagent/sleep_api/persistence.py`, `postgres_runtime.py`, and
  `runtime.py`;
- `sleepagent/sleep_domain/agent_bridge.py`, `authority_migration.py`,
  `postgres_slice.py`, `product_data.py`, `repository.py`, and
  `worker_adapters.py`.

Every edge is a cutover item. No compatibility import alias will remain.
