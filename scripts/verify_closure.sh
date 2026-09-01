#!/usr/bin/env bash
set -uo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_CLOSURE_PYTHON:-${SLEEPAGENT_E2E_PYTHON:-python}}"
REQUESTED="release"
ENV_FILE="${SLEEPAGENT_CLOSURE_ENV_FILE:-${REPOSITORY_ROOT}/.env.test.example}"

while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    release|all|static|architecture|openapi|unit-contract|postgres|process-fault|report-e2e|closure)
      REQUESTED="$1"
      shift
      ;;
    *)
      echo "usage: $0 [release|LANE] [--env-file PATH]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -r "${ENV_FILE}" ]]; then
  echo "closure verifier environment file is unreadable: ${ENV_FILE}" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
cd "${REPOSITORY_ROOT}"

if [[ "${SLEEPAGENT_BACKEND_SIGNING_KEY_REF:-}" == "file:/run/sleepagent/actor-public.pem" ]]; then
  export SLEEPAGENT_BACKEND_SIGNING_KEY_REF="file:${REPOSITORY_ROOT}/tests/fixtures/keys/replay_actor_public.pem"
fi

LANES=(
  STATIC
  ARCHITECTURE
  OPENAPI
  UNIT_CONTRACT
  POSTGRES
  PROCESS_FAULT
  REPORT_E2E
  CLOSURE_C1A_C1B_C2_C3
)
declare -A STATUS
for lane in "${LANES[@]}"; do
  STATUS["${lane}"]="SKIPPED_EXPLICIT"
done

selected() {
  local requested_lane="$1"
  [[ "${REQUESTED}" == "release" || "${REQUESTED}" == "all" || "${REQUESTED}" == "${requested_lane}" ]]
}

record_lane() {
  local lane="$1"
  shift
  echo "[${lane}] START"
  "$@"
  local code=$?
  case "${code}" in
    0) STATUS["${lane}"]="PASS" ;;
    77) STATUS["${lane}"]="ENV_BLOCKED" ;;
    *) STATUS["${lane}"]="FAIL" ;;
  esac
  echo "[${lane}] ${STATUS[${lane}]}"
}

postgres_preflight() {
  if [[ -z "${SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN:-}" \
     || -z "${SLEEPAGENT_TEST_POSTGRES_API_DSN:-}" \
     || -z "${SLEEPAGENT_TEST_POSTGRES_WORKER_DSN:-}" ]]; then
    echo "PostgreSQL test DSNs are not exported" >&2
    return 77
  fi
  "${PYTHON_BIN}" -c 'import os, psycopg; connection = psycopg.connect(os.environ["SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN"]); version = connection.execute("SHOW server_version_num").fetchone()[0]; connection.close(); raise SystemExit(0 if int(version) >= 160000 else 1)' \
    >/dev/null 2>&1 || return 77
  return 0
}

postgres_prepare() {
  postgres_preflight || return $?
  "${PYTHON_BIN}" -m sleepagent.persistence.migrate \
    --database-url-env SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN apply || return $?
  "${PYTHON_BIN}" -m sleepagent.persistence.migrate \
    --database-url-env SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN check
}

postgres_lane() {
  postgres_prepare || return $?
  "${PYTHON_BIN}" -m pytest -q -m postgres
}

process_fault_lane() {
  if [[ "${STATUS[POSTGRES]}" != "PASS" && "${REQUESTED}" != "process-fault" ]]; then
    echo "controlled PostgreSQL fault proofs require a passing POSTGRES lane" >&2
    return 1
  fi
  postgres_prepare || return $?
  "${PYTHON_BIN}" -m pytest -q \
    tests/unit/test_backend_process_fault_probe.py \
    tests/integration/test_backend_postgres_foundation.py \
    -m "not postgres" || return $?
  "${PYTHON_BIN}" -m pytest -q -m postgres \
    tests/integration/test_worker_postgres_integration.py \
    -k "expired_claim or reserved_invocation or delivery_reclaim or business_retry"
}

