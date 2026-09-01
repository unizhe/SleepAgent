# SleepAgent Remediation Execution Ledger

## Ledger metadata

| Field | Value |
|---|---|
| Master plan | `SLEEPAGENT_REMEDIATION_AND_CLOSURE_PLAN.md` |
| Baseline goal | G0 — repository re-baseline and implementation planning |
| Baseline captured | 2026-08-30 (Asia/Shanghai) |
| Repository | `/mnt/data4/wz/SleepAgent/sleepagent` |
| Git branch | `agent/repository-structure-cleanup` |
| Git HEAD | `51947e6da69d9d8e0e0eeb36669e227865fc26ff` |
| Worktree at capture | Dirty: 19 modified tracked files and 1 untracked master-plan file; branch 13 commits ahead of upstream |
| G0 status | `PASS_WITH_LIMITATIONS` |

This ledger is the implementation authority for G1–G8 only after the current
worktree state has been preserved or reconciled. The master plan defines the
direction. Current source, tests, migrations, dependency graph, and runtime
contracts define implementation details.

## Status vocabulary

Every goal uses exactly one of:

`NOT_STARTED | IN_PROGRESS | PASS | PASS_WITH_LIMITATIONS | BLOCKED`

`PASS_WITH_LIMITATIONS` means the goal's bounded objective is complete but its
evidence has an explicitly recorded environmental or compatibility limitation.
It is not permission to skip the limitation in a later goal.

## Frozen implementation discipline

These rules apply to every future goal:

1. No broad rewrite.
2. Database changes are additive migrations only.
3. Never modify an already-applied historical migration.
4. Preserve behavior with explicit compatibility shims while consumers move.
5. Do not delete a legacy path before consumer-zero evidence exists.
6. A shadow or legacy path must never trigger a real external effect.
7. No LLM-generated recipient address is accepted or resolved.
8. UTC remains authoritative; local values are deterministic projections from
   an IANA timezone and pinned context.
9. Live Push, live Pull, and Replay must converge on one canonical acceptance
   and semantic-validation boundary.
10. Every goal leaves the repository testable.
11. Every goal records explicit acceptance evidence in this ledger.
12. Do not automatically start the next goal after the current goal completes.
13. Do not guess an ambiguous historical movement value into either index or
   count semantics.
14. A hard-finalized night is immutable; late evidence creates a superseding
   revision.
15. Urgent deterministic zero-model authority must not be weakened.

## G0 verdict

`G0_VERDICT = PASS_WITH_LIMITATIONS`

The master plan is directionally sound and implementable incrementally, but G1
must use the re-baselined current behavior rather than several stale naming and
packaging assumptions in the plan. No production implementation was performed
in G0. The only repository addition is this documentation ledger.

Limitations:

- The baseline was captured from a dirty worktree containing active, user-owned
  reporting changes. G1 must first preserve/reconcile that exact worktree.
- PostgreSQL migration attestation, PostgreSQL tests, process-fault tests, and
  full E2E were not runnable without a database URL/service.
- The repository `.venv` is incomplete. Tests used the available Python 3.13
  environment, which is outside the declared `>=3.11,<3.12` range. Compilation,
  imports, and OpenAPI checks were independently repeated with the available
  Python 3.11.15 runtime.
- Mypy is declared but not installed in either available environment.
- Both committed OpenAPI snapshots are already stale.

## Current architecture findings

### A. Movement semantics and downstream consumers

Evidence:

- `sleepagent/domain/contracts.py:298-303` defines one
  `MovementPayload.v1` with units `count | index | event` and defaults to
  `index`; it has no metric identifier or aggregation window.
- `sleepagent/domain/ontology.py:22-37` registers only one movement metric:
  `movement/index` from `device_measured` sources.
- `sleepagent/integrations/perceptor/push.py:430-479` maps Push
  `BodyShake` to `MovementPayload(value=...)`, therefore the default `index`.
- `sleepagent/integrations/perceptor/pull.py:329-397` maps historical
  `body_shake` compact samples through the same default payload.
- `sleepagent/integrations/perceptor/pull.py:587-669` maps both SleepReport
  hourly `{hour,count}` and `{time_long,value}` bodies to that same payload.
  Hourly count is consequently serialized as unit `index`, despite the source
  field and limitation explicitly saying it is a count bucket.
- `sum_body_shake_times` is separately retained as a vendor sleep-profile
  summary metric (`pull.py:674-690`) and must remain separate.
- `sleepagent/domain/product_data.py:641-720` groups numeric samples solely by
  `observation_type` and averages the `movement` array into
  `vital_centers.movement`; unit, source kind, and aggregation kind are ignored.
- `sleepagent/runtime/tools.py:239`, `runtime/schemas/canonical.py:137`, and
  `runtime/policies.py:221` also expose a `movement_count` risk/report concept.
  That concept is not a safe substitute for the ambiguous canonical movement
  array and needs an explicit mapping in G2.

Finding: realtime/history vendor movement and SleepReport counts can enter the
same canonical type and the Product aggregation can average them. The plan's D1
is confirmed and is the first behavior-changing remediation dependency.

### B. Observation ingestion and validation parity

Common validation that already exists:

- Pydantic schema/discriminator checks and envelope/payload type matching in
  `AdapterObservationCandidate` and `SleepObservation`.
- Provider/provenance ID consistency in `AdapterObservationCandidate`.
- Explicit DeviceBinding application through `bind_adapter_candidate`.
- Adapter-specific raw integrity, timestamp, identity, and contract checks.

Path evidence:

| Path | Current construction | Ontology validation |
|---|---|---|
| Push | `push.normalize_push_envelope` → namespace-scoped candidate → `bind_adapter_candidate` → reconciliation/persist (`integrations/perceptor/ingestion.py:323-432`) | No call |
| Pull | endpoint-specific functions in `pull.py` → namespace-scoped candidate → `bind_adapter_candidate` → reconciliation/persist (`pull_ingestion.py:840-1060`) | No call |
| Replay | generator/adapter creates `ReplayObservationInput`; persistence later creates candidate and observation (`domain/postgres_slice.py:122-179`, `3520-3590`) | Yes, in `ReplayObservationInput.validate_replay_input` |

`validate_observation_ontology` has one production call site, the Replay input
validator. There is no `CanonicalObservationFactory`, and the three paths do not
have equivalent acceptance/rejection semantics. A SleepReport movement carrying
`vendor_derived` source kind would also be rejected by the current ontology if
the live paths actually called it. The validator must therefore be redesigned,
not merely inserted into Push and Pull.

### C. UTC, local date, and report localization

What is already correct:

- Perceptor epoch and binding-local timestamps are normalized to aware UTC.
- `NightEpisodeV2` validates IANA timezone, UTC instants, local dates, offset,
  and DST fold (`domain/episodes.py:47-176`).
- Wake-date ownership prefers observed wake, then vendor wake date, then a
  deterministic deadline projected through the pinned timezone
  (`domain/episodes.py:294-339`).
- Legacy v1 dates are upcast as `legacy_unknown` and cannot masquerade as a
  canonical wake date.

What is not correct or incomplete:

- `ProductNightEvidence.deterministic_night_summary` formats a stored aware
  event directly with `strftime("%H:%M")` and labels it `local_time`
  (`domain/product_data.py:680-704`) without first using
  `ZoneInfo(self.timezone_name)`.
- The current dirty worktree adds a timezone-aware Elder presentation view,
  but that is an uncommitted, Elder-only mitigation and not a reporting context
  contract.
- `SharedAnalysisSourceV1` carries `wake_date` but no timezone, locale, or
  renderer version (`runtime/reports.py:616-652`).
- `ProductEpisodeRunner.analyze_shared` builds `summary_lines` from
  `EvidenceClaim.statement` plus a care title (`runtime/runner.py:561-572`).
- `SharedNightAnalysis` validates that those exact strings are retained
  (`runtime/reports.py:831-840`).
- Family and doctor deterministic projections copy `shared.summary_lines`
  under Chinese headings (`runtime/reports.py:1579-1705`). The dirty worktree
  introduces typed Elder atoms, but family/doctor still consume free-form claim
  strings. This is the precise source of mixed Chinese/English output.

Current semantic path:

`EvidenceClaim.statement → ProductEvidencePacket → SharedNightAnalysis.summary_lines → RoleProjection.text → Product API/report CLI/public read models`

Finding: the plan's UTC authority should be retained, but it must not replace
the already-correct NightEpisodeV2 date model. G3 should focus on canonical
reporting context and structured facts/rendering, plus the remaining direct
`strftime` misuse.

### D. Shared and legacy report pipelines

Current durable route:

1. Product API `POST /product/sleep/reports/run` reserves
   `product.report.run.v1` on queue `product_agent`
   (`api/postgres.py:2150-2235`, `2838-2844`).
2. `ProductAgentWorkHandlerAdapter` routes that operation through
   `ProductAgentProcessor.route_report_request`.
3. The deterministic quality/risk gate closes urgent/unusable requests without
   shared analysis.
4. An analyzable request converges on a unique
   `product.shared_analysis.v1` operation keyed by desired analysis SHA-256
   (`workers/product.py:971-1065`, `3120-3335`).
5. `prepare_shared` performs one role-neutral analysis and builds three
   deterministic role projections; optional Elder narrative is separate.

Legacy/compatibility state:

- `ProductAgentProcessor.prepare` still executes three complete per-role Agent
  runs (`workers/product.py:1274-1414`). It remains reachable for operation
  types other than `product.shared_analysis.v1` handled by the Product worker.
- `prepare_shared` is a separate executable branch
  (`workers/product.py:1416-1684`).
- Every eligible automatic fast-path handoff currently creates both a
  `product.report.run.v1` operation and a pending `product_agent` compatibility
  operation on queue `product_agent_compatibility`
  (`domain/postgres_slice.py:2600-2690`).
- The compatibility queue is absent from `DEFAULT_QUEUE_ORDER` and no handler
  is registered for it. The bridge is intentionally non-claimable and is
  completed/failed by shared orchestration (`workers/product.py:2733-3135`).
- Shared commit copies a `product_agent_result.v1` compatibility result while
  also persisting `shared_night_analysis.v1`, `role_projection.v1`, and the v3
  Product attempt.

Current consumers and cutover dependencies:

| Consumer | Current state | Cutover dependency |
|---|---|---|
| Product report API | Shared-ready; validates exact shared analysis/projection identity | Keep strict v2 read model and historical upcaster policy |
| `sleepagent-report` CLI | Shared-ready via Product HTTP API | Update DTO/snapshot only after API contract is frozen |
| Product `/today` | Reads `public_today_json` from role-view rows without requiring shared-analysis schema | Add shared authority marker or documented historical adapter |
| Public v1 role view | Reads any protocol-v2 role view and returns generic `view_json.content` | Migrate/guard before legacy role view removal |
| Demo CLI | Uses public/Product HTTP read models and role projection IDs | Update after read models; preserve replay verification |
| SQL/read functions | Migration 002 today read path and migration 010 technical trace depend on role views and v3/legacy-shaped result fields | Inventory exact functions before write-path deletion; additive migration only |
| Tests/fixtures | Many tests assert `product_agent_result.v1`, compatibility completion, role-view shape, and current trace behavior | Reclassify as historical-read vs retired-write tests |

Finding: the repository is already shared-primary for new report requests, not
legacy-primary. The remaining problem is executable legacy code plus compatibility
write/read contracts, not a need to introduce SharedNightAnalysis from scratch.

### E. Architecture boundaries and cycles

Confirmed reverse dependencies:

- `domain/product_data.py` imports `runtime.contracts`, `runtime.cold_start`, and
  `runtime.tools`.
- `domain/postgres_slice.py` imports persistence and `workers.retention` and
  contains SQL, encryption, repositories, queue creation, and projection logic.
- `workers/effects.py` imports a concrete handler from `workers.demo`.
- `workers/runtime.py` imports `process`, persistence functions, and domain IDs,
  and lazily imports every concrete handler factory in `_cli_handlers`.

Confirmed self-imports:

- `runtime/agents.py`
- `workers/retention.py`
- `runtime/contracts.py`
- `runtime/knowledge.py`
- `runtime/registry.py`
- `config.py` (not listed in the master plan)

Composition cycle:

- `app.py` imports `SleepBackendRuntime` and `build_backend_runtime` from
  `process.py`.
- `process.build_backend_runtime` lazily imports
  `app.build_api_runtime_services`.
- API composition and deployment side effects are appended to `app.py` after
  the app factory.

An AST package graph found six cyclic components:

- one 14-module SCC spanning app, process, domain PostgreSQL slice, Perceptor
  ingress, simulation journey/replay, and all major worker modules;
- one 8-module runtime SCC (`cold_start`, `contracts`, `governance`, `hitl`,
  `invocation`, `memory`, `registry`, `results`);
- one 2-module SCC (`domain.product_data` ↔ `runtime.tools`);
- three self-SCCs (`config`, `runtime.agents`, `runtime.knowledge`). Other
  self-imports participate in the larger SCCs.

The current architecture test has only two rules: retired-subsystem imports and
the narrow Perceptor importer allowlist. It does not enforce the master plan's
domain, worker-kernel, or app/process boundaries.

### F. Device automation and finalization

- `DeviceBinding.v1` is a strict domain contract with IANA timezone and
  effective interval validation (`domain/contracts.py:507-546`).
- PostgreSQL persists device bindings and Push/Pull resolve them authoritatively.
- There is no production DeviceBinding application service, management API, or
  CLI. Production Python contains no binding INSERT/UPDATE service; integration
  tests provision bindings with direct SQL. `docs/operations/perceptor.md`
  explicitly documents the manual gap.
- `DurablePerceptorPullIngress` and `PerceptorPullBackfillRunner` provide safe,
  bounded Pull and durable checkpoint semantics, including History and
  SleepReport (`pull_ingestion.py:192-767`). There is no durable scheduler,
  schedule table, or supported scheduler command.
- Current NightEpisode closure is date/lifecycle closure: observed bed-out or an
  explicitly invoked deterministic deadline changes the episode to
  `awaiting_report` (`domain/postgres_slice.py:477-692`). No persistent scan
  invokes deadline closure automatically.
- There is no separate OPEN/SOFT_FINALIZED/HARD_FINALIZED aggregate. Existing
  `date_state='finalized'` means date ownership is finalized, not that all
  acquisition sources are complete.
- Existing revisions and lateness fields provide useful foundations, but there
  is no hard-finalized immutable supersession workflow for late Push/Pull or a
  vendor SleepReport arriving after reporting.

### G. External care effects

Existing reusable boundary:

- `ActionProposal`, `HumanDecisionRequest`, `ApprovalGrant`, and
  `VerifiedApprovalCapability` implement exact-target HITL in
  `runtime/hitl.py`.
- `CareActionCandidate`, `CareDeliveryDecision`, and `ExternalActionTarget`
  provide candidate/effect contracts in `runtime/contracts.py`.
- Durable outbox, delivery intents, lease/fence/retry, invocation journal,
  outcome-unknown, and reconciliation already exist in migrations 001/005/008
  and `workers/runtime.py`/`workers/effects.py`.
- The replay sink is idempotent and queryable by semantic effect key.

Current boundary limits:

- Delivery v2 is fixed by database constraint to destination
  `replay_care_notification` and handler `deterministic_replay_sink`.
- The replay sink requires `synthetic_non_release=true` and writes to a
  replay-only effects table.
- Recipient identity is an actor plus actor-subject role binding. It is not a
  governed email/contact destination.
- There is no `ContactBinding`, email address storage/reference, email adapter,
  provider receipt/webhook, recipient acknowledgement, action execution, or
  next-night outcome model.
- Agent context explicitly forbids `email`, which is correct and should remain;
  deterministic application code must resolve a contact reference.

Best seam: preserve the existing durable delivery intent and invocation
journal, introduce a delivery command/adapter port between approved intent
creation and concrete dispatch, keep the deterministic replay adapter as one
implementation, and add email as a live-only implementation selected in the
composition root. Do not place the port in `workers.runtime` while its handler
composition responsibilities remain entangled.

## Demonstrated discrepancies versus the master plan

| ID | Plan assumption | Current evidence / required correction |
|---|---|---|
| D-G0-01 | P0 flags name report modes `legacy | shadow | shared_only` while preserving current behavior | Current automatic/report API path is shared-primary plus compatibility bridge. Defaulting to `legacy` would regress to three role runs. G1 must define an explicit current mode such as `shared_compat`, or separate primary pipeline from compatibility emission. |
| D-G0-02 | P1 package examples assume `application/` and later `infrastructure/` roots | Neither root exists. G2 must add a narrow application boundary without broad re-layout; G5 owns the larger move. |
| D-G0-03 | P1 can appear to add the current ontology validator to all paths | Current validator accepts only movement/index/device-measured; it would reject SleepReport vendor-derived movement. M1 semantic registry must precede M2 convergence. |
| D-G0-04 | P2 frames timezone handling as generally missing | NightEpisodeV2 wake-date/offset/fold handling is already strong. The concrete defect is report projection/formatting and missing reporting context, so do not rewrite episode date ownership. |
| D-G0-05 | P3 describes a transition from old and new chains without identifying the current default | Shared is already the primary report-request path; legacy `prepare` and compatibility contracts remain. G4 starts with inventory/shadow/authority marking, not with introducing shared execution. |
| D-G0-06 | P4 self-import list is complete | `config.py` also self-imports (`config.py:505`). Add it to the mechanical cleanup inventory. |
| D-G0-07 | P5 can build finalization by extending current episode semantics | Current `date_state='finalized'` is specifically date ownership. New finalization must be a separate aggregate/state, as D6 says, and must not reinterpret old rows. |
| D-G0-08 | Suggested migrations are `014`–`017` | This remains accurate: the current immutable inventory is exactly 001–013. Actual migration boundaries may be split more finely; numbering must be assigned at implementation time without editing 001–013. |
| D-G0-09 | Role renderer work starts from unmodified v1 projections | The dirty worktree already adds typed Elder message atoms/presentation authority. G1 must preserve and characterize this work; G3 must decide whether to promote it into the structured v2 renderer design, not duplicate it. |

The master remediation plan was not rewritten. These mismatches are recorded
here first, as required.

## Goal and milestone map

