#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_DEMO_PYTHON:-python}"
ENV_FILE="${REPOSITORY_ROOT}/.env.test.example"
TRACE=0

while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --trace)
      TRACE=1
      shift
      ;;
    *)
      echo "usage: $0 [--env-file PATH] [--trace]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -r "${ENV_FILE}" ]]; then
  echo "portfolio demo environment file is unreadable: ${ENV_FILE}" >&2
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

required=(
  SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN
  SLEEPAGENT_TEST_POSTGRES_API_DSN
  SLEEPAGENT_TEST_POSTGRES_WORKER_DSN
  SLEEPAGENT_TEST_POSTGRES_API_USER
  SLEEPAGENT_TEST_POSTGRES_API_PASSWORD
  SLEEPAGENT_TEST_POSTGRES_DEMO_USER
  SLEEPAGENT_TEST_POSTGRES_DEMO_PASSWORD
  SLEEPAGENT_TEST_POSTGRES_WORKER_USER
  SLEEPAGENT_TEST_POSTGRES_WORKER_PASSWORD
  SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL
  SLEEPAGENT_TEST_POSTGRES_DEMO_PRINCIPAL
  SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL
  SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "portfolio demo requires ${name}" >&2
    exit 2
  fi
done

if ! "${PYTHON_BIN}" -c \
  'import os, psycopg; c=psycopg.connect(os.environ["SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN"]); v=int(c.execute("SHOW server_version_num").fetchone()[0]); c.close(); raise SystemExit(0 if v >= 160000 else 1)' \
  >/dev/null 2>&1; then
  echo "portfolio demo requires reachable PostgreSQL 16 test DSNs" >&2
  exit 77
fi

DEMO_MAINTENANCE_DSN="${SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN}"
DEMO_DB_NAME="sleepagent_portfolio_${$}_${RANDOM}"
if [[ ! "${DEMO_DB_NAME}" =~ ^sleepagent_portfolio_[0-9]+_[0-9]+$ ]]; then
  echo "portfolio demo generated an invalid temporary database name" >&2
  exit 1
fi

rewrite_database_name() {
  "${PYTHON_BIN}" -c \
    'import sys, urllib.parse; p=urllib.parse.urlsplit(sys.argv[1]); print(urllib.parse.urlunsplit((p.scheme,p.netloc,"/"+sys.argv[2],p.query,p.fragment)))' \
    "$1" "${DEMO_DB_NAME}"
}

cleanup_demo_database() {
  local best_effort="${1:-false}"
  if [[ -z "${DEMO_DB_NAME:-}" || ! "${DEMO_DB_NAME}" =~ ^sleepagent_portfolio_[0-9]+_[0-9]+$ ]]; then
    return
  fi
  if ! SLEEPAGENT_DEMO_MAINTENANCE_DSN="${DEMO_MAINTENANCE_DSN}" \
    SLEEPAGENT_DEMO_DATABASE_NAME="${DEMO_DB_NAME}" \
      "${PYTHON_BIN}" -c \
        'import os, psycopg; from psycopg import sql; c=psycopg.connect(os.environ["SLEEPAGENT_DEMO_MAINTENANCE_DSN"], autocommit=True); c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(os.environ["SLEEPAGENT_DEMO_DATABASE_NAME"]))); c.close()' \
      >/dev/null 2>&1; then
    if [[ "${best_effort}" == "true" ]]; then
      return
    fi
    echo "portfolio demo could not clean its temporary database" >&2
    return 1
  fi
  DEMO_DB_NAME=""
}

trap 'cleanup_demo_database true' EXIT INT TERM
SLEEPAGENT_DEMO_MAINTENANCE_DSN="${DEMO_MAINTENANCE_DSN}" \
SLEEPAGENT_DEMO_DATABASE_NAME="${DEMO_DB_NAME}" \
  "${PYTHON_BIN}" -c \
    'import os, psycopg; from psycopg import sql; c=psycopg.connect(os.environ["SLEEPAGENT_DEMO_MAINTENANCE_DSN"], autocommit=True); c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(os.environ["SLEEPAGENT_DEMO_DATABASE_NAME"]))); c.close()'

export SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="$(rewrite_database_name "${SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN}")"
export SLEEPAGENT_TEST_POSTGRES_API_DSN="$(rewrite_database_name "${SLEEPAGENT_TEST_POSTGRES_API_DSN}")"
export SLEEPAGENT_TEST_POSTGRES_WORKER_DSN="$(rewrite_database_name "${SLEEPAGENT_TEST_POSTGRES_WORKER_DSN}")"
export SLEEPAGENT_TEST_POSTGRES_DEMO_DSN="$(rewrite_database_name "${SLEEPAGENT_TEST_POSTGRES_DEMO_DSN:-${SLEEPAGENT_TEST_POSTGRES_API_DSN}}")"
export SLEEPAGENT_TEST_POSTGRES_DB="${DEMO_DB_NAME}"
export SLEEPAGENT_BACKEND_DATABASE_IDENTITY="${DEMO_DB_NAME}"
export SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE="${SLEEPAGENT_TEST_POSTGRES_API_USER}"
export SLEEPAGENT_BOOTSTRAP_API_DATABASE_PASSWORD="${SLEEPAGENT_TEST_POSTGRES_API_PASSWORD}"
export SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_ROLE="${SLEEPAGENT_TEST_POSTGRES_DEMO_USER}"
export SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_PASSWORD="${SLEEPAGENT_TEST_POSTGRES_DEMO_PASSWORD}"
export SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_ROLE="${SLEEPAGENT_TEST_POSTGRES_WORKER_USER}"
export SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_PASSWORD="${SLEEPAGENT_TEST_POSTGRES_WORKER_PASSWORD}"
export SLEEPAGENT_BOOTSTRAP_API_SERVICE_PRINCIPAL_ID="${SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL}"
export SLEEPAGENT_BOOTSTRAP_DEMO_SERVICE_PRINCIPAL_ID="${SLEEPAGENT_TEST_POSTGRES_DEMO_PRINCIPAL}"
export SLEEPAGENT_BOOTSTRAP_WORKER_SERVICE_PRINCIPAL_ID="${SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL}"
export SLEEPAGENT_BOOTSTRAP_ACTOR_VERIFICATION_KEY_SHA256="${SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256}"

"${PYTHON_BIN}" -m sleepagent.persistence.migrate \
  --database-url-env SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN apply >/dev/null
SLEEPAGENT_BACKEND_DATABASE_DSN="${SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN}" \
  "${PYTHON_BIN}" -m sleepagent.persistence.test_bootstrap >/dev/null
"${PYTHON_BIN}" -m sleepagent.persistence.migrate \
  --database-url-env SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN check >/dev/null

DEMO_LOG="$(mktemp /tmp/sleepagent-portfolio-demo.XXXXXX)"
cleanup_demo() {
  rm -f -- "${DEMO_LOG:-}"
  cleanup_demo_database true
}
trap cleanup_demo EXIT INT TERM

run_proof() {
  local node="$1"
  : >"${DEMO_LOG}"
  if ((TRACE)); then
    if ! "${PYTHON_BIN}" -m pytest -vv -s -m postgres "${node}" \
      2>&1 | tee "${DEMO_LOG}"; then
      return 1
    fi
  elif ! "${PYTHON_BIN}" -m pytest -q -m postgres "${node}" \
    >"${DEMO_LOG}" 2>&1; then
    cat "${DEMO_LOG}" >&2
    return 1
  fi
  if grep -Eq '(^|[^0-9])[0-9]+ skipped' "${DEMO_LOG}"; then
    cat "${DEMO_LOG}" >&2
    echo "portfolio demo refused a skipped proof" >&2
    return 1
  fi
}

run_proof \
  tests/integration/test_perceptor_pull_postgres.py::test_push_pull_reconciliation_and_crash_replay_postgres
echo "睡眠输入：Observation V2 与夜间修订已确认"

run_proof \
  tests/integration/test_product_postgres_integration.py::test_soft_report_before_hard_automatically_reevaluates_care
echo "睡眠报告：共享分析与三角色投影路径已确认"
echo "照护建议：HARD 证据上的治理候选已确认"

run_proof \
  tests/integration/test_c3_personalization_governance_postgres.py::test_c3_receipt_governance_and_next_shared_analysis_process_proof
echo "人工批准：ApprovalGrant 与 CarePlan 已确认"
echo "计划执行：可信操作员人工完成记录已确认"
echo "执行后观察结果：非因果 CareOutcome 已确认"
echo "个性化记忆确认：待审候选经人工 ACCEPT"
echo "下一周期读取：已确认 Memory 修订被固定并读取"
cleanup_demo_database false
echo "测试状态：隔离临时数据库已清理"
echo "PORTFOLIO_DEMO = PASS"
