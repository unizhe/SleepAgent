# Plan Review Log: SleepAgent 即插即用能力与真实雷达闭环
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **NightEpisode and the existing Agent Episode can be conflated.** That would leak internal run ids into the public API and make one reasoning retry look like a second night. **Fix:** define the domain aggregate and internal analysis execution separately and link them by `analysis_run_id`.
2. **Live and replay data can still mix.** Production currently defaults to `FakeRadarProductDataProvider`, and the plan did not require source mode on every downstream artifact. **Fix:** introduce `data_mode`, isolate namespaces/baselines and make production live-only startup fail closed.
3. **Timestamp provenance is too coarse.** The current normalizer can mistake the signed request timestamp for measurement time and assume UTC for naive strings. **Fix:** model signed, measured, occurred and received times separately and permit binding-timezone interpretation only under a verified compatibility profile.
4. **Idempotency can hide collisions or legitimate same-second samples.** `message_id` alone and weak timestamp keys are insufficient. **Fix:** retain pre-normalization payload hashes, quarantine same-id/different-payload collisions and use a versioned full-payload fallback.
5. **Crash recovery between ACK and normalization is not fully specified.** A durable raw row alone can remain permanently unprocessed. **Fix:** atomically write raw/work/outbox intent, then use leases and a stranded-work reprocessor.
6. **AlarmStop correlation is unsafe.** A stop event cannot close an alert reliably without an instance key. **Fix:** add a verified alert-instance state machine and preserve unmatched stops as orphan signals.
7. **Pull cursor and report association can skip or misattach data.** Advancing a cursor before commit or attaching to the latest night is unsafe. **Fix:** use overlapping windows/watermarks and match reports by subject, binding and sleep-time overlap.
8. **Auth assertions are not tightly bound to the HTTP request.** A valid actor blob could be replayed against another endpoint/body. **Fix:** bind assertion id/key/audience/method/path/body hash, add replay protection and bounded key rotation.
9. **The migration cutover lacks shadow comparison and rollback points.** Directly replacing the webhook authority risks split-brain or loss. **Fix:** require expand, backfill, shadow-read compare, gated cutover and later contract, with replay data isolated.
10. **`/tmp`/SQLite remains too easy to use as “production.”** The risk note is insufficient for multi-worker durability. **Fix:** make live production require encrypted shared durable storage and restrict standalone SQLite to explicit local/dev mode.

VERDICT: REVISE

### Codex response

Accepted all ten findings. The plan now separates the two Episode concepts, makes live/replay provenance and startup behavior explicit, decomposes timestamps, hardens idempotency and crash recovery, adds alert correlation and pull watermarks, binds actor assertions to requests, defines a reversible migration sequence and prohibits `/tmp`/standalone SQLite as live production authority.

## Round 2 — Codex review

1. **The future Model Tool seam is overbuilt for the locked scope.** A runnable descriptor/receipt system without raw signals or a deployable model would freeze a speculative interface. **Fix:** implement only producer/provenance fields and document future descriptor/receipt requirements; build no model runtime in v1.
2. **Night identity and revision transitions remain ambiguous.** Vendor date, UTC date and internal Agent Episode could still be mistaken for the sleep night. **Fix:** add a timezone/DST-aware versioned `night_key` policy, pending association and explicit revision-parent/current-pointer semantics.
3. **Monitoring can flap on noisy in/out-of-bed signals.** Hybrid triggers have no priority, debounce or manual-override rule. **Fix:** add a versioned deterministic transition policy with dwell, priority, override expiry and restart recovery.
4. **Collision response can cause a permanent vendor retry storm.** A valid same-id/different-payload event is not recoverable through identical retries. **Fix:** durably quarantine and operationally alert it, then return the verified success envelope while preserving internal collision status.
5. **Push/pull dedupe can erase a real conflict.** Ingestion identity is not the same as physiological-fact identity. **Fix:** allow one fact to cite multiple acquisition receipts, but retain differing normalized facts as a conflict rather than choosing a channel.
6. **“Current risk” can still report false reassurance during a data outage.** Risk level lacks mandatory sufficiency/scope semantics, and thresholds have no review status. **Fix:** require sufficiency/scope/policy fields, make stale coverage unknown and block unreviewed rules from health-facing escalation.
7. **Actor signing risks becoming custom cryptography.** The plan lists claims but not a standard envelope. **Fix:** use standard service authentication plus asymmetric JWS actor assertions, request binding, replay defense and key rotation.
8. **Async operation behavior is incomplete.** Idempotency, worker crash and unknown side effects are not tied to a state machine. **Fix:** define operation states, request-hash conflicts, leases, stable errors and commit-before-success.
9. **Deletion language promises the impossible for delivered events.** SleepAgent cannot recall bytes already consumed. **Fix:** deny future reads, erase retained data and publish scoped tombstones while stating the downstream obligation honestly.
10. **The plan is too large to cut over safely as one implementation.** Partial completion could be presented as full v1. **Fix:** add independently reversible foundation, ingestion, Episode, Agent/API and live-cutover gates with separate capability verdicts.

VERDICT: REVISE

### Codex response

Accepted all ten findings. The plan now keeps Model Tool work at a non-executable seam, defines night/revision and monitoring policies, corrects collision ACK and push/pull reconciliation, makes risk insufficiency explicit, standardizes actor assertions, closes operation/revocation semantics and splits delivery into five gated reversible slices.

## Round 3 — Codex review

1. **Adapter verification and deployment state are conflated.** A verified Adapter can be disabled, and a failed capability should not disable unrelated verified capabilities. **Fix:** split per-capability/environment verification from registration/deployment lifecycle.
2. **“Append-only raw” contradicts an in-row processing status.** Mutable status would rewrite the purported immutable record and obscure repair history. **Fix:** keep encrypted raw immutable and record processing/quarantine/repair as append-only receipts plus a projection.
3. **The current canonical type can leak raw payloads to models.** `RadarSourceMetadata` embeds both raw and data payloads. **Fix:** make provider-neutral provenance reference-only and strip legacy payload fields at every tool/FactSnapshot compatibility boundary.
4. **Quarantine release can become a wrong-binding backdoor.** An operator could bind the current elder and replay old ambiguous data. **Fix:** require authorized effective-time binding, trustworthy event time and unique interval attribution; never edit raw or infer backdating.
5. **Morning publication can block indefinitely on a vendor report.** A bounded pull deadline exists but no user-visible outcome is defined. **Fix:** publish an explicit pending/data-insufficient first revision and append a later revision when the report arrives.
6. **Risk and bed-state events can flood consumers.** Per-sample emission would harm the chatbot/OS and hide meaningful transitions. **Fix:** emit transitions/reminders with versioned hysteresis, cooldown, dedupe and suppressed-count receipts.
7. **Cross-aggregate event polling lacks a cursor order.** Per-aggregate sequence alone cannot advance one polling cursor. **Fix:** add a monotonic delivery offset with no global business-order claim, bind cursors to auth projection and define expiry/resync.
8. **The implementation mapping is too vague for a frozen build plan.** “Existing product-device/domain packages” could produce another provider-specific block in `backend/main.py`. **Fix:** name the provider-neutral domain/API modules and their exact bridge to the existing runtime.
9. **The latency isolation goal is not testable.** “No LLM on critical path” does not prove prompt ACK or stored-result query behavior. **Fix:** set a durable ACK target below the vendor retry interval and measure stored reads separately from async Agent work.

