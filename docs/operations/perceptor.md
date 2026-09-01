# Perceptor cloud operations

SleepAgent consumes vendor-processed mmWave radar data through a cloud-to-cloud integration. It does not ingest raw ADC/IQ waveforms, point clouds, UART, or local MQTT data.

## Current path

```text
DeviceBinding
  → signed Push and bounded read-only Pull
  → encrypted durable ingress
  → Observation Semantics V2
  → NightEpisode projection / immutable late revisions
  → SOFT then HARD NightFinalization
  → shared analysis and role projections
```

`DeviceBinding` is the only authority mapping a provider device to a subject. Missing or ambiguous bindings quarantine data; SleepAgent does not guess. No real device ID, provider credential, response payload, or private credential path belongs in Git or logs.

## Push

`POST /integrations/perceptor/webhook` verifies the frozen exact-body HMAC-SHA1/Base64 contract and freshness before encrypting the raw body in PostgreSQL. The API acknowledges only after durable commit. The `ingestion` worker normalizes under lease/fence authority; generation-scoped idempotency converges provider retries.

## History and SleepReport Pull

The allowlisted read-only Platform client supports device discovery plus bounded current/history/SleepReport reads. History overlap Pull repairs Push gaps. SleepReport contributes vendor-processed night facts. Responses are encrypted at durable ingress; checkpoints advance only after normalization and reconciliation commit. NO_DATA is recorded explicitly without invented facts.

Automatic scheduling is productized but feature-gated. A configured acquisition schedule is scanned by `python -m sleepagent.bootstrap.scheduler once|run`; the scheduler uses PostgreSQL `SKIP LOCKED` to create idempotent work and never performs Pull itself. Workers consume:

```text
perceptor.history_overlap_pull
perceptor.sleep_report_pull
night.finalization_scan
```

`SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED` remains `false` by default. Enable it only after schema, API, worker, binding, provider, queue, and protected operational status checks pass.

## Observation V2 and Episode projection

Vendor JSON stays behind the adapter boundary. Trusted V2 metrics carry explicit identity, unit, window/coverage semantics, and provenance. `movement_index` is never substituted for `movement_event_count`; ambiguous V1 movement is compatibility evidence only.

The Episode projector creates or updates the subject-local NightEpisode. A material observation that arrives after closure is associated with an eligible closed Episode and creates an immutable superseding Episode revision. The finalizer progresses `OPEN → SOFT_FINALIZED → HARD_FINALIZED`; date conflict enters reconciliation-required state. A material late revision can create a new hard-finalization revision and bounded report/Care/outcome reevaluation.

## Configuration and health

Live profiles require `data_mode=live`, explicit `perceptor_push` surface/queues, namespace/account identity, a current `DeviceBinding`, and protected `env:`/`file:` credential references. The provider base URL must be HTTPS without embedded credentials.

```bash
python -m sleepagent.persistence.migrate check
python -m sleepagent.workers.runtime healthcheck
python -m sleepagent.device_cli --help
```

Use `/livez` only for process liveness and protected `/internal/readyz` plus aggregate status for release readiness. Stop new schedules before draining workers. On provider outage, keep checkpoints unchanged and resume with the overlap window; never reassign raw rows or advance checkpoints manually.

Natural real-device Alarm-to-urgent acceptance and ongoing provider availability remain deployment acceptance responsibilities. Local/CI verification uses sanitized controlled fixtures and never contacts a real device.
