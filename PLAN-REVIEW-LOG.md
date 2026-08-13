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

## Act 4 — P2 Build

### Codex verification

Implemented the frozen P2 L2 wiring without replacing the retained Habit or
Governed Memory reducers. One additive migration (`009`) supplies separate
append-only Habit and Memory revisions, governed Memory read receipts, question
selection persistence, and a narrowly checked family-to-elder Habit confirmation
handoff. The canonical Product API owns proposal/confirmation/read commands; the
durable Product worker pins effective Habit state and purpose-scoped Memory
receipts into each role Episode before Evidence/Care consumption.

Real PostgreSQL verification found and minimally corrected three adapter defects:
psycopg JSONB parameters were bound as `bytea`, family-originated elder handles
were rejected by the original actor-local RLS policy, and strict tuple DTOs did
not normalize decoded JSON arrays. No product semantics or safety boundary was
relaxed.

- Existing completed suites retained: 464 unit tests and 65 non-PostgreSQL
  integration tests.
- Final targeted regression after the real-backend fixes: 80 passed.
- Focused real PostgreSQL Habit/Memory + Product pinning scenarios: 2 passed.
- Canonical signed HTTP Habit/Memory proposal, exact confirmation, and read:
  passed against schema 009.
- Canonical baseline versus personalized reanalysis pinned L2 versions 0/0 then
  1/1; `confirmed_habit` and governed `morning_voice` consumption changed from
  absent to present, with Care receipts returning the allowed item and Evidence
  receipts remaining empty.
- Canonical `worsening-vital-trend` verifier: `verified=true`.
- Canonical `urgent-zero-model` verifier: `verified=true`, terminal
  `unexpected_urgent_route`; Product/model/Care counts remained zero and the
  fast path persisted one succeeded operation plus four receipts/projections.
- Migration ledger/manifest check: schema version 009.
- `001`–`008` remained immutable; no new production Python file or package was
  introduced.
- All isolated API, Demo API, worker, and PostgreSQL processes were stopped.
- GitHub remote write: none.
