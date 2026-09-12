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
    release|all|static|architecture|openapi|unit-contract|postgres|real-asgi|database-reclaim|report-contract|process-fault|report-external-e2e|closure)
      REQUESTED="$1"
      shift
      ;;
    report-e2e)
      REQUESTED="report-external-e2e"
      shift
      ;;
    *)
      echo "usage: $0 [release|all|static|architecture|openapi|unit-contract|postgres|real-asgi|database-reclaim|report-contract|process-fault|report-external-e2e|closure] [--env-file PATH]" >&2
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
  REAL_ASGI
  DATABASE_RECLAIM_FOUNDATION
  REPORT_CONTRACT
  CLOSURE_C1A_C1B_C2_C3
  PROCESS_FAULT
  REPORT_EXTERNAL_E2E
)
REQUIRED_RELEASE_LANES=(
  STATIC
  ARCHITECTURE
  OPENAPI
  UNIT_CONTRACT
  POSTGRES
  REAL_ASGI
  DATABASE_RECLAIM_FOUNDATION
  REPORT_CONTRACT
  CLOSURE_C1A_C1B_C2_C3
)
declare -A STATUS
for lane in "${LANES[@]}"; do
  STATUS["${lane}"]="SKIPPED_EXPLICIT"
done
STATUS[PROCESS_FAULT]="NOT_RUN"
STATUS[REPORT_EXTERNAL_E2E]="NOT_RUN"

selected() {
  local requested_lane="$1"
  [[ "${REQUESTED}" == "release" || "${REQUESTED}" == "all" || "${REQUESTED}" == "${requested_lane}" ]]
}

selected_optional() {
  local requested_lane="$1"
  if [[ "${REQUESTED}" == "all" || "${REQUESTED}" == "${requested_lane}" ]]; then
    return 0
  fi
  if [[ "${REQUESTED}" != "release" ]]; then
    return 1
  fi
  case "${requested_lane}" in
    process-fault)
      [[ "${SLEEPAGENT_CLOSURE_REQUIRE_PROCESS_FAULT:-0}" == "1" ]]
      ;;
    report-external-e2e)
      [[ "${SLEEPAGENT_CLOSURE_REQUIRE_EXTERNAL_REPORT_E2E:-0}" == "1" ]]
      ;;
    *) return 1 ;;
  esac
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

pytest_no_skips() {
  SLEEPAGENT_PYTEST_FAIL_ON_SKIP=1 "${PYTHON_BIN}" -m pytest "$@"
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
  pytest_no_skips -q -m postgres
}

real_asgi_lane() {
  pytest_no_skips -q -m asgi_lifespan \
    tests/integration/test_backend_app.py
}

database_reclaim_foundation_lane() {
  if [[ "${STATUS[POSTGRES]}" != "PASS" && "${REQUESTED}" != "database-reclaim" ]]; then
    echo "database reclaim foundation requires a passing POSTGRES lane" >&2
    return 1
  fi
  postgres_prepare || return $?
  pytest_no_skips -q \
    tests/unit/test_backend_process_fault_probe.py \
    tests/integration/test_backend_postgres_foundation.py \
    -m "not postgres" || return $?
  pytest_no_skips -q -m postgres \
    tests/integration/test_worker_postgres_integration.py \
    -k "expired_claim or reserved_invocation or delivery_reclaim or business_retry"
}

process_fault_lane() {
  SLEEPAGENT_E2E_PYTHON="${PYTHON_BIN}" \
    "${REPOSITORY_ROOT}/scripts/verify_backend.sh" fault-process
}