| Goal | Status | Milestones | Exact bounded outcome | Depends on |
|---|---|---|---|---|
| G1 — Safety baseline | `NOT_STARTED` | M0 | ADRs, strong default-preserving flags, characterization fixtures, legacy consumer inventory, and no-growth architecture checks; no production behavior change | G0 |
| G2 — Observation semantics | `NOT_STARTED` | M1, M2, M3 | Movement v2 registry/contracts, one canonical factory for Push/Pull/Replay, additive persistence/upcaster, and metric-safe aggregation | G1 |
| G3 — Reporting context/localization | `NOT_STARTED` | M4, M5 | UTC-derived reporting context, structured facts, zh-CN deterministic role renderers, semantic/projection hash separation | G2 |
| G4 — Shared report cutover | `NOT_STARTED` | M6, M7, M8, M9 | Shadow comparison, all readers on authoritative projections, zero new compatibility writes, then legacy write-path retirement | G3 |
| G5 — Dependency repair | `NOT_STARTED` | M10, M11 | Mechanical self-import cleanup, worker kernel/composition roots, domain inversion removal, known SCC elimination | G4; G1 no-growth guard already active |
| G6 — Device automation | `NOT_STARTED` | M12, M13, M14 | Governed DeviceBinding service/CLI, durable acquisition scheduler, separate night finalization and late-data revisions | G2, G3, G5 |
| G7 — Live care effects | `NOT_STARTED` | M15, M16, M17 | Generic delivery port, email provider/receipts, HITL-to-ack/execution flow, outcome/effect receipts | G4, G5, G6 |
| G8 — Closure | `NOT_STARTED` | M18 | PostgreSQL/process/fault E2E, operational metrics, consumer-zero evidence, final permitted legacy cleanup | G2–G7 |

## M0–M18 affected-module map

| Milestone | Likely current modules/files affected |
|---|---|
| M0 | `sleepagent/config.py`; `docs/architecture/adr/` (new); `docs/remediation/`; `tests/architecture/`; movement/ingestion/report characterization tests; OpenAPI snapshots after contract is intentionally changed |
| M1 | `domain/contracts.py`, `domain/ontology.py`, new narrow observation contract/semantic modules, movement fixtures/tests |
| M2 | `integrations/perceptor/push.py`, `pull.py`, `ingestion.py`, `pull_ingestion.py`, `simulation/replay_ingress.py`, `domain/postgres_slice.py`, new canonical factory |
| M3 | additive migration 014 or next available; `persistence/migration_manifest.json`; `persistence/migrations.py`; `domain/product_data.py`; trend/risk/report tools and tests; legacy upcaster |
| M4 | `domain/episodes.py` only if additive context is needed; `domain/product_data.py`; `runtime/contracts.py`; `runtime/reports.py`; worker fact construction; DST/local-date tests |
| M5 | `runtime/contracts.py`, `runtime/runner.py`, `runtime/reports.py`, `workers/product.py`, `api/postgres.py`, `api/product_contracts.py`; new `presentation/zh_cn/` modules; report tests/snapshots |
| M6 | `config.py`, `workers/product.py`, `domain/postgres_slice.py`, report trace/read models, consumer inventory and shadow-comparison persistence/tests |
| M7 | `api/postgres.py`, `api/public_runtime.py`, `api/product_contracts.py`, `report_cli.py`, `simulation/cli.py`, reference client, SQL read functions through additive migration |
| M8 | `domain/postgres_slice.py`, `workers/product.py`, config defaults/flags, operation metrics/tests |
| M9 | `workers/product.py`, legacy Product contracts/results, compatibility orchestration, worker queue grants/config, legacy-write-only tests/SQL indexes after consumer-zero proof |
| M10 | new `workers/kernel.py` and `bootstrap/worker.py`; `workers/runtime.py`; concrete worker modules; `app.py`, `process.py`; new API/worker bootstrap modules |
| M11 | `domain/postgres_slice.py`, `domain/product_data.py`, `runtime/tools.py`, `workers/retention.py`, `workers/effects.py`, `workers/demo.py`; new infrastructure/application contracts and re-export shims |
| M12 | `domain/contracts.py` or new device aggregate; additive migration; new application service and admin CLI; Perceptor resolution/adapters; tests/docs |
| M13 | additive scheduler migration; new acquisition scheduler service/worker/bootstrap; `pull_ingestion.py` reuse; config/metrics/process tests |
| M14 | `domain/episodes.py`, `domain/postgres_slice.py` or extracted services; additive finalization migration; Product eligibility/read models; late-data/supersession tests |
| M15 | `runtime/hitl.py`, delivery contracts/ports, `workers/effects.py`, `workers/runtime.py`, additive live-delivery migration, new email infrastructure adapter/composition |
| M16 | `workers/commands.py`, HITL/application command service, delivery intents/attempts/receipts, webhook/API/acknowledgement contracts, contact binding service |
| M17 | care action/execution/outcome contracts and repositories, finalization event consumers, Habit/Memory governed update path, effect-receipt tests |
| M18 | `scripts/verify_backend.sh`, E2E/fault tests, observability/internal metrics, final legacy modules/shims/SQL identified by the retirement checklist |

## Exact proposed scope for G1 (M0)

G1 is implementation of safeguards only. It must not change report output,
operation counts, scheduling, ingestion acceptance, or external effects.

### G1 deliverables

1. Preserve or reconcile the current dirty worktree before editing overlapping
   report modules. Record the resulting start HEAD/worktree identity here.
2. Add five ADRs under a consistent new `docs/architecture/adr/` location:
   observation semantics v2, reporting pipeline v2, night finalization, care
   delivery/outcome, and layering/composition roots.
3. Add strongly typed, fail-closed configuration with defaults matching the
   current implementation:
   - `observation_semantics_version=v1`
   - `report_pipeline_mode=shared_compat` (recommended correction to the plan)
   - `emit_legacy_report_compatibility=true`
   - `acquisition_scheduler_enabled=false`
   - `live_delivery_enabled=false`
4. If the plan's exact report enum must be preserved instead, model primary
   pipeline and compatibility emission as independent values so the default is
   still current shared behavior. Never default to executable legacy per-role
   analysis merely to match the plan's label.
5. Add characterization fixtures/tests for:
   - historical/Push `body_shake=39.9` versus SleepReport hourly count;
   - current mixed aggregation behavior, explicitly marked as a defect fixture;
   - the same valid and invalid observation presented to Push, Pull, and Replay,
     recording the current non-parity;
   - `17:35Z` and DST cases for current local-date/time behavior;
   - an English claim statement under current Chinese role headings;
   - exact automatic operation DAG and non-claimable compatibility operation.
6. Add a machine-readable or tabular legacy consumer inventory covering Product
   report API, Product `/today`, public v1 role view, report CLI, Demo CLI,
   reference client, SQL functions/views, trace, fixtures, and result/operation
   IDs. Mark each `legacy_only | dual_read | shared_ready | retired`.
7. Add AST import tests that forbid new violations while grandfathering each
   exact current violation. Required no-growth rules:
   `domain -> runtime/workers/infrastructure`, `workers kernel/runtime -> concrete
   handlers`, `process -> app`, and module self-imports.
8. Update committed OpenAPI snapshots only if G1 intentionally changes public
   schema. Since the snapshots already drift at baseline, snapshot reconciliation
   must be an explicit reviewed G1 change, not hidden in another fixture update.
9. Run the full supported-Python unit/non-PostgreSQL suite and PostgreSQL suite
   when an environment is available. Record operation/provider/effect counts as
   unchanged.

### G1 likely file list

Existing files likely touched:

- `sleepagent/config.py`
- `tests/architecture/test_runtime_dependency_boundaries.py` or a new adjacent
  remediation-boundary test
- `tests/unit/test_sleep_domain_contracts.py`
- `tests/unit/test_perceptor_production_ingestion.py`
- `tests/unit/test_perceptor_pull.py`
- `tests/unit/test_replay_ingress_adapter.py`
- `tests/unit/test_product_data_tools.py`
- `tests/unit/test_product_report_contracts.py`
- `tests/unit/test_product_elder_narrative_worker.py`
- `tests/unit/test_report_cli.py`
- `docs/contracts/openapi/backend-bff-v1.json` and `demo-v1.json` only through an
  explicit snapshot reconciliation

New files/directories likely added:

- `docs/architecture/adr/ADR-Observation-Semantics-v2.md`
- `docs/architecture/adr/ADR-Reporting-Pipeline-v2.md`
- `docs/architecture/adr/ADR-Night-Finalization.md`
- `docs/architecture/adr/ADR-Care-Delivery-and-Outcome.md`
- `docs/architecture/adr/ADR-Layering-and-Composition-Roots.md`
- `docs/remediation/LEGACY_REPORT_CONSUMERS.md` or an equivalent machine-readable
  inventory referenced from this ledger
- narrowly scoped characterization fixture files under `tests/fixtures/`

No migration is expected in G1. No production behavior branch should consume
the new flags yet except validation/observability proving their default values.

### G1 acceptance evidence

- Flag default test proves byte-for-byte/current-operation behavior remains
  unchanged.
- Characterization fixtures reproduce every listed current behavior and defect.
- Architecture no-growth check passes with an explicit baseline allowlist and
  fails when a synthetic new forbidden edge is added.
- Legacy consumer inventory has an owner/status/cutover condition for every
  current consumer.
- OpenAPI snapshots either pass after a separately reviewed reconciliation or
  remain an explicit blocker; no silent regeneration.
- Provider calls, operation creation counts, queue routes, and external effects
  are unchanged from the G0 fixture baseline.

## Goal dependencies, risks, and blockers

### Blocking before G1 implementation

- Decide how to preserve/reconcile the existing 19 modified files. They include
  exactly the Product/report modules G1 needs to characterize. G1 must not
  overwrite them or assume HEAD alone is the baseline.
- Adopt the corrected current report mode name/shape described in D-G0-01.

### Cross-goal risks

- Movement v2 changes ontology, storage, Product aggregation, trend cohorts,
  cold-start metrics, and care evaluation. Partial conversion would be worse
  than the current explicit defect.
- Current SleepReport movement is `vendor_derived`, while the v1 ontology allows
  movement only as `device_measured`; naive factory reuse would quarantine valid
  live data.
- The dirty Elder atom work changes projection hashes and OpenAPI/read-model
  behavior. G3 must integrate it deliberately.
- Shared and compatibility operations share result IDs and role-view storage.
  Deleting `prepare` before public `/today`, public v1 role view, SQL trace, and
  historical readers are migrated can break existing reads.
- The 14-module SCC makes scheduler/email composition risky until G5. G1 must
  prevent further growth; G6/G7 depend on the corrected composition root.
- Current `date_state` must not be renamed or reinterpreted as data finalization;
  doing so would corrupt report eligibility semantics.
- Existing delivery schema is replay-specific by CHECK constraint. Live email
  needs additive schema/adapter design, not conditionals that weaken the replay
  proof.
- No database baseline was available in G0. Before the first migration goal,
  migration attestation and PostgreSQL tests are mandatory.
- The OpenAPI snapshots are stale before remediation starts; consumer contract
  drift can otherwise be misattributed to later goals.

## Baseline verification evidence

| Verification | Result | Notes |
|---|---|---|
| `git status --short --branch` | `PASS_WITH_LIMITATIONS` | Ahead 13; 19 modified tracked files; untracked master plan |
| `git diff --check` | PASS | No whitespace errors in current tracked diff |
| Python 3.11 compileall | PASS | `sleepagent`, `reference_client`, `scripts`, `tests` |
| Python 3.11 import smoke | PASS | app/process/domain/workers/Perceptor/Replay imports |
| Unit suite | PASS | 948 passed in 9.09s under available Python 3.13 |
| Full non-PostgreSQL, non-E2E, non-ASGI-lifespan suite | PASS | 1028 passed, 38 deselected in 17.51s; loopback-only allowance required |
| Targeted required-area tests | PASS | 200 passed in 4.69s |
| Current architecture tests | PASS | 2 passed; coverage is too narrow, as documented |
| Loopback provider tests | PASS | 9 passed when local `127.0.0.1` bind was allowed; sandbox-only initial failures |
| OpenAPI snapshot check | FAIL | `backend-bff-v1.json` and `demo-v1.json` drift; current generated schemas contain substantial uncommitted/unrefreshed surface changes |
| Migration inventory | PASS | Exactly 001–013; manifest target 13; all observed SQL SHA-256 values match manifest; manifest SHA-256 `c027b2a2e7828713422770d14e171a795c94b32e716b5eb077e33bd610880a06` |
| Database migration check | ENV_BLOCKED | `migration check refused: PostgreSQL database URL is required` |
| PostgreSQL/integration/process E2E | ENV_BLOCKED | No database URL/service supplied; not silently treated as passing |
| Mypy | ENV_BLOCKED | `mypy` is not installed in available Python 3.11 or 3.13 environments |
| Static import graph | FAIL (baseline finding) | Six SCCs, reverse dependencies, and self-imports listed above |

The initial full non-PostgreSQL run inside the restricted sandbox reported eight
loopback bind failures (`PermissionError: operation not permitted`). Every
affected test passed when rerun with local-loopback permission, and the clean
full pass above is the authoritative result.

## Verification log for future goals

Append one entry per goal. Do not rewrite prior evidence.

| Goal | Commit/worktree identity | Tests and proofs | Provider/operation/effect delta | Status |
|---|---|---|---|---|
| G0 | HEAD `51947e6...`; dirty baseline documented above | See baseline table | No external provider or production effect invoked; only local deterministic tests | `PASS_WITH_LIMITATIONS` |
| G1 | — | — | Must be zero behavior delta | `NOT_STARTED` |
| G2 | — | — | — | `NOT_STARTED` |
| G3 | — | — | — | `NOT_STARTED` |
| G4 | — | — | — | `NOT_STARTED` |
| G5 | — | — | — | `NOT_STARTED` |
| G6 | — | — | — | `NOT_STARTED` |
| G7 | — | — | — | `NOT_STARTED` |
| G8 | — | — | — | `NOT_STARTED` |

## Legacy compatibility retirement checklist

Nothing may be checked complete without query/test evidence recorded in the
verification log.

- [ ] Legacy report consumer inventory is complete and reviewed.
- [ ] Product report API is authoritative shared-only for new reports.
- [ ] Product `/today` either requires shared authority or uses an explicit
      historical read adapter.
- [ ] Public v1 role-view API either requires shared authority or uses an
      explicit historical read adapter.
- [ ] Report CLI consumes only the authoritative versioned read model.
- [ ] Demo CLI/read models consume only the authoritative versioned read model.
- [ ] Reference client is migrated and contract-tested.
- [ ] SQL views/functions and technical trace no longer require a legacy write.
- [ ] Tests/fixtures are classified as historical-read, dual-run comparison, or
      retired-write coverage.
- [ ] No current caller depends on a compatibility operation ID.
- [ ] No current caller depends on `product_agent_result.v1` for a newly created
      episode.
- [ ] Shadow comparison cannot create HITL, delivery intent, email, or any other
      external effect.
- [ ] Historical legacy role reports remain readable through an explicit,
      non-authoritative adapter.
- [ ] CareAction creation rejects legacy/shadow source authority.
- [ ] `legacy_report_operation_created_count` is observed at zero for the agreed
      complete regression window.
- [ ] New compatibility operation count is zero for the same window.
- [ ] Queue grants/config no longer expose a claimable legacy path.
- [ ] Legacy `ProductAgentProcessor.prepare` has no production caller.
- [ ] Compatibility complete/failure orchestration has no production caller.
- [ ] Legacy-only SQL indexes/functions have consumer-zero evidence.
- [ ] Historical retention/audit requirements have been reviewed before any
      table or row deletion.
- [ ] Final deletion is split from read migration and has a tested rollback or
      forward-fix strategy.

## G0 closure

- Master plan read in full: yes.
- Required seven investigation areas inspected: yes.
- Baseline verification run without fixes: yes.
- Production code changed in G0: no.
- Master plan rewritten: no.
- G1 begun: no.

## G1 execution record

`G1_STATUS = PASS_WITH_LIMITATIONS`

### G1 pre-existing dirty-worktree baseline

This section is the first G1-owned incremental change to this pre-existing
untracked ledger. The ledger content before this heading is authoritative G0
user work and has SHA-256
`28797d13023b92dc65f1bceff514c845f3f1b4afb2b746db1938c340983590e5`.

- Capture time: 2026-08-30 (Asia/Shanghai)
- HEAD: `51947e6da69d9d8e0e0eeb36669e227865fc26ff`
- Branch: `agent/repository-structure-cleanup`, 13 commits ahead of upstream
- Index: clean; cached diff SHA-256
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- Tracked working-tree diff: 19 modified files, 2,163 insertions and 79
  deletions; binary patch captured before G1 at
  `/tmp/sleepagent-g1-preexisting.patch`, SHA-256
  `801fb46331429b457e38a641ecebf6523dd9f77b1324db6923294bcd46cdbd04`
- Untracked baseline: the master plan and this execution ledger; master-plan
  SHA-256
  `1bc1592b16676e79a6d835b0402d5427bded81d483e23b47dca1b727af6482f2`.

Every path below is classified `PRE_EXISTING_DIRTY_BASELINE`:

| Path | Baseline status | Baseline numstat (+/-) |
|---|---|---:|
| `PLAN-REVIEW-LOG.md` | modified tracked | 71/0 |
| `sleepagent/api/postgres.py` | modified tracked | 57/13 |
| `sleepagent/api/product_contracts.py` | modified tracked | 8/2 |
| `sleepagent/domain/product_data.py` | modified tracked | 325/0 |
| `sleepagent/report_cli.py` | modified tracked | 98/4 |
| `sleepagent/runtime/agent_invocation_coordinator.py` | modified tracked | 9/1 |
| `sleepagent/runtime/agents.py` | modified tracked | 268/6 |
| `sleepagent/runtime/contracts.py` | modified tracked | 6/1 |
| `sleepagent/runtime/deterministic_model.py` | modified tracked | 46/0 |
| `sleepagent/runtime/governance.py` | modified tracked | 34/2 |
| `sleepagent/runtime/reports.py` | modified tracked | 655/25 |
| `sleepagent/runtime/runner.py` | modified tracked | 32/1 |
| `sleepagent/workers/product.py` | modified tracked | 60/2 |
| `tests/unit/test_product_agent_runner.py` | modified tracked | 188/11 |
| `tests/unit/test_product_data_tools.py` | modified tracked | 143/1 |
| `tests/unit/test_product_elder_narrative_worker.py` | modified tracked | 26/2 |
| `tests/unit/test_product_report_contracts.py` | modified tracked | 47/7 |
| `tests/unit/test_product_sleep_api.py` | modified tracked | 4/1 |
| `tests/unit/test_report_cli.py` | modified tracked | 86/0 |
| `SLEEPAGENT_REMEDIATION_AND_CLOSURE_PLAN.md` | untracked | complete file, 1,476 lines |
| `docs/remediation/EXECUTION_LEDGER.md` | untracked | complete pre-G1 file, 596 lines |

