#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SUITE="${1:-all}"

cd "${REPOSITORY_ROOT}"
"${PYTHON_BIN}" -m sleepagent.persistence.migrate check

run_suite() {
  case "$1" in
    core)
      "${PYTHON_BIN}" -m pytest -q \
        tests/unit/test_backend_runtime.py \
        tests/unit/test_worker_runtime.py \
        tests/unit/test_product_agent_runner.py \
        tests/integration/test_sleep_postgres_vertical_slice.py \
        -m "not postgres and not e2e"
      ;;
    read-models)
      "${PYTHON_BIN}" -m pytest -q \
        tests/integration/test_backend_app.py \
        tests/unit/test_product_sleep_api.py \
        -m "not postgres and not asgi_lifespan and not e2e"
      ;;
    delivery-recovery)
      "${PYTHON_BIN}" -m pytest -q \
        tests/unit/test_product_publication_service.py \
        tests/unit/test_worker_runtime.py \
        -m "not postgres and not e2e"
      ;;
    retention)
      "${PYTHON_BIN}" -m pytest -q tests/unit/test_retention.py
      ;;
    *)
      echo "usage: $0 [core|read-models|delivery-recovery|retention|all]" >&2
      return 2
      ;;
  esac
}

if [[ "${SUITE}" == "all" ]]; then
  for item in core read-models delivery-recovery retention; do
    run_suite "${item}"
  done
else
  run_suite "${SUITE}"
fi
