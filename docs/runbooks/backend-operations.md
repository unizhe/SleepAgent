# Backend operations runbook

## Process and credential topology

Deploy `backend.main:app` separately for BFF (`public_v1,product`), Demo
(`demo`, replay/test only), and Internal (`internal`). Run
`sleepagent.worker_runtime run` separately with an explicit queue list. Run
`sleepagent.persistence.migrate` with the migration credential only. Never
share the migration DSN, Demo token, Internal token, actor private key, or
Worker credential with another process profile.

`/livez` proves only that the HTTP process is alive. Use authenticated
`/internal/readyz` for database/schema readiness, `/internal/status` for the
non-secret dependency manifest, `/internal/metrics` for bounded counters, and
`/internal/reconciliation/{operation_id}` for effect recovery.

`/internal/metrics` deliberately separates process-local HTTP/queue/signal
counters from the `durable` PostgreSQL snapshot. The durable section is the
cross-process authority for queue depth/age, reclaimable leases,
retry/dead-letter/unknown outcomes, Product attempts, and Safety states. Labels
are fixed categories; do not add namespace, subject, resource, provider request,
or free-form error identifiers.

## Migrations

The only supported lineage is the ordered manifest in
`sleepagent/persistence/migration_manifest.json`. Run:

```bash
python -m sleepagent.persistence.migrate status
python -m sleepagent.persistence.migrate apply
python -m sleepagent.persistence.migrate check
```

`apply` takes a session advisory lock and commits each transactional file
atomically. A missing, extra, renamed, gapped, checksum-drifted, unfinished, or
newer ledger fails closed. Do not edit an applied migration; add the next
manifest entry. This release rejects non-transactional migrations and requires
manual review before that policy can change.

The migration command never provisions application roles, grants permissions,
or loads replay fixtures. For the isolated compose test profile only, run the
explicit `test-bootstrap` service after `migrate apply`; production deployments
must provision roles and grants through their reviewed infrastructure tooling.

## Queues, retry, and fairness

Enabled queues must have exactly one real handler. Claims use PostgreSQL server
time, `SKIP LOCKED`, exact authorization snapshots, namespace concurrency
limits, and a per-queue namespace last-served ledger. Heartbeats extend leases;
all commits require the current lease generation and fencing token.

Retry only short database-local work after the classified retry delay. Never
repeat an external call merely because a lease expired. `outcome_unknown`
moves to reconciliation; delivery is resent only when the deterministic
provider query proves `not delivered` or the effect key itself is idempotent.
Dead-letter and reconciliation receipts are durable evidence, not success.

For graceful shutdown stop claiming, wait for the configured drain deadline,
then exit. After `SIGKILL` or a PostgreSQL disconnect, restart the same queue
set; expired leases are reclaimable and stale workers cannot commit.

## Permissions and scope

API and Worker roles receive only table verbs and SECURITY DEFINER functions
listed by the bootstrap command. All canonical subject data is protected by
forced RLS. Each Unit of Work sets exact namespace/generation/run/arm,
subject/actor or worker, purpose, and authorization/privacy/retrieval epochs.
Pool reset performs `RESET ALL` and verifies every custom scope GUC is empty.

Treat an RLS denial as an authority or generation fence until proven otherwise;
do not work around it with a broader role. Migration ownership/BYPASSRLS is for
schema installation and narrowly reviewed definer functions only.

## Episode date and read semantics

Night assignment uses observed wake local date, then vendor wake date, then the
explicit deadline fallback. Timezone/offset/fold and assignment basis are
frozen in the committed episode revision. A date conflict creates
reconciliation-required state; it must not be silently republished.

GET routes read committed projections only and never invoke a model. Cursors
are opaque and bound to role, policy, generation, and all governance epochs.

## Bounded replay retention and reset

Replay raw writes use a subject/raw-domain envelope DEK and an atomic binding
plus short retention job. Scheduled expiry waits for every bound normalization
row to become terminal and for every deadline to pass. The key provider call
occurs outside the transaction; the final fenced transaction writes the
immutable receipt and removes the wrapped DEK. Canonical raw reads then return
stable `key_destroyed`; committed derived Episode/Product projections remain.

`POST /demo/v1/reset` is the replay-only subject-forget coordinator. Reservation
atomically bumps generation and governance epochs, revokes pending handles,
invalidates current query pointers, and creates required key jobs. Completion
requires every key outcome and one allowlisted final receipt. Old actor
assertions and cursors must fail; a current actor sees `/today` as `no_data`.
This proves the retention interface and cryptographic-erasure behavior only; it
does not claim managed KMS, backup/media propagation, legal hold, or a
production lifecycle platform.

## Troubleshooting and proofs

- `schema mismatch`: run migration `status` and `check`; compare the packaged
  manifest, never patch the ledger manually.
- `worker not ready`: confirm every configured queue has a handler and the role
  can execute all required claim/heartbeat/finalize functions.
- `generation_fenced` or stale authority: discard the lease/assertion/cursor and
  resolve current database authority again.
- `outcome_unknown`: inspect authenticated reconciliation status and provider
  query evidence; do not force success or blind resend.
- retention waiting: verify DB time, binding deadlines, legal hold, and terminal
  normalization counts.
- pool leak: remove code that uses session-scoped settings or retains a
  transaction; never disable the reset verification.

Local deterministic gates are `pytest`, OpenAPI snapshot check, and the stage
verification scripts in `scripts/`. Process proofs require Docker, fresh
PostgreSQL volumes, independently running API/Demo/Worker processes, and an
ephemeral verifier Ed25519 key whose private half is never mounted server-side.
Build release wheels from a fresh sdist or a clean build workspace; a retained
setuptools `build/lib` directory can contain files deleted from the current
source tree and is not valid release evidence.

Run `scripts/verify_backend_first_slice.sh all` to execute the clean slice, the
Worker `SIGKILL` recovery slice, and the PostgreSQL restart/disconnect slice.
The database-restart mode records one root before the fault and fails unless
the same progressed, nonterminal root resumes after every service recovers.

Run `scripts/verify_backend_stage4.sh` only against its fresh, disposable
Compose project. It locks the deterministic receiver boundary, observes the
durable delivery invocation at `send_started`, kills the Worker with
`SIGKILL`, then requires reconciliation and one final public effect. Its fault
probe reads the migration DSN only from
`SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN`; never pass credentials on argv.

The `postgres` pytest marker includes the concurrent namespace load/fairness
gate. That test creates isolated replay namespaces, so run it only against the
fresh test database provisioned by the Compose harness. A collected skip is
evidence that the gate exists, not evidence that PostgreSQL behavior passed.
