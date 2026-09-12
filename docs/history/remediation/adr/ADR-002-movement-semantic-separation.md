# ADR-002: Movement semantic separation

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M1-M3 movement migration; no G1 behavior change

## Context

`movement_payload.v1` permits `count`, `index`, and `event`, defaults to
`index`, and lacks a metric identifier. Realtime/vendor `body_shake` indices
and SleepReport hourly counts can consequently enter the same `movement`
series and be averaged together.

## Decision

The future canonical model distinguishes exactly these meanings:

```text
movement_index
movement_event_count
legacy_ambiguous_movement
```

`movement_index` is a vendor index. `movement_event_count` is a non-negative
count with an explicit aggregation window or event-time contract.
`legacy_ambiguous_movement` is an audit/read classification for historical
values whose meaning cannot be proven.

## Invariants

- Semantically incompatible movement metrics are never aggregated, trended,
  compared, or substituted for each other.
- Legacy ambiguity is never guessed into index or count semantics.
- Ambiguous values do not authorize risk or care conclusions.
- `sum_body_shake_times` remains a separate whole-night vendor summary.

## G1 boundary

G1 preserves and labels the current mixed aggregation as
`known_semantic_gap`. It does not implement Movement V2, upcasting, persistence
changes, or an aggregation fix.
