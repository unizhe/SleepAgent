# Limitations

## Scope

SleepAgent is a personal research/portfolio software project, not a medical device or clinical product. It does not diagnose disease, provide emergency monitoring, or claim clinical validation or medical efficacy.

## Care identity

Terminal Care execution uses a trusted-operator boundary. Its human execution/completion records are attestations made under privileged service/database authority, not cryptographically authenticated elder/family login and not device-verified execution.

## Outcome

CareOutcome is a deterministic observational comparison and always records `causal_claim=false`. It does not establish that a care action caused a change. Pending/rejected personalization candidates do not affect later analysis; only human-accepted governed Memory does.

## Device

The real integration consumes vendor-processed Perceptor cloud Push/Pull data. SleepAgent does not process raw radar ADC/IQ signals or point clouds. Natural live-device Alarm-to-urgent acceptance and provider availability remain deployment responsibilities.

## Scale

The immutable Episode revision model copies the cumulative observation-ID set and has accepted write/storage amplification at portfolio scale. The R1 PostgreSQL 16.14 benchmark measured, under its specific serial fixture setup:

| Measurement | Result |
| --- | ---: |
| Canonical observations | 497 |
| Episode revisions | 495 |
| Measured PostgreSQL physical delta | ~7.45 MB |
| Serial ingest wall time | ~103 s |
| Full Episode reconstruction median | ~1.25 s |

These are measurements from one controlled benchmark setup, not universal production throughput or capacity claims. The design has not been optimized for multi-tenant/public scale.

## Deployment

The acquisition scheduler is feature-gated and disabled by default. Production process supervision, TLS, network policy, secrets management, PostgreSQL role/ACL provisioning, backups, monitoring, and vendor credentials are external operational setup. Hosted CI proves feasible static/contract/PostgreSQL foundations; the full local closure verifier remains release evidence for controlled process/fault lanes.

External email, SMS, WeChat, notifications, delivery providers, and device control are intentionally not implemented. A retained `live_delivery_enabled` input is deprecated/internal disabled configuration, not a feature.

## Rollback and compatibility

Observation V1, historical report reads, and report rollback/shadow paths intentionally remain. `shared_only` is the current public/default path; compatibility mode names should not be interpreted as independent modern architectures.