VERDICT: REVISE

### Codex response

Accepted all nine findings. The plan now separates Adapter state dimensions, makes raw immutability and repair auditable, blocks legacy raw leakage, constrains binding repair, defines report-deadline output, coalesces event transitions, specifies cursor semantics and exact source modules, and adds latency/privacy acceptance gates.

## Round 4 — Codex review

1. **The Adapter/binding order is impossible as written.** An Adapter cannot produce a `SleepObservation` containing `subject_id` before the system resolves DeviceBinding. **Fix:** introduce a device-scoped `AdapterObservationCandidate`; only the effective-time binding service creates the elder-scoped canonical observation.
2. **“Independent service” is only a logical API boundary.** If webhook, API and slow Agent work share one blocking process, model latency can still harm the chatbot integration and vendor ACK. **Fix:** define independent deployment plus separate API/ingestion and persistent background worker roles.
3. **One failing Adapter can still consume the service.** Controlled registration alone does not bound provider hangs, payload size or exception blast radius. **Fix:** add timeouts, size limits, circuit breakers and per-provider bulkheads with typed failure receipts.
4. **Perceptor pull constraints are not executable.** The documented one-hour/20-device history limits, explicit report date and reconstructed cadence are missing from the plan. **Fix:** encode partitioning, validation and reconstructed-time quality markers.
5. **Agent backlog has no backpressure or fairness rule.** Reanalysis storms could starve morning reports while the fast path remains technically separate. **Fix:** use persistent leased operations, bounded worker pools, per-subject ordering and fair scheduling.
6. **Feedback provenance can collapse actors.** “Family/caregiver feedback” could be misrepresented as elder self-report. **Fix:** preserve authenticated actor/relationship and distinct Evidence source categories.
7. **External compatibility and cache authorization are underspecified.** Schema changes or a stale role cache could break clients or leak a clinician view. **Fix:** version request/response/error/events, define deprecation and bind caches to revision, role/scope and authorization epoch.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now uses a candidate-before-binding pipeline, defines process-level service isolation, bounds Adapter and Agent-worker failures, codifies Perceptor pull limits, preserves feedback identity and adds public compatibility/cache authorization rules.

## Round 5 — Codex review

The final consistency pass checked the user-locked product boundary, all four meanings of plug-and-play, current Perceptor code/document evidence, provider-neutral contracts, identity/time attribution, the three state machines, fast/slow paths, external auth/API/events, migration gates and real-device claim limits.

Five non-blocking consistency defects were corrected during this pass:

1. Adapter acceptance still required a binding version despite the new pre-binding candidate contract; candidate and canonical-observation assertions are now separate.
2. One persistence criterion incorrectly claimed raw intake and asynchronously produced observations share one transaction; intake and normalization transaction boundaries are now explicit.
3. Declared Perceptor capabilities could be mistaken for verified capabilities; promotion now requires an immutable real-evidence receipt and authorized human approval.
4. Service authentication named only external infrastructure choices; the reference profile now has a concrete rotated Bearer-principal verifier while retaining the same interface for mTLS/OAuth2 deployments.
5. `clinician` and `doctor` role vocabulary drifted across the public contract; the external vocabulary now consistently uses `doctor`.

No blocking or major unresolved architecture issue remains. In particular:

- live and replay data cannot mix or silently select the fake provider;
- an Adapter cannot choose an elder, interpret health meaning or leak raw vendor payload to an Agent;
- real push/pull, binding, NightEpisode revisions and outbox have explicit crash, duplicate, late-data and rollback behavior;
- stored-result queries and deterministic risk handling remain independent of model latency;
- ResSleepNet stays outside v1 until its actual waveform/domain/service prerequisites exist;
- fixture success cannot be presented as verification of another device or a blocked vendor capability.

VERDICT: APPROVED

### Codex response

No further plan revision is required. The remaining items under Risks / open questions are explicit external confirmations, policy approvals or future validation work; none leaves the v1 architecture or failure behavior undefined.

## Act 3 — Build

### Round 1 — Codex build

Implemented only the user-authorized Foundation Gate contract slice:

- added immutable, versioned, `extra="forbid"` provider-neutral contracts in
  `sleepagent/sleep_domain/contracts.py`;
- added one-way legacy product-device Radar conversion in
  `sleepagent/sleep_domain/radar_compat.py`, with reference/hash-only canonical
  provenance and no reverse projection;
- added contract-level tests in `tests/test_sleep_domain_contracts.py`.

One self-review fix pass separated legacy top-level signed request timestamps
from `data.DateTime` measurement timestamps and retained naive local timestamps
as `timezone_unknown`. No persistence, repository, registry behavior, runtime
ingestion, binding service, aggregation, Agent bridge, API, commit, or push was
implemented.

### Codex verification

- Focused proof:
  `pytest -q tests/test_sleep_domain_contracts.py tests/test_product_device_schemas.py tests/test_perceptor_adapters.py`
  — 29 passed.
- Compile proof:
  `python -m py_compile sleepagent/sleep_domain/contracts.py sleepagent/sleep_domain/radar_compat.py sleepagent/sleep_domain/__init__.py`
  — passed.
- Full repository regression:
  `pytest -q` — 572 passed, 3 failed in pre-existing unrelated memory-governance
  and acceptance-material fixture assertions.
- Contract introspection found 32 exported Pydantic models, all versioned and
  `extra="forbid"`; candidate fields contain no subject, binding, or
  NightEpisode identity; canonical provenance contains references/hashes only.
- `git diff --no-index --check` reported no whitespace errors for the three new
  implementation/test files.

### Round 2 — Codex build

Implemented only the user-authorized Foundation Gate unified-persistence slice:

- appended `010_sleep_domain_foundation` to the existing
  `RadarPersistenceStore` migration ledger, adding 15 provider-neutral tables
  without changing migrations `001`–`009` or deleting legacy tables;