For overlapping files, ownership is:

```text
PRE_EXISTING_DIFF (the baseline above)
+
G1_INCREMENTAL_DIFF (only explicitly identified G1 additions)
```

G1 will not reset, revert, stash, clean, overwrite, or silently claim any
baseline change. Selective isolation will be evaluated after verification.

### G1 ADRs frozen

All are accepted remediation intent; none activates its future migration:

- `docs/remediation/adr/ADR-001-canonical-observation-boundary.md`
- `docs/remediation/adr/ADR-002-movement-semantic-separation.md`
- `docs/remediation/adr/ADR-003-report-authority.md`
- `docs/remediation/adr/ADR-004-external-effect-authority.md`
- `docs/remediation/adr/ADR-005-time-authority.md`
- `docs/remediation/adr/ADR-006-dependency-direction.md`
- `docs/remediation/adr/ADR-007-night-finalization.md`

### G1 feature-switch contract

The existing authoritative `SleepBackendSettings` now owns these typed,
fail-closed fields. The fields are configuration/observability contracts only;
no production migration branch consumes a future state in G1.

| Field / environment variable | Type / accepted values | Default |
|---|---|---|
| `observation_semantics_version` / `SLEEPAGENT_BACKEND_OBSERVATION_SEMANTICS_VERSION` | `ObservationSemanticsVersion`: `v1`, `v2` | `v1` |
| `report_pipeline_mode` / `SLEEPAGENT_BACKEND_REPORT_PIPELINE_MODE` | `ReportPipelineMode`: `legacy`, `shared_compat`, `shadow`, `shared_only` | `shared_compat` |
| `emit_legacy_report_compatibility` / `SLEEPAGENT_BACKEND_EMIT_LEGACY_REPORT_COMPATIBILITY` | strict bool; environment text exactly `true` or `false` | `true` |
| `acquisition_scheduler_enabled` / `SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED` | strict bool; environment text exactly `true` or `false` | `false` |
| `live_delivery_enabled` / `SLEEPAGENT_BACKEND_LIVE_DELIVERY_ENABLED` | strict bool; environment text exactly `true` or `false` | `false` |

Unprefixed lookalike variables are ignored rather than reinterpreted. Invalid
enum or boolean values fail settings construction. Defaults preserve current
shared-primary plus compatibility behavior and do not activate Movement V2,
scheduling, shared-only reporting, or live delivery.

### G1 characterization evidence

Fixture: `tests/fixtures/remediation/g1_characterization.json`.

Tests: `tests/unit/test_g1_characterization.py`.

- `legacy_characterization` records history/realtime `body_shake=39.9` and a
  SleepReport hourly `count=2` as the same `movement_payload.v1` unit `index`.
  The current `known_semantic_gap` average is `20.9`; it was not corrected.
- Equivalent valid movement through Push and Pull produces the same canonical
  payload as Replay. Push/Pull invoke no ontology validator; Replay invokes it.
- Pull accepts a vendor-derived SleepReport movement count that equivalent
  Replay rejects because v1 ontology requires device-measured movement.
- An Asia/Shanghai event at `17:35Z` belongs to local date 2026-07-10 and clock
  01:35. Current `deterministic_night_summary` labels it `local_time=17:35`,
  making the known UTC-as-local display defect executable evidence. A sleep
  stage window across local midnight is retained as UTC authority.
- An English accepted EvidenceClaim is retained in
  `SharedNightAnalysis.summary_lines` and copied under the Chinese Family
  heading, making localization leakage observable without changing rendering.
- Existing full-suite report/worker tests continue to characterize the current
  report operation DAG, compatibility copy, urgent zero-model path, provider
  counts, and result identities.

### G1 legacy/shared report inventory

`docs/remediation/LEGACY_SHARED_REPORT_CONSUMERS.md` is the authoritative
inventory. It covers Product report/today/public APIs, Demo API/CLI, report CLI,
reference client, automatic and explicit operation creation, worker routing,
legacy and compatibility processors, shared commit, read models, applied SQL
functions/views, simulation journey, and unit/PostgreSQL fixtures. Every entry
uses one of `LEGACY_ONLY`, `COMPAT_ONLY`, `DUAL_READ`, `SHARED_READY`, or
`RETIREMENT_UNKNOWN` and names exact evidence plus a cutover condition.

No consumer was removed.

### G1 architecture no-growth baseline

- Human-readable authority:
  `docs/remediation/ARCHITECTURE_NO_GROWTH_BASELINE.md`.
- Machine-readable authority:
  `tests/fixtures/remediation/architecture_import_baseline.json`, SHA-256
  `d3dd8d467cb1a036eea757a52c24ce9d762fda0167bd2142f7cb5d4770739110`.
- Executable guard:
  `tests/architecture/test_remediation_dependency_no_growth.py`.

The six current SCCs are frozen as three multi-module SCCs (14-module
app/process/worker/ingress, eight-module runtime, and two-module
`domain.product_data`/`runtime.tools`) plus standalone self cycles in `config`,
`runtime.agents`, and `runtime.knowledge`. Six exact self-import edges and 11
forbidden-direction edges are recorded. The guard permits debt to split/shrink
but fails for a new SCC, self-import, domain-to-runtime/worker/infrastructure
edge, `process -> app` edge, or worker-runtime-to-concrete-worker edge. A
synthetic test proves rejection of both a new cycle and forbidden edge.

No existing import was moved or repaired.

### G1 OpenAPI drift classification

Full evidence: `docs/remediation/OPENAPI_DRIFT_CLASSIFICATION.md`.

| Group | Classification | Exact diff |
|---|---|---|
| Product personalization | `STALE_SNAPSHOT`; current API is `INTENTIONAL_CURRENT_API` | 7 added paths, 12 added schemas |
| Product report API | `STALE_SNAPSHOT`; current API is `INTENTIONAL_CURRENT_API` | 3 added paths, 11 added schemas |
| Demo technical trace | `STALE_SNAPSHOT`; current API is `INTENTIONAL_CURRENT_API` | 1 added path, 1 added schema |

There are no removed or changed existing routes/schemas and no identified
`UNINTENDED_API_DRIFT`. The canonical check still fails for both snapshots.
Snapshots were not refreshed because current generation would also absorb
pre-existing dirty Elder/report-contract changes. API behavior was not changed
to satisfy an old snapshot.

### G1 verification evidence

| Verification | Result | Notes |
|---|---|---|
| Focused G1 settings, characterization, architecture | PASS | 18 passed in 4.12s |
| Architecture suite | PASS | 5 passed, including synthetic rejection and debt-shrink cases |
| Unit-marked suite | PASS | 1,047 passed, 35 deselected in 19.55s; local loopback permission used |
| Full non-PostgreSQL/non-E2E/non-ASGI-lifespan suite | PASS | 1,044 passed, 38 deselected in 19.10s; local loopback permission used |
| Python 3.13 compileall | PASS | `sleepagent`, `reference_client`, `scripts`, `tests` |
| Supported Python 3.11.15 compileall | PASS | Same source/test roots via `sleepagent-p3` environment |
| Supported Python 3.11.15 import smoke | PASS | app, process, config, domain, workers, Push, Pull, Replay; new enum defaults observed |
| `git diff --check` | PASS | No whitespace errors |
| OpenAPI canonical snapshot check | FAIL (known baseline) | Both snapshots; classified above, not overwritten |
| PostgreSQL migration check | `ENV_BLOCKED` | Refused because no PostgreSQL database URL was present |
| PostgreSQL/integration/process E2E | `ENV_BLOCKED` | No database URL/service; no persistence evidence fabricated |
| Mypy | `ENV_BLOCKED` | Declared but unavailable; no unrelated installation attempted |

The first sandboxed broad/unit runs each reported the same eight loopback bind
failures (`PermissionError`). All eight passed when rerun with local
`127.0.0.1` permission; the clean reruns above are authoritative.

No external LLM/Perceptor provider call, production operation, production
schedule, delivery intent, email/SMS, or real effect occurred. The loopback
provider tests use a local scripted server. G1 changes no report operation
count, queue route, compatibility emission, or external-effect authority.

### G1 deviations and limitations

- The attached G1 work order explicitly required ADRs under
  `docs/remediation/adr/`, superseding the master plan's suggested
  `docs/architecture/adr/` path. Seven focused ADRs cover both instruction sets.
- The G0 correction `report_pipeline_mode=shared_compat` was adopted rather
  than defaulting to the master plan's stale `legacy` label.
- OpenAPI snapshots remain failing instead of being regenerated across the
  dirty report-contract baseline.
- PostgreSQL and mypy limitations remain environmental, not passes.
- Python tests ran under available 3.13.9; supported Python 3.11.15 provided
  compile/import proof but its environment lacks pytest.

### G1 git isolation and G1-owned diff

`PLAN-REVIEW-LOG.md` was already modified before G1: its complete pre-G1
working-tree content is 492 lines with SHA-256
`dc9bb7bb980c85154bd327c3df9b43b7b68c323228db6262b364326a9e56c72e`.
Only the later appended `Act 3 — Build / G1` section is G1-owned.

This ledger was an untracked 596-line pre-existing file. Its pre-existing hash
and the G1 append boundary are recorded at the start of this G1 record. Thus
both overlapping documents use:

```text
PRE_EXISTING_DIFF
+
G1_INCREMENTAL_DIFF
```

All other G1-owned paths are `sleepagent/config.py`, the seven ADRs, the three
remediation evidence documents, two remediation fixtures, the architecture
guard, and two focused unit test files. No pre-existing dirty production/report
or unit-test file was edited by G1. The final binary diff of those exact 18
baseline paths has SHA-256
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`,
byte-identical to the same path subset in the pre-G1 captured patch.

`G1_GIT_RESULT = COMMIT_BLOCKED_BY_PREEXISTING_DIRTY_WORKTREE`

A selective commit would leave characterization tests depending on the
uncommitted authoritative report baseline and would split the untracked master
plan/ledger provenance. No file was staged, no commit was created, and nothing
was pushed.

### G2 entry criteria

`G2_SAFE_TO_BEGIN = NO`

Before the combined M1-M3 Observation Semantics goal begins:

1. Preserve or reconcile the 19 pre-existing modified files and the untracked
   master plan/ledger so G1 and user-owned reporting work have reviewable
   history without contamination.
2. Provide an isolated PostgreSQL URL/service and pass migration attestation
   plus the relevant PostgreSQL baseline before M3 persistence/upcasting.
3. Reconcile the two stale OpenAPI snapshots from a clean, reviewed report API
   baseline, or explicitly accept the classified failure as a pinned external
   contract baseline before any later API-affecting work.
4. Keep every G1 default and no-growth guard active; do not enable `v2`, a
   scheduler, shared-only reporting, or live delivery as part of entry cleanup.

After these prerequisites, G2 must start with M1 semantic registry/contracts,
then M2 canonical-path convergence, then M3 aggregation/upcasting. G1 does not
begin any of them.

### G1 supplemental verification — supported Python 3.11 baseline

The original G1 evidence above is retained as execution history. A later G1
verification pass safely installed the repository-declared, hash-locked
`requirements/dev.lock` into an isolated `/tmp` virtual environment using
`/home/wz/miniconda3/envs/sleepagent-p3/bin/python` (Python 3.11.15). The
authoritative tools were pytest 9.1.1 and mypy 2.3.0.

| Verification | Result | Notes |
|---|---|---|
| Python 3.11 compileall | PASS | `sleepagent`, `reference_client`, `scripts`, and `tests` |
| Python 3.11 import smoke | PASS | app, process, config, domain, workers, Perceptor Push/Pull, and Replay imports |
| Focused G1/report safeguard selection | PASS | 46 passed in 3.60s |
| Architecture suite | PASS | 5 passed in 3.44s |
| Unit-marked suite | PASS | 1,047 passed, 35 deselected in 20.88s |
| Non-PostgreSQL/non-E2E/non-ASGI-lifespan suite | PASS | 1,044 passed, 38 deselected in 20.27s |
| Mypy | FAIL (known existing baseline) | 86 errors in 20 files; archived clean HEAD already has 63 errors in 20 files, and the additional errors are in the pre-existing dirty report/runtime baseline, not `sleepagent/config.py` |
| OpenAPI canonical snapshot check | FAIL (known stale snapshot) | Both snapshots remain at their pre-G1 hashes and were not overwritten |
| PostgreSQL migration/runtime verification | `ENV_BLOCKED` | No database URL; no PostgreSQL binaries on `PATH`; Docker socket is inaccessible to the current uid/gid even with approved daemon access |

The first broad Python 3.11 runs reproduced eight `PermissionError` failures
solely because the sandbox denied a scripted server's `127.0.0.1` bind. The
same selections passed cleanly with approved localhost permission; no external
provider was contacted. The exact 18-file pre-G1 patch remains byte-identical
at SHA-256
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`.
This addendum changes neither `G1_GIT_RESULT` nor `G2_SAFE_TO_BEGIN`.

## G1.5 — Baseline consolidation and verification readiness

`G1_5_STATUS = PASS_WITH_LIMITATIONS`

G1.5 performs no M1, M2, or M3 behavior migration. It consolidates the
preserved pre-G1 work, G1 safeguards, remediation records, and deterministic
OpenAPI reconciliation into one reviewable pre-G2 checkpoint.

### Checkpoint composition and provenance

The exact 40-path checkpoint allowlist is recorded in
`docs/remediation/G1_5_CHECKPOINT_COMPOSITION.md`:

| Classification | Paths | Meaning |
|---|---:|---|
| `PRE_G1_WORK` | 18 | Frozen user-owned report/runtime/test patch |
| `G1_SAFEGUARD` | 6 | Default-preserving settings, characterization, fixtures, and architecture guard |
| `REMEDIATION_DOC` | 13 | Master plan, ledger, ADRs, inventories, baselines, and checkpoint manifest |
| `OTHER_INTENTIONAL_BASELINE` | 3 | Build log and two canonical OpenAPI snapshots |

