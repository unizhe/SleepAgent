# Operations

SleepAgent supports Python 3.11 and PostgreSQL 16. `compose.yaml` is a development/integration harness; production process supervision, network policy, secrets, backups, TLS, and ACL provisioning are external responsibilities.

## Startup and activation

1. Start PostgreSQL 16.
2. Apply/check schema target 028 with the migration owner.
3. Start API profiles and require `/livez`; for the protected internal profile, require authenticated `/internal/readyz`.
4. Start workers with an explicit queue list and require `python -m sleepagent.workers.runtime healthcheck` under that exact profile.
5. With `SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=false`, run one bounded scheduler safety probe.
6. Inspect protected aggregate operational status, then enable the scheduler only in the controlled live profile that owns the intended Perceptor account/namespace.

```bash
python -m sleepagent.persistence.migrate check
python -m sleepagent.persistence.migrate status
python -m sleepagent.workers.runtime healthcheck
export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=false
python -m sleepagent.bootstrap.scheduler once
export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=true
python -m sleepagent.bootstrap.scheduler run
```

The scheduler is disabled by default. `scheduler once` is the bounded disabled-state probe: it reports zero fires and performs no authoritative acquisition write. `scheduler run` requires the setting to be explicitly `true`; running it while disabled exits 2. Enabling continuous scheduling is an operator deployment decision.

## Required worker queues

Use only queues required by the deployed surfaces. The full replay/closure profile includes:

```text
ingestion
fast_path
product_agent
care.outcome.evaluate.v1
sleep_command
product_interaction
demo_advance
replay_journey
reconciliation
```

Live automated Perceptor acquisition additionally needs:

```text
perceptor.history_overlap_pull
perceptor.sleep_report_pull
night.finalization_scan
```

When outcome evaluation is enabled, `care.outcome.evaluate.v1` must have a configured consumer; settings/readiness fail closed on a mismatch. External email/SMS/WeChat delivery queues are not a supported feature.

## Readiness and status

`/livez` is process liveness only. Readiness checks the started runtime, schema/release attestation, database authority, surfaces, queues, and required capability configuration. Protected internal status exposes aggregate scheduler lag, durable backlog, lease/reclaim/retry/terminal counts, finalization age, report progress, Care execution, outcome, and personalization-governance counts. It contains no raw health payload or subject/device identity.

## Shutdown, drain, and recovery

For planned shutdown, disable/stop new scheduler scans, signal workers to stop claiming, let current claims drain within the configured window, then stop APIs and PostgreSQL. A drain timeout is not permission to mark work successful.

After a crash, restart the same capability profile. Expired leases become reclaimable up to a fixed ceiling; a newer fence wins and stale owners cannot finalize. Provider timeouts retry without inventing observations. NO_DATA is an explicit reconciliation result. Ambiguous commits retry through idempotency keys; checkpoints must never be advanced manually. Terminal/dead-letter/outcome-unknown state requires operator reconciliation.

See [durable runtime operations](operations/runtime-operations.md) and [Perceptor operations](operations/perceptor.md).

## Closure verification

```bash
scripts/verify_closure.sh release --env-file .env.test
```

The required release contract reports `STATIC`, `ARCHITECTURE`, `OPENAPI`, `UNIT_CONTRACT`, `POSTGRES`, `REAL_ASGI`, `DATABASE_RECLAIM_FOUNDATION`, `REPORT_CONTRACT`, and `CLOSURE_C1A_C1B_C2_C3`. Required pytest lanes fail on any unexpected skip.

`PROCESS_FAULT` and `REPORT_EXTERNAL_E2E` are separate optional capabilities and report `NOT_RUN` in the default release matrix. `PROCESS_FAULT` runs the Docker/Compose worker-kill and process/PostgreSQL restart harness; `REPORT_EXTERNAL_E2E` requires an explicitly configured external report boundary. Neither `NOT_RUN` state is presented as PASS or included in the default required verdict. Set `SLEEPAGENT_CLOSURE_REQUIRE_PROCESS_FAULT=1` or `SLEEPAGENT_CLOSURE_REQUIRE_EXTERNAL_REPORT_E2E=1` to make that capability required for a particular release invocation; an unavailable required capability yields `FINAL = NOT_VERIFIED`.
