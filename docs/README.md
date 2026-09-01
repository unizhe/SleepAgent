# SleepAgent documentation

The documents in **CURRENT** describe schema target **027** and the repository at public closure. Material under `history/` and the immutable audit snapshots describe earlier checkpoints and is not current architecture.

## CURRENT

1. [Architecture](architecture.md) — current product and persistence topology.
2. [Operations](operations.md) — startup, readiness, activation, recovery, and verification.
3. [Perceptor operations](operations/perceptor.md) — real vendor-cloud Push/Pull and device-to-night flow.
4. [Durable runtime operations](operations/runtime-operations.md) — queues, leases, fencing, retry, drain, and status.
5. [Care governance](architecture/care-governance.md) — CareStrategy, HITL, trusted-operator Care Plan execution.
6. [Outcome and personalization](architecture/outcome-personalization.md) — non-causal outcomes and human-governed Memory.
7. [Portfolio demo](demo.md) — one deterministic end-to-end story.
8. [Limitations](limitations.md) — scope, trust, scale, and deployment boundaries.

Supporting current references:

- [HTTP contracts](contracts/README.md)
- [Product report CLI](operations/product-report.md)
- [Resume/interview claim matrix](resume-claims.md)
- [Pre-closure audit status](audit/README.md)

## HISTORICAL / AUDIT

- [`history/plans/`](history/plans/) — prior plans and review logs.
- [`history/remediation/`](history/remediation/) — public-safe remediation ADRs and checkpoint evidence.
- [`history/audits/`](history/audits/) — earlier P4 audits.
- [`history/pre-closure/`](history/pre-closure/) — superseded G8/G9/G10/P4 documents, retained unchanged.

The two local `docs/audit/FINAL_*.md` snapshots and two path-bearing remediation records are immutable pre-closure evidence. They are intentionally not tracked because the originals contain private absolute workstation paths; [the status index](audit/README.md) records their hashes and current finding disposition without rewriting them.
