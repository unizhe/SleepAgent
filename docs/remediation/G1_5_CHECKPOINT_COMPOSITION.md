# G1.5 pre-G2 checkpoint composition

Classification date: 2026-08-30.

This manifest enumerates every path authorized for the G1.5 local checkpoint.
It preserves the complete coherent baseline rather than claiming that all
included work was authored by G1. No ignored environment, cache, generated
junk, secret-bearing file, or unrelated untracked file is authorized.

## `PRE_G1_WORK` (18 paths)

- `sleepagent/api/postgres.py`
- `sleepagent/api/product_contracts.py`
- `sleepagent/domain/product_data.py`
- `sleepagent/report_cli.py`
- `sleepagent/runtime/agent_invocation_coordinator.py`
- `sleepagent/runtime/agents.py`
- `sleepagent/runtime/contracts.py`
- `sleepagent/runtime/deterministic_model.py`
- `sleepagent/runtime/governance.py`
- `sleepagent/runtime/reports.py`
- `sleepagent/runtime/runner.py`
- `sleepagent/workers/product.py`
- `tests/unit/test_product_agent_runner.py`
- `tests/unit/test_product_data_tools.py`
- `tests/unit/test_product_elder_narrative_worker.py`
- `tests/unit/test_product_report_contracts.py`
- `tests/unit/test_product_sleep_api.py`
- `tests/unit/test_report_cli.py`

These paths are the user-owned report/runtime/test patch that predated G1. Its
binary diff SHA-256 remains
`f306cc80aa4e25df543501ccbdd23e9917c57ecf9dfc9a152abb436381f78806`,
identical to the frozen pre-G1 path subset.

## `G1_SAFEGUARD` (6 paths)

- `sleepagent/config.py`
- `tests/architecture/test_remediation_dependency_no_growth.py`
- `tests/fixtures/remediation/architecture_import_baseline.json`
- `tests/fixtures/remediation/g1_characterization.json`
- `tests/unit/test_g1_characterization.py`
- `tests/unit/test_remediation_settings.py`

These paths contain only default-preserving feature contracts,
characterization evidence, and the architecture no-growth guard.

## `REMEDIATION_DOC` (13 paths)

- `SLEEPAGENT_REMEDIATION_AND_CLOSURE_PLAN.md`
- `docs/remediation/ARCHITECTURE_NO_GROWTH_BASELINE.md`
- `docs/remediation/EXECUTION_LEDGER.md`
- `docs/remediation/G1_5_CHECKPOINT_COMPOSITION.md`
- `docs/remediation/LEGACY_SHARED_REPORT_CONSUMERS.md`
- `docs/remediation/OPENAPI_DRIFT_CLASSIFICATION.md`
- `docs/remediation/adr/ADR-001-canonical-observation-boundary.md`
- `docs/remediation/adr/ADR-002-movement-semantic-separation.md`
- `docs/remediation/adr/ADR-003-report-authority.md`
- `docs/remediation/adr/ADR-004-external-effect-authority.md`
- `docs/remediation/adr/ADR-005-time-authority.md`
- `docs/remediation/adr/ADR-006-dependency-direction.md`
- `docs/remediation/adr/ADR-007-night-finalization.md`

These paths preserve the authoritative remediation plan, execution history,
ADRs, inventories, classified baselines, and this exact checkpoint manifest.

## `OTHER_INTENTIONAL_BASELINE` (3 paths)

- `PLAN-REVIEW-LOG.md`
- `docs/contracts/openapi/backend-bff-v1.json`
- `docs/contracts/openapi/demo-v1.json`

The build log records provenance across the pre-G1, G1, and G1.5 work. The two
OpenAPI files are deterministic outputs of the existing canonical generator;
their G1.5 reconciliation is additive-only and makes the established gate pass.

## Explicit exclusions

The checkpoint excludes `.env`, `.env.*`, `.venv/`, `.pytest_cache/`, Python
bytecode/cache directories, local key fixtures, ignored frontend/reference
artifacts, local document attachments, and every other ignored or unlisted
path. Staging must use the exact paths above; `git add .` and `git add -A` are
not permitted.

## Verification binding

- Python: `/tmp/sleepagent-g1_5-py311/bin/python`, version 3.11.15.
- Dependencies: `requirements/dev.lock`, installed with `--require-hashes` and
  `--only-binary=:all:`.
- OpenAPI hashes after canonical reconciliation:
  - backend: `4c5c87124b5db77cf5c54364ceb2c117e1252ed39a107787d7934e478fa76e5a`
  - Demo: `c1b3d32a0e8acc98f04b69b8cc42e170cefd2d5e979e10772d8317215cac817a`
- Migration manifest/hash verification: 13 migrations through version 13,
  manifest SHA-256
  `c027b2a2e7828713422770d14e171a795c94b32e716b5eb077e33bd610880a06`.
- Actual PostgreSQL execution remains `ENV_BLOCKED`; it is not represented as
  a pass.

The checkpoint commit hash is necessarily recorded by a subsequent
documentation-only attestation commit because a commit cannot contain its own
hash.
