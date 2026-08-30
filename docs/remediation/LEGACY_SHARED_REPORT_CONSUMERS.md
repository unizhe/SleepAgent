# Legacy/shared report consumer inventory

Authoritative as of G1 at HEAD
`51947e6da69d9d8e0e0eeb36669e227865fc26ff` plus the preserved dirty baseline.
This inventory records reads and identity dependencies; it does not authorize
consumer removal.

Classification vocabulary:

- `LEGACY_ONLY`: requires legacy per-role/result shape.
- `COMPAT_ONLY`: exists solely for the compatibility bridge.
- `DUAL_READ`: accepts or joins legacy/compatibility and shared artifacts.
- `SHARED_READY`: reads and validates shared analysis/projections directly.
- `RETIREMENT_UNKNOWN`: exact retirement dependency is not yet proved.

| Consumer / owner | Classification | Artifacts and IDs consumed | Exact evidence | Cutover / retirement condition |
|---|---|---|---|---|
| Product report API write route (`ProductApiService`) | `SHARED_READY` | creates `product.report.run.v1`; returns shared operation identity | `sleepagent/api/product.py:56,319`; `sleepagent/api/postgres.py:2199,2839` | Keep desired-analysis identity and strict shared read contract stable. |
| Product report API read model (`PostgresProductBackend`) | `DUAL_READ` | `shared_night_analysis.v1`, `role_projection.v1`, report/shared operation IDs, generic role-view rows | `sleepagent/api/postgres.py:772-1052,3948`; `sleepagent/api/product_contracts.py` | Require explicit shared authority for new rows and retain a labeled historical adapter. |
| Product `/today` backend/read model | `DUAL_READ` | `sleep_domain_analysis_role_views.public_today_json`, projection ID/hash; does not require shared schema | `sleepagent/api/postgres.py:549-642`; `sleepagent/api/product.py` today route | Add shared authority marker or explicit historical-read adapter before compatibility writes stop. |
| Public v1 role-view API | `DUAL_READ` | protocol-v2 generic role-view content and operation-linked projection | `sleepagent/api/public.py:84,266-288`; `sleepagent/api/postgres.py` role-view queries | Separate historical compatibility reads from authoritative shared reads. |
| `sleepagent-report` CLI | `SHARED_READY` | Product report endpoints, projection payload, shared-analysis trace fields | `sleepagent/report_cli.py:43-87,675-852` | Update DTO only after the authoritative Product API version is frozen. |
| Demo API runtime | `DUAL_READ` | demo operation/trace plus public/Product projections | `sleepagent/api/demo.py`; `sleepagent/api/public_runtime.py`; `sleepagent/api/postgres.py` | Keep replay-only trace compatibility until demo/read models prove shared-only authority. |
| Demo CLI | `DUAL_READ` | `/product/sleep/today`, projection IDs/content, operation trace | `sleepagent/simulation/cli.py:229,644-731,2115-2182,3662-3694` | Migrate trace and today readers together; preserve replay verification. |
| Reference client | `DUAL_READ` | public role view, report run/status/list, snapshot role view | `reference_client/sleep_api_v1_client.py:345-396,487` | Version and contract-test the shared-only read surface before removal. |
| Automatic fast-path handoff | `COMPAT_ONLY` | creates `product_agent_compatibility.v1` operation/queue alongside report run | `sleepagent/domain/postgres_slice.py:2600-2720` | New compatibility-operation count must be observed at zero after explicit M8 cutover. |
| Explicit report command creation | `SHARED_READY` | reserves `product.report.run.v1` | `sleepagent/workers/commands.py:910-956` | Preserve semantic idempotency and queue route. |
| Product worker report router | `DUAL_READ` | routes `product.report.run.v1`, converges on `product.shared_analysis.v1`, retains legacy `prepare` | `sleepagent/workers/product.py:151-154,971-1684,1895-1976` | Prove no production caller reaches legacy `prepare` before deletion. |
| Compatibility completion/failure bridge | `COMPAT_ONLY` | compatibility operation, `product_agent_compatibility_result.v1`, copied `product_agent_result.v1` | `sleepagent/workers/product.py:2733-3138,2881,2982` | Stop new bridge writes, prove no caller depends on its operation/result ID, then retain history read-only. |
| Shared analysis commit path | `DUAL_READ` | persists shared analysis and role projections while copying compatibility result | `sleepagent/workers/product.py:4895,5246,5460-5556` | Separate authoritative shared persistence from compatibility copy in M8. |
| Legacy per-role processor | `LEGACY_ONLY` | three full Agent role runs and `product_agent_result.v1` | `sleepagent/workers/product.py:1274-1414` | Production caller count zero and historical read path identified. |
| Role-view table/read functions | `DUAL_READ` | role view JSON, public today JSON, projection/operation IDs | `sleepagent/persistence/migrations/002_replay_journey_and_today.sql:2027-2059`; `004_stage3_reads_and_scenario_clock.sql:54-63` | Additive read migration; never edit applied migrations. |
| Demo journey completion function | `DUAL_READ` | `product_operation_id`, `analysis_revision_id`, `role_projection_ids` | `sleepagent/persistence/migrations/002_replay_journey_and_today.sql:1851-2059` | Update through a new migration only after Demo readers are shared-only. |
| Technical trace SQL | `DUAL_READ` | role-run publication/role-view and operation identities | `sleepagent/persistence/migrations/010_terminal_demo_technical_trace.sql:2,68-72` | Add a versioned shared-authority trace before retiring legacy fields. |
| Simulation journey repository | `SHARED_READY` | analysis revision and three role projection IDs | `sleepagent/simulation/journey.py:818-896,1254-1267` | Preserve exact three-role projection completion semantics. |
| Product PostgreSQL integration tests | `DUAL_READ` | report/shared operations, compatibility lifecycle, role views/results | `tests/integration/test_product_postgres_integration.py:3245,3640` and compatibility/result assertions throughout | Reclassify individual cases as historical-read, shared-write, or retired-write during M7-M9. |
| Product worker unit fixtures | `DUAL_READ` | shared analysis plus compatibility `product_agent_result.v1` | `tests/unit/test_product_elder_narrative_worker.py:480,1072,1168`; `tests/unit/test_product_agent_runner.py` | Retain characterization while changing write authority explicitly. |
| Product report contract tests | `SHARED_READY` | strict shared analysis/projection identity and operation selection | `tests/unit/test_product_report_contracts.py:408,815,1011-1036,1225-1233` | Keep as shared-authority regression coverage. |
| Other legacy-result test consumers | `RETIREMENT_UNKNOWN` | `product_agent_result.v1` and legacy tool/factory contracts | `tests/unit/test_product_agent_factory.py`; `tests/unit/test_product_agent_tooling.py` | Audit each assertion before M9; no bulk deletion. |