- added configured Fernet encryption/retention policy and a
  `SleepDomainRepository` over the existing store connection, with separate
  intake and normalization transactions, DB-enforced raw immutability,
  live/replay namespaces, idempotency, leases, CAS, append-only receipts and
  revisions, current pointers, and processing/domain outboxes;
- added SQLite execution, PostgreSQL runner/statement compatibility, populated
  previous-version upgrade, transaction rollback, concurrent binding/lease/CAS,
  encryption, retention, idempotency, and legacy-store compatibility tests.

Two bounded self-review passes strengthened database guarantees: the first
added cross-dialect raw UPDATE/DELETE rejection and PostgreSQL provider-account
locking for interval/idempotency races; the second made JSONB idempotency
comparison semantic, protected immutable Operation identity during CAS, and
kept the serialized NightEpisode current pointer synchronized with its CAS
projection.

No legacy webhook import, data-source switch, Perceptor behavior, binding
resolution service, NightEpisode aggregation, Agent bridge, API, commit, or
push was implemented.

### Codex verification

- Focused persistence and previous-version proof:
  `pytest -q tests/test_sleep_domain_persistence.py tests/test_sleep_domain_contracts.py tests/test_radar_agent_persistence.py tests/test_product_agent_persistence.py tests/test_habit_profile_persistence.py tests/test_dynamic_agent_runtime.py`
  — 90 passed.
- Full repository regression:
  `pytest -q` — 590 passed, 3 failed in the same pre-existing unrelated
  memory-governance and acceptance-material fixture assertions observed before
  this slice.
- Compile proof:
  `python -m py_compile` over the sleep-domain and migration modules — passed.
- Migration audit found all 15 requested `sleep_domain_*` tables, both raw
  immutability triggers, no mutable status column on Raw Inbox, no old-table
  drop/delete, and a PostgreSQL runner test that executes the PL/pgSQL function
  and triggers as complete statements.
- `git diff --no-index --check` reported no whitespace errors for all new or
  changed persistence implementation/test files.

### Round 3 — Codex build

Implemented only the user-authorized controlled Adapter Protocol/Registry
slice:

- added a deployment-owned static allowlist and deny-by-default exact/latest
  strict-SemVer resolution in `sleepagent/sleep_domain/registry.py`; there is
  no registration, upload, dynamic import, or hot-load surface;
- added append-only deployment events, granular capability/environment
  verification receipts, and immutable `AdapterResolutionLock` persistence
  through additive migration `011_adapter_registry_control`;
- kept deployment and verification independent, with disable, supersede,
  explicit rollback, persisted reference counts, and replay through an
  already-retained lock after disable/supersede;
- added a non-escalatable Adapter effect profile, candidate-only output
  validation, provider/config/provenance lock checks, bounded input/output,
  timeout, exception isolation, circuit breaker, and per-provider bulkhead;
- added a declarative-only Perceptor v1 descriptor whose supported real
  capabilities remain `PENDING` and whose `raw_resp_waveform` capability is
  explicitly unsupported;
- added a deterministic hardware-free fixture Adapter and conformance report
  that remains `UNVERIFIED` and cannot be promoted to a real-device
  `VERIFIED` capability.

Two bounded self-review passes strengthened the gate. The first added an
explicit Adapter artifact hash and immutable test-result hashes to capability
verification, plus exact provider-account configuration matching. The second
made `data_mode` explicit on new envelopes, made the no-Agent/no-external-write/
no-raw-to-LLM/no-subject-assignment effect policy non-escalatable, prevented
declarative/fixture registrations from claiming real-device verification, and
isolated `BaseException` failures as typed Adapter execution receipts.

No real Perceptor network call, binding resolution, persistent ingestion
workflow, quarantine workflow, NightEpisode link/aggregation, Agent bridge,
LLM call, external-state write, API, commit, or push was implemented.

### Codex verification

- Focused registry/foundation proof:
  `pytest -q tests/test_sleep_domain_adapter_registry.py tests/test_sleep_domain_contracts.py tests/test_sleep_domain_persistence.py tests/test_radar_agent_persistence.py --disable-warnings --maxfail=1`
  — 61 passed.
- Full repository regression:
  `pytest -q --disable-warnings` — 608 passed, 3 failed in the same pre-existing
  unrelated memory-governance and acceptance-material fixture assertions seen
  before this slice.
- Compile proof:
  `python -m py_compile` over contracts, repository, registry, and package
  exports — passed.
- Contract/dependency audit found all six Registry Pydantic envelopes versioned
  and `extra="forbid"`; `AdapterResolutionLock` contains no subject, binding,
  or NightEpisode identity; the Registry has no Agent, HTTP client, dynamic
  import, or model-runtime dependency.
- Additive migration audit found three Registry control tables with
  `ON DELETE RESTRICT`, no old-table drop/delete, and successful SQLite plus
  PostgreSQL statement-runner coverage.
- Whitespace/EOF audit passed for all ten changed Registry/Foundation
  implementation and test files.

### Round 4 — Codex build

Implemented only the requested DeviceBinding and safe candidate-promotion
slice:

- added an internal administrative DeviceBinding service with deployment-owned
  actor/proof grants, namespaced opaque provider-device keys, explicit subject
  and IANA timezone contracts, monotonically versioned half-open effective
  intervals, one-device/one-subject interval enforcement, prospective
  rebinding, command idempotency, persistent CAS, and immutable audit events;
- added `AdapterObservationCandidate` promotion that uses only independently
  zoned measurement/event time, never signed/request receipt time, and creates
  a canonical `SleepObservation` only when exactly one non-revoked binding
  interval matches;
- added atomic candidate/quarantine or candidate/canonical persistence, typed
  reasons for unknown time, unbound identity, interval gaps and overlapping
  bindings, plus authorized/audited quarantine reprocessing that reloads the
  immutable stored candidate and only appends receipts/results;
- added additive migration `012_device_binding_promotion` for namespaced device
  identity, binding audit and quarantine-reprocess audit authorities.

Two bounded self-review passes were used. The first updated migration-ledger
expectations and strengthened the configured authorization-proof check. The
second made binding-command retries stable, made missing/stale expected
versions explicit CAS conflicts, and added a direct competing-subject
constraint test.

No public administration API, Perceptor behavior, NightEpisode aggregation,
Agent bridge/runtime, LLM call, external action, or risk judgment was added.

### Codex verification

- Focused DeviceBinding/ingestion/persistence proof:
  `pytest -q tests/test_sleep_domain_binding_promotion.py
  tests/test_sleep_domain_contracts.py tests/test_sleep_domain_persistence.py
  tests/test_sleep_domain_adapter_registry.py
  tests/test_radar_agent_persistence.py` — 71 passed.