The 18-path frozen patch remains byte-identical at SHA-256
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`.
Ignored environments, caches, secrets, key material, generated junk, local
attachments, and all unlisted paths are excluded.

`CHECKPOINT_RESULT = PASS`

`CHECKPOINT_COMMIT_SHA = 0aa1674653da93e135572b06f858cd0d18f41926`

`CHECKPOINT_TREE_SHA = 3639228c82af4509c885ec23dc15459630b74eae`

The checkpoint has parent `51947e6da69d9d8e0e0eeb36669e227865fc26ff`
and subject `checkpoint(remediation): freeze pre-g2 baseline`. This hash is
recorded by the required documentation-only follow-up commit because including
a commit's own hash in its tree is impossible by construction.

### OpenAPI reconciliation

The current candidate repeated G1's structural result exactly: 10 added
backend paths and 23 added backend schemas, plus one added Demo path and one
added Demo schema; zero existing paths/schemas were removed or changed. The
existing generator then refreshed both canonical files without changing API
runtime behavior.

| Snapshot | Reconciled SHA-256 | Gate |
|---|---|---|
| `backend-bff-v1.json` | `4c5c87124b5db77cf5c54364ceb2c117e1252ed39a107787d7934e478fa76e5a` | PASS |
| `demo-v1.json` | `c1b3d32a0e8acc98f04b69b8cc42e170cefd2d5e979e10772d8317215cac817a` | PASS |

`OPENAPI_BASELINE = PASS`

### Authoritative Python 3.11 baseline

- Executable: `/tmp/sleepagent-g1_5-py311/bin/python`.
- Version: Python 3.11.15, matching `requires-python = ">=3.11,<3.12"`.
- Environment source: isolated virtual environment created from the existing
  `sleepagent-p3` Python 3.11 interpreter.
- Dependency authority: `requirements/dev.lock`, installed with
  `--require-hashes --only-binary=:all:`.
- Tool versions: pytest 9.1.1; mypy 2.3.0.

| Verification | Result |
|---|---|
| Compileall and import smoke | PASS |
| Focused G1/report safeguards | 46 passed |
| Architecture suite | 5 passed |
| Unit-marked suite | 1,047 passed; 35 deselected |
| Non-PostgreSQL/non-E2E/non-ASGI suite | 1,044 passed; 38 deselected |
| Broader non-PostgreSQL/non-E2E suite | 1,047 passed; 35 deselected |
| Canonical OpenAPI check | PASS |

Eight sandbox-only `127.0.0.1` bind denials in initial broad runs disappeared
under approved localhost permission. The tests use a scripted local server;
no external provider was called.

`PYTHON_311_BASELINE = PASS`

### PostgreSQL isolation and verification

The repository-supported isolation path is valid and remains the required M3
path: `compose.yaml` defines `postgres:16`, a dedicated
`sleepagent_replay_test` database, distinct migration/API/Demo/worker test
roles, host port 15432, and a dedicated Compose volume. The public
`.env.test.example` contains test-only placeholders, and
`docker compose --env-file .env.test.example config --quiet` passes.

Static release verification passes for all 13 immutable migrations through
version 13. The migration manifest SHA-256 is
`c027b2a2e7828713422770d14e171a795c94b32e716b5eb077e33bd610880a06`.

Actual database execution is unavailable: no `psql`, `postgres`, or
`pg_isready` binary is on `PATH`, and `/var/run/docker.sock` is owned by
`nobody:nogroup` while the current process is uid/gid 1018. Approved
`docker info` access still returns permission denied. Therefore no migration
application, PostgreSQL-marked test, persistence integration test, or
worker/database smoke is claimed.

`POSTGRESQL_BASELINE = ENV_BLOCKED`

### Mypy/tooling result

Mypy is an explicit development dependency and ran under Python 3.11. It
reports 86 errors in 20 files. An isolated archive of clean HEAD independently
reports 63 errors in the same 20-file count; the working-tree delta is in the
pre-existing dirty report/runtime baseline, and no error is in G1's sole
production file, `sleepagent/config.py`. Broad typing cleanup is outside G1.5.

`MYPY_TOOLING_STATUS = MYPY_EXISTING_FAILURE_BASELINE`

### G1 safeguard re-verification

- Feature defaults remain v1, `shared_compat`, compatibility enabled,
  scheduler disabled, and live delivery disabled.
- Mixed movement aggregation, Push/Pull/Replay validator mismatch,
  UTC-as-local behavior, and English-in-Chinese leakage remain reproducible.
- The architecture suite reports no new SCC, self-import, or forbidden edge.
- The report consumer inventory is unchanged; no consumer was removed or
  migrated.
- No Movement V2, canonical factory, semantic convergence, aggregation fix,
  upcaster, timezone/renderer repair, report cutover, scheduler, finalization,
  delivery, external effect, or business migration was implemented.

### Remaining limitations and G2 readiness

- Mypy has a recorded existing failure baseline rather than a passing gate.
- PostgreSQL runtime verification remains environment-blocked.

`G2_M1_SAFE_TO_BEGIN = YES`

M1 is bounded to contracts/registry/fixtures and has authoritative Python 3.11
and no-growth proof.

`G2_M2_SAFE_TO_BEGIN = YES`

M2 may proceed as a bounded factory/adapter convergence goal with the existing
feature default held at v1 and the non-PostgreSQL parity suite authoritative.
It must not absorb M3 persistence or aggregation work.

`G2_M3_SAFE_TO_BEGIN = NO`

M3 requires actual migration application and PostgreSQL persistence evidence,
which this environment cannot provide.

`G2_SAFE_TO_BEGIN = NO`

The coherent recommendation is `G2A = M1-M2`, followed by PostgreSQL readiness
and then `G2B = M3`. G1.5 does not begin either goal.

## G2A — M1-M2 Observation Semantics V2 and canonical ingestion convergence

`G2A_STATUS = PASS_WITH_LIMITATIONS`

G2A implements only M1 and M2. Observation semantic definition and ingestion
convergence are complete; persisted analytics migration remains deferred to
G2B/M3. The PostgreSQL limitation does not weaken the deterministic M1/M2
proof, but it prevents G2B and therefore remains explicit.

### Entry baseline and scope

- Entry HEAD: `cfa79ef46bb10a57dd9b83360fc1d5e00aae005d`
  (`docs(remediation): attest g1.5 checkpoint`).
- Entry worktree: clean; branch 15 commits ahead of upstream.
- Implemented scope: M1 movement contracts/registry and M2 shared canonical
  factory plus Push/Pull/Replay feature-gated convergence.
- Excluded scope: every M3 migration, persisted semantic cutover, historical
  upcast, aggregation/trend/risk/CareStrategy migration, and report change.

### Exact Observation Semantics V2 movement contract

The domain contract is `movement_payload.v2` under
`observation_semantics.v2` and carries `metric_id`, numeric value, semantic
unit, optional/required UTC aggregation boundaries, and preserved vendor
semantic code.

| Metric ID | Unit | Value | Window | Allowed source | Trusted V2 analytics |
|---|---|---|---|---|---|
| `movement_index` | `vendor_index` | non-negative int/float | optional | `device_measured` | yes |
| `movement_event_count` | `count` | non-negative integer semantic value | both boundaries required; end after start | `vendor_derived` | yes |
| `legacy_ambiguous_movement` | `legacy_unknown` | non-negative numeric audit value | not inferred | device or vendor historical evidence | no; rejected from new V2 ingestion |

`legacy_ambiguous_movement` is an audit/compatibility identity only. It cannot
enter trusted V2 aggregation, trends, risk, or recommendations. No historical
row was classified or rewritten in G2A.

### Semantic registry and error contract

`sleepagent/domain/observation_semantics.py` is the minimal authoritative
movement registry. Each definition owns metric identity, allowed units,
allowed source kinds, window requirement, payload schema, semantic version,
and trusted-analytics status. Unit alone cannot turn one movement metric into
another.

The single rejection type `CanonicalObservationRejected` exposes these stable
categories: `invalid_schema`, `unsupported_metric`, `invalid_unit`,
`invalid_value`, `missing_aggregation_window`,
`invalid_aggregation_window`, `invalid_source_provenance`,
`invalid_timestamp`, and `unsupported_vendor_semantics`.

### Canonical factory authority and identity

`CanonicalObservationFactoryV2` in
`sleepagent/domain/canonical_observation.py` is the one explicit-V2 authority.
It owns versioned schema acceptance, metric/unit/value/window validation,
source/provenance validation, authoritative UTC normalization, semantic
identity, ontology/normalizer versions, and trusted-analytics classification.

The V2 canonical result separates a transport receipt identity from a
SHA-256 semantic identity. The latter excludes adapter-specific receipt IDs
and formatting, but includes provider/device semantic scope, metric, unit,
value, authoritative instant, aggregation window, source kind, and normalized
vendor semantic code. Equivalent Push/Pull/Replay facts therefore share a
semantic identity while retaining distinct provenance.

Current persistence accepts only `sleep_observation.v1`. The factory therefore
returns a clearly named `compatibility_candidate` after V2 has accepted the
fact. That projection is the only input to the unchanged persistence path; it
does not revalidate, fall back, or mutate V2 authority. Persisting V2 metric
columns/payloads is an explicit M3 prerequisite.

### Push/Pull/Replay convergence and provenance decision

Production worker composition passes the typed
`observation_semantics_version` into live Push/Pull normalization, replay
journey adaptation, and replay normalization.

```text
v1 -> unchanged adapter/candidate/legacy ontology behavior
v2 -> transport field mapping -> CanonicalObservationFactoryV2
      -> explicit v1 persistence compatibility candidate
```

Transport mappings are:

- Push `BodyShake` -> `movement_index/vendor_index`, vendor semantic code
  `perceptor.body_shake.index`, source `device_measured`.
- Pull realtime/history `body_shake` -> the same movement-index identity.
- Pull SleepReport `{hour,count}` ->
  `movement_event_count/count`, source `vendor_derived`, explicit one-hour UTC
  aggregation interval.
- Replay `replay_observation_input.v2` carries the explicit V2 movement
  payload. Generated replay movement is mapped to the same Perceptor movement
  index identity; a replay event count is accepted only with its explicit
  count window and vendor-derived provenance.
- Pull SleepReport `{time_long,value}` movement has no proved vendor meaning
  in the current contract and is rejected as `unsupported_vendor_semantics`.
  It is not guessed into index or count.

The G1 provenance mismatch is resolved by accepting a proved vendor-derived
hourly count in both Pull and Replay. `movement_index` remains device-measured;
`movement_event_count` remains vendor-derived. Push has no documented count
field, so count acceptance is not invented for Push. Invalid count-window,
metric/unit, and source combinations reach the same factory/category after
transport decoding.

### Feature-gate and V1 compatibility evidence

- The authoritative default remains
  `observation_semantics_version=ObservationSemanticsVersion.V1`.
- Explicit V1 continues to construct `movement_payload.v1` with unit `index`
  and retains Replay's legacy ontology behavior.
- Explicit V2 creates `replay_observation_input.v2` and invokes the shared
  factory for all three paths.
- Unsupported setting values still fail enum/settings construction.
- A V2 factory rejection propagates; no V1 retry/fallback exists.
- `tests/unit/test_g1_characterization.py` remains unchanged and passing.
- The known V1 mixed movement result remains exactly `20.9`.

### M3 scope intentionally deferred

G2A adds no migration and changes no Product aggregation, persisted canonical
schema, historical row, trend cohort, risk policy, care recommendation, or
report semantic. It does not claim the product-level movement defect is fully
fixed. The compatibility candidate can still be persisted as legacy Movement
until M3 creates and verifies the additive V2 storage/read boundary.

### Verification evidence — Python 3.11.15

Authoritative executable: `/tmp/sleepagent-g1_5-py311/bin/python`.

| Verification | Result |
|---|---|
| G2A contract/factory/parity/feature tests | 24 passed |
| Focused G2A + G1 + Perceptor + Pull + Replay + architecture selection | 190 passed in 6.04s |
| G1 settings/characterization transition selection | 36 passed in 0.80s |
| Unit-marked suite | 1,071 passed; 35 deselected in 20.65s |
| Non-PostgreSQL/non-E2E/non-ASGI-lifespan suite | 1,068 passed; 38 deselected in 20.75s |
| Full architecture suite | 5 passed in 3.34s |
| OpenAPI canonical snapshot check | PASS; no snapshot changed |
| Compileall | PASS for `sleepagent`, `reference_client`, `scripts`, `tests` |
| Import smoke | PASS for settings, registry, factory, Push, Pull, Replay |
| New semantic/factory mypy isolation | PASS with `--follow-imports=skip` |
| Repository mypy | existing failure baseline remains; no broad typing cleanup |
| `git diff --check` | PASS |

The first sandboxed unit run reproduced the same eight local-loopback
`PermissionError` failures recorded in G1/G1.5. The authoritative rerun with
permission for the scripted `127.0.0.1` server passed; no external provider was
contacted.

### Architecture baseline comparison

The executable snapshot remains at three grandfathered multi-module SCCs plus
the standalone self-cycle baseline, six exact self-import edges, and 11 exact
forbidden-direction edges. `new_debt` is empty for SCCs, self-imports, and
forbidden edges. No M10-M11 cleanup was performed.

The master plan suggested a future `application/observations/` package. G2A
instead places the pure factory beside the current domain contracts because no
application root exists and importing a new upper-layer factory from the
existing replay persistence seam would worsen dependency direction. This is a
material but plan-consistent implementation deviation; M10-M11 may move the
boundary behind an application port after the current SCC cleanup.

### PostgreSQL and G2B entry

`POSTGRESQL_STATUS = ENV_BLOCKED`

G2A did not attempt Docker repair, native installation, service mutation, or a
database migration. The G1.5 evidence remains authoritative: no PostgreSQL
client/server is available and the Docker socket is inaccessible.

Exact G2B/M3 prerequisites:

1. Provide an isolated PostgreSQL 16 test service/URL and verify all immutable
   migrations through version 13 before adding the next migration.
2. Design and apply an additive V2 persistence schema for metric ID, semantic
   unit, aggregation boundaries, semantic/ontology/normalizer versions, and
   ambiguity status without rewriting migrations 001-013.
3. Implement evidence-based historical upcasting; unprovable Movement becomes
   `legacy_ambiguous_movement`, never guessed.
4. Cut Product aggregation, trends, risk, and CareStrategy to explicit
   compatible metrics, with `movement_index` and event counts never mixed.
5. Run migration application/rollback-forward checks, PostgreSQL ingestion and
   deduplication tests, historical-read compatibility, and affected Product
   integration/process suites.
6. Only after that evidence, retire the V1 persistence compatibility projection
   for new V2 observations.

### Git isolation

All G2A changes began from the clean authoritative baseline and are isolated
for one local commit with subject
`remediation(g2a): add canonical observation semantics v2`. The commit hash is
reported in the G2A handoff because a commit cannot contain its own hash. No
push is authorized or performed.

## G2B-Preflight — PostgreSQL 16 verification readiness

`G2B_PREFLIGHT_STATUS = PASS`

### Entry and blocker diagnosis

- Entry HEAD was the required
  `29700bd2c15544f7a04541b9c8793436bca8887f`; the worktree was clean and the
  branch was 16 commits ahead of upstream.
- Docker 29.3.0 and Compose 5.1.1 are installed and the default context points
  to `unix:///var/run/docker.sock`. The socket is mode `0660`, owned by
  `nobody:nogroup` (uid/gid 65534), while the process is uid/gid 1018.
  `docker info` therefore fails with permission denied. Classification:
  `DOCKER_SOCKET_PERMISSION`; no host permission, group, daemon, service, or
  firewall mutation was attempted.
- The earlier binary diagnosis was incomplete because PostgreSQL was absent
  from `PATH`. A user-owned PostgreSQL 16 distribution exists at
  `/mnt/data4/wz/.sleepagent/pg16/bin`, so Docker is not required for this
  preflight.

### Runtime and isolation

- Actual server: PostgreSQL 16.14, 64-bit, user-owned native distribution.
- Verification cluster: unique `/tmp/sleepagent-g2b-preflight.*` data and
  socket directories, owned by uid 1018; the unrelated pre-existing demo
  cluster on port 55448 was not touched.
- Network: exact `listen_addresses=127.0.0.1`, port 15432, private Unix socket
  directory with mode 0700, then loopback TCP authentication upgraded to
  SCRAM-SHA-256.
- Database: `sleepagent_replay_test`. Redacted admin DSN:
  `postgresql://sleepagent_test_migration:***@127.0.0.1:15432/sleepagent_replay_test`.
- Roles: migration owner plus distinct API, Demo, and Worker login roles. The
  three runtime roles have no superuser, createdb, createrole, replication, or
  bypass-RLS capability. All four public test credentials authenticated over
  SCRAM.
- The Compose example now binds PostgreSQL explicitly to loopback. Static
  `docker compose --env-file .env.test.example config --quiet` passes.

### Fresh migration proof

The dedicated database was dropped/recreated, then the unchanged repository
migration runner applied 001 through 013 from zero. `apply`, `status`, and
`check` each reported schema version 013. The ledger contains exactly 13
contiguous applied rows (min 1, max 13) and 13 distinct SQL checksums. The
manifest SHA-256 remains
`c027b2a2e7828713422770d14e171a795c94b32e716b5eb077e33bd610880a06`.
No historical migration changed and no manual repair or migration 014 was
created.

Post-application catalogs contain 101 forced-RLS tables, 102 policies, and 49
non-internal triggers. The command, delivery, scenario-clock, Perceptor Push,
Perceptor Pull, and Pull history-planner functions are present. A final
post-test migration check remains clean at 013.

### Existing PostgreSQL baseline

The first full run found two `TEST_CONFIGURATION_ERROR` failures: the two
Perceptor PostgreSQL fixtures inserted grants for hard-coded principals absent
from the canonical test bootstrap. The minimal test-only repair makes both
fixtures use `SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL` and
`SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL`, matching the rest of the suite.
The focused rerun passed 2/2.

The authoritative run used a fresh database, migrations 001-013, canonical
test bootstrap, loopback-only networking, and SCRAM credentials:

- PostgreSQL marker: 32 passed, 1 skipped, 1,073 deselected in 23.90s.
- The one skip is the existing completed-process evidence reader, which
  requires `SLEEPAGENT_FIRST_SLICE_ROOT_OPERATION_ID`; it is not runnable on
  an otherwise fresh database and is not an environment failure.
- A deliberate second run without reset reproduced durable-state collisions.
  The contract therefore requires a database reset before the full marker.

`POSTGRES_TEST_BASELINE = PASS`

### G2A and architecture re-verification

- Observation Semantics V2 plus G1 characterization: 29 passed.
- Architecture suite: 5 passed; the G1 no-growth baseline remains unchanged.
- V1 remains the default; no V2 persistence, migration, upcast, aggregation,
  trend, risk, CareStrategy, report, scheduler, DeviceBinding, or delivery
  work was performed.
- `git diff --check` passes and all migrations 001-013 retain their recorded
  hashes.

### Reproducibility, cleanup, and readiness

The authoritative Compose and verified native start/readiness/reset/migrate/
bootstrap/test/stop procedures are recorded in
`docs/remediation/POSTGRESQL_VERIFICATION_BASELINE.md`. Docker remains
permission-blocked for this uid, but the native path requires no privileged
host change and supplies actual PostgreSQL 16 evidence.

The temporary verification server is stopped during final cleanup; the
one-command native restart and the canonical Compose procedure are both in the
contract. No environment-generated data, credentials, or key material is
committed.

`POSTGRES16_RUNTIME_READY = YES`

`MIGRATION_BASELINE_READY = YES`

`POSTGRES_TEST_BASELINE_READY = YES`

`G2B_M3_SAFE_TO_BEGIN = YES`

G2B-Preflight stops here. M3 is not started.

## G2B — M3 persisted Observation Semantics V2 and metric-safe analytics

`G2B_M3_STATUS = PASS`

### Entry, scope, and persistence authority

- Entry was the exact clean G2B-Preflight commit
  `3717c07d052feba42b3ecf4c912059a50a97eeb2`.
- M3 adds only migration `014_observation_semantics_v2.sql`; migrations
  001-013 retain the hashes recorded above. Migration 014 is
  `96756b31a3515b82756ef2ab0c30b432a6b259a185000993002bfacbc661a368`.
  The 014 manifest is
  `ed1afd6c4ba76673d1530d4a3e988eba485c56f85b5a5d9573d38e4f2c52ba80`.
- `sleep_domain_observation_semantics_v2` is an immutable, forced-RLS sidecar
  keyed to the exact canonical observation/namespace/mode/subject. It is the
  sole V2 semantic and analytic authority. The existing observation JSON is
  retained only as the explicit V1 compatibility projection; raw/vendor rows
  are not rewritten.
- The sidecar persists the typed semantic payload/value, metric and unit,
  authoritative and aggregation timestamps, source/provenance, vendor code,
  semantic/ontology/normalizer versions, stable semantic identity, transport
  identity, analytic trust, upcast status, and classification evidence.
- Database checks enforce index versus count versus ambiguous Movement,
  positive count windows, non-negative integral event counts, analytic trust,
  non-movement separation, namespace/mode shape, and unique semantic identity.
  The semantic-identity index and existing reconciliation advisory lock prevent
  duplicate trusted facts. The bounded upcast adds its own per-identity
  transaction advisory lock.

### Native V2 persistence and historical classification

- Push, Pull, and Replay now retain `CanonicalObservationV2` through their
  transaction and write the sidecar beside the existing V1 compatibility row.
  The global/default setting remains V1 and no V2 rejection falls back to V1.
- Exact Push/Pull overlap attaches or validates one sidecar on the reconciler's
  retained observation. Independent acquisition provenance remains in the
  acquisition ledger; a later transport cannot mutate the first semantic row.
- `observation_semantics_upcast` is a bounded, batch-committing, resumable CLI
  with `--dry-run`, `--batch-size`, `--max-rows`, and explicit category counts.
  It reports already-classified and error counts, uses scoped keyset pagination,
  and keys its identity cache by namespace and data mode. Reruns skip existing
  sidecars and cannot create a second semantic identity. A malformed row rolls
  back to its own savepoint, increments the error count, and makes the CLI exit
  nonzero without discarding successful rows in the bounded batch.
- Historical classification is evidence-only:
  Perceptor device-measured BodyShake with an allowlisted Push/Pull adapter and
  legacy `index` unit becomes `movement_index`; the Pull report hour-bucket
  flag plus the explicit vendor-count limitation and an integral value becomes
  `movement_event_count` with its proved one-hour window. Unproved report
  `{time_long,value}` and all other unsupported shapes become audit-visible,
  analytically untrusted `legacy_ambiguous_movement`. Numeric magnitude is
  never a classifier.

### Metric-safe aggregation and Product integration

