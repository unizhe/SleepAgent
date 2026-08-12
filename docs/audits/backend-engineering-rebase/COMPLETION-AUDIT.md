# Completion audit: backend-engineering rebase

_Audit date: 2026-08-12_

## Authority and status vocabulary

This document audits the current uncommitted worktree against the locked
`PLAN.md`. Historical prose is not counted as evidence; every `PROVED` row
below has a fresh local Python 3.11, PostgreSQL, process, HTTP, packaging, or
static proof from this build session.

| Status | Meaning |
|---|---|
| `PROVED_PROCESS_PG` | Independent processes/public HTTP and PostgreSQL invariants ran successfully. |
| `PROVED_LOCAL` | Deterministic, contract, security, static, or packaging proof ran successfully. |
| `OUT_OF_SCOPE` | The locked PLAN explicitly excludes the capability. |

## Outcome

The first real backend slice is implemented and proved from a fresh database:

`facts-only replay seed → encrypted raw ingress → ordered normalization →`
`committed NightEpisode → Product Runtime → three role projections → signed`
`Product API`.

No known application-layer capability remains missing from that path. Stages
2–5 are also proved through public HTTP plus database invariants, including
commands/interactions, multi-night reads, urgent zero-model routing, exact
delivery fault recovery, subject forget, and scheduled raw crypto-shred while
derived Product data remains readable.

All locked application, process, PostgreSQL, wheel and production-image gates
now have executed evidence. The Docker proof found and fixed one release-only
import defect before the final image passed, so wheel evidence was not used as
a substitute for the image gate.

## Requirement-to-evidence audit

### Stage 1 — first typed `/today` closure

| Requirement | Status | Fresh evidence |
|---|---|---|
| manifest-pinned additive migrations and exact attestation | `PROVED_PROCESS_PG` | Fresh `001 → 007` apply and current-ledger check passed under the migration owner; API/Demo/Worker roles were independently scoped. |
| facts-only fixture, adapter and canonical ingress manifest | `PROVED_PROCESS_PG` | Fresh replay roots consumed allowlisted external facts; manifest/adapter hashes and server-owned IDs were persisted and checked. |
| durable Demo root, bootstrap, journey and ordering | `PROVED_PROCESS_PG` | Public Demo reservation, wait-aware journey, normalization and Product commits completed against PostgreSQL; process recovery retained the same root. |
| strict role-discriminated `/today` | `PROVED_PROCESS_PG` | Elder/family/doctor signed reads returned typed projections; field-deny and authorization tests pass. |
| clean and restart process proof | `PROVED_PROCESS_PG` | Clean, Worker-restart and PostgreSQL restart/disconnect paths completed through independent sockets and database invariants. |

### Stage 2 — commands and interactions

| Requirement | Status | Fresh evidence |
|---|---|---|
| monitoring, feedback, reanalysis and interaction state machines | `PROVED_PROCESS_PG` | Public command/interaction suite and PostgreSQL invariant passed with durable idempotency, epochs, single-use handles and frozen snapshots. |
| confirmed effect exactly once; decline has zero effect | `PROVED_PROCESS_PG` | Public result and invariant rows proved one committed local effect and no decline effect. |
| bounded database retry and invocation journal | `PROVED_LOCAL` | Deadlock/serialization retry, CAS and invocation-journal regression suite passed. |

### Stage 3 — read models and abnormal journeys

| Requirement | Status | Fresh evidence |
|---|---|---|
| trends/records/care projections and cursors | `PROVED_PROCESS_PG` | Four-night public read suite completed with dedicated DTOs, pagination and generation-fenced cursors. |
| abnormal, insufficient-quality and urgent zero-model paths | `PROVED_PROCESS_PG` | Device-abnormal and quality-insufficient paths produced typed results; urgent path proved zero model invocation. |
| durable generation-bound ScenarioClock advance | `PROVED_PROCESS_PG` | `demo_advance` released 1,490 staged facts atomically on its first 120-second fenced lease; ControlClock was unchanged. |

### Stage 4 — induction, delivery and reconciliation

