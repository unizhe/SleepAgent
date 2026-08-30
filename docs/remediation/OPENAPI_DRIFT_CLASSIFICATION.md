# G1 OpenAPI drift classification

Classification date: 2026-08-30. Generated schemas were compared in memory by
the canonical `scripts/generate_openapi_snapshots.py` implementation. No
snapshot file was regenerated or overwritten.

## Result

Both snapshot checks fail because committed snapshots predate intentional,
tested API surfaces. The drift consists only of additions: no route is removed,
no existing route changes, and no existing component schema changes.

| Snapshot / group | Diff | Classification | Authority evidence |
|---|---|---|---|
| `backend-bff-v1.json`: `/product/sleep/personalization/habit*` (4 routes) and `/product/sleep/personalization/memory*` (3 routes) | 7 added paths; 12 added habit/memory schemas | `STALE_SNAPSHOT` (present API is `INTENTIONAL_CURRENT_API`) | Routes are registered in `sleepagent/api/product_router.py:306-385`; their implementation landed before current HEAD and is covered by Product/L2 tests. |
| `backend-bff-v1.json`: `/product/sleep/reports`, `/product/sleep/reports/run`, `/product/sleep/reports/{wake_date}` | 3 added paths; 11 added report schemas | `STALE_SNAPSHOT` (present API is `INTENTIONAL_CURRENT_API`) | Current HEAD commit `51947e6` is `feat(product): add durable shared-analysis report CLI`; routes have unit/reference-client and PostgreSQL integration coverage (`tests/unit/test_product_sleep_api.py`, `tests/unit/test_product_report_contracts.py`, `tests/unit/test_sleep_api_reference_client.py`, `tests/integration/test_product_postgres_integration.py:3205-3290`). |
| `demo-v1.json`: `/demo/v1/technical-trace` | 1 added path and `DemoTechnicalTraceResponse` | `STALE_SNAPSHOT` (present API is `INTENTIONAL_CURRENT_API`) | Route is registered at `sleepagent/api/demo.py:355` and asserted in `tests/integration/test_backend_app.py:748` plus Demo CLI tests. |

The backend snapshot has 10 added paths and 23 added schemas in total. The Demo
snapshot has one added path and one added schema. No `UNINTENDED_API_DRIFT` was
identified in this comparison.

## Why snapshots remain unchanged in G1

The last snapshot commit is `9283cfb` (2026-08-13), while the current report and
technical-trace APIs were added later. A snapshot refresh is mechanically
justified, but the current generated report schema also reflects
`PRE_EXISTING_DIRTY_BASELINE` Elder fallback/report-contract work. Updating the
snapshot now would silently absorb user-owned uncommitted changes into G1.

Therefore the failure remains an explicit known baseline:

```text
OPENAPI_CHECK = FAIL
CAUSE = STALE_SNAPSHOT
REFRESH = DEFERRED_BY_PRE_EXISTING_DIRTY_BASELINE
```

## G1.5 reconciliation

G1.5 first repeated the structural comparison against the complete checkpoint
candidate. It reproduced exactly 10 backend paths and 23 backend schemas plus
one Demo path and one Demo schema, with zero removed paths/schemas and zero
changed existing paths/schemas. No new unexplained drift appeared.

The canonical generator then reconciled both snapshots. The authoritative
hashes are:

- `backend-bff-v1.json`:
  `4c5c87124b5db77cf5c54364ceb2c117e1252ed39a107787d7934e478fa76e5a`
- `demo-v1.json`:
  `c1b3d32a0e8acc98f04b69b8cc42e170cefd2d5e979e10772d8317215cac817a`

`python scripts/generate_openapi_snapshots.py --check` now passes under the
authoritative Python 3.11 environment. Runtime API behavior was not changed to
match the old snapshots.

The next safe reconciliation must either first preserve/commit the user-owned
report contract or generate and selectively review a patch that proves no
unrelated schema is absorbed. API behavior must not be changed to match the old
snapshot.
