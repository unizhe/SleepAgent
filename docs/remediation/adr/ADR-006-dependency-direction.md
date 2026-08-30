# ADR-006: Dependency direction and composition

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M10-M11 architecture migration; G1 no-growth only

## Decision

The long-term dependency direction is:

```text
domain -> application -> ports/contracts
infrastructure implements ports
interfaces call application
bootstrap owns concrete assembly
```

Here arrows describe allowed source dependency toward the next stable
abstraction. Domain code remains independent of runtime, workers, persistence,
and concrete infrastructure. Worker kernels depend on handler contracts, not
concrete handlers. Bootstrap/composition roots choose implementations.

## Invariants

- New reverse dependencies do not expand the frozen debt baseline.
- No new import strongly connected component or self-import is accepted.
- Baseline exceptions are exact module/edge identities and can only shrink.
- Architectural debt is removed incrementally; a broad package rewrite is not
  a prerequisite for feature work.

## G1 boundary

G1 installs an AST no-growth guard around the current graph. It does not move
modules, split composition roots, or clean existing cycles.
