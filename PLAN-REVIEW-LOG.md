## Act 3 — Build

### Round 1 — Codex build

Implemented the frozen P1.5 CareStrategy production-routing work order from the
referenced attachment. Morning Review now permits, but does not require, Care;
accepted Evidence and typed risk receipts feed the existing coordination policy;
Care runs only when that policy returns candidate intents without an urgent
preemption. A revision-bound three-night vital trend supplies the missing typed
production signal, and accepted Care candidates remain in publication.

The initial real PostgreSQL worsening run exposed one contract-shape defect:
`trend_signals` had been placed inside the deterministic `data` payload and was
therefore ignored by the risk tool. The smallest fix moved `trend_signals` and
`trend_observation` to the existing top-level structured tool arguments. A fresh
real PostgreSQL run then produced six CareStrategy work products across the two
triggered nights, and all six retained Care candidate references appeared in
publication.

No Habit, Memory, frontend, radar, migration, legacy, scenario-name, or test-only
routing changes were made.

### Codex verification

- Focused Product/Care tests: 88 passed.
- Core runtime/governance/PostgreSQL-adjacent regression set: 140 passed.
- PostgreSQL Product repository integration: 6 passed before the final
  tool-argument nesting correction; the corrected nesting is covered directly by
  the focused unit regression.
- Full non-PostgreSQL/non-e2e suite excluding nine known environment or dependency
  compatibility nodes: 522 passed, 21 deselected.
- The nine excluded nodes consist of eight tests that require loopback socket
  creation denied by the sandbox and one existing FastAPI route-enumeration test
  incompatible with the installed lazy `_IncludedRouter` representation.
- `git diff --check`: passed.
- Python compilation: passed.
- Mypy: the changed scope is clean; three pre-existing errors remain only in the
  explicitly out-of-scope Habit/Memory-related files `runtime/memory.py` and
  `runtime/governance.py`.
- Real PostgreSQL `normal-one-night`: passed with Evidence present and zero Care.
- Real PostgreSQL `worsening-vital-trend`: passed its public verifier; four Product
  operations succeeded, Care ran on the two policy-triggered nights, and every
  accepted Care candidate was retained in publication.
- Real PostgreSQL `urgent-zero-model`: passed against a fresh isolated PostgreSQL
  16 database plus canonical BFF API, Demo API, and durable worker. The verifier
  returned `verified=true` with the expected terminal error code
  `unexpected_urgent_route`. Database cross-checks showed zero Product attempts,
  zero model/provider invocations, zero analysis revisions/Care products, one
  succeeded `fast_path` operation, and four persisted fast-path signal receipts
  and projections. The replay journey terminated at the authoritative boundary
  exactly as designed. All three temporary service processes and the database
  were stopped after verification.

Diff review found no scenario-name branching, unconditional four-agent pipeline,
Habit/Memory scope creep, migration change, or remote write. One manifest snapshot
hash was updated because the Morning Review registry contract intentionally
changed. Fix rounds used: 1 of 2.