- V2 aggregation keeps `movement_index` and `movement_event_count` in separate
  structures. Index exposes mean/median/sample count. Event count exposes a
  total and hourly maximum only after exact-duplicate deduplication and removal
  of conflicting or overlapping windows. The aggregation boundary revalidates
  source authority; non-hourly windows may contribute to a disjoint total but
  are explicitly excluded from `hourly_max`. Coverage minutes, gaps, overlap,
  conflicts, duplicates, incompatible windows, invalid semantics, and
  ambiguous exclusions remain visible.
- `legacy_ambiguous_movement` and any unclassified V1 row are excluded from V2
  totals instead of becoming zero. Mixed V2 Product evidence has no generic
  `vital_centers.movement` and cannot reconstruct the legacy `20.9` result.
  The separate unchanged V1 characterization still produces exactly `20.9`.
- The exact-revision Product loader joins sidecars by the pinned observation
  set and carries explicit metric, unit, window, trust, classification, and
  semantic identity through `ProductRevisionFacts`, deterministic night
  evidence, and the provider-facing evidence tool input.
- No new medical threshold was invented. The existing legacy
  `RadarNightSummary.movement_count` trend/risk threshold cannot be proved to
  mean either new metric and is classified `LEGACY_COMPAT_ONLY`. V2 Product
  movement analytics publish `SEMANTIC_THRESHOLD_UNRESOLVED`; neither V2
  movement metric enters that legacy trend/risk/care threshold.

### Downstream consumer trace

| Consumer | Classification and M3 result |
|---|---|
| Canonical observation persistence | `USES_BOTH_DISTINCTLY`; migrated to the V2 sidecar while retaining V1 compatibility. |
| SQL functions/views | `SHOULD_NOT_USE_MOVEMENT`; no current SQL aggregate consumes generic Movement. |
| `ProductRevisionFacts` and night/revision aggregation | `USES_BOTH_DISTINCTLY`; migrated to metric-safe V2 facts and aggregation. |
| Longitudinal Product vital trend | `SHOULD_NOT_USE_MOVEMENT`; it consumes only HeartRate and Respiration and is unchanged. |
| Runtime `RadarNightSummary.movement_count` trend/risk | `LEGACY_COMPAT_ONLY`; semantic threshold is unresolved and receives no V2 Movement mapping. |
| EvidenceReasoning/shared-analysis input | `USES_BOTH_DISTINCTLY` through deterministic Product evidence; ambiguous numeric facts are excluded. |
| CareStrategy/personalization inputs | `SHOULD_NOT_USE_MOVEMENT` directly; no generic canonical Movement field was found, and no new mapping was added. |
| API/read models and CLI/Demo projections | `LEGACY_COMPAT_ONLY` or `SHOULD_NOT_USE_MOVEMENT`; no M3-owned public Movement projection required a contract change. |

### PostgreSQL 16 proof

- The verified user-owned PostgreSQL 16.14 runtime was started in a unique
  loopback-only `/tmp/sleepagent-g2b-m3-pg16` cluster on port 15432. The
  unrelated port-55448 cluster was not touched.
- Fresh empty-database migration 001-014, canonical test-role bootstrap, and
  read-only migration check all reported schema 014.
- A separate temporary upgrade database applied 001-013, seeded proved index,
  proved hourly count, unproved movement, a non-movement observation, and a
  duplicate semantic fact, then applied 014 and ran dry-run plus real upcast.
  Result: two index candidates (one semantic duplicate), one event count, one
  ambiguous row, three inserted sidecars, one duplicate identity, zero
  non-movement sidecars. Raw pre-normalization and encrypted-payload hashes
  were identical before and after. The rerun inserted zero rows.
- The PostgreSQL test exercises index/count/ambiguity checks and the immutable
  trigger. Catalog and runtime proof confirm forced RLS, no PUBLIC grant,
  Worker SELECT/INSERT only, no API INSERT, no Demo SELECT, and zero rows for an
  unscoped Worker. Native Worker transactions successfully insert V2 rows.
- Explicit V2 Push persisted four distinct semantic identities including one
  Movement index; the raw retry created no duplicate. Explicit V2 Pull history
  attached three semantics to retained Push facts, detected exact overlap, and
  preserved one semantic identity per fact. Replay V2 retains its semantic
  object through the repository handoff and the shared PostgreSQL adapter is
  the same sidecar writer.

### Verification and architecture

- Focused G2B/M3 plus G2A/G1/Product/Perceptor selection: 113 passed.
- Migration/tooling/architecture selection: 69 passed.
- Unit-marked suite: 1,081 passed, 36 deselected.
- Non-PostgreSQL/non-E2E/non-ASGI suite: 1,081 passed, 36 deselected.
- PostgreSQL marker on a clean 001-014 database: 33 passed, one existing
  completed-process evidence reader skipped, 1,083 deselected.
- OpenAPI canonical snapshots, compileall, import smoke, migration discovery,
  and `git diff --check`: PASS.
- The G1 architecture executable reports no new SCC, self-import, or forbidden
  edge. In particular `domain/canonical_observation.py` remains persistence
  independent. No ADR is added because the sidecar directly implements the
  frozen canonical-boundary and movement-separation ADRs without changing
  their decision.

`V2_PERSISTED_ANALYTICS_READY = YES`

`V2_DEFAULT_CUTOVER_READY = NO`

Default cutover remains a separate decision. Before it can be authorized, the
legacy runtime `movement_count` threshold needs a proved metric or permanent
retirement, and explicit-V2 coverage must resolve the existing vendor-derived
realtime bed-presence ontology mismatch that remains outside M3. V1 therefore
stays the global default. No reporting/localization, later remediation,
DeviceBinding, scheduling, delivery, or topology work was started.

## G2C — Default Observation Semantics V2 cutover

`G2C_STATUS = PASS`

### Entry and bounded scope

- Entry HEAD: `d206ee20d4d1db39f1fa7316a6eefab1203eede1`.
- Entry worktree: clean; authoritative migration level 014; PostgreSQL 16.14
  verification profile available.
- Scope: resolve or retire the generic Movement threshold, resolve the Pull
  bed-presence ontology mismatch, exercise explicit V2 deployment paths, and
  make the bounded default-cutover decision. No reporting, report-cutover,
  architecture, scheduling, finalization, or delivery work is included.

### Generic Movement threshold decision

`GENERIC_MOVEMENT_THRESHOLD = OBSOLETE_LEGACY_THRESHOLD`

Repository history proves that `RadarNightSummary.movement_count` did not own
one stable vendor metric:

- the original deterministic quality path computed it as the number of sampled
  `body_movement` values satisfying an undocumented `>= 2.0` cutoff;
- an alternate report path accepted a supplied `movement_count` with unit
  `count`;
- the authority-migration path later counted every legacy `MovementPayload`
  observation regardless of value or cadence;
- risk then used `movement_count >= 25`, and trend used absolute delta 3 / 20%,
  without a pinned sampling interval or vendor semantic code.

The value is therefore a cadence-dependent compatibility heuristic, not
`movement_index` and not the proved hourly `movement_event_count`. Current
production code has no authoritative V2 producer for `RadarNightSummary`; its
remaining constructors are tests/legacy runtime contracts. M3 already keeps
both V2 metrics separate, publishes `SEMANTIC_THRESHOLD_UNRESOLVED`, and never
feeds either into this threshold. The threshold remains only in the explicit
V1 legacy characterization surface and cannot influence V2 Product analytics.
No guessed replacement threshold was added.

### Pull bed-presence ontology decision

`PULL_BED_PRESENCE_DECISION = WRONG_ONTOLOGY_SOURCE_ALLOWLIST`

The recorded Perceptor contract establishes two different authorities:

- Push `OnBed`/`Onbed` is retained as direct `device_measured` state.
- Pull `smbdFlag` and `probStatus` are vendor status classifications and carry
  explicit `vendor_*` quality flags/caveats, so `vendor_derived` is correct.

The mismatch came from the V1 ontology's blanket fallback that required every
non-sleep-profile observation to be `device_measured`. Ontology v2 now permits
exactly `device_measured | vendor_derived` for `bed_presence`, while unproved
user, external, and model-derived sources still fail closed. The numeric
metric/unit registry is unchanged and the Pull mapping/caveats are preserved.

### Default cutover and rollback

`V2_DEFAULT_CUTOVER_READY = YES`

The authoritative typed `SleepBackendSettings` default and its environment
fallback are now `ObservationSemanticsVersion.V2`. Production worker
composition already propagates that setting to Push, Pull, Replay journey, and
normalization handlers. Explicit
`SLEEPAGENT_BACKEND_OBSERVATION_SEMANTICS_VERSION=v1` remains accepted and
continues to produce `movement_payload.v1`; the frozen G1 legacy aggregation
characterization remains exactly `20.9`. Unsupported setting values still fail
closed and no V2 rejection falls back to V1.

### Verification evidence

Authoritative Python: 3.11.15 at
`/tmp/sleepagent-g1_5-py311/bin/python`.

| Verification | Result |
|---|---|
| G2C semantic/settings/legacy/Product/Perceptor/Replay selection | 125 passed |
| Explicit V2 Pull bed-presence source and fail-closed tests | 3 passed within the selection |
| Fresh PostgreSQL 001-014 migration/bootstrap/check | PASS; schema 014 |
| Explicit V2 PostgreSQL Push/Pull/semantic persistence | 3 passed in 10.59s |
| Unit-marked regression | 1,085 passed; 36 deselected in 20.74s |
| Architecture suite | 5 passed; no debt growth |
| OpenAPI canonical check | PASS; no snapshot changed |
| Compileall | PASS for source, reference client, scripts, and tests |
| `git diff --check` | PASS |

The unit regression includes deterministic Product aggregation and CLI/Demo
coverage. Worker composition tests run with the new typed default and retain
the explicit V1 rollback test. No real Perceptor request, external model,
production operation, scheduler, delivery, email/SMS, or other external effect
occurred.

### Migrations, compatibility, and checkpoint

- No migration was added or modified; migrations 001-014 retain their recorded
  hashes.
- V1 compatibility remains explicit and reversible.
- The obsolete generic Movement threshold is retained for historical V1
  characterization only; it is not reinterpreted as a V2 fact.
- Checkpoint subject:
  `remediation(g2c): resolve observation v2 cutover authority`.
  Its commit hash is reported by the next execution record/final handoff
  because a commit cannot contain its own hash.

`G3_SAFE_TO_BEGIN = YES`

## G3 — Reporting time and locale semantics

`G3_STATUS = PASS`

### Entry and bounded scope

- Entry checkpoint: `5119826` (`remediation(g2c): resolve observation v2
  cutover authority`); worktree clean at phase entry.
- Scope: one UTC-authoritative reporting context, deterministic local-date/time
  conversion, a minimal structured reporting-fact layer, deterministic zh-CN
  role rendering, and separate semantic/projection identities. No report-chain
  cutover, architecture extraction, scheduling, delivery, or external action
  was included.
- No migration was added or modified. Existing JSON contracts accept the new
  fields, while historical serialized shared analyses and projections retain
  their explicit legacy hash path.

### Time and local-night authority

- `ReportingContextV1` pins the IANA timezone, `zh-CN` locale, audience,
  authoritative UTC start/end boundaries, local sleep date, and renderer
  version. New shared analysis requires this context; each role projection
  receives the same time authority with its own audience and renderer pin.
- UTC boundaries must be timezone-aware and normalized to UTC. The local sleep
  date is owned once by the authoritative end boundary converted with
  `zoneinfo.ZoneInfo`; role renderers do not derive their own report date.
- Product desired-work identity now includes timezone, UTC boundaries, and
  local sleep date. The source timezone is the exact-revision Product fact,
  preserving historical-night interpretation rather than consulting a future
  mutable binding.
- `ProductRevisionFacts.deterministic_night_summary` now converts bed-exit
  timestamps with the revision timezone before labeling `local_time`. The
  characterized `17:35Z` defect is intentionally retired: Asia/Shanghai emits
  the next-day local clock, and Los Angeles repeated-hour behavior retains the
  correct offset and `fold`.

### Structured semantics and deterministic zh-CN projections

- `ReportSemanticFact` is the minimal report-facing structured layer. It binds
  fact kind, metric/value/unit/window/comparison, quality, sources, authority,
  and caveat under a content-derived fact ID.
- Direct Product metrics come only from deterministic night evidence; accepted
  Agent claims contribute their typed semantic/comparison metadata and source
  authority, never `EvidenceClaim.statement` as fact authority. Quality and
  pending-care facts remain explicit.
- New shared-analysis hashes use `structured_facts.v1`: they bind semantic
  facts, accepted-product target identities, and time semantics while excluding
  arbitrary summary prose, locale wording, audience, and renderer version.
  Historical JSON keeps the exact `legacy_summary.v1` verification branch.
- Elder, Family, and Doctor projections are deterministic `zh-CN` views of one
  shared semantic identity. Elder remains respectful and concise; Family shows
  timing, state, uncertainty, and pending care; Doctor shows metrics, units,
  quality, provenance, and caveats without diagnostic claims. Arbitrary English
  claim statements may remain on the internal compatibility surface but are
  not rendered into product role text.
- Projection hashes bind audience, localized text, reporting context, and
  renderer version. Changing the renderer changes all role projection hashes
  without changing the shared semantic analysis hash.

### Verification evidence

Authoritative Python: 3.11.15 at
`/tmp/sleepagent-g1_5-py311/bin/python`.

| Verification | Result |
|---|---|
| Focused report/shared-analysis suite | 169 passed |
| Reporting/time/Product characterization selection | 84 passed |
| Broader Product/report regression | 347 passed |
| Unit-marked regression | 1,088 passed; 36 deselected |
| Fresh PostgreSQL 001-014 Product/report integration | 19 passed |
| Architecture suite | 5 passed; no debt growth |
| OpenAPI canonical check | PASS; no public snapshot changed |
| Compileall and `git diff --check` | PASS |

The PostgreSQL proof used a fresh database on the existing isolated PostgreSQL
16.14 cluster and reapplied migrations 001-014 before the integration run.
Strict isolated mypy scanning found no diagnostic in the newly added reporting
context/fact definitions after the fix pass; the touched large modules retain
their previously recorded unrelated strict-typing debt. No production
Perceptor call, external model, scheduler, email/SMS, or other external effect
occurred.

### Compatibility, limitations, and checkpoint

- Public API/OpenAPI contracts are unchanged. New context and semantic facts
  live inside existing governed JSON artifacts; old artifacts remain readable
  and hash-verifiable through the explicit legacy branch.
- `summary_lines` remains serialized for historical/debug compatibility but is
  no longer a localized fact source. Its final retirement belongs to G4 after
  shadow equivalence and rollback evidence.
- DeviceBinding temporal persistence is not redesigned here. G3 preserves the
  correct contract by consuming the timezone already pinned to exact-revision
  Product facts; Phase F owns formal binding lifecycle/version persistence.
- Checkpoint subject:
  `remediation(g3): normalize reporting time and locale`.
  Its hash is reported by the next execution record/final handoff because a
  commit cannot contain its own hash.

`G4_SAFE_TO_BEGIN = YES`

## G4 — Shared report shadow migration

`G4_STATUS = PASS`

### Entry and authority

- Entry checkpoint: `857249c` (`remediation(g3): normalize reporting time and
  locale`); worktree clean at phase entry.
- Current new-report routing was re-read rather than inferred from the G1
  inventory. The default `shared_compat` path already creates one
  `product.shared_analysis.v1`, commits one shared analysis, and publishes
  exactly three deterministic role projections. The dormant three-Agent
  `prepare` path has no new per-role operation creator.
- Shared analysis is the sole externally visible authority in both
  `shared_compat` and the newly active `shadow` mode. Shadow legacy output is
  never committed as an AnalysisRevision, role view, compatibility result,
  Induction input, CareAction, Habit/Memory mutation, delivery, or outbox
  authority.

### Structured shadow parity

- `report_pipeline_mode=shadow` now runs the retained legacy preparation only
  inside the already governed model invocation and compares it with the shared
  artifact before persistence. Other modes do not pay the extra execution
  cost.
- `ReportShadowComparison` records the shared authority, an invariant
  `external_side_effects_permitted=false`, the legacy attempt hash, shared
  analysis hash, per-category material hashes, explicit mismatch categories,
  and a content-derived comparison identity.
- The comparison is semantic, never textual. It covers accepted fact
  identities, metric values/units/windows, quality status, risk classification,
  CareCandidate semantics, role-visible fact sets, and evidence source
  references. Generated claim IDs are resolved to semantic signatures before
  role-visible comparison, so deterministic wording and ID differences do not
  create false blockers.
- A clean deterministic PostgreSQL sample produced `mismatch_categories=[]`
  across all seven categories. The prepared audit row was durable while the
  legacy shadow created zero analysis revisions and zero role-view rows; only
  the later shared commit may publish those tables.

### Consumer migration and executable inventory

- `docs/remediation/LEGACY_SHARED_REPORT_CONSUMERS.md` now contains the G4
  rescan with exact current symbols/call sites. Product report/today, public
  role-view, report CLI, Demo API/CLI, simulation presentation, and reference
  client are classified `SHARED_READY` for new data. Applied SQL and generic
  historical role-view reads remain `COMPAT_ONLY`.
- `python -m sleepagent.report_consumer_audit` produces deterministic JSON from
  AST call sites plus worker, API, CLI/reference-client, and SQL evidence. Its
  regression test refuses a grep-only zero claim.
- Machine audit result: zero new legacy per-role operation creators and zero
  active new-report readers requiring a legacy Agent result. Nonzero retained
  items are the explicit shadow/historical legacy implementation, the
  non-claimable compatibility bridge write, and historical compatibility
  reads.

### Verification evidence

| Verification | Result |
|---|---|
| Shadow/config/Product focused unit selection | 81 passed |
| Executable consumer-audit unit proof | 1 passed |
| Fresh PostgreSQL 001-014 shadow persistence/no-publication proof | 1 passed |
| Structured parity categories on deterministic sample | 7/7 equal; zero mismatches |
| Unit-marked regression with loopback provider profile | 1,089 passed; 37 deselected |
| Architecture suite | 5 passed; no debt growth |
| Compile, consumer-audit CLI, and `git diff --check` | PASS |

The first unit regression attempt was sandboxed: 1,081 tests passed and eight
existing loopback HTTP-provider tests could not create sockets. The identical
approved loopback-only rerun passed all 1,089 unit-marked tests. The first
PostgreSQL attempt found stale pending work from the prior phase; the exact
isolated `sleepagent_replay_test` database was recreated, migrations 001-014
and canonical test bootstrap were applied, and the fresh shadow proof passed.
No external delivery or real care effect occurred.

