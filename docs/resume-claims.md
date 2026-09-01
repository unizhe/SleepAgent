# Resume and interview claim matrix

This matrix is derived from the post-C3/R1 source at schema target 027 and the public-closure surface. Use the wording column; do not promote qualified statements into broader claims.

| Topic | Classification | Defensible wording |
| --- | --- | --- |
| Multi-Agent | CLAIM_WITH_QUALIFIER | Four fixed, centrally governed role runtimes with allowlisted coordination; not an autonomous peer swarm. |
| Real radar integration | CLAIM_WITH_QUALIFIER | Real Perceptor vendor-cloud Push/Pull integration for processed mmWave radar facts; not raw-signal processing. |
| LIVE device-to-night | CLAIM_WITH_QUALIFIER | Bound LIVE observations flow through production normalization into NightEpisode/finalization; live provider deployment acceptance remains external. |
| Observation V2 | SAFE_TO_CLAIM | Trusted typed semantics separate movement index from event count across normalization and Product facts. |
| Late-data immutable revisions | CLAIM_WITH_QUALIFIER | Eligible late observations associate to closed Episodes and create immutable superseding revisions when material. |
| PostgreSQL durable runtime | SAFE_TO_CLAIM | PostgreSQL-backed queues, schedules, checkpoints, revisions, leases, fencing, retry/reclaim, and idempotency. |
| Fault recovery | CLAIM_WITH_QUALIFIER | Controlled process/database fault tests prove reclaim and stale-fence rejection within bounded policies; this is not universal disaster recovery. |
| Exactly-once | DO_NOT_CLAIM | Claim fenced at-least-once processing with idempotent convergence instead. |
| Shared analysis | SAFE_TO_CLAIM | One canonical SharedNightAnalysis produces deterministic role projections on the default path. |
| Deterministic zh-CN role projections | SAFE_TO_CLAIM | Elder, family, and doctor projections derive deterministically from accepted shared analysis and policy. |
| HITL | SAFE_TO_CLAIM | Human approval creates explicit durable grant authority before a CarePlan. |
| Human Care execution | CLAIM_WITH_QUALIFIER | Trusted-operator, human-attested terminal execution; not end-user-authenticated or device-verified. |
| CareOutcome | CLAIM_WITH_QUALIFIER | Deterministic, non-causal observational comparison over pinned HARD-finalized evidence. |
| Personalization feedback loop | CLAIM_WITH_QUALIFIER | Human-accepted outcome-derived governed Memory is pinned and consumed by a later analysis cycle. |
| Automatic personalization | DO_NOT_CLAIM | The outcome worker cannot confirm Memory; pending/rejected candidates are excluded and Habit is not auto-mutated. |
| Habit / Memory | CLAIM_WITH_QUALIFIER | Append-only governed context with bounded exact retrieval and human confirmation; no vector retrieval. |
| Medical efficacy | DO_NOT_CLAIM | No diagnosis, treatment-effect, clinical-validation, or efficacy claim. |
| Production-ready | DO_NOT_CLAIM | External deployment, security operations, scale work, and live acceptance remain. |
| Open-source-ready | SAFE_TO_CLAIM | MIT-licensed, documented, reproducible public repository; this does not imply production readiness or community governance. |

## Source anchors

- Fixed roster and allowlists: `sleepagent/runtime/registry.py`; orchestration: `sleepagent/runtime/runner.py`.
- Observation V2 and late association: `sleepagent/domain/observation_semantics.py`, `sleepagent/application/acquisition.py`, migrations 014 and 023.
- Durable recovery: workers plus migrations 026–027 and process/fault tests.
- Shared analysis/report modes: `sleepagent/workers/product.py` and `sleepagent/report_consumer_audit.py`.
- Care and terminal trust: `sleepagent/application/care_actions.py`, `sleepagent/application/care_execution.py`, and `sleepagent/care_cli.py`.
- Outcome/personalization bridge: `sleepagent/application/care_outcomes.py`, `sleepagent/application/personalization_governance.py`, migration 025, and the C3 PostgreSQL process proof.
