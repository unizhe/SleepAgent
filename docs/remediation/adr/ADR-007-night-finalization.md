# ADR-007: Night finalization is separate from date ownership

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M12-M14 device/finalization migration; no G1 state machine

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