### Compatibility and G5 gate

- No migration or public OpenAPI contract changed. Shadow evidence is internal
  governed JSON on the prepared/shared result.
- `shared_compat` remains the default at this G4 checkpoint. It is the rollback
  mode and still creates/completes the non-claimable compatibility operation.
- All G5 cutover gates are satisfied for the tested Product reporting path:
  authoritative reads are shared-ready, role projections bind shared
  authority, the structured sample has no unresolved parity mismatch, shadow
  has no external effect, active consumers do not require new legacy work, and
  rollback exists. G5 may therefore disable new compatibility writes while
  preserving historical reads and explicit `shared_compat` rollback.
- Checkpoint subject: `remediation(g4): migrate reports to shared analysis`.

`G5_SAFE_TO_BEGIN = YES`

## G5 — Shared-only report cutover and legacy write retirement

`G5_STATUS = PASS`

### Gate decision and default cutover

- Entry checkpoint: `c01ae23` (`remediation(g4): migrate reports to shared
  analysis`); worktree clean at phase entry.
- G4 established all conditional gates: Product/public reads are shared-ready,
  all three projections bind one shared authority, the deterministic shadow
  sample had zero semantic mismatches, shadow permitted no external effect,
  no active consumer required a new legacy result, and `shared_compat` remained
  an explicit rollback.
- `SleepBackendSettings` now defaults to `report_pipeline_mode=shared_only` and
  `emit_legacy_report_compatibility=false`, including environment fallbacks.
  The pair fails closed unless shared-only disables compatibility or an
  explicit rollback/shadow mode enables it.

### Retired default write path

- Fast path now creates and reports a distinct authoritative
  `report_operation_id`. A compatibility operation ID is generated only when
  the explicit compatibility switch is true. New shared-only fast-path work
  therefore creates `product.report.run.v1` directly and no longer inserts the
  non-claimable `product_agent_compatibility` operation.
- Replay journey progress follows report request → linked shared operation →
  authoritative shared result. It no longer uses a copied compatibility result
  as the Product child authority.
- The production Product handler disables direct legacy `product_agent`
  execution under shared-only mode with a terminal retired-path result. The
  three-Agent implementation is retained behind explicit shadow/rollback only;
  it cannot receive new default work.
- Shared compatibility completion/failure helpers and applied historical SQL
  remain for rollback/history. They are no-ops for new requests without a
  compatibility ID and cannot authorize new care, personalization, delivery,
  or other effects.

### Consumer-zero and rollback evidence

The executable audit reports zero default legacy per-role creators, zero
default legacy prepare execution, and zero default compatibility bridge writes.
It separately reports retained rollback implementations and historical reads,
avoiding a false deletion claim.

Explicit rollback requires both:

```text
SLEEPAGENT_BACKEND_REPORT_PIPELINE_MODE=shared_compat
SLEEPAGENT_BACKEND_EMIT_LEGACY_REPORT_COMPATIBILITY=true
```

The pair is covered by typed settings tests. Inconsistent combinations are
rejected rather than silently re-enabling or dropping compatibility writes.

### Verification evidence

| Verification | Result |
|---|---|
| Settings/audit/FastPath/simulation/backend focused selection | 68 passed |
| Fast-path shared-only/rollback ID matrix | 3 passed |
| Unit-marked regression | 1,095 passed; 38 deselected |
| Fresh PostgreSQL 001-014 Product integration suite | 19 passed |
| Shared-only PostgreSQL topology | 1 report + 1 shared + 3 projections + 0 legacy/compat operations |
| Migration/hash/RLS check | PASS; schema 014 |

No migration or public OpenAPI schema was changed. No compatibility artifact
was deleted, no external delivery occurred, and no email/SMS was sent.

### Checkpoint

Checkpoint subject:
`remediation(g5): retire legacy report write path`.

`G6_SAFE_TO_BEGIN = YES`

## G6 — Architecture boundary cleanup

`G6_STATUS = PASS`

### Entry and debt reduction

- Entry checkpoint: `b8c382d` (`remediation(g5): retire legacy report write
  path`); worktree clean at phase entry.
- The executable G1 fixture contained three concrete strongly connected
  components spanning the six ledger categories, six self-imports, and eleven
  forbidden-direction edges. The G6 fixture contains one bounded
  runtime-contract SCC, zero self-imports, and zero forbidden edges.
- All six self-imports were redundant in-module imports and were removed before
  structural moves. Import compilation remained green after the cleanup.

### Composition, kernel, and dependency direction

- `sleepagent.bootstrap.worker` is the concrete worker composition root. It
  assembles queue registries, rejects overlaps and missing handlers, and owns
  the CLI entry point. `workers.runtime` no longer imports concrete handlers;
  its historical `main` delegates dynamically to the bootstrap root.
- `sleepagent.workers.kernel` now owns only lease/fence claims, handler/context
  contracts, result/disposition contracts, invocation primitives, and shared
  worker errors. Concrete handlers import these contracts from the kernel;
  the runtime retains source-compatible re-exports.
- `build_backend_runtime` receives API services through an injected factory.
  The API composition path supplies that factory, removing the
  `process -> app` reverse edge while preserving one shared UoW factory.
- The Product data provider moved to `sleepagent.application.product_data` and
  its neutral reporting signal moved to `sleepagent.application.reporting`.
  The old domain module is a compatibility re-export only.
- The PostgreSQL sleep slice moved to
  `sleepagent.infrastructure.postgres_sleep_slice`. The old domain path is a
  dynamic compatibility facade; all production and test consumers now use the
  infrastructure authority.

### Architecture gate and verification

| Gate | G1 executable fixture | G6 fixture |
|---|---:|---:|
| Strongly connected components | 3 (six categorized debts) | 1 runtime-internal |
| Self-import edges | 6 | 0 |
| Forbidden-direction edges | 11 | 0 |

The remaining SCC is limited to the existing runtime contract/governance
cluster (`cold_start`, `contracts`, `governance`, `hitl`, `invocation`,
`memory`, `registry`, and `results`). Splitting that cohesive contract cluster
would be a broad runtime redesign with no remaining forbidden edge, so it is
retained as the exact no-growth ceiling rather than hidden or expanded.

| Verification | Result |
|---|---|
| Focused moved-module/runtime regression | 233 passed |
| Extracted-kernel regression | 111 passed |
| Unit-marked regression with loopback provider profile | 1,096 passed; 38 deselected |
| Architecture suite | 6 passed; exact reduced baseline |
| Worker bootstrap and compatibility CLI help | PASS |
| Compile and `git diff --check` | PASS |

The first full unit run was sandboxed: 1,087 tests passed, eight existing
loopback HTTP tests could not bind sockets, and the source audit found one
stale pre-move path. The audit was pointed at the new infrastructure authority;
the approved loopback rerun then passed all 1,096 unit-marked tests. No schema,
OpenAPI contract, production device, external provider, delivery, email, or SMS
was touched.

### Compatibility and checkpoint

The two moved authorities retain narrow historical import shims, and the
runtime retains the worker-kernel public names. These are rollback/read
compatibility surfaces, not authoritative dependency directions. The reduced
architecture fixture may only shrink in later work.

Checkpoint subject: `remediation(g6): restore architecture boundaries`.

`G7_SAFE_TO_BEGIN = YES`

## G7 — DeviceBinding, durable acquisition, and night finalization

`G7_STATUS = PASS_WITH_LIMITATIONS`

### DeviceBinding authority

- Entry checkpoint: `12d1a22` (`remediation(g6): restore architecture
  boundaries`); the phase began from a clean worktree.
- `DeviceBindingService` is the single application authority for
  discover/list, bind, show, rebind/transfer, end/unbind, revoke, and validate.
  `sleepagent.device_cli` supplies the MVP operational surface and issues no
  arbitrary SQL.
- Migration 015 adds valid-IANA checks, one-device non-overlap, immutable
  temporal versions, CAS lifecycle transitions, append-only audit, audited
  cross-subject transfer, and forced-RLS policies. API commands require exact
  actor/subject `device_binding_management` authority.

### Durable scheduler and worker handoff

- Migration 016 persists enabled state, due/success timestamps, failure
  metadata, policy hash/version, binding version, cadence, jitter, attempts,
  and CAS. Concurrent due scans use `FOR UPDATE SKIP LOCKED`.
- Each `(schedule, scheduled_for)` creates at most one deterministic UUIDv7
  operation and one fire row. The scheduler holds only
  `acquisition_schedule` enqueue authority; the operation snapshot carries the
  exact namespace/generation/subject epochs, production `worker` purpose, and
  one allowed handler.
- Existing workers perform `perceptor.history_overlap_pull`,
  `perceptor.sleep_report_pull`, or `night.finalization_scan`. History retains
  a 15-minute overlap; SleepReport local date uses the binding IANA timezone;
  Perceptor ingestion/checkpoint code remains authoritative. Scheduled Pull
  requires a dedicated vendor client-ID reference and never reuses the
  SleepAgent service credential.
- Pause/resume is CAS-guarded. Failed fires apply bounded exponential backoff;
  expired leases are reclaimed with a new fence but no duplicate business
  attempt. The feature gate defaults to false and disabled mode performs no
  database work.

### Distinct finalization authority

- Migration 017 and `NightFinalizationService` keep data completeness separate
  from episode date ownership. The additive states are OPEN,
  SOFT_FINALIZED, HARD_FINALIZED, and RECONCILIATION_REQUIRED.
- SOFT requires the configured local wake/deadline grace and carries explicit
  provisional/partial coverage plus a caveat. HARD requires a non-empty vendor
  report or the explicit maximum-wait policy. Date conflict routes directly to
  reconciliation.
- Material late evidence creates an immutable superseding finalization
  revision. Prior revisions remain auditable and a bounded UUIDv7 fast-path
  reanalysis operation is created when policy requires it.

### Native PostgreSQL evidence

The final clean PostgreSQL 16.14 run applied manifest-pinned migrations
001–017, bootstrapped distinct non-owner API/Worker roles, and passed the full
PostgreSQL-marked suite. Its G7 end-to-end matrix covers binding
replay/conflict/rebind/transfer/CAS/RLS, two-instance scheduler claim, schedule
idempotency, expired-lease recovery, failure backoff, duplicate fire,
OPEN→SOFT, SOFT→HARD, direct OPEN→HARD, late revision/reanalysis,
reconciliation, and UTC/local-date crossing. The one suite skip requires a
separately completed process-proof database and is not a G7 behavior omission.

| Verification | Result |
|---|---|
| Focused unit/settings/architecture selection | 28 passed |
| Native PostgreSQL 16.14 G7 matrix | 1 passed |
| Full PostgreSQL-marked regression | 36 passed; 1 process-proof fixture skipped; 0 failed |
| Full unit-marked regression | 1,105 passed; 39 deselected |
| Non-E2E/non-PostgreSQL regression | 1,105 passed; 39 deselected |
| Architecture suite | 6 passed; exact reduced baseline |
| OpenAPI snapshots and default legacy-writer audit | PASS; unchanged / zero writers |
| Schema migration apply/check | PASS; schema 017 |
| Python 3.11 compileall, three CLI helps, and `git diff --check` | PASS |
| Feature-gate default / disabled no-op | `false` / PASS |

No production credential, provider cloud, device, delivery, email, or SMS was
contacted. Real automatic Perceptor acceptance remains
`LIVE_ACCEPTANCE_DEFERRED`; deterministic/PostgreSQL scope is complete.

Checkpoint subject: `remediation(g7): automate device acquisition lifecycle`.

## G7.1 — Controlled live Perceptor acquisition acceptance

`G7_1_STATUS = NOT_ACCEPTED`

### Entry and authorized scope

- Entry HEAD was exactly
  `719e186d2d3918f2225d30054656b6a843f14c52`; the worktree was clean.
- The acceptance reused the single previously authorized live Perceptor
  binding. Sanitized authority: binding ref `396ac1640613`, version `2`,
  subject ref `98b24d1d005e`, device ref `2178099316ee`, timezone
  `Asia/Shanghai`, status active, CAS `0`. Exactly one active binding and zero
  temporal overlaps were found; no provider/device configuration was changed.
- The owner-only credential file remained outside the repository and supplied
  only the vendor secret. The separate client ID was recovered in memory from
  an authenticated, encrypted, checksum-matched prior Push receipt. Neither
  value was printed or persisted in acceptance evidence.
- The acceptance profile was limited to one namespace/binding, three schedules,
  the existing API/Worker principals, `report_pipeline_mode=shared_only`, and
  `live_delivery_enabled=false`. No Alarm, AlarmStop, control, configuration,
  email, SMS, care notification, or other external effect was invoked.

### Provider and live semantic evidence

- Authentication, product/device discovery, binding match, device detail,
  History, Current, and SleepReport all returned valid provider responses over
  the project's normal Perceptor adapter. All provider operations were
  read-only.
- A known non-empty History window (`2026-08-25 08:30:55+08:00` through
  `08:45:55+08:00`) returned four provider records and 903 V2 candidates:
  189 heart-rate, 189 respiratory-rate, 189 movement-index, and 336
  missing-interval facts. Timestamps were authoritative UTC, provenance was
  `device_measured`, ontology was V2, and transport identities were present.
  Semantic identities were not all unique within the response, an overlap/
  missing-interval finding retained for operational follow-up.
- Current returned V2 bed-presence and connectivity facts. Observed Pull
  `smbdFlag`/`probStatus` mapped to `vendor_derived`; the authenticated Push
  `OnBed` contract remains `device_measured`. This confirms the G2C mapping.
- SleepReport for `2026-08-25` was non-empty: 892 candidates including heart
  rate, respiratory rate, movement, missing intervals, sleep stages, bed exits,
  and vendor profile facts. V2 correctly rejected the payload with
  `invalid_source_provenance`: vendor-derived heart-rate conflicts with the
  ontology requirement that heart-rate be device-measured. Validation was not
  weakened and the response was not reinterpreted.

### Controlled scheduler/worker evidence

- Exactly three schedules were created through `sleepagent.device_cli`:
  History ref `4f8cbc9c4f44`, SleepReport ref `833359d8f0cb`, and finalization
  ref `bf8c21f64b11`; all used binding version 2, policy
  `acquisition-default.v1`, zero jitter, and the expected per-job policy hash.
- Pausing History followed by an actual scheduler scan emitted no History fire.
  Resuming the exact eligible slot emitted fire ref `f11ddbd8a832` and operation
  ref `ef43ae707936`. Re-evaluating the identical slot returned the same refs;
  PostgreSQL retained one fire and one operation.
- The checkpoint-safe scheduled History slot was a valid provider no-data
  outcome. After bounded retries it committed one encrypted raw receipt,
  completed one normalization work item, advanced the cursor from
  `10:00:00+08:00` to `10:59:57+08:00`, retained the three-second overlap, and
  reached `succeeded`. It created no observation because the provider returned
  no data for that slot.
- Worker attempts reused the same fire/operation through lease generations
  1–5 and reached one terminal success after the missing scheduled-Pull
  authority was corrected. The G7 PostgreSQL matrix independently proved an
  expired claim is reclaimed under a new fence and cannot create a second
  authoritative operation.
- The scheduled SleepReport fire ref `3aa1d7c172db` and operation ref
  `a5669db59bc4` reached `succeeded`. The live response committed a raw receipt
  and resolved as a semantic duplicate of the existing durable report work;
  the independent V2 preflight finding above remains a hard semantic failure.

### Minimal acceptance fixes and migration

- Fix round 1 corrected the two concrete entrypoints to use the actual
  `PsycopgPoolProvider.open()` lifecycle contract and allowed the DeviceBinding
  CLI and scheduler processes to start.
- Fix round 2 preserved the claimed Worker scope through scheduled Pull
  planning/ingress, extended the SQL functions with exact Worker/handler grant
  checks while retaining the existing API authority and migration-013 retry
  no-op, and granted the two functions to bootstrapped Worker roles.
- Application-only correction was insufficient because migrations 012/013
  hard-coded `api` plus `perceptor_ingress` in SECURITY DEFINER authority.
  Immutable migration 018 therefore replaces only the two affected functions;
  migrations 015–017 were not modified. Fresh 001→018, exact entry-HEAD
  001→017 followed by 017→018, restored 013→018, manifest check, API retry
  no-op, and Worker planner authority all passed on PostgreSQL 16.14.

### Finalization, late data, and handoff

- The selected real night created one `hard_finalized` revision directly from
  OPEN under `night-finalization.v1` (policy hash
  `4d2d84e4dbff693870389181aedfc78da64a7b99731e631d6e00679c4bae6f1d`).
  The cause was `maximum_wait_elapsed`; coverage was partial and non-
  provisional. No provider report version was linked to that episode.
- Current frozen policy: OPEN remains open until a gate is met; OPEN→SOFT
  requires the deterministic wake/close deadline plus 7,200 seconds and at
  least one observation while a non-empty report is absent; SOFT→HARD occurs
  when a non-empty vendor report arrives or the maximum wait elapses;
  OPEN→HARD may skip SOFT when a non-empty report is already linked or the
  86,400-second maximum wait has elapsed. A date conflict routes to
  RECONCILIATION_REQUIRED before either final state.
- Material identity is the tuple of episode revision ID, source report version
  ID, target state, coverage status, and policy hash. A changed material hash
  creates immutable revision N+1; an unchanged hash reuses the existing
  revision. The focused PostgreSQL matrix passed SOFT→HARD late material
  revision, prior-revision readability, bounded fast-path reanalysis,
  unchanged-material non-churn, and reconciliation visibility/no repeated
  enqueue using controlled local evidence shaped from the accepted night.
- The actual selected night's first finalization emitted no report/reanalysis
  operation, no SharedNightAnalysis, and no zh-CN RoleProjection. The local
  late-revision path can enqueue fast-path reanalysis, but that does not satisfy
  the required first-finalization shared handoff. `SHARED_REPORT_HANDOFF=FAIL`.

### Operational metric inventory