report_e2e_lane() {
  if [[ "${SLEEPAGENT_E2E_REPORT_ENABLED:-}" == "1" ]]; then
    local required=(
      SLEEPAGENT_REPORT_BASE_URL
      SLEEPAGENT_REPORT_SERVICE_CREDENTIAL
      SLEEPAGENT_REPORT_ACTOR_PRIVATE_KEY
      SLEEPAGENT_REPORT_ACTOR_ID
      SLEEPAGENT_REPORT_SUBJECT_ID
      SLEEPAGENT_REPORT_ROLE
      SLEEPAGENT_E2E_REPORT_WAKE_DATE
    )
    local name
    for name in "${required[@]}"; do
      if [[ -z "${!name:-}" ]]; then
        echo "missing report E2E variable: ${name}" >&2
        return 77
      fi
    done
    echo "REPORT_E2E_MODE=EXTERNAL_PROCESS"
    "${PYTHON_BIN}" -m pytest -q tests/e2e/test_product_report_cli.py
    return $?
  fi
  if [[ "${SLEEPAGENT_CLOSURE_REQUIRE_EXTERNAL_REPORT_E2E:-0}" == "1" ]]; then
    echo "external report process proof was explicitly required but is not configured" >&2
    return 77
  fi
  if [[ "${STATUS[POSTGRES]}" != "PASS" && "${REQUESTED}" != "report-e2e" ]]; then
    echo "controlled report proof requires a passing POSTGRES lane" >&2
    return 1
  fi
  echo "REPORT_E2E_MODE=CONTROLLED_EQUIVALENT"
  postgres_prepare || return $?
  "${PYTHON_BIN}" -m pytest -q \
    tests/unit/test_report_cli.py \
    tests/unit/test_product_report_contracts.py \
    tests/unit/test_report_consumer_audit.py || return $?
  "${PYTHON_BIN}" -m pytest -q -m postgres \
    tests/integration/test_product_postgres_integration.py::test_product_report_exact_reservation_and_stateless_reads_are_postgres_safe
}

closure_lane() {
  if [[ "${REQUESTED}" == "release" || "${REQUESTED}" == "all" ]]; then
    if [[ "${STATUS[POSTGRES]}" != "PASS" ]]; then
      echo "closure proof requires the complete PostgreSQL lane" >&2
      return 1
    fi
    echo "CLOSURE_EVIDENCE=EXECUTED_IN_FULL_POSTGRES_LANE"
    "${PYTHON_BIN}" -m pytest --collect-only -q \
      tests/integration/test_perceptor_pull_postgres.py::test_push_pull_reconciliation_and_crash_replay_postgres \
      tests/integration/test_product_postgres_integration.py::test_soft_report_before_hard_automatically_reevaluates_care \
      tests/integration/test_c3_personalization_governance_postgres.py::test_c3_receipt_governance_and_next_shared_analysis_process_proof \
      >/dev/null
    return $?
  fi
  postgres_prepare || return $?
  "${PYTHON_BIN}" -m pytest -q -m postgres \
    tests/integration/test_perceptor_pull_postgres.py::test_push_pull_reconciliation_and_crash_replay_postgres \
    tests/integration/test_product_postgres_integration.py::test_soft_report_before_hard_automatically_reevaluates_care \
    tests/integration/test_c3_personalization_governance_postgres.py::test_c3_receipt_governance_and_next_shared_analysis_process_proof
}

if selected static; then
  record_lane STATIC "${PYTHON_BIN}" -m compileall -q sleepagent scripts tests
fi
if selected architecture; then
  record_lane ARCHITECTURE "${PYTHON_BIN}" -m pytest -q tests/architecture
fi
if selected openapi; then
  record_lane OPENAPI "${PYTHON_BIN}" -m pytest -q \
    tests/integration/test_backend_app.py -k openapi -m "not postgres"
fi
if selected unit-contract; then
  record_lane UNIT_CONTRACT "${PYTHON_BIN}" -m pytest -q \
    -m "not postgres and not e2e and not asgi_lifespan"
fi
if selected postgres; then
  record_lane POSTGRES postgres_lane
fi
if selected process-fault; then
  record_lane PROCESS_FAULT process_fault_lane
fi
if selected report-e2e; then
  record_lane REPORT_E2E report_e2e_lane
fi
if selected closure; then
  record_lane CLOSURE_C1A_C1B_C2_C3 closure_lane
fi
echo
for lane in "${LANES[@]}"; do
  printf '%-26s %s\n' "${lane}" "${STATUS[${lane}]}"
done

final="PASS"
if [[ "${REQUESTED}" == "release" || "${REQUESTED}" == "all" ]]; then
  for lane in "${LANES[@]}"; do
    case "${STATUS[${lane}]}" in
      FAIL) final="FAIL"; break ;;
      ENV_BLOCKED|SKIPPED_EXPLICIT) final="NOT_VERIFIED" ;;
    esac
  done
else
  for lane in "${LANES[@]}"; do
    case "${STATUS[${lane}]}" in
      FAIL) final="FAIL" ;;
      ENV_BLOCKED) final="NOT_VERIFIED" ;;
    esac
  done
fi
echo "FINAL = ${final}"

case "${final}" in
  PASS) exit 0 ;;
  NOT_VERIFIED) exit 77 ;;
  *) exit 1 ;;
esac
