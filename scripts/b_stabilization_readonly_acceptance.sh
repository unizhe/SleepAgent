#!/usr/bin/env bash
set -euo pipefail

wake_date=${1:-2026-09-07}
runtime_root=${SLEEPAGENT_RUNTIME_ROOT:-/var/lib/sleepagent}
repo=${SLEEPAGENT_REPOSITORY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
output=${2:-${runtime_root}/logs/b-stabilization/morning-${wake_date}.txt}
report_env=${SLEEPAGENT_REPORT_ENV_FILE:-${runtime_root}/one-night-real-report.env}
psql_bin=${SLEEPAGENT_PSQL_BIN:-${runtime_root}/pg16/bin/psql}
python_bin=${SLEEPAGENT_PYTHON_BIN:-${runtime_root}/py311/bin/python}

umask 077
touch "${output}"
chmod 600 "${output}"
set -a
source "${report_env}"
set +a

{
  echo "readonly_acceptance_start|$(date -Ins)|wake_date=${wake_date}|timezone=Asia/Shanghai"
  "${psql_bin}" -X \
    "${SLEEPAGENT_ONE_NIGHT_READ_DSN}" -v wake_date="${wake_date}" \
    -f "${repo}/scripts/b_stabilization_readonly_acceptance.sql"
  "${python_bin}" \
    "${repo}/scripts/b_stabilization_log_summary.py" \
    --wake-date "${wake_date}" --log-root "${runtime_root}/logs/b-stabilization"
  for log in api cpolar ingestion-realtime ingestion-repair acquisition-worker scheduler; do
    path="${runtime_root}/logs/b-stabilization/${log}.log"
    if [[ -f "${path}" ]]; then
      echo "persistent_log|${log}|bytes=$(stat -c %s "${path}")|modified=$(stat -c %y "${path}")"
    else
      echo "persistent_log|${log}|NOT_OBSERVED"
    fi
  done
  echo "readonly_acceptance_end|$(date -Ins)"
} 2>&1 | tee "${output}"