- The new tests cover opaque numeric-looking ids, provider/account namespacing,
  invalid IANA zones, both DST-fold instants, half-open interval boundaries,
  gaps, prospective rebinding, unchanged old observations, duplicate command
  idempotency, concurrent CAS, competing subjects, unknown/untrusted time,
  legacy overlap ambiguity, authorization, reprocessing and raw immutability.
- Compile proof over contracts, repository, binding/ingestion services and
  migrations passed.
- Full repository regression: `pytest -q` — 617 passed, 3 failed in the same
  unrelated memory-governance and historical acceptance-material assertions
  that also fail when run alone.
- Scope/dependency audit found no NightEpisode, Agent, risk, FastAPI/router or
  public-API dependency in the new services; whitespace checks passed.

### Round 5 — Codex build

Implemented only the user-authorized Perceptor push-ingestion slice:

- replaced the production FastAPI webhook's parsed-dict/standalone-SQLite path
  with a bounded raw HTTP body/header boundary and one unified
  `RadarPersistenceStore` runtime; the deprecated local webhook repository is
  no longer called or dual-written by the production route;
- added explicit PENDING compatibility profiles with an algorithm allowlist,
  exact signing path/mode, HMAC verification, signed-time windows, nonce
  replay records, provider-account namespace isolation, opaque external ids,
  message-id idempotency and pre-normalization payload-hash collision handling;
- made accepted intake atomically persist encrypted immutable raw bytes,
  normalization work and a processing-outbox intent before ACK; malformed and
  collision requests atomically include terminal quarantine state, while exact
  retries retain the successful semantic result;
- added a local no-network Perceptor Adapter for the documented object/string,
  casing and numeric/string push variants, while every capability declaration
  remains `PENDING`;
- added a leased/restart-safe worker that loads encrypted raw input, executes
  the retained Adapter lock, persists device-scoped candidates and invokes only
  the effective-time DeviceBinding promotion service; it does not invoke an
  Agent, LLM, NightEpisode or risk path;
- added an explicit production runtime that requires shared PostgreSQL and
  configured Fernet encryption/retention, permits standalone SQLite only for
  local/test, and can reuse an injected shared store without owning/closing it.

Two bounded self-review fixes were applied. The first made retained Adapter
lock creation concurrency-safe, recorded exact duplicate nonces without
rejecting the same delivery, redacted ACK/log surfaces and failed production
SQLite startup closed. The second removed the raw/work-to-quarantine race by
committing terminal malformed/collision quarantine in the same intake
transaction, then ordered idempotency collision detection ahead of nonce
comparison so an exact collision retry is still acknowledged while unrelated
nonce reuse remains rejected.

No public management API, pull/reconciliation, NightEpisode, Agent/LLM,
external action, risk judgment or live capability verification was added.

### Codex verification

- Focused Perceptor/unified-persistence/Registry/DeviceBinding proof:
  `pytest -q tests/test_perceptor_push_ingestion.py
  tests/test_perceptor_push_runtime.py tests/test_perceptor_webhook.py
  tests/test_sleep_domain_persistence.py
  tests/test_sleep_domain_adapter_registry.py
  tests/test_sleep_domain_binding_promotion.py
  tests/test_radar_agent_persistence.py tests/test_health.py`
  — 86 passed.
- Full repository regression after the atomic-quarantine repair:
  `pytest -q` — 641 passed, 3 failed in the same unrelated
  memory-governance and historical acceptance-material fixture assertions
  present before this slice.
- Tests cover object/escaped-string data, `Onbed`/`OnBed`, numeric/string
  rates, seconds/milliseconds/ISO timestamps, connectivity/alert events,
  invalid algorithm/signature/profile time, nonce replay, exact retry,
  missing/message-id collision semantics, concurrency, stranded lease
  recovery, malformed and unbound quarantine, binding promotion, raw
  encryption/immutability, size/header limits, durable-before-ACK rollback,
  redacted ACK/intent data, runtime restart and production fail-closed storage.
- Compile and whitespace proofs passed for the changed ingestion, Adapter,
  runtime, repository, Registry, migration, FastAPI and test modules.

### Round 6 — Codex build

Implemented only the user-authorized Perceptor pull, sleep-report
normalization and bounded history-reconciliation slice:

- made `getSleepReport` require a canonical explicit local date and added a
  bounded `getHistoryData` client contract; the pull service partitions
  provider calls into windows no longer than one hour and batches no larger
  than 20 devices;
- added content-addressed, idempotent source-report versions (including empty
  reports), encrypted immutable Raw Inbox intake, overlap/lateness policy,
  durable CAS checkpoints and restart-safe window replay;
- added a deterministic pull Adapter that turns aware stage intervals,
  minute heart/respiratory rates, movement, get-up events and allowlisted
  explainable profile metrics into typed vendor-derived candidates; naive
  stage times retain source text without timezone inference, `8-0` remains an
  unknown source value, and unknown algorithm/confidence stay unknown;
- required equal history-series lengths, reconstructed timestamps backward
  from `send_time` at the configured nominal three-second cadence, marked
  every sample `reconstructed_time`, and converted the `-1` sentinel to an
  invalid/missing interval rather than a physiological value;
- declared respiratory values as `respiratory_rate_series` with explicit
  not-waveform/not-SignalArtifact/not-ResSleepNet limitations;
- added append-only fact-value/acquisition/conflict persistence so equal
  push/pull facts share one canonical observation with multiple acquisition
  records, while differing values at the same fact slot remain separate
  conflicting observations;
- kept `start` plus `getRealTimes` behind an explicit fallback flag and routed
  fallback results through the same Raw Inbox/Adapter/Binding pipeline.

Two bounded self-review fixes were applied. The first added pull Adapter-lock
concurrency protection and stricter runtime validation for dates, device
targets, stream keys and fallback gap keys. The second distinguished malformed
or non-finite values from the vendor `-1` sentinel so quarantine/missing reason
codes do not misstate the source.

No public management API, NightEpisode, AnalysisRevision, pending
NightEpisode association, Agent/LLM, risk judgment, SignalArtifact or
ResSleepNet path was added.

### Codex verification

- Focused pull/client/push/Binding/Registry/persistence proof:
  `pytest -q tests/test_perceptor_pull_reconciliation.py
  tests/test_perceptor_client.py tests/test_perceptor_push_ingestion.py
  tests/test_sleep_domain_binding_promotion.py
  tests/test_sleep_domain_persistence.py tests/test_sleep_domain_contracts.py
  tests/test_sleep_domain_adapter_registry.py
  tests/test_radar_agent_persistence.py` — 117 passed.
- Post-self-review pull/push proof:
  `pytest -q tests/test_perceptor_pull_reconciliation.py
  tests/test_perceptor_client.py tests/test_perceptor_push_ingestion.py`
  — 46 passed.
