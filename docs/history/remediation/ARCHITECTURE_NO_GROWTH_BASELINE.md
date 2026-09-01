# G1 architecture no-growth baseline

Captured at HEAD `51947e6da69d9d8e0e0eeb36669e227865fc26ff` against
the preserved dirty baseline. The machine-readable authority is
`tests/fixtures/remediation/architecture_import_baseline.json`; the executable
guard is `tests/architecture/test_remediation_dependency_no_growth.py`.

## Six existing cycle components

The baseline contains three multi-module strongly connected components:

1. A 14-module component containing `app`, `process`,
   `domain.postgres_slice`, Perceptor Push/Pull ingestion, replay journey and
   ingress, and the commands/demo/effects/ingestion/product/retention/runtime
   worker modules.
2. `domain.product_data` with `runtime.tools`.
3. An eight-module runtime component containing `cold_start`, `contracts`,
   `governance`, `hitl`, `invocation`, `memory`, `registry`, and `results`.

The other three SCCs are standalone self-import cycles in `config`,
`runtime.agents`, and `runtime.knowledge`. Exact self-import edges inside the
larger components (`runtime.contracts`, `runtime.registry`, and
`workers.retention`) are separately frozen in the JSON baseline.

## Frozen forbidden-edge debt

- Domain to runtime: `domain.product_data` to `runtime.cold_start`,
  `runtime.contracts`, and `runtime.tools`.
- Domain to worker: `domain.postgres_slice` to `workers.retention`.
- Composition reversal: `process` to `app`.
- Worker-kernel growth: `workers.runtime` to the concrete commands, demo,
  effects, ingestion, product, and retention worker modules.

The exact 11 edges are listed in the JSON fixture. They are grandfathered, not
approved design.

## Guard semantics

The test fails on a new SCC, new self-import, or new forbidden edge. A current
SCC may split into subsets and any exact edge may disappear, so later goals can
shrink debt without rewriting the baseline first. A synthetic test proves that
a new domain-to-worker edge and a new cycle are rejected.

G1 does not move imports or repair any baseline cycle.
