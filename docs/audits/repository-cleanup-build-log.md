# Repository cleanup build log

## Baseline

- Accepted Phase A–E regression suite: 1,094 passed, 5 skipped.
- Local checkpoint: `b0fb820`; tag `local/phase-a-e-accepted-20260811`.

## Phase F

- Consolidated root planning material under `docs/audits/`.
- Removed vendored HealthClaw extraction and generated ZIP archives.
- Moved product, persistence, device, integration, diagnostic, and replay code out of `sleepagent.radar_agent` and removed that package.
- Removed the historical runtime API/store branch and production legacy ASGI app.
- Squashed the development migration chain into one canonical PostgreSQL baseline and one SQLite development schema.
- Reorganized tests by responsibility and renamed phase-oriented test modules to current behavior.

Final proof commands and any environment-specific exceptions are recorded in the completion report.

## Verification

- clean-source-copy architecture suite: 102 passed; public API: 25 symbols
- full regression: 1,076 passed, 5 skipped
- architecture suite: 102 passed
- compileall: passed
- `git diff --check`: passed
- hash-locked dependency install in a temporary environment: passed
- isolated wheel build and install/import: passed; 185 entries, no historical namespace/shadow
- console scripts (`radar-agent`, `sleepagent-migrate`): passed
- Compose configuration: passed with `.env.test.example`
- frontend typecheck and production build: passed
- strict mypy: ran and reported 569 existing errors in 77 files; retained as technical debt because correcting them is outside the directory cleanup
- live PostgreSQL baseline proof: unavailable because this environment denies access to the Docker daemon; SQLite clean-create/idempotence and migration regression proofs passed