- Full repository regression: `pytest -q` — 653 passed, 3 failed in the same
  pre-existing unrelated governed-memory handle and historical
  acceptance-material 10/22 fixture assertions present before this slice.
- Compile proof over the changed Perceptor client, pull/push Adapter and
  ingestion, contracts, promotion, repository, Registry, migrations and tests
  passed; `git diff --check` reported no whitespace errors.
- Tests cover provider exceptions/timeouts, token expiry refresh, required
  report dates, empty/changed reports, source-report idempotency, window/device
  partitioning, checkpoint recovery and stale CAS, unequal series isolation,
  reconstructed times, `-1`, ambiguous source text, naive-time quarantine,
  fallback gating, and same-value/different-value push/pull reconciliation.

### Round 7 — Codex build

Implemented only the user-authorized NightEpisode aggregation, revision and
deterministic lifecycle slice:

- added an elder-centered, timezone-aware NightEpisode service over existing
  canonical observations, exact DeviceBinding intervals and source-report
  versions, with versioned `night_key`, cross-midnight/DST handling, event-time
  membership, pending association, lateness watermarks and morning deadlines;
- added orthogonal persistent Monitoring (`Dormant ↔ Active`) and NightEpisode
  (`Collecting → AwaitingReport → Analyzed/Closed`, then `Revised`) state
  machines with trigger priority, debounce, minimum dwell, authorized and
  expiring manual overrides, versioned fallback schedules, subject leases and
  restart recovery;
- added additive migration `015_night_episode_lifecycle` for Monitoring
  snapshots, leases, append-only transition receipts, observation/report
  membership, pending association and idempotent revision-publication
  authorities;
- made lifecycle transitions and their domain-outbox records commit in the same
  repository transaction, and made revision creation, parent linkage,
  monotonically increasing revision number, current-pointer CAS, optional late
  membership/report linkage and revision event one atomic publication;
- made a missed report deadline publish an explicit immutable
  `report_pending` or `data_insufficient` revision; late/changed reports,
  late observations and authorized explicit reanalysis append new revisions
  without rewriting prior revisions;
- retained exact Adapter, observation Schema, boundary-policy and
  transition-policy versions for each Episode. A restarted service may activate
  newer policies for later nights while an in-progress Episode continues under
  its pinned versions.

No Agent or LLM was invoked or wired, no role report was generated, and no
physiological threshold, risk policy or medical interpretation was added.

Two bounded self-review passes were used. The first removed stale
`vendor_report_pending` flags after a report arrives, aligned source-report
references with their exact content hashes, rolled pathological post-deadline
starts forward safely and made explicit reanalysis authorization fail closed.
The second retained and resolved pinned transition-policy versions across
restart, added a versioned fallback schedule window, and verified both DST
folds and a nonexistent spring-forward deadline.

### Codex verification

- Focused Episode/Binding/Pull/Push/Registry/persistence proof:
  `pytest -q tests/test_night_episode_lifecycle.py
  tests/test_sleep_domain_persistence.py tests/test_sleep_domain_contracts.py
  tests/test_sleep_domain_binding_promotion.py
  tests/test_sleep_domain_adapter_registry.py
  tests/test_perceptor_pull_reconciliation.py
  tests/test_perceptor_push_ingestion.py
  tests/test_radar_agent_persistence.py --disable-warnings --maxfail=1`
  — 116 passed.
- Full repository regression: `pytest -q --disable-warnings` — 662 passed,
  3 failed in the same pre-existing memory-governance handle and historical
  acceptance-material 10/22 concept assertions present before this slice; no
  new failure was introduced.
- Compile proof over contracts, repository, lifecycle, Episode aggregation,
  package exports and migrations passed.
- Whitespace/EOF checks passed for the new lifecycle, Episode, migration and
  test files.
- Tests cover cross-midnight membership, both DST folds, a spring-forward gap,
  event-time versus receipt-time attribution, pending association, binding
  references, late watermarks, report deadlines, missing and changed reports,
  duplicate/noisy triggers, priority/dwell/manual override expiry, authorized
  commands, fallback schedule rejection, concurrent revision parent chains,
  CAS current pointers, restart/expired-lease recovery, version pinning and
  transaction rollback when outbox insertion fails.

### Round 8 — Codex build

Implemented only the user-authorized deterministic quality, risk,
CareFollowup and public ServiceMode projection slice over existing Canonical
Observations and NightEpisodes:

- added pinned deterministic quality policy and append-only assessments for
  coverage, missing intervals, invalid samples, staleness, offline state and
  clock/timezone validity; every gap/stale/offline/clock-invalid/insufficient
  result fails closed as `unknown/data_insufficient`;
- added `CurrentRisk` with explicit data sufficiency, exact source scope,
  policy version and observed time. A sufficient scope without a reviewed
  match is explicitly not an all-clear conclusion;
- correlated vendor alerts by exact provider/account/device/vendor instance,
  retained unmatched AlarmStop as an orphan receipt and never closed alerts by
  code alone; vendor alerts remain `vendor_alert_signal` operational evidence,
  not a diagnosis;
- required immutable review evidence before a deterministic vendor rule may
  become a reviewed signal or permit health-facing escalation. Missing and
  unreviewed mappings remain `PENDING_DOMAIN_REVIEW`;
- added persisted risk, bed, offline and quality projections with first-event
  delivery, transition hysteresis, cooldown reminders, episode dedupe and
  suppressed-repeat receipts;
- added the authorized `None → PendingFeedback → FollowingUp →
  Completed/Ended` CareFollowup state machine with per-subject leases,
  monotonic CAS, append-only command receipts and atomic domain outbox;
- added a read-only ServiceMode projector: Monitoring Active takes priority,
  otherwise any open CareFollowup yields Follow-up, otherwise Dormant. It has
  no table or writeback path, so tonight's Active state can coexist with a
  prior night's open follow-up;
- added additive migration `016_deterministic_fast_path` for append-only
  assessments/receipts and CAS projections, without physiological values,
  medical thresholds, Agent state or a persisted ServiceMode.

Two bounded self-review passes were applied. The first assigned consecutive
NightEpisode outbox sequence numbers when several fast-path states emit in one
transaction and retained unknown/unkeyed alert signals for operational review.
The second strengthened CareFollowup temporal ordering, made the ServiceMode
formula a contract invariant and added explicit proof that pending-domain-
review rules cannot produce a reviewed signal or health escalation.

No Agent or LLM was run or wired, no role report was generated, and no
physiological or medical alert threshold was introduced.

### Codex verification

