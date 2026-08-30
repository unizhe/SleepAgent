# ADR-003: Report authority

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M4-M9 report migration; no G1 cutover

## Context

The current repository is not legacy-primary. New report requests use shared
analysis as the primary path while also maintaining a non-claimable
compatibility operation and `product_agent_result.v1` compatibility result.
The accurate current state is `shared_compat`. Executable legacy per-role
preparation and historical/generic readers remain.

## Decision

The eventual single reporting authority is:

```text
SharedNightAnalysis
    -> deterministic role projections
```

Role projections may differ in audience presentation, not in underlying facts.
Compatibility and historical adapters are read-only/non-authoritative and may
be retired only after consumer-zero evidence.

## Invariants

- One accepted role-neutral analysis owns report semantics for a revision.
- Every role projection binds to that exact shared-analysis identity.
- `shared_compat` remains the default until an explicit later cutover.
- Legacy or compatibility results cannot become care/effect authority.

## G1 boundary

G1 adds switches and a consumer inventory only. It does not change operation
creation, report output, compatibility emission, readers, or legacy execution.
