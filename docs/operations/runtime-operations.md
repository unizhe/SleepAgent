# SleepAgent runtime operations

This document is the G7.2 process-supervision contract for the PostgreSQL 16
backend. It is orchestration-platform neutral. Credentials below are references
or placeholders; resolved credentials must never be written to logs or command
history.

## Process topology and authority

| Process | Entry point | Database authority | Capability boundary |
| --- | --- | --- | --- |
| Migration/bootstrap | `python -m sleepagent.persistence.migrate apply` | migration owner | schema only; never serves traffic or claims work |
| Public API | `uvicorn sleepagent.app:app` | API role/principal | `public_v1,product`; no worker handlers |
| Demo API | `uvicorn sleepagent.app:app` | demo role/principal | `demo` only; test/development only |
| Internal API | `uvicorn sleepagent.app:app` | API role/principal | authenticated `internal` health/status only |
| Scheduler | `python -m sleepagent.bootstrap.scheduler run` | worker role/principal | scans acquisition schedules and creates durable operations |
| Worker | `python -m sleepagent.workers.runtime run` | worker role/principal | claims only explicitly configured queues |

Every process must receive an explicit profile, deployment mode, process role,
data mode, database identity/role/DSN, service principal, namespace prefixes,
feature gates, and either API surfaces or worker queues. API and worker roles
must not use the migration owner. Live Perceptor credentials are loaded only by
processes with the scheduled Pull queues, using protected `file:`/`env:`
references. `SLEEPAGENT_BACKEND_PERCEPTOR_BASE_URL` must be an HTTPS origin
without embedded credentials, query, or fragment.

The scheduler uses a worker capability profile containing the scheduled queues,
but does not compose or execute their handlers. Workers may scale horizontally;
the durable claim, lease generation, fencing token, and PostgreSQL transaction
remain authoritative. Schedulers may also run in more than one instance:
`(schedule_id, scheduled_for)` is unique and the fire function serializes the
schedule transition before creating its one operation.

## Start, readiness, and activation

Start in this order:

1. PostgreSQL 16, then `migrate check` (or `apply` during rollout).
2. APIs; require `/livez`, then authenticated internal `/readyz` where present.
3. Workers; require `python -m sleepagent.workers.runtime healthcheck` with the
   exact production queue profile.
4. Scheduler, initially with
   `SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=false`.
5. Inspect the internal operational snapshot, then deploy a controlled profile
   with the gate set to `true` and the intended live namespace/account.

The disabled scheduler returns `{"enabled":false,"fires":0}` and performs no
authoritative acquisition write. Enabling it is a deployment decision, not a
source default. `SLEEPAGENT_BACKEND_LIVE_DELIVERY_ENABLED` remains `false` until
a later delivery/HITL goal.

`/livez` answers only whether the API process is alive. `/readyz` checks the
started runtime, database/schema attestation, and exact release configuration.
The authenticated internal metrics endpoint adds durable system state. A
provider NO_DATA result does not make liveness fail. Schema mismatch, database
loss, missing handler composition, or failed database authority prevents
readiness/startup. Durable backlog and staleness are exposed as
healthy/degraded/unhealthy operator state rather than being hidden in liveness.

## Operational snapshot

The internal API calls the protected
`sleepagent_internal_operational_metrics()` function. It is
`SECURITY DEFINER`, has a fixed search path, requires an authenticated
`internal_status` API context, returns aggregate-only values, and has no PUBLIC
grant. It contains no subject, device, namespace, provider-request, health
payload, prompt, or free-form error identifier.

| Signal | Source | Healthy policy | Degraded policy | Unhealthy policy / action |
| --- | --- | --- | --- | --- |
| scheduler due lag | schedules | no overdue slot | overdue by one configured cadence | overdue by two cadences; inspect scheduler authority/restart |
| last fire / success | schedules | advances with enabled cadence | stale with due work | exhausted schedule; verify provider and worker grants |
| schedule failures | schedules | zero consecutive failures | nonzero below `max_attempts` | `max_attempts` exhausted; inspect sanitized error code |
| last history / SleepReport success | normalization receipts | advances at expected cadence | older than deployment cadence policy | correlate scheduler and provider status |
| received / persisted / deduplicated | reconciliation receipts | internally consistent monotonic totals | duplicate increase is explainable | persistence divergence; stop rollout and investigate |
| oldest ready work | durable work tables | zero or within retry policy | ready backlog exists | terminal/uncertain count nonzero; reconcile before rollout |
| active / reclaimable leases | durable work tables | active leases have future expiry | expired work is reclaimable | repeated reclaim growth; inspect worker crashes/contention |
| reclaim / retry / dead-letter / outcome unknown | durable work tables | no terminal/uncertain work | retry is bounded | any dead-letter/outcome-unknown is unhealthy and requires reconciliation |
| oldest OPEN / SOFT_FINALIZED night | finalization tables | within policy window | older than 86,400 seconds | reconciliation-required is unhealthy; inspect evidence/date authority |
| late finalization revisions | immutable revisions | explainable monotonic count | unexpected growth | verify materiality and upstream duplicate data |
| latest report / shared-analysis completion | operations | advances after eligible finalized nights | stale with eligible work | inspect Product queue/model boundary without exposing health context |

The one-day night-age default is conservative operational policy, not a medical
threshold. Scheduler thresholds are relative to each configured cadence;
terminal or uncertain work is always unhealthy. Future tuning belongs to a
versioned operational policy, not clinical logic.

## Fault recovery expectations

- Scheduler crash: restart the scheduler. A committed slot remains discoverable
  and cannot be duplicated; an uncommitted transaction disappears and is
  retried by the next scan.
- Worker crash after claim: wait for the lease to expire, then start another
  worker with the same queue capability. A newer lease generation/fencing token
  wins; the stale owner cannot commit.
- Provider timeout: the operation becomes retryable. No raw trust decision,
  canonical observation, or checkpoint advancement is inferred from timeout.
- Provider NO_DATA: persist the explicit reconciliation result and advance the
  authorized checkpoint without inventing observations.
- Ambiguous commit: retry through existing idempotency/reconciliation keys;
  never reset a checkpoint manually.
- Duplicate finalizer: an unchanged material hash returns the existing immutable
  revision and report handoff. Material late evidence creates one superseding
  revision; the parent remains readable.

## Shutdown and restart

For planned shutdown, first stop new scheduler scans. Send SIGTERM/SIGINT to
workers so they stop claiming, drain current work within the configured drain
window, and emit `backend_worker_shutdown_requested` followed by
`backend_worker_drain_completed`. Then stop APIs and finally PostgreSQL. A drain
timeout is a failed shutdown, not permission to mark work successful.

After an abrupt worker or scheduler exit, restart it without database repair.
Inspect lease-reclaim and outcome-unknown counts before restoring normal
capacity. PostgreSQL remains the work and checkpoint authority; process memory
is disposable.

Structured lifecycle events cover schedule creation, claim/reclaim, sanitized
provider reconciliation, checkpoint advancement, finalization/report handoff,
and shutdown/drain. Do not add credential values, raw payloads, report prose,
subject/device identifiers, or unrestricted provider request identifiers to
logs or metric labels.