- Focused deterministic/NightEpisode/persistence/Binding/Registry/Perceptor
  proof:
  `pytest -q tests/test_night_episode_lifecycle.py
  tests/test_sleep_domain_fast_path.py tests/test_sleep_domain_persistence.py
  tests/test_sleep_domain_binding_promotion.py
  tests/test_sleep_domain_adapter_registry.py
  tests/test_perceptor_push_ingestion.py
  tests/test_perceptor_pull_reconciliation.py
  tests/test_radar_agent_persistence.py --disable-warnings --maxfail=1`
  — 109 passed.
- Full repository regression: `pytest -q --disable-warnings` — 675 passed,
  3 failed in the same pre-existing governed-memory handle and historical
  acceptance-material 10/22 fixture assertions present before this slice; no
  new failure was introduced.
- Compile proof passed for contracts, repository, deterministic fast path,
  CareFollowup/ServiceMode, package exports and migrations; `git diff --check`
  reported no whitespace errors.
- Tests cover coverage gaps, stale streams, explicit offline state,
  clock-invalid samples, missing intervals, fail-closed CurrentRisk, pending
  domain review, alert instance correlation, orphan and exact AlarmStop,
  event storms, cooldown, hysteresis, suppressed-repeat counts, concurrent
  component-state coexistence, read-only ServiceMode priority, transaction
  rollback on outbox failure and restart recovery of quality, risk, signal and
  care projections.

### Round 9 — Codex build

Implemented only the user-authorized exact NightEpisode-revision bridge to the
existing four-Agent ProductEpisodeRunner:

- added a persistent Product data provider that loads one exact committed
  NightEpisodeRevision and projects only authorized Canonical Observations,
  deterministic quality/risk summaries, unresolved conflict references and
  opaque provenance references;
- used strict per-payload allowlists to remove vendor title/message/timestamp
  text and legacy `raw_payload`/`data_payload`/encrypted payload shapes before
  FactSnapshot Tool inputs or prompts; raw records are never decrypted by the
  provider;
- added `sleepagent.sleep_domain.agent_bridge` as an injected bridge to the
  existing ProductEpisodeRunner, with no new Agent identities or second Agent
  runtime;
- restricted slow-path submission to morning analysis, explicit reanalysis,
  feedback, follow-up and internal analysis triggers. Submission persists an
  Operation and makes no model call; webhook, per-sample and committed-result
  query triggers do not exist in the allowlist;
- added fair per-subject persistent Operation leasing and a bounded worker
  pool, keeping Agent latency/failure out of ingestion, deterministic fast
  path and committed-view queries;
- atomically append one AnalysisRevision, elder/family/doctor views, a minimal
  domain-outbox event and the terminal Operation state. `analysis_run_id`
  names the elder Product Agent Episode and every role view records its exact
  Product Agent Episode while remaining bound to the same NightEpisode
  revision;
- isolated Product Agent care/memory keys by durable live/replay namespace and
  rejected cross-mode or cross-subject observations before Agent input;
- made data insufficiency deterministic/degraded and model unavailability
  explicitly degraded with a failed Operation; neither path can claim a ready
  or verified slow path;
- added additive migration `017_product_agent_bridge` for the indexed
  `analysis_run_id`, append-only role views and Agent-operation fairness.

Two bounded fix passes were used. The first corrected an existing fast-path
migration test that selected the newly latest migration instead of explicitly
selecting migration 016. The second made legacy AnalysisRevision construction
fail closed as pending, removed a PostgreSQL locking suffix that is unsafe on
the fairness query's window/outer-join shape, and made the background
coordinator survive and log a transient repository failure.

No `/api/v1`, external route, second runtime, webhook mutation, signal model,
ResSleepNet path, capability promotion or production cutover was added.

### Codex verification

- Focused bridge/provider privacy, conflict, live/replay, model-outage,
  version-linkage, role-authorization, trigger and fairness proof:
  `pytest -q tests/test_sleep_domain_agent_bridge.py --disable-warnings
  --maxfail=1` — 10 passed.
- Focused Product Agent/NightEpisode/fast-path/persistence regression before
  final self-review: 130 passed. Post-review compile plus focused regression:
  125 passed.
- Full repository regression: `pytest -q --disable-warnings` — 685 passed,
  3 failed in the same pre-existing governed-memory handle and historical
  acceptance-material 10/22-versus-22 fixture assertions recorded before this
  slice; no new failure was introduced.
- `py_compile`/`compileall` passed for the bridge, provider, contracts,
  repository, migrations and tests. Static checks found no whitespace errors,
  external `/api/v1`, new four-Agent classes, `VERIFIED` slow-path claim or
  destructive SQL in this slice. Ruff was not installed in the environment.

### Round 10 — Codex build

Implemented only the requested standalone `/api/v1` sleep-domain API,
authentication/authorization boundary and durable asynchronous Operation
slice:

- added a standalone ASGI application whose OpenAPI contains only 11
  versioned `/api/v1` sleep-domain paths; no event-consumer client or event
  polling route was added;
- added independent versioned request, response, page and stable error
  contracts for committed lifecycle, CurrentRisk, NightEpisode
  summary/revision, elder/family/caregiver/doctor views and Operation status;
- made every query read committed repository projections only. Query code has
  no slow-path runtime dependency and creates no Operation;
- added rotating HTTPS Bearer service credentials and asymmetric
  EdDSA/ES256/RS256 JWS validation bound to issuer, audience, key id, actor,
  subject, role, scopes, issue/expiry time, nonce, method, path and exact raw
  request-body hash;
- added persistent assertion-id/nonce replay rejection, bounded dual-key
  rotation windows and a fail-closed authoritative role-binding resolver
  rechecked on every request and again before queued command execution;
- keyed role-view caches and pagination cursors by the exact subject,
  revision, authenticated role/effective scopes and authorization epoch;
  cursor scope additionally includes service principal and actor;
- added atomic command-payload plus Operation creation, scoped
  Idempotency-Key handling, canonical request hashes, persistent
  pending/running/succeeded/failed/cancelled state, lease recovery, attempt
  count, stable error codes and result resource ids;
- implemented activate/deactivate, elder feedback, authorized
  family/caregiver feedback and explicit reanalysis workers. Feedback
  provenance remains role-distinct; a successful command follows the
  committed domain revision/outbox mutation;
- added migration `018_sleep_api_v1` for persistent replay protection,
  restart-safe command payloads and feedback provenance, plus production
  fail-closed deployment configuration and the integration guide
  `plug_and_play/API_V1.md`.

Two bounded self-review passes were applied. The first updated historical
migration tests to select their own migration while recognizing 018 as the
latest additive version. The second separated the public ASGI/OpenAPI surface
from the existing combined debug backend, minimized CurrentRisk/role-view
projections, bound cursor/cache keys more tightly, fixed standard ES256 JWS
signature conversion, and made replay insertion safe under concurrent unique
conflicts.