| Metric | Classification | Evidence |
|---|---|---|
| Scheduler due lag | DERIVABLE | `next_run_at`, enabled state, and database time |
| Last fire time | AVAILABLE_NOW | schedule `last_fire_at` and immutable fire rows |
| Last successful acquisition | AVAILABLE_NOW | schedule `last_success_at` and terminal operation/fire state |
| History success/failure | AVAILABLE_NOW | job-typed fires, operations, attempts, and error code |
| SleepReport success/failure | AVAILABLE_NOW | job-typed fires, operations, raw receipts, and work state |
| Records received/persisted/deduplicated | DERIVABLE | raw/work/semantic/acquisition identities; no single aggregate |
| Oldest acquisition operation age | DERIVABLE | non-terminal operation `created_at`/`available_at` |
| Night OPEN age | DERIVABLE | finalization/episode state and timestamps |
| Night SOFT age | DERIVABLE | current state plus revision `created_at` |
| RECONCILIATION_REQUIRED count | AVAILABLE_NOW | finalization and episode reconciliation state |
| Late revision count | DERIVABLE | revision number/parent and `late_material_evidence` cause |

### Verification and safe shutdown

| Verification | Result |
|---|---|
| Actual CLI/scheduler/Worker processes | PASS |
| Focused G7 PostgreSQL matrix | 1 passed |
| Observation V2 focused unit/PostgreSQL suite | 117 passed |
| Product/report/finalization focused suite | 90 passed |
| Full PostgreSQL marker regression | 36 passed; 1 expected process-proof skip |
| Broader non-E2E/non-PostgreSQL regression | 1,110 passed; 39 deselected |
| Architecture gate | 6 passed |
| OpenAPI snapshot gate | PASS; unchanged |
| Migration discovery/hash and schema checks | PASS; schema 018 |

All three schedules were paused through the audited CLI. A final enabled
scheduler scan returned zero fires. Scheduler and Worker processes exited,
the source feature default remained false, external delivery remained disabled,
and the DeviceBinding was retained. Re-enable only in the controlled profile by
resuming the desired schedule with its current CAS and bounded `next_run_at`,
then starting a scoped scheduler/Worker with the corresponding exact handlers.

### Verdict

| Dimension | Verdict |
|---|---|
| LIVE_DEVICE_AUTHENTICATION | PASS |
| LIVE_HISTORY_PULL | NO_DATA (scheduled slot; non-empty endpoint preflight passed) |
| LIVE_SLEEP_REPORT_PULL | PASS |
| LIVE_OBSERVATION_V2 | FAIL |
| LIVE_BED_PRESENCE_MAPPING | PASS |
| SCHEDULER_FIRE_AUTHORITY | PASS |
| SCHEDULER_IDEMPOTENCY | PASS |
| PAUSE_RESUME | PASS |
| WORKER_RECOVERY | PASS |
| NIGHT_FINALIZATION | PASS |
| LATE_DATA_REVISION | PASS (controlled local evidence) |
| SHARED_REPORT_HANDOFF | FAIL |
| SAFE_SHUTDOWN | PASS |

`G7_1_LIVE_ACCEPTANCE_READY = NO`

Blocking acceptance findings are real SleepReport V2 provenance incompatibility,
absence of persisted new live V2 observations for the scheduled History slot,
and absence of the required first-finalization shared report handoff. No
external-effect work was started.

## G7.1-R1 — Live acquisition handoff blocker remediation

`G7_1_R1_STATUS = ACCEPTED`

### Entry, scope, and root causes

- Entry HEAD was exactly
  `34e4a55a2c1281c1291e0964238df5691ee132e4`, subject
  `remediation(g7.1): validate live acquisition lifecycle`. The R1 worktree
  started clean. The previously authorized binding remained unchanged:
  binding ref `396ac1640613`, version `2`, subject ref `98b24d1d005e`.
- Provenance root cause classification:
  `ADAPTER_PROVENANCE_MAPPING_WRONG`. The repository's Perceptor V2.5.2
  contract identifies SleepReport `heart_rate_data` and `breathe_data` as
  sleep-period sensor measurement series, but the Pull adapter blanket-labeled
  every SleepReport field-series `vendor_derived`. The shared V2 ontology
  correctly requires physiological heart/respiratory measurements to be
  `device_measured`, so the first real heart-rate candidate failed closed as
  `invalid_source_provenance` before persistence.
- Handoff root cause classification:
  `FINALIZATION_DID_NOT_ENQUEUE_REPORT`. `NightFinalizationService._persist`
  created fast-path report/reanalysis work only when a parent finalization
  revision existed. The initial revision has no parent, so the first
  soft/hard finalization committed without any downstream operation. There was
  no queue, handler, lease, revision-pin, or transaction-visibility failure;
  the graph diverged before enqueue.

### Fixes and durable regressions

- SleepReport physiological series now carry explicit `device_measured`
  provenance plus the documented sensor-measurement limitation. Sleep stages,
  summary/profile metrics, bed-exit series, and movement-count series remain
  explicitly `vendor_derived`; no live/provider bypass, V1 fallback, or
  unknown-to-trusted coercion was added.
- The sanitized real-shape fixture
  `sanitized_recorded_real_pull_get_sleep_report_provenance.json` retains the
  vendor's extra physiological `type` field while removing credentials and
  real subject/device identifiers. It proves heart-rate and respiratory-rate
  candidates cross the one CanonicalObservationFactoryV2 boundary as trusted
  device measurements. Replaying the full sanitized real response now crosses
  the original provenance failure and reaches a separate existing summary
  metric rejection (`heart_rate_avg`, missing canonical unit).
- Batch behavior remains the existing atomic/fail-closed contract. R1 did not
  silently drop invalid records or introduce partial acceptance. The selected
  heterogeneous History response is entirely supported and therefore needed
  no rejected-record side channel.
- Every first SOFT/HARD finalization now inserts an `initial_report` fast-path
  handoff in the same transaction as its immutable revision. Later material
  revisions use the same durable linkage with `late_reanalysis`. The existing
  migration-017 `reanalysis_operation_id` FK is reused; no fake prior revision
  is created.
- Independent normalization, soft-finalization, and hard-finalization
  fast-path operations for the same Episode revision now converge under an
  advisory semantic lock on one report operation. An existing eligible report
  is validated and reused; if still pending/retry, its pinned deterministic
  gate is atomically refreshed before claim. The PostgreSQL regression proves
  three handoffs create one report, one shared analysis, and three role views.
- Simultaneous History gaps exposed a pre-existing V2 identity defect:
  missing-interval heart, respiration, and movement candidates at one instant
  hashed the same generic null value. Their semantic identity now includes
  target observation type, missing state, and reason code. Exact immutable
  retries against the proved legacy hash remain accepted only when all
  semantic content matches; changed target/content still fails closed. The
  normalizer version remains unchanged because this is an in-place identity
  bug correction, not a new ontology contract.

### Migration audit

- Migration 018 was required because migrations 012/013 SECURITY DEFINER
  planner/ingress functions hard-coded API plus `perceptor_ingress` authority.
  It replaces only those two functions, adds exact Worker-handler grant checks
  and test-role execution grants, and preserves the API retry no-op. It stores
  no acceptance evidence.
- Migrations 001–017 are byte/hash unchanged from entry; migration 018 is also
  unchanged with SHA-256
  `a799b10d7f9ea773e1ca457cc66c1a68beb6799d86df2f045983658dbe656875`.
  Migration discovery remains exactly 18. No migration 019 was required.
- Fresh PostgreSQL 16.14 databases applied and checked 001→018. The accepted
  G7.1 001→017→018 upgrade evidence remains authoritative because none of the
  migration files or manifest entries changed in R1.

### Focused controlled live proof

- The original evidence database remained under
  `default_transaction_read_only=on`; all R1 mutations were confined to a
  disposable local clone. The real provider call was the read-only History
  endpoint `/vitalSigns/getHistoryData`. No additional SleepReport provider
  call was needed.
- A due schedule for the authorized known-nonempty window ending
  `2026-08-25T00:45:56Z` created one immutable fire and one acquisition
  operation. The real response contained 3 records and normalized to 900
  accepted candidates: 188 heart-rate, 188 respiratory-rate, 188 movement
  index, and 336 explicit missing intervals. Intentionally rejected was 0,
  conflicts were 0, and the operation reached `succeeded` with checkpoint
  advancement.
- All 900 candidates have native V2 sidecars and trusted
  `device_measured` provenance. The scheduled acquisition resolved as 900
  semantic duplicates of already committed canonical identities (created 0,
  deduplicated 900), proving durable idempotent persistence rather than a
  second copy.
- The automatic scheduled finalization created revision 1 with no parent,
  then an `initial_report` fast-path operation, one
  `product.report.run.v1`, one `product.shared_analysis.v1`, one ready shared
  analysis revision, and exactly three distinct role projections. The retained
  identifiers are sanitized refs: root `055702c6ca48`, finalization
  `e33e1f426ab1`, handoff `32ff1ea3388f`, report `ceb930f036d4`, and shared
  `296173250764`.
- The large retained Episode exceeded the temporary harness's first 15-second
  SQL UOW timeout while loading the report. Its exact durable lease was expired
  and reclaimed under the intended 300-second acceptance timeout; the same
  report operation then succeeded. No duplicate authority was created.
- Replaying the identical finalization schedule slot returned the same fire
  and root operation. Authoritative counts remained exactly: finalization 1,
  fast-path 1, report 1, shared operation 1, analysis revision 1, role views 3.
- Legacy `product_agent`/compatibility execution count was 0.
  `report_pipeline_mode=shared_only`, `emit_legacy_report_compatibility=false`,
  and `live_delivery_enabled=false` throughout. Two optional, unexecuted
  induction/narrative operations were cancelled during clone shutdown.
- Every audited external-effect delta was 0: delivery intents/journal,
  delivery reconciliation/replay effects, care actions/followups/transitions,
  command receipts/commands, and delivery operations. No email, SMS, care
  notification, Alarm, AlarmStop, or device-control call occurred.
- Shared analysis used ModelMode.LIVE through a controlled OpenAI-compatible
  loopback endpoint (3 successful requests, no errors). No health context was
  sent to an external model provider. All schedules were paused, the temporary
  schedule grant was removed, and the clone Worker grant was restored from the
  read-only source.

### Verification and verdict

| Verification | Result |
|---|---|
| Provenance/Observation/automation focused selection | 150 passed |
| Device automation + Observation V2 + Pull PostgreSQL | 3 passed |
| Shared-only Product PostgreSQL focus | 3 passed |
| Full PostgreSQL marker on clean 001→018 database | 37 passed; 1 expected process-proof skip |
| Broad non-E2E/non-PostgreSQL regression | 1,113 passed; 40 deselected |
| Architecture gate | 6 passed |
| OpenAPI snapshot, compileall, migration discovery/check, diff check | PASS |

```text
PROVENANCE_BLOCKER_RESOLVED             = YES
FIRST_FINALIZATION_HANDOFF_RESOLVED     = YES
LIVE_HISTORY_NONEMPTY_SCHEDULED_PULL    = PASS
LIVE_OBSERVATION_V2                     = PASS
SHARED_FIRST_FINALIZATION_HANDOFF       = PASS
SHARED_HANDOFF_IDEMPOTENCY              = PASS
LEGACY_REPORT_EXECUTION                 = ZERO
EXTERNAL_EFFECTS                        = ZERO
G7_1_LIVE_ACCEPTANCE_READY              = YES
```

Checkpoint commit: this goal's single local commit, subject
`remediation(g7.1-r1): fix live acquisition handoff blockers`; no push.

## G7.1-R2 — Complete SleepReport Observation V2 semantics

`G7_1_R2_STATUS = ACCEPTED`

### Entry, evidence, and semantic boundary

- Entry HEAD is exactly
  `f4e8236c7895104f0934fdd721511051af9487b1`, subject
  `remediation(g7.1-r1): fix live acquisition handoff blockers`; the entry
  worktree was clean.
- The authoritative vendor evidence is
  `云云对接API通用版（V2.5.2）.docx`, SHA-256
  `2e12cde7fb92b301adc6a94841fd08dd22ef9ad6fc7eae0c360d6d17c434a498`.
  Numeric plausibility was not used to establish metric identity, unit,
  source, or aggregation semantics.
- `PERCEPTOR_SLEEP_REPORT_SEMANTICS.md` is the complete field-level authority
  for documented, adapter-recognized, retained-real, intentionally ignored,
  unsupported, and unknown-extension behavior.
- The recorded-real structural derivative
  `sanitized_recorded_real_pull_get_sleep_report_full.json` retains the full
  relevant top-level/profile/series structure, nullable vendor `type` and
  display fields, and synthetic values only. It contains no real identifier,
  credential, or raw health payload.

### Mapping and consumer decisions

- Sleep-period heart-rate and respiratory samples remain trusted
  `device_measured` facts. Their documented whole-sleep means are distinct
  `heart_rate_mean` / `respiratory_rate_mean` facts with the same canonical
  physical units, `vendor_derived` provenance, and the authoritative
  sleep-stage envelope as the required aggregation window.
- Documented body-movement totals map to `movement_event_total/count`;
  deep-sleep rate and sleep efficiency map to explicit percent metrics. Each
  is vendor-derived and requires the same report envelope. Hourly movement
  counts retain their exact binding-local one-hour windows.
- The seven profile strings whose timestamp/date, duration encoding, or
  interval grammar is not established remain raw-only. Documented apnea chart
  data and legacy adapter-recognized apnea optionals also remain explicit
  raw-only unsupported evidence. Unknown structural fields fail the report
  atomically.
- Product continues to compute displayed vital centers from canonical point
  samples. Vendor report means, movement total, deep-sleep ratio, and sleep
  efficiency are supporting evidence only; no prompt, care rule, risk rule,
  medical threshold, or role projection was expanded.
- Missing facts arise when Perceptor emits an invalid sentinel or other
  out-of-contract value at a concrete sensor timestamp. SleepAgent does not
  synthesize cadence gaps in this path. The target, missing state, reason, and
  deterministic adapter processing remain explicit. With no system-derived
  source category, the originating device-measurement authority remains
  `device_measured`.

### Deterministic and PostgreSQL evidence

- The complete sanitized report canonicalizes through the actual adapter and
  `CanonicalObservationFactoryV2` into 18 trusted facts: 3 stages, 3 heart
  samples, 3 respiratory samples, 2 movement buckets, 2 bed exits, and one
  each of heart mean, respiratory mean, movement total, deep-sleep ratio, and
  sleep efficiency. Seven ambiguous profile fields are explicitly unsupported;
  vendor display/type fields are explicitly ignored; unexpected fields and
  semantic rejects are zero.
- Reprocessing the retained encrypted complete real response, without printing
  payload values, yields 885 candidates and 885 accepted V2 facts: 410 heart
  samples, 419 respiratory samples, 23 explicit invalid/missing facts, 16
  stages, 8 movement buckets, 4 bed exits, and 5 supported vendor summaries.
  Seven profile fields remain documented raw-only; unexpected semantic rejects
  are zero.
- A fresh isolated PostgreSQL 16.14 database at schema 018 persisted the full
  sanitized path with `created=18`, `deduplicated=0`, `conflicts=0`, and every
  V2 row carrying a non-null canonical unit and trusted semantic sidecar. The
  authoritative retained evidence database was not reset or mutated.
- No migration was required. Migrations 001–018 and their manifest hashes are
  unchanged. Fresh apply and migration check both report schema version 018.

### Fresh bounded live closure

- The isolated non-login acceptance process explicitly sourced the authorized
  owner-only external reference bootstrap. The bootstrap was a regular,
  non-symlink owner-only file; all three expected reference variables were
  visible, and the referenced client-ID and client-secret files passed the
  production `BackendKeyProvider` resolver plus absolute-path, non-symlink,
  owner, mode, readability, and non-empty checks. No credential value or
  credential file entered command output, repository state, or this ledger.
- Provider access remained limited to authentication and read-only
  `/vitalSigns/getSleepReport`. A preliminary retained date returned the
  documented no-report shape and created no candidates or persistence. Local
  isolated-harness seeding and grant preflights were then corrected without a
  repository change or provider-side write before the final known-nonempty
  report was accepted. No Alarm, AlarmStop, device configuration, delivery,
  email, SMS, or other provider write-side action ran.
- The final fresh full response contained 9 relevant top-level fields, 9
  profile fields, and 880 series records. The current adapter created 885
  candidates and `CanonicalObservationFactoryV2` accepted all 885: 410 heart
  samples, 419 respiratory samples, 23 explicit invalid/missing facts, 16
  stages, 8 hourly movement buckets, 4 bed exits, and 5 supported vendor
  summaries. Seven profile fields remained intentionally raw-only and three
  display/type fields remained intentionally ignored.
- Every encountered field classified as `KNOWN_SUPPORTED` or
  `KNOWN_RAW_ONLY`; `UNKNOWN_EXTENSION` and `CONTRACT_DRIFT` were empty.
  `heart_rate_avg` produced `heart_rate_mean/beats_per_minute/vendor_derived`
  with the bounded report envelope and did not reproduce `invalid_unit`.
  Heart-rate and respiratory samples plus their explicit invalid/missing facts
  retained `device_measured` authority. All five supported summaries retained
  `vendor_derived` authority and their documented units and windows.
- Durable ingress and Worker reconciliation persisted all 885 trusted V2
  facts with `created=885`, `deduplicated=0`, `conflicts=0`, no quarantine,
  and zero unexpected semantic rejection categories. Legacy report execution,
  external effects, and write-side provider effects remained zero.

### Verification to date

| Verification | Result |
|---|---|
| Focused semantic/adapter/Observation V2/Product selection | 316 passed |
| Unit marker | 1,117 passed; 40 deselected |
| Broad non-E2E/non-PostgreSQL regression | 1,117 passed; 40 deselected |
| Focused Perceptor/Observation V2 PostgreSQL + exact G7.1-R1 handoff | 3 passed |
| Full PostgreSQL marker | 37 passed; 1 expected process-proof skip; 1,119 deselected |
| Architecture gate | 6 passed |
| OpenAPI, migration 001–018 discovery/check, Python 3.11 compile/import, diff check | PASS |

### Final acceptance decision

The current full real SleepReport passes the Observation Semantics V2 contract
through the normal current client, adapter, canonical factory, encrypted raw
ingress, persistence, and reconciliation path. The live response adds no
unknown extension or supported-field contract drift, and no additional G7.1
lifecycle replay is required. The Perceptor semantic integration phase is
closed at G7.1-R2; later Process/E2E, monitoring, HITL, delivery, CareOutcome,
and other phases remain outside this goal.

