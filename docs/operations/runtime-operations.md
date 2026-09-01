# Durable runtime operations

PostgreSQL 16 is the authority for work, schedule, checkpoint, revision, and governance state. The supported guarantee is **fenced at-least-once processing with idempotent convergence**, not exactly-once distributed execution.

## Processes

| Process | Entry point | Boundary |
| --- | --- | --- |
| Migration | `python -m sleepagent.persistence.migrate apply` | schema owner only |
| API | `uvicorn sleepagent.app:app` | explicitly enabled public/product/demo/internal/Perceptor surfaces |
| Scheduler | `python -m sleepagent.bootstrap.scheduler run` | scans schedules and creates work; executes no handler |
| Worker | `python -m sleepagent.workers.runtime run` | claims only explicitly configured queues |

API and worker principals must not be the migration owner. Live Perceptor credentials belong only to profiles that need scheduled Pull.

## Claim and recovery model

Claims use PostgreSQL `SKIP LOCKED`, a lease generation, fencing token, and bounded lease. An expired claim may be reclaimed, but every row has a separate reclaim ceiling. Handler failure uses bounded retry/attempt authority. Exhaustion becomes visible quarantine, dead-letter, or outcome-unknown state according to the effect boundary.

Idempotency is semantic and persisted. Exact retries converge on existing work/evidence; materially changed input creates a new revision. If ownership changes, the old fence cannot commit. Process memory is disposable and does not decide durable success.

Schedulers can run concurrently: the schedule slot identity is unique, schedule transitions serialize, jitter is strictly less than cadence, and overdue schedules advance rather than burst every missed slot. The scheduler is disabled by default.

## Queues and capabilities

The production-shaped flow uses ingestion, reconciliation, Perceptor history/SleepReport Pull, finalization scan, fast path, product agent/report, Care evaluation, outcome evaluation, and interaction commands according to enabled features. `care.outcome.evaluate.v1` must be consumed whenever outcome evaluation capability is enabled. Startup/readiness rejects mismatched configuration.

`shared_only` is the current/default report path. Legacy/shared-compat/shadow modes remain rollback and diagnostic compatibility; they substantially converge on shared analysis, while shadow additionally runs a no-external-effect legacy comparison.

`SLEEPAGENT_BACKEND_LIVE_DELIVERY_ENABLED` remains a deprecated/internal disabled configuration input. It has no implemented email/SMS/WeChat behavior and is retained only to avoid breaking unknown external configuration contracts.

## Readiness and operational state

Worker healthcheck validates schema attestation, database authority, queue handlers, and capability/profile coherence. Protected aggregate status reports scheduler lag, successful Pull/finalization times, oldest ready work, active/reclaimable leases, retry/reclaim/terminal counts, open/finalized night age, report progress, Care execution, outcome evaluation, and pending/accepted/rejected personalization candidates.

No subject/device identifiers, credentials, provider payloads, prompts, report prose, or notes are status labels.

## Drain and incident recovery

1. Stop/disable new scheduler scans.
2. Signal workers; stop new claims and drain the current lease within the configured window.
3. Stop APIs, then PostgreSQL.
4. After an abrupt exit, restart the same capability profile and inspect reclaim/terminal/outcome-unknown counts.

Provider timeout retries without changing fact authority. NO_DATA remains explicit. A duplicate finalizer reuses unchanged material; material late evidence creates a superseding immutable revision. Never repair state by editing checkpoints, work rows, or governance tables manually.

The authoritative proof is `scripts/verify_closure.sh release --env-file .env.test`; full process-fault evidence is a local release lane when hosted CI cannot provide equivalent process control.