No event polling client, event polling endpoint, external delivery transport,
device administration surface, second slow-path runtime, signal model,
provider cutover or commit was added.

### Codex verification

- Required focused acceptance:
  `pytest -q tests/test_sleep_api_v1.py` — 20 passed.
- The focused tests cover standalone OpenAPI isolation, version/error
  compatibility, bounded pagination, committed-query no-work behavior,
  service and actor key rotation, all required JWS bindings, overreach,
  replay, canonical command idempotency, changed-body conflict, role/scope/
  epoch cache and cursor isolation, execution-time revocation, feedback
  provenance, Operation status, lease crash recovery without duplicate
  revision, and database restart recovery.
- Full repository run: `pytest -q` — 697 passed and 3 failed in the same
  unrelated governed-memory handle and historical acceptance-material
  10/22-versus-22 fixture assertions already recorded by prior slices.
- Full regression excluding exactly those three pre-existing failures:
  `pytest -q --deselect=...` — 705 passed, 3 deselected.
- `py_compile` passed for the public API, domain changes and tests. The
  standalone OpenAPI inspection reported 11 paths, all under `/api/v1`, with
  none of the prohibited internal concepts. Static whitespace/diff checks
  passed. Ruff was not installed in the environment.

### Round 11 — Codex build

Implemented only the requested external Domain Outbox projection, authenticated
event polling and provider-agnostic reference client:

- added additive migration `019_sleep_api_event_polling` with durable cursor
  sessions and stable revocation tombstones on the existing shared database;
- added `GET /api/v1/subjects/{subject_id}/events` with bounded polling,
  `at_least_once` delivery, `event_id` deduplication semantics, event/schema
  versions, aggregate version, per-aggregate sequence and committed
  occurred/persisted timestamps;
- kept the monotonic delivery offset out of public events and used it only
  inside an authenticated-encrypted opaque cursor. No global business order
  is claimed;
- projected internal outbox records through explicit role/scope rules and
  payload allowlists. Public events omit aggregate type, raw attributes,
  correlation/causation values, authority ids and internal analysis names;
- bound every persistent cursor to namespace, consumer service, actor,
  subject, role, exact effective scope hash, authorization epoch, expiry and
  event Schema generation. Authority is rechecked on every valid poll;
- returned stable `CURSOR_RESYNC_REQUIRED` behavior for invalid, expired,
  Schema-mismatched or authorization-changed cursors. Revocation denies future
  reads and returns a repeatable scoped tombstone that requires local cache
  deletion and explicitly states that already delivered data cannot be
  remotely recalled;
- kept authority outages fail-closed as retryable authorization failures
  rather than fabricating revocation state;
- added the separately installable `reference_client` package. It depends
  only on HTTPS/public JSON contracts, signs asymmetric actor assertions,
  submits idempotent asynchronous commands, polls Operations, reads committed
  lifecycle/risk/episode/role views, persists cursors/event ids, suppresses
  duplicates, resynchronizes snapshots and handles revocation tombstones;
- documented the event contract and deployment TTL/Schema-generation
  configuration in `plug_and_play/API_V1.md` and `.env.example`.

Two bounded self-review passes were applied. The first corrected the test ASGI
transport so exact signed request bytes reach command endpoints and made
resync tolerate a legitimately pending role view. The second preserved
authorization-unavailable semantics, moved authority recheck ahead of
expiry/Schema resync, made tombstones stable across repeated polls, enforced
household-versus-doctor event projection and encrypted the opaque cursor.

No outbound webhook delivery, internal-package import in the reference
client, conversation-history transport, second event source, new Agent
runtime, provider cutover or commit was added.

### Codex verification

- Event authorization, minimized projections, at-least-once duplicate
  polling, encrypted-cursor tampering, expiry, scope/epoch/Schema resync,
  revocation/tombstone stability, authority outage and database restart:
  `pytest -q tests/test_sleep_api_events_v1.py` — 5 passed.
- Independent client import boundary, commands, Operation polling, committed
  queries, durable cursor/event-id state, duplicate suppression, expiry
  resync, restart and revocation cache deletion:
  `pytest -q tests/test_sleep_api_reference_client.py` — 2 passed.
- Public API plus event/client regression:
  `pytest -q tests/test_sleep_api_events_v1.py
  tests/test_sleep_api_reference_client.py tests/test_sleep_api_v1.py` —
  27 passed.
- API/event/client plus persistence migration regression — 50 passed before
  the final security self-review.
- Full repository run — 712 passed with exactly the same 3 unrelated,
  pre-existing governed-memory handle and historical acceptance-material
  fixture failures.
- Full regression excluding exactly those three known failures — 712 passed,
  3 deselected.
- `compileall`, OpenAPI boundary inspection and whitespace/diff checks passed.
  OpenAPI reports 12 routes, all under `/api/v1`, including the single event
  polling route and no prohibited internal event concepts.

### Round 12 — Codex build

Implemented only the requested compatibility migration, authority cutover,
rollback rehearsal and evidence-bounded real Perceptor acceptance slice:

- added additive migration `020_legacy_authority_cutover`, preserving all old
  records while adding compatibility-projection metadata, audited import
  runs/records, privacy-minimized shadow comparisons, append-only cutover
  events and a versioned state row that serializes concurrent phase changes;
- added a one-way, read-only legacy webhook importer. It preserves legacy raw
  IDs and normalized-content hashes and writes payload bytes only through the
  unified encrypted Raw Inbox. A reviewed manifest must bind exact payload
  hash, signature representation/profile/time, provider-device hash, data
  mode, binding, Adapter lock and compatibility profile; otherwise the record
  enters terminal `replay:legacy-quarantine:*` quarantine. Orphan normalized
  rows, unknown providers, malformed time and mismatched signed payloads fail
  closed. Repeated and concurrent imports return the same durable result;
- added `CompatibilityMigrationController` for persisted
  `expanded → backfilled → shadow_verified → cutover →
  rollback_rehearsed/rolled_back` transitions. Backfill requires a succeeded
  import (including an audited zero-row import), shadow comparison must cover
  every current revision with zero unaccepted critical mismatches, and
  cutover requires a finalized compatibility row for every current revision;
- kept `radar_night_summaries` as a migration-only current-revision read
  model. Canonical NightEpisode commits do not dual-write it. Shadow records
  persist hashes, field names and match results but no compared health values;
  the rollback probe parses and identity-checks the compatibility rows before
  recording successful restoration to canonical authority;
- made fake providers require an explicit non-production mode plus a
  `replay:*` namespace. Removed default fake selection from Perceptor client
  construction and status reporting. Production sleep API, push, Product
  Agent, legacy Radar runtime and Habit Profile paths now reject SQLite,
  `/tmp`, replay/fake authority or missing shared database configuration;