```text
FULL_SLEEP_REPORT_SEMANTIC_MATRIX_COMPLETE = YES
HEART_RATE_AVG_SEMANTICS_RESOLVED           = YES
ALL_SUPPORTED_SUMMARY_UNITS_RESOLVED        = YES
ALL_SUPPORTED_SUMMARY_PROVENANCE_RESOLVED   = YES
MISSING_INTERVAL_PROVENANCE_RESOLVED        = YES
SANITIZED_FULL_REPORT_REGRESSION             = PASS
FULL_REAL_SLEEP_REPORT_V2                    = PASS
UNEXPECTED_SEMANTIC_REJECTIONS               = 0
LEGACY_REPORT_EXECUTION                      = ZERO
EXTERNAL_EFFECTS                             = ZERO
G7_PERCEPTOR_SEMANTIC_INTEGRATION_COMPLETE   = YES
```

## G7.2 — Process boundary, fault recovery, and operational acceptance

`G7_2_STATUS = ACCEPTED`

### Frozen entry and process topology

- Entry HEAD was exactly
  `48c0d03708401ef8d92ce59858e9d916b2a89cdf`; the entry worktree was clean.
- PostgreSQL 16.14 ran in a user-owned isolated cluster. Migration/bootstrap,
  API, demo API, internal API, Scheduler, and Worker used distinct current
  entry points and the existing migration/API/demo/worker database roles.
- The authoritative entries remain `sleepagent.app:app`,
  `sleepagent.workers.runtime run|healthcheck`,
  `sleepagent.bootstrap.scheduler once|run`, and
  `sleepagent.persistence.migrate apply|check`. No second runtime framework or
  privileged Docker repair was introduced.
- Scheduler and Worker profiles explicitly pinned live/replay mode, namespace,
  service principal, queue capability, schema 019, shared-only reporting, and
  disabled external delivery. The controlled Perceptor endpoint was TLS-only;
  its client values and the controlled model key were public isolated test
  fixtures. Real G7.1 credentials were not loaded or needed.

### Independent-process and process-proof evidence

- Independent public, demo, and internal ASGI processes, Scheduler processes,
  and multiple Worker processes authenticated to the isolated database. API
  liveness and authenticated readiness passed; the Worker healthcheck attested
  schema 019 and exact database role/handler composition.
- The unchanged replay process verifier crossed the public API into a durable
  root and independent Worker. It persisted 497 canonical observations, 494
  immutable episode revisions/membership sets, one exact report-to-shared
  chain, one `SharedNightAnalysis`, and exactly three zh-CN role projections.
  The strengthened terminal SQL fence revalidated that current shared-only
  chain atomically. Legacy `product_agent` execution and external effects were
  zero.
- With the process-produced root exported, the previously skipped
  `test_backend_first_slice_postgres.py` ran unchanged at its precondition and
  passed. It now asserts the current report/shared chain and rejects a legacy
  report execution.
- An independent live Scheduler created due history, SleepReport, and
  finalization operations. The live Worker crossed a TLS loopback Perceptor
  read into encrypted raw ingress, V2 normalization, canonical persistence,
  and checkpoint advancement. Full controlled SleepReport produced 18 trusted
  canonical observations. A correctly aligned history slot produced 18 more.
  One deliberately misaligned recorded-history window was quarantined rather
  than trusted.
- A bounded validated episode fixture allowed the actual Worker finalizer to
  create one hard-finalization revision and report handoff. A repeated scan
  returned the same material revision/handoff without churn. Validated material
  late revisions created superseding immutable finalization revisions and
  bounded reanalysis operations. The policy-correct revision completed its
  actual fast path; stale recorded evidence correctly prevented an additional
  live Product/model export. The replay process proof remains the authoritative
  complete shared-analysis/three-projection result.

### Fault and shutdown evidence

- Scheduler: an actual long-running Scheduler committed one due slot, was sent
  SIGKILL by exact PID, then restarted. The next scan created zero fires. The
  PostgreSQL concurrency test additionally proves two evaluations of the same
  slot converge under the schedule lock and `(schedule_id, scheduled_for)`
  uniqueness.
- Worker: Worker A was sent SIGKILL after a durable shared-analysis artifact
  was prepared. Worker B reclaimed the operation. The killed owner's fence was
  rejected while the live fence succeeded; business attempt count, invocation,
  artifact, analysis, projections, and handoff cardinalities remained one.
- Provider timeout: the controlled TLS endpoint timed out twice. Both attempts
  were retryable and left the checkpoint and canonical count unchanged. The
  recovered provider read was accepted; its semantically duplicate recorded
  payload was quarantined instead of mutating trusted facts.
- NO_DATA: a separate controlled history slot emitted explicit `pull_no_data`,
  completed normalization successfully, advanced its checkpoint, and invented
  no observation.
- Commit uncertainty and duplicate-finalizer/materiality semantics are also
  covered by the PostgreSQL fault suites. Idempotent raw/batch/semantic keys
  converge to one trusted set; no checkpoint reset or manual work-state repair
  was used.
- SIGTERM/SIGINT caused Workers to stop claiming, emit shutdown requested, drain
  with `in_flight=0`, and exit successfully. Restart required no database
  repair. The abrupt Worker and Scheduler cases separately proved durable
  discovery after process loss.

### Operational surface and policy

- Additive migration 019 exposes the protected aggregate-only
  `sleepagent_internal_operational_metrics()` snapshot through the existing
  authenticated internal API. It reports all 18 required scheduler,
  acquisition, queue/lease, finalization, and report signals, plus controlled
  product/safety outcome dimensions. It returns no subject, device, namespace,
  raw request, prompt, payload, or free-form error value.
- The function is `SECURITY DEFINER` with a fixed search path, explicit
  internal-status principal context, and no PUBLIC grant. Fresh 001→019 and
  additive 018→019 paths, manifest hash integrity, and privilege discovery are
  acceptance gates.
- Scheduler lag thresholds derive from configured cadence (one cadence
  degraded, two unhealthy); configured `max_attempts` exhaustion and any
  dead-letter/outcome-unknown/reconciliation-required work are unhealthy. The
  conservative one-day OPEN/SOFT age is operational, not medical.
- Structured sanitized events cover schedule fire, claim/reclaim, Pull result,
  checkpoint advance, finalization/handoff, and Worker shutdown/drain. The
  platform-independent start/readiness/activation/shutdown/restart contract is
  documented in `docs/operations/runtime-operations.md`.

### Closure

Final regression evidence:

- targeted G7.2 and affected suites: 260 passed, then 79 passed;
- architecture suite: 6 passed; OpenAPI and migration discovery/check: passed;
- Python 3.11 compile/import and `git diff --check`: passed;
- unit-marked suite: 1120 passed, 40 deselected;
- broad non-E2E/non-PostgreSQL suite: 1120 passed, 40 deselected;
- definitive PostgreSQL-marker suite: 38 collected, 37 passed, 1 skipped,
  0 failed, 0 errors. The sole skip is the environment-gated independent
  process verifier; that exact test passed separately against the process-
  produced root and isolated database.

```text
PROCESS_PROOF_SKIP_RESOLVED          = YES
STALE_WORKER_FENCE_REJECTED          = PASS
PROCESS_BOUNDARY_E2E                 = PASS
FAULT_RECOVERY                       = PASS
SAFE_RESTART                         = PASS
OPERATIONAL_ACCEPTANCE               = PASS
LEGACY_REPORT_EXECUTION              = ZERO
EXTERNAL_EFFECTS                     = ZERO
G7_RUNTIME_AND_OPERATIONS_COMPLETE   = YES
```

## G8 — Governed CareAction proposal and HITL approval authority

`G8_STATUS = IN_PROGRESS`

### Pre-implementation authority baseline

Recorded before G8 production-code changes at entry HEAD
`0ca22a29d833bd716527ed4f1faa643b268582da`:

```text
SharedNightAnalysis
  -> accepted CareStrategy work product
  -> structured runtime CareActionCandidate
  -> deterministic G8 care policy (allowlist, evidence, finalization,
     source currency, subject/audience, urgency, duplicate and TTL checks)
  -> immutable durable CareActionProposal in AWAITING_APPROVAL
  -> authenticated Product principal plus authoritative actor-subject binding
  -> server-side role/scope policy
  -> append-only human decision with transactional CAS
  -> distinct durable ApprovalGrant bound to proposal semantic hash, subject,
     action type, audience/scope, approver authority, policy and expiry
  -> inert future G9 capability only; no DeliveryIntent or external effect
```

The repository already has reusable generic `ActionProposal`,
`HumanDecisionRequest`, `ApprovalGrant`, `VerifiedApprovalCapability`, expiry,
revocation, exact-target validation, and in-memory/persistent CAS patterns in
`runtime/hitl.py`. It also has authenticated Product principals, authoritative
`backend_actor_subject_bindings`, role-specific scopes, subject epochs, RLS,
and append-only authorization audit. The canonical shared Product commit stores
the accepted `CareStrategy` inside the exact `SharedNightAnalysis` revision.

The existing generic path is not itself sufficient for G8: its persistent
adapter stores a whole decision aggregate as JSON, a grant appears only when an
execution capability is acquired, and its proposal is not a normalized,
source-pinned care-action authority. The older interaction confirmation and
`backend_care_actions_v2` path is interaction/replay-delivery oriented and is
not sourced from canonical `SharedNightAnalysis`. G8 therefore evolves the
existing HITL contracts and CAS/authority conventions with one canonical,
normalized care proposal/decision/grant persistence path. It does not create a
second execution or delivery framework.

The current reviewed Care catalog contains `consistent-wake-time`,
`morning-light`, `nighttime-gentle-support`, and `morning-review-feedback`.
G8 policy will permit only explicit non-medical semantic mappings from this
catalog; unknown catalog/action values, arbitrary recipients, report prose,
legacy/shadow outputs, and urgent zero-model safety results fail closed.

### G8 closure evidence

`G8_STATUS = COMPLETE`

- `CareActionCandidateV2` is accepted only from canonical structured
  `SharedNightAnalysis.care` and the exact CareStrategy invocation. Report or
  EvidenceClaim prose, legacy/shadow output, malformed/unsupported catalog
  actions, urgent safety results, and arbitrary recipient/channel fields fail
  closed.
- `care-action-governance.v1` verifies the closed four-action non-medical
  taxonomy, evidence, current hard finalization and analysis, exact human
  role/scope, and action-specific TTL. It persists its decision, reason,
  version, and hash.
- Additive migration 020 stores immutable source-pinned proposals, append-only
  decisions, and separate inert approval grants. Semantic retries deduplicate;
  material analysis N+1 expires older authority and requires a new proposal.
- Authenticated Product list/detail/approve/reject/revoke routes and SECURITY
  DEFINER functions enforce the service principal, subject binding, exact
  role, current epoch, scope, RLS, CAS, expiry, and idempotency. Direct API
  table writes are denied.
- The grant binds proposal/candidate hashes, subject, action, semantic
  audience/scope, approver authority, policy, epoch, and expiry. Revoked,
  expired, wrong-subject, and wrong-action grants are unusable.
- The protected aggregate operational snapshot exposes pending/oldest,
  approved-unconsumed, expired, revoked-grant, and conflict/error counts with
  no subject data. The full frozen contract is
  `docs/architecture/care-action-governance.md`.

Native isolated PostgreSQL 16.14 proved fresh 001→020, upgrade 019→020, and
manifest check at schema 020. Migration 020 is pinned to
`c08627937dc0f4096038dc095af18da418469a507eb4667b04ce44a52922c846`.
The non-owner API role resolved human authority in an independent Python
process, approved a worker-persisted proposal, and issued one durable grant.
Retry returned that grant; API pool restart retained it; revocation remained
durable. The same database proof covered wrong-role denial, concurrent
approve/reject convergence, post-expiry denial, analysis supersession and
replacement, direct-write denial, and append-only conflict audit.

Verification evidence:

- focused G8/Product/app/foundation/architecture: 98 passed;
- final focused unit/Product/architecture: 38 passed;
- independent-process PostgreSQL G8 acceptance: 1 passed;
- unit marker: 1,139 passed, 41 deselected;
- broad non-E2E/non-PostgreSQL: 1,139 passed, 41 deselected;
- definitive fresh PostgreSQL marker: 38 passed, 1 skipped, 1,141 deselected;
  the sole skip is the pre-existing environment-gated completed process-root
  reader, while the G8 independent-process test passed in this marker suite;
- architecture, OpenAPI, compile/import, migration check, and diff whitespace:
  PASS.

Namespace-scoped post-approval/revocation counts were zero for delivery
intents, delivery journal, replay delivery effects, and governed Memory
revisions. No email, SMS, notification, Alarm/AlarmStop, device control,
DeliveryIntent producer, or effect consumer was added.

```text
CARE_ACTION_GOVERNANCE_READY       = YES
HITL_APPROVAL_AUTHORITY_READY      = YES
PROCESS_BOUNDARY_HITL_PROOF        = PASS
EXTERNAL_EFFECTS                   = ZERO
G8_COMPLETE                        = YES
```

## G9 — Terminal Care Plan and human execution tracking

`G9_STATUS = COMPLETE_PENDING_COMMIT`

### Re-baselined care/execution authority

At committed G8 entry `90e07c6cffe13673439f558eef28799884826071`, the
repository contained the governed G8 proposal/decision/grant path, generic HITL
CAS conventions, authenticated Product authority, append-only audit patterns,
the older replay/delivery-oriented `backend_care_actions_v2` interaction, and a
per-night `sleep_domain_care_followups` projection. There was no terminal care
CLI or durable human-attested execution model. The older interaction and
follow-up structures do not have the source-pinned grant authority or separate
execution state required by G9, so they were not reused as execution truth.

G9 reuses the G8 grant, principal/binding resolution, role/scope/epoch checks,
RLS, unit-of-work, CAS, idempotency, sanitized logs, and aggregate operational
surface. It adds one normalized immutable plan table, one current-state
projection, and one append-only event table rather than a delivery or generic
workflow framework.

### Authority and execution contract

```text
ACTIVE ApprovalGrant
  -> one deterministic immutable CarePlanEntry
  -> deterministic zh-CN terminal view
  -> authorized START / COMPLETE / CANCEL
  -> append-only human_attested CareExecutionEvent
  -> durable separate execution projection
```

The plan binds the exact G8 grant/proposal/candidate semantics, subject, closed
action taxonomy, executor role, source hard-finalization/shared-analysis,
CareStrategy invocation/version, evidence references, structured parameters,
policy/renderer identities, and a window bounded by G8 authority. Grant insert
creates the plan atomically; deterministic identity plus unique grant binding
deduplicates retries. Migration 021 idempotently backfills valid existing
grants. Rejected and unapproved proposals create no plan.

Execution states are `NOT_STARTED`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED`,
with derived/persisted `EXPIRED`, `INVALIDATED`, and `SUPERSEDED` authority-loss
conditions. Consistent wake time is a bounded-period action requiring START.
Morning light is one-time and permits direct completion. Manual follow-up and
morning review are follow-up tasks permitting direct completion. These rules
are deterministic versioned policy, not CLI ordering.

Every human event records `source_authority=human_attested`, the resolved actor
principal/binding/role, subject and epoch, occurrence/record times, previous and
resulting CAS state/version, idempotency key/fingerprint, and optional bounded
sanitized note. Completion is not measurement, clinical verification, or
outcome. Exact retries return the original event; different command semantics
under the same key and concurrent stale versions fail closed.

Revocation, expiry, or source supersession blocks future commands and retains
earlier events. Nonterminal projections become invalidated/expired/superseded.
A valid completion that predates later authority loss remains historical
completion while current grant authority is reported separately. No historical
event is rewritten or transferred to a replacement analysis.

### Product, operations, and isolation

`python -m sleepagent.care_cli` provides list/show/start/complete/cancel/history,
state filters, deterministic zh-CN templates, JSON, and bounded trace output.
The CLI resolves server-side authority and calls the application boundary, not
SQL. Existing Product care projection now distinguishes plan not-started,
in-progress, completed, cancelled, expired, invalidated, and superseded without
claiming success/effectiveness.

The protected operational aggregate reports plan lifecycle counts, oldest
executable age, and command conflict/error count with no subject or note.
Migration 021 uses RLS/FORCE RLS on plan/state/event tables, no PUBLIC mutation,
append-only plan/event triggers, state transition validation, and an API-only
SECURITY DEFINER command under current binding/scope/epoch authority.

Independent Python processes using the actual terminal parser, application
service, non-owner API role, and PostgreSQL adapter proved list -> START ->
process restart -> IN_PROGRESS -> COMPLETE -> immutable history. Exact retries,
including a deliberately dropped post-commit client response, restart recovery,
wrong subject/role/scope/epoch, expiry, revocation, completed-before-revocation
history, supersession, and START/CANCEL plus COMPLETE/CANCEL contention converge
safely. The real internal-status application surface returned every required
aggregate metric without subject/note content, and captured structured logs
proved the complete sanitized G9 lifecycle vocabulary.

Plan creation, start, completion, and cancellation create zero delivery or
network effect and zero governed Habit/Memory revision. No CareOutcome or
follow-up evaluation was added. The authoritative operator contract is
`docs/operations/terminal-care-plan.md`.

Native PostgreSQL 16.14 proved fresh 001→021, manifest check at schema 021,
and exact committed G8 schema 020→021 with a populated, future-valid active
grant. That upgrade immediately produced one plan and one state with
`created_at >= issued_at` and `valid_until <= expires_at`. Migration 021 is
pinned to
`123c9b09dd6d22b2371d7a58608527a59c50d9dfd4f9f1bb49c174c684ee8e73`;
migrations 001–020 remain unchanged.

Verification evidence:

- G9 domain/application/CLI: 18 passed;
- focused G9/G8 real-PostgreSQL process and authority proofs: 3 passed, with
  the strengthened G9-only RLS/append-only proof 2 passed;
- focused Product/app/foundation/architecture: 124 passed;
- unit marker: 1,158 passed, 43 deselected;
- broad non-E2E/non-PostgreSQL: 1,158 passed, 43 deselected;
- definitive fresh PostgreSQL marker: 40 passed, 1 skipped, 1,160 deselected;
  the sole skip is the pre-existing environment-gated completed G7.2
  process-root reader, while G9's independent terminal processes passed;
- architecture: 6 passed; OpenAPI snapshot, Python 3.11 compilation/import,
  migration apply/check, manifest hash, and `git diff --check`: PASS.

The packaged `python -m sleepagent.care_cli` entry point also resolved the
non-owner API authority directly and returned the completed zh-CN morning-light
plan with `completion_semantics=human_attested_execution_only`.

```text
TERMINAL_CARE_PLAN_READY           = YES
HUMAN_EXECUTION_TRACKING_READY     = YES
EXTERNAL_EFFECTS                   = ZERO
OUTCOME_EVALUATION                 = NOT_STARTED
G9_COMPLETE                        = YES
```
