# Public Closure build log

## Act 3 — Build

### Round 1 — Codex build

Added the authorized standard MIT License, rewrote the public entry and schema-027 canonical documentation, added current claim/limitation/audit-status matrices, separated historical material, reconfirmed and removed the unreferenced runtime actions module, and added a deterministic portfolio-demo wrapper plus hosted CI foundations.

Initial demo proof correctly refused a container-only signing-key path and a reused test database. The wrapper was corrected to resolve the committed public verification key from the repository and to create/force-drop only one validated uniquely named temporary database.

### Round 2 — Codex build

Full release verification exposed bootstrap-only environment variables leaking into a contract test. Those values were removed from the shared profile and scoped to the one-shot demo/CI bootstrap process. Clean-copy packaging validation then removed the redundant legacy license classifier while retaining the SPDX `MIT` expression and root License.

Historical content was byte-compared with its entry Git blobs. Two path-bearing remediation records joined the two immutable final-audit snapshots in the owner-local untracked audit area; this preserves their bytes while keeping absolute workstation paths out of the public tree.

### Codex verification

- `scripts/run_portfolio_demo.sh --env-file .env.test.example`: PASS, including governed Memory consumption in the next shared-analysis cycle and temporary-database cleanup.
- `scripts/verify_closure.sh release --env-file <isolated profile>` on fresh PostgreSQL 16: all eight lanes PASS and `FINAL = PASS`.
- Unit/contract: 1,215 passed; PostgreSQL: 50 passed and one documented skip; process-fault foundation/PostgreSQL: 45 + 11 passed; controlled report: 62 + 1 passed.
- Clean-copy smoke: imports/schema 027, static verifier, wheel build, relative paths, and the complete portfolio demo PASS.
- Public Markdown: 29 internal links checked, PASS.
- Public sensitive scan: zero absolute private-path files, zero private-key blocks, zero real credential findings. Test-only placeholder credentials, one synthetic secret constant, a public verification key, and sanitized device fixtures remain explicit non-production fixtures.
- Historical/final-audit hashes remained unchanged. Migrations remained unchanged.

Deviations from the work order:

- Original audit snapshots and two path-bearing remediation records are preserved owner-locally rather than tracked because publishing them unchanged would expose absolute workstation paths; rewriting them was forbidden.
- Hosted CI runs supported static, architecture, OpenAPI, unit/contract, PostgreSQL, and process-fault foundation checks. The complete controlled local verifier remains release evidence.

Fix rounds used: 2. The closure commit was recorded after explicit user approval; no push was performed.