| Requirement | Status | Fresh evidence |
|---|---|---|
| induction and delivery persistent outcomes | `PROVED_PROCESS_PG` | Induction receipts, delivery journal, deterministic sink and public CareAction were verified. |
| exact process kill after `send_started` | `PROVED_PROCESS_PG` | Worker PID was killed with `SIGKILL` after durable `send_started`; recovery reconciled `known_not_delivered`, retried under a new fence, and produced one public active CareAction. |
| known delivered/not-delivered/unknown and dead-letter behavior | `PROVED_PROCESS_PG` | Reconciliation receipts and three exact delivery attempts passed the Stage 4 invariant. |
| no enabled no-op handler | `PROVED_LOCAL` | Composition/readiness and architecture tests reject unknown or no-op enabled queues. |

### Stage 5 — bounded retention

| Requirement | Status | Fresh evidence |
|---|---|---|
| per-domain envelope DEK on fresh replay writes | `PROVED_PROCESS_PG` | v33 invariant proved every source-generation raw row uses encryption protocol v2, a raw-domain DEK generation and a retention binding. |
| durable subject forget and epoch/generation fence | `PROVED_PROCESS_PG` | v33 reset `019ff34c-b5cb-7aec-9d58-b34ee2b6e13a` completed generation `1 → 2`; old assertion returned 403, old cursor returned typed 409 `cursor_resync_required`, and new `/today` returned `no_data`. |
| provider-outside-transaction checkpoints and wait budget | `PROVED_PROCESS_PG` | The reset root committed its epoch bump and high-priority child first; while waiting it remained `retry` with `attempt_count=0/20`, then completed after one child attempt. |
| scheduled expiry and stable `key_destroyed` | `PROVED_PROCESS_PG` | v32 scheduled job succeeded on attempt 1, wrote one receipt/event, nulled the wrapped DEK, and `PostgresRawRetentionReader` raised stable `RetentionKeyDestroyed(code=key_destroyed)` without plaintext recovery. |
| derived data survives scheduled raw expiry | `PROVED_PROCESS_PG` | Signed `/today` remained `ready` with the same analysis revision `019ff338-689d-788b-b4d6-fceb503dc21b`; one analysis and three role views remained. |
| deadline, unfinished normalization, stale fence and repeat behavior | `PROVED_PROCESS_PG` | Unit gates prove no provider call before deadline/terminal normalization or after a stale fence; live worker idle confirmed one attempt, one receipt and one completion event. |
| managed KMS, backup propagation and production lifecycle platform | `OUT_OF_SCOPE` | The release intentionally proves the port, fence, invalidation and receipt boundary only. |

### Stage 6 — hardening and final proof

| Requirement | Status | Fresh evidence |
|---|---|---|
| canonical runtime/Worker caller graph | `PROVED_LOCAL` | Architecture tests permit the direct Runner only in the canonical PostgreSQL Product Worker. |
| forced RLS, scoped grants and namespace fairness | `PROVED_PROCESS_PG` | Current-schema PostgreSQL marker ran with migration/API/Worker roles; prepared-fence, column-scoped grants and concurrent four-namespace fairness passed. |
| strict errors, private operational surfaces and watermarks | `PROVED_LOCAL` | API/ASGI/security suites and OpenAPI snapshots passed. |
| PHI-free logs, metrics and audit | `PROVED_LOCAL` | Redaction, fixed-label signals and durable operational metrics tests passed. |
| server wheel/oracle isolation | `PROVED_LOCAL` | The final Python 3.11 Docker wheel-builder produced a 204-file wheel with no tests, verifier goldens, expected/actions oracle or pyc. |
| production Docker image/oracle inspection | `PROVED_LOCAL` | The production target built as image `sha256:c57a461cd7077eeed40649af703c046fe42a1e276bcb0736ac9f826c11cf361f`; exact cleanup, core imports, UID/GID 1000, entrypoint/config and zero test/verifier-oracle files were inspected in the final image. |
| OpenAPI/reference client/real e2e marker | `PROVED_LOCAL` | Snapshots, reference-client boundaries and real-socket verifier commands are present and passed where services were started manually. |
| process kill, PostgreSQL restart/disconnect, load/fairness and UUIDv7 rollback | `PROVED_PROCESS_PG` | Stage 1/4 process faults and the current PostgreSQL load gate passed; independent-process UUIDv7/clock rollback tests passed locally. |

## Fresh proof record