report_contract_lane() {
  if [[ "${STATUS[POSTGRES]}" != "PASS" && "${REQUESTED}" != "report-contract" ]]; then
    echo "report contract proof requires a passing POSTGRES lane" >&2
    return 1
  fi
  echo "REPORT_CONTRACT_MODE=CONTROLLED_REPOSITORY"
  postgres_prepare || return $?
  pytest_no_skips -q \
    tests/unit/test_report_cli.py \
    tests/unit/test_product_report_contracts.py \
    tests/unit/test_report_consumer_audit.py || return $?
  pytest_no_skips -q -m postgres \
    tests/integration/test_product_postgres_integration.py::test_product_report_exact_reservation_and_stateless_reads_are_postgres_safe
}

report_external_e2e_lane() {
  if [[ "${SLEEPAGENT_E2E_REPORT_ENABLED:-}" != "1" ]]; then
    echo "external report E2E is not configured" >&2
    return 77
  fi
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
      echo "missing external report E2E variable: ${name}" >&2
      return 77
    fi
  done
  echo "REPORT_EXTERNAL_E2E_MODE=EXTERNAL_PROCESS"
  pytest_no_skips -q -m e2e tests/e2e/test_product_report_cli.py
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
  pytest_no_skips -q -m postgres \
    tests/integration/test_perceptor_pull_postgres.py::test_push_pull_reconciliation_and_crash_replay_postgres \
    tests/integration/test_product_postgres_integration.py::test_soft_report_before_hard_automatically_reevaluates_care \
    tests/integration/test_c3_personalization_governance_postgres.py::test_c3_receipt_governance_and_next_shared_analysis_process_proof
}

if selected static; then
  record_lane STATIC "${PYTHON_BIN}" -m compileall -q sleepagent scripts tests
fi
if selected architecture; then
  record_lane ARCHITECTURE pytest_no_skips -q tests/architecture
fi
if selected openapi; then
  record_lane OPENAPI pytest_no_skips -q \
    tests/integration/test_backend_app.py -k openapi \
    -m "not postgres and not asgi_lifespan and not e2e"
fi
if selected unit-contract; then
  record_lane UNIT_CONTRACT pytest_no_skips -q \
    -m "not postgres and not e2e and not asgi_lifespan and not process_harness"
fi
if selected postgres; then
  record_lane POSTGRES postgres_lane
fi
if selected real-asgi; then
  record_lane REAL_ASGI real_asgi_lane
fi
if selected database-reclaim; then
  record_lane DATABASE_RECLAIM_FOUNDATION database_reclaim_foundation_lane
fi
if selected report-contract; then
  record_lane REPORT_CONTRACT report_contract_lane
fi
if selected closure; then
  record_lane CLOSURE_C1A_C1B_C2_C3 closure_lane
fi
if selected_optional process-fault; then
  record_lane PROCESS_FAULT process_fault_lane
fi
if selected_optional report-external-e2e; then
  record_lane REPORT_EXTERNAL_E2E report_external_e2e_lane
fi
echo
for lane in "${LANES[@]}"; do
  printf '%-26s %s\n' "${lane}" "${STATUS[${lane}]}"
done

final="PASS"
if [[ "${REQUESTED}" == "release" || "${REQUESTED}" == "all" ]]; then
  verdict_lanes=("${REQUIRED_RELEASE_LANES[@]}")
  if [[ "${REQUESTED}" == "all" ]]; then
    verdict_lanes=("${LANES[@]}")
  else
    if [[ "${SLEEPAGENT_CLOSURE_REQUIRE_PROCESS_FAULT:-0}" == "1" ]]; then
      verdict_lanes+=(PROCESS_FAULT)
    fi
    if [[ "${SLEEPAGENT_CLOSURE_REQUIRE_EXTERNAL_REPORT_E2E:-0}" == "1" ]]; then
      verdict_lanes+=(REPORT_EXTERNAL_E2E)
    fi
  fi
  for lane in "${verdict_lanes[@]}"; do
    case "${STATUS[${lane}]}" in
      FAIL) final="FAIL"; break ;;
      ENV_BLOCKED|SKIPPED_EXPLICIT|NOT_RUN) final="NOT_VERIFIED" ;;
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