## Frozen findings

- Current default is `shared_compat`, not `legacy`.
- `product_agent_compatibility` is deliberately absent from the default
  claimable queue order and has no registered handler; shared orchestration
  completes or fails it.
- Operation/result IDs remain cross-cutting compatibility contracts. Absence of
  a direct public field does not prove consumer-zero.
- No consumer in this inventory is removed in G1.

## G4 rescan at entry checkpoint `857249c`

This section supersedes the G1 classifications for forward writes and current
reads. The original table remains above as frozen migration evidence. The
executable inventory is `python -m sleepagent.report_consumer_audit`; its unit
test requires AST call-site evidence, worker routes, Product API joins,
Product/Demo/reference-client flows, and historical SQL references rather than
accepting a grep-only zero claim.

| Consumer / exact current call site | G4 classification | Evidence and disposition |
|---|---|---|
| Product report command/router — `workers/product.py:1069,1155,3338` | `SHARED_READY` | Every new `product.report.run.v1` converges on `product.shared_analysis.v1`; one shared commit owns externally visible semantics. |
| Product report API — `api/postgres.py:893,926,1052,3745` | `SHARED_READY` | Current report reads join the linked shared operation and require `role_projection.v1`; generic historical rows remain an internal compatibility read. |
| Product `/today` and public v1 role views — `workers/product.py` role-projection persistence plus `api/postgres.py` current-revision reads | `SHARED_READY` | New rows serialize the deterministic `RoleProjection` as `view_json`; no per-role Agent result is required. Historical generic role-view JSON remains readable. |
| `sleepagent-report`, Demo CLI/API, simulation presentation, and reference client — paths recorded by `report_consumer_audit.v1` | `SHARED_READY` | They consume Product/public projection DTOs produced from the current shared-backed role-view row. They do not create or select a legacy analysis authority. |
| Shared worker execution — `workers/product.py:1514` | `SHARED_READY` | One role-neutral Agent analysis produces exactly three deterministic projections. Optional Elder narrative is presentation-only and cannot change facts. |
| Shadow comparator — `workers/product.py:1808,7193` | `SHARED_READY` | `report_pipeline_mode=shadow` executes the dormant legacy preparation only for structured comparison. It persists category hashes/mismatches, declares shared authority, and permits no external effects. |
| Three-Agent legacy preparation — `workers/product.py:1372` and the guarded shadow call at `:1804` | `LEGACY_ONLY` | No new production operation creator targets this path. It remains temporarily executable for explicit shadow evidence and historical direct-operation characterization. |
| Automatic compatibility operation creator — `domain/postgres_slice.py` `product_agent_compatibility.v1` insert | `COMPAT_ONLY` | Still writes a non-claimable bridge operation in `shared_compat`; this is the principal G5 write-path retirement target. |
| Compatibility completion/failure — `workers/product.py:3138,3330,3651` | `COMPAT_ONLY` | Copies the authoritative shared result identity into the old operation/result envelope. It does not run legacy role Agents or create a second analysis. |
| Applied SQL role-view/today/demo/technical-trace functions | `COMPAT_ONLY` | Historical reads must remain. New writes behind these functions are shared projections; applied migrations are immutable. |
| Unit/integration legacy fixtures | `COMPAT_ONLY` | Kept as rollback, historical-read, and shadow-characterization evidence; they are not runtime consumers. |

### G4 consumer-zero status

- New legacy per-role operation creators: zero, confirmed by the executable AST
  and operation-literal inventory plus the report/fast-path PostgreSQL tests.
- Active new report reads requiring a legacy per-role Agent result: zero. The
  Product API resolves the shared operation and deterministic projection for
  new report requests.
- Retained legacy `prepare` implementations/call sites: nonzero, intentionally
  limited to shadow comparison and historical direct-operation handling.
- Retained compatibility bridge writes: nonzero in `shared_compat`; therefore
  G5 must disable them under a proved `shared_only` mode before claiming full
  legacy write-path consumer zero.
- Retained historical compatibility reads: nonzero by design; they do not
  authorize new CareAction, Habit/Memory, delivery, or external effects.
