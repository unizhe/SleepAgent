# ADR-001: Canonical observation boundary

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M1-M3 observation migration; no G1 cutover

## Context

Push and Pull currently construct `AdapterObservationCandidate` values through
adapter-specific normalizers and then bind them before reconciliation. Replay
constructs `ReplayObservationInput` and alone invokes
`validate_observation_ontology`. The three paths therefore do not share one
acceptance and semantic-validation boundary.

## Decision

All production Push, Pull, and Replay observations will converge on one
canonical validation and normalization authority:

```text
Perceptor Push
Perceptor Pull
Replay
    -> one canonical observation validation/normalization boundary
```

That boundary owns schema/version validation, metric and ontology validation,
provenance normalization, binding attribution, and UTC normalization. Adapters
may parse provider formats but may not define an independent canonical meaning.

## Invariants

- Semantically equivalent input has the same acceptance result and canonical
  payload independent of acquisition path.
- Provenance retains the adapter/acquisition identity without changing metric
  semantics.
- Invalid input is rejected or quarantined before durable canonical authority.
- There is one semantic validator, not path-specific copies.

## G1 boundary

G1 records current path discrepancies in characterization tests. It does not
create or route production code through a `CanonicalObservationFactory`.