- Python: exact `/tmp/sleepagent-py311/bin/python` (CPython 3.11).
- Full suite after the independent CR fixes: `1200 passed, 14 skipped`.
- Unit marker: `1189 passed, 15 deselected`.
- ASGI lifespan marker: `4 passed, 1200 deselected`.
- Current-schema PostgreSQL marker with exact principals and Stage 5 proof:
  `9 passed, 4 skipped, 1191 deselected`; the four skips require the separate
  Stage 1–4 proof JSONs, whose supervisor/invariant runs passed earlier in the
  same build.
- Stage 5 PostgreSQL invariant alone: `1 passed`.
- Final release-artifact architecture suite: `7 passed`, including an isolated
  source-tree rehearsal of the exact production replay cleanup followed by
  retention/backend/Worker imports.
- Targeted final Stage 5/Demo/foundation regression: `53 passed`; targeted
  stable error-contract regression: `19 passed`.
- Migration release target: `007`; manifest SHA-256
  `15c71048be298ee85d0e5fec8f41ba7176ead74d4abcdd93214d5bdfeb2fc30a`.

  | Version | Filename | SHA-256 |
  |---|---|---|
  | `001` | `001_initial_schema.sql` | `c62168ddb802f975dd2afdeb5ca7854fed380695c7295061f72133af099a023f` |
  | `002` | `002_replay_journey_and_today.sql` | `400f2c8b75a7189e58618aeffaf704647c88a73df0aac4f949bb2d526e920b7d` |
  | `003` | `003_stage2_commands_and_interactions.sql` | `43e409849c6830668ceec2ce4b64f5aff84ae63f2faca34f04622c5a645f786d` |
  | `004` | `004_stage3_reads_and_scenario_clock.sql` | `bdf8e6570a8ae134180e0c7c7c56d45a988ec53b66e51281f3300fa6a33bb290` |
  | `005` | `005_stage4_induction_delivery_reconciliation.sql` | `2ff2b099f96d66db62a316001ab3b295a3ce0bd38a4bb1e2591c77fc4328409d` |
  | `006` | `006_stage5_bounded_retention_and_reset.sql` | `830fa812897dba7cb9a754422fc7b7dee4a854b1982e3ef3e97cfadd9cd1fe56` |
  | `007` | `007_stage6_system_hardening.sql` | `08bd381723afd3c533d27d6a01596619f7ecf33619f2a76c03b6ecdfba1be0dc` |
- Final Python 3.11 wheel: 202 files, 11 facts-only runtime scenarios, SHA-256
  `b31e2fe22f0711ba370352f7a595b9f66a56d044769d6ba9b7676489b43523dc`,
  with no tests, verifier goldens, expected/actions oracle or bytecode.
- Final production image: 185,854,885 bytes, `USER sleepagent`, `/app`, port
  18000, and the canonical uvicorn command. Its 357 application files contain
  no test or verifier-oracle file; replay fixtures, the legacy replay package,
  replay seed registry and temporary build inputs are absent. Core retention,
  backend, Worker and Product API imports pass inside the image.
- Starting the final image without settings exits nonzero on the stable
  fail-closed boundary `SLEEPAGENT_BACKEND_PROFILE is required`.
- OpenAPI snapshots, compileall, shell syntax, `git diff --check`, and Docker
  Compose configuration: passed.

## Docker validation environment

The host system socket remained unavailable, so the final target was built by
a private Docker 29.3.0 rootless daemon using `fuse-overlayfs`. This host lacks
subordinate-ID mapping helpers; validation therefore used a single-UID
namespace, ownership-normalized verified Python 3.11.15 base layers, and
temporary build-only `adduser`/`chown` shims. The original tools were restored
inside the resulting image and explicitly checked. The final image config and
passwd database both fix `sleepagent` at UID/GID 1000; container content and
the fail-closed entrypoint were exercised with a root override because this
validation namespace cannot execute UID 1000. A standard release daemon should
still rebuild the target normally, but this environment limitation does not
leave an application-layer or oracle-isolation gate unexecuted.

## Non-gate static typing observation

A strict mypy run rooted at the four replay-boundary modules expanded through
their imports and reported the repository's current typing debt: 525 errors in
59 files. The locked PLAN did not define mypy-clean as an acceptance gate, so
this result is neither labeled passed nor allowed to replace any runtime proof.
It should be handled as a separate repository-wide typing effort.

No commit, push, release, remote mutation, or production-data operation was
performed.
