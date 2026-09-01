# P4 repository consolidation

## Verdict

```text
P4_REPOSITORY_CONSOLIDATION_VERDICT = PASS
P4_COMPLETION = COMPLETE_WITH_LIMITATIONS
RELEASE_READY = NO

P4_HISTORICAL_ARCHIVE = PASS
PRODUCTION_DEPENDENCY_CLOSURE = PASS
P4_DISCOVERED_PRODUCTION_REGRESSIONS = PRESERVED
CORE_TEST_DEPENDS_ON_P4_SCRIPT = NO

MIGRATION_HISTORY_IMMUTABLE = PASS
MIGRATION_MANIFEST_VALID = PASS
SCHEMA_TARGET_VALID = PASS
SANITIZED_CONTRACT_FIXTURES = PASS
FIXTURE_REFERENCES_VALID = PASS

README_CURRENT_ARCHITECTURE = PASS
STALE_NO_PERCEPTOR_CLAIM = 0
ARCHITECTURE_DOC_CURRENT = PASS
KNOWN_STALE_ARCHITECTURE_CLAIMS = 0

PYTHON_VERSION = 3.11.15
PY311_FOCUSED_TESTS = PASS (354 passed, 19 PostgreSQL-environment skips)
PY311_FULL_TESTS = PASS_WITH_DOCUMENTED_ENV_SKIPS (906 passed, 25 skipped)
PY311_COMPILE_IMPORT = PASS
GIT_DIFF_CHECK = PASS

SECRET_LEAKAGE = PASS
TOKEN_LEAKAGE = PASS
IDENTIFIER_LEAKAGE = PASS
PRIVATE_DATA_LEAKAGE = PASS

DELETED_FILE_REFERENCES = 0
BROKEN_P4_SCRIPT_IMPORTS = 0
BROKEN_DOC_LINKS = 0
TEMPORARY_P4_SCRIPT_COUNT_AFTER = 0
SCRIPT_ONLY_TESTS_REMOVED = 10

FILES_DELETED = 41
FILES_RENAMED = 0
FILES_CREATED = 6
FILES_MODIFIED = 12
LINES_REMOVED_APPROX = 18535
LINES_ADDED_APPROX = 681
NET_LINE_CHANGE_APPROX = -17854

READY_FOR_COMMIT = YES
```

## Inventory and disposition

The frozen 111-path working-tree inventory was classified before deletion:

| Classification | Paths | Disposition |
|---|---:|---|
| `PRODUCTION_KEEP` | 34 | Perceptor/runtime/migration closure retained. |
| `PRODUCTION_REGRESSION_KEEP` | 35 | Stable tests and sanitized fixtures retained. |
| `ACCEPTANCE_SCAFFOLD_EXTRACT_THEN_DELETE` | 3 | Stable contracts extracted, coordinators removed. |
| `ACCEPTANCE_SCAFFOLD_DELETE` | 2 | Session-only helpers removed. |
| `HISTORICAL_EVIDENCE_ARCHIVE_THEN_DELETE` | 36 | Archived with checksums, then removed. |
| `REVIEW` | 1 | P4 diary condensed; P0–P3 history preserved. |

Before cleanup there were 34 tracked modifications, 77 untracked paths, 13
P4 stage scripts, 10 stage-script tests and 17 stage audit reports. After
cleanup there are 39 tracked modifications, 42 untracked paths, zero P4 stage
scripts, zero stage-script tests and two consolidated P4 audit documents.
Git-visible canonical files decreased from 243 to 208; newline-counted text is
approximately 154,108 lines before versus 136,254 after.

The 41 removed paths comprise the root plan plus 17 construction reports, 13
one-shot scripts and 10 script-only tests. No production module, migration,
sanitized fixture or stable regression was deleted.

## Historical archive

All removal candidates and the complete pre-cleanup P4 construction log were
copied to a repository-external archive before deletion. The archive leaf and
its parent are owner-only `0700`; files are `0600`; 42 archived source/evidence
items pass the recorded SHA-256 manifest. The archive contains no credential or
private raw-evidence payload and is absent from Git status.

## Extracted stable contracts

- `sleep_report_is_no_data` in the production Pull module replaces the only
  retained test import from a stage script.
- `sleepagent.persistence.frozen_evidence` retains owner-only archive,
  credential-key rejection, manifest tamper detection, file hash and all-field
  frozen restore-semantic checks without retaining P4 session/restore
  orchestration.
- `test_frozen_evidence_integrity.py` gives these contracts stable domain
  naming and verifies every production-relevant snapshot field.

All 19 P4-discovered production regression categories were mapped to retained
domain tests. The cleanup also fixed one concrete full-suite finding: provider
context projection now serializes nested date/datetime values while preserving
the identifier-free allowlist, and the personalization test reads the public
projection rather than reconstructing a private `ContextPacket`.

## Verification

The native project Python 3.11 environment compiled/imported all 84 production
Python files. Focused verification covered Perceptor signing/contracts,
webhook, Pull, reconciliation, DeviceBinding, quality, lease/fence, Product
privacy, urgent zero-model and architecture boundaries. Nineteen focused skips
were solely the absent local PostgreSQL DSN/server.

The complete local suite ran with external provider credentials and PostgreSQL
DSNs explicitly removed. It passed 906 tests; 25 skips were the compose/process
proof and PostgreSQL integration lanes unavailable in this checkpoint. Local
loopback HTTP tests ran successfully. No vendor, model-provider or other
external request was made.

Migration `001–010` files are unchanged. The ordered `001–013` manifest,
filenames, identities and file checksums validate, and schema target 13 imports
successfully. `git diff --check`, deleted-basename/import scans and Markdown
link validation pass.

The canonical leakage scan covered every tracked/untracked text file. Key-like
values are confined to explicit synthetic loopback/contract tests; long
identifier shapes are synthetic/sanitized fixtures, lock hashes or an integer
sentinel. No private key, JWT, live credential, private evidence path, real
device/subject identifier, raw private payload or provider prompt/response is
present.

## Documentation result

README and the main architecture now describe the real YunYun/Perceptor Push +
Pull chain and schema 013. Stable Perceptor architecture and operations docs
state the vendor-processed-data boundary, durable semantics, health/recovery
steps and current provisioning/scheduler/finalization gaps. The final P4
summary preserves the real evidence, remediation root causes, privacy/safety
repairs and immutable limitations without retaining the construction diary.
