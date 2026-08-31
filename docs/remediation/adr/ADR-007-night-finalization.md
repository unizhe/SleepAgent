# ADR-007: Night finalization is separate from date ownership

- Status: Implemented
- Date: 2026-08-30
- Scope: M12-M14 device automation; additive to the G1 date state machine

## Context

Current `NightEpisodeV2` date state determines wake-date ownership and episode
lifecycle. It does not prove that Push, Pull, and vendor SleepReport acquisition
is complete. Reinterpreting existing `date_state='finalized'` as data
finalization would corrupt established semantics and rows.

## Decision

Future data completeness uses a distinct additive finalization aggregate:

```text
OPEN -> SOFT_FINALIZED -> HARD_FINALIZED
late accepted evidence -> new immutable revision -> reanalysis
```

## Invariants

- Date ownership and acquisition/data finalization remain separate contracts.
- A hard-finalized revision is immutable.
- Late valid evidence creates a superseding revision; it never overwrites the
  evidence behind a published report.
- Finalization eligibility is deterministic and cannot be decided by an LLM.
- Scheduler automation must reuse durable Pull/checkpoint behavior and remain
  disabled until its owning goal explicitly activates it.

## G1 boundary

`acquisition_scheduler_enabled` defaults to false. G1 adds no scheduler,
finalization state, database migration, or automatic report trigger.

## Implementation

Migrations 015–017 implement the accepted boundary without changing
`NightEpisodeV2.date_state`. Device binding lifecycle, acquisition schedules,
and night-data finalization are separate RLS-scoped authorities. The scheduler
only creates UUIDv7 durable operations; existing workers perform Perceptor Pull
or deterministic finalization under exact workload snapshots.

The runtime feature gate still defaults to false. Native PostgreSQL 16.14 tests
cover temporal conflicts/transfers, multi-instance `SKIP LOCKED` firing,
expired-lease recovery, duplicate-fire idempotency, SOFT/HARD/reconciliation
transitions, immutable late revisions, and UTC/local-date crossing. No live
device acceptance was performed (`LIVE_ACCEPTANCE_DEFERRED`).