- hid legacy `/radar-agent/*` and `/product/radar/*` surfaces outside explicit
  non-production dev mode. The old `server.py` is now a loopback-only,
  standard-library diagnostic with no CSV/SQLite write and a `410` legacy
  `/receive`; `Radar_monitor.py` is also explicit non-production diagnostic
  only. Formal writes remain on the unified FastAPI ingestion route;
- added strict real-device acceptance models with exactly eight verdicts:
  transport, push authentication, pull/report normalization, binding,
  NightEpisode, deterministic fast path, Agent slow path and API/reference
  client. Automated code cannot issue `VERIFIED`; immutable real evidence,
  immutable test results and a named human reviewer are required. Adapter
  `CapabilityVerificationReceipt`s remain separate from platform stages;
- recorded the current real acceptance as PENDING in
  `plug_and_play/REAL-PERCEPTOR-ACCEPTANCE.md`. No Perceptor credential,
  authoritative live binding, vendor signing facts, production authority
  database or complete real-night evidence exists in the workspace, so no
  fixture/replay result was substituted and no real request was attempted;
- rejected non-canonical base64url encodings for opaque cursors and actor JWS
  components after a full-suite tamper test exposed equivalent trailing-bit
  encodings.

Two bounded self-review passes were applied. The first bound reviewed legacy
signature and binding evidence to the exact retained payload/device hashes and
added concurrent-import waiting. The second made rollback rehearsal execute a
real compatibility read/identity probe, blocked cross-subject legacy-device
reuse, required exact Adapter capability receipts for applicable real
acceptance stages and removed the last implicit fake mode reported by health
status.

No v1 endpoint, event-consumer behavior, Agent roster, signal model,
irreversible deletion, long-term dual write, production callback alias,
commit, push or capability promotion was added or performed.

### Codex verification

- Populated 019→020 migration, encrypted/quarantined legacy import, exact
  proof admission, concurrent idempotency, orphan normalized row handling,
  shadow failure blocking, serialized cutover, compatibility finalization,
  rollback probe and production authority rejection:
  `pytest -q tests/test_legacy_authority_migration.py` — 8 passed.
- Real verdict/receipt evidence rules and production/debug authority
  boundaries:
  `pytest -q tests/test_perceptor_real_acceptance.py
  tests/test_authority_runtime_boundaries.py` — 9 passed.
- Public API/OpenAPI, authentication, replay, idempotency, cache isolation,
  restart, events and reference-client regression plus new migration tests:
  42 passed in the final focused run.
- Final full repository run: `pytest -q` — 729 passed and exactly 3 failed in
  the same pre-existing governed-memory handle and historical
  acceptance-material 10-versus-22 assertions recorded before this slice.
- Final full regression excluding exactly those three known failures:
  729 passed, 3 deselected.
- `compileall`, `py_compile`, OpenAPI inspection, Compose configuration and
  `git diff --check` passed. The standalone public OpenAPI still has 12 paths,
  all under `/api/v1`; no new route was added. Ruff/Black are not installed in
  the environment.
- Environment-name and workspace-artifact scans found no configured Perceptor
  credential/database/authority variable and no real-night evidence or SQLite
  authority file. Therefore all eight real-device stages and push/pull/sleep
  report capability receipts remain PENDING.

No commit or push was performed.

### Round 13 — Codex build

Completed the remaining executable real-night gate without fabricating a
device run or expanding the v1 service:

- added a privacy-minimized
  `real_perceptor_acceptance_manifest.v1` contract with exactly eight stage
  evidence sections, content-addressed redacted artifacts, exact live
  Adapter/configuration/profile hashes, opaque committed resource scope and
  duplicate-push/report-re-pull probes;
- added a read-only `RealPerceptorAcceptanceAuditor`. It follows only
  committed unified-authority projections from current NightEpisode revision
  to canonical observations, effective binding, encrypted Raw Inbox metadata,
  source-report version, deterministic quality/CurrentRisk, intelligent
  analysis/elder-family-doctor role views and domain outbox event. It never
  decrypts raw payloads, calls Perceptor, invokes an Agent or mutates a
  resource;
- made fake/replay/simulated identifiers, inline/raw payload references,
  credential-bearing URIs, changed duplicate resource IDs and changed scoped
  before/after counts fail closed. The output contains hashes and verdicts,
  not raw/message/subject/device identifiers;
- bound every automated stage result to a SHA-256 of the manifest and the
  privacy-minimized hashes of every committed fact inspected. Passing
  automation remains PENDING until an authorized human decision binds that
  exact manifest, audit digest and audit timestamp. A reviewer cannot override
  PENDING/FAILED checks or approve an Adapter capability without all of its
  applicable passing stages;
- kept Adapter receipts limited to push, pull and sleep-report. Transport and
  push authentication share the push receipt; binding, NightEpisode, fast
  path, slow path and external client stages explicitly mark receipts not
  applicable;
- added the production-only `real-perceptor-acceptance` CLI. It reuses the
  ordinary production runtime checks for shared PostgreSQL, canonical
  cutover, encrypted raw policy, service/actor authentication and authoritative
  roles. It returns `0` only for fully VERIFIED, `3` for PENDING and `2` for
  failed/fail-closed configuration;
- documented the executable workflow and deliberately did not check in a
  placeholder “real” manifest while device/night facts are absent.

No `/api/v1` route, event-polling behavior, provider request, Agent runtime,
callback alias, signal model, destructive migration, capability promotion,
commit or push was added or performed.

### Codex verification

- Evidence safety, automated non-promotion, exact human approval binding,
  partial capability approval, duplicate/re-pull stability, output privacy
  and a complete committed-chain audit covering all eight stages:
  `pytest -q tests/test_perceptor_real_acceptance.py
  tests/test_perceptor_real_acceptance_auditor.py` — 13 passed.
- Legacy import/cutover, production authority, Perceptor push/pull/restart,
  persistence, lifecycle, fast path, Agent bridge, public API and real
  acceptance combined regression — 137 passed.
- Full repository run: `pytest -q` — 739 passed and exactly the same 3
  unrelated, pre-existing governed-memory handle and historical
  acceptance-material failures.
- Full regression excluding exactly those three known failures: 739 passed,
  3 deselected.
- `compileall`, `py_compile`, `git diff --check`, production CLI fail-closed
  behavior and package import checks passed. Ruff/Black remain unavailable in
  the environment.
- Environment-name and workspace-artifact scans again found no configured
  Perceptor credential/database/authority variables, real-night manifest,
  evidence pack or SQLite authority file. The live device/night execution
  therefore remains PENDING; no fixture/replay evidence was substituted.

No commit or push was performed.
