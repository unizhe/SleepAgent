#!/usr/bin/env bash
set -uo pipefail

timezone=Asia/Shanghai
repo=${SLEEPAGENT_REPOSITORY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
runtime_root=${SLEEPAGENT_RUNTIME_ROOT:-/var/lib/sleepagent}
archive_root=${SLEEPAGENT_NIGHTLY_ARCHIVE_ROOT:-${runtime_root}/nightly-runs}
report_env=${SLEEPAGENT_REPORT_ENV_FILE:-${runtime_root}/one-night-real-report.env}
psql_bin=${SLEEPAGENT_PSQL_BIN:-${runtime_root}/pg16/bin/psql}
python_bin=${SLEEPAGENT_PYTHON_BIN:-${runtime_root}/py311/bin/python}
acceptance=${SLEEPAGENT_NIGHTLY_ACCEPTANCE:-${repo}/scripts/b_stabilization_readonly_acceptance.sh}
nightly_sql=${SLEEPAGENT_NIGHTLY_SQL:-${repo}/scripts/nightly_readonly.sql}
renderer=${SLEEPAGENT_NIGHTLY_RENDERER:-${repo}/scripts/nightly_summary.py}

usage() {
  echo "Usage: ./scripts/nightly.sh [YYYY-MM-DD|list]" >&2
}

if [[ $# -gt 1 ]]; then
  usage
  exit 2
fi

if [[ ! -r "${report_env}" ]]; then
  echo "Nightly report environment is not readable: ${report_env}" >&2
  exit 2
fi

umask 077
set -a
# shellcheck disable=SC1090
source "${report_env}"
set +a

if [[ -z ${SLEEPAGENT_ONE_NIGHT_READ_DSN:-} ]]; then
  echo "SLEEPAGENT_ONE_NIGHT_READ_DSN is required" >&2
  exit 2
fi

mode=${1:-}
if [[ "${mode}" == list ]]; then
  list_json=$(mktemp "${TMPDIR:-/tmp}/sleepagent-nightly-list.XXXXXX") || exit 2
  trap 'rm -f "${list_json}"' EXIT
  if ! "${psql_bin}" -X -Atq "${SLEEPAGENT_ONE_NIGHT_READ_DSN}" \
      -v list_mode=1 -f "${nightly_sql}" >"${list_json}"; then
    echo "Night list read failed" >&2
    exit 1
  fi
  "${python_bin}" "${renderer}" list --input "${list_json}"
  exit $?
fi

if [[ -z "${mode}" ]]; then
  clock=${SLEEPAGENT_NIGHTLY_NOW:-now}
  wake_date=$(TZ="${timezone}" date --date="${clock} - 1 day" +%F 2>/dev/null) || {
    echo "Unable to resolve the previous date in ${timezone}" >&2
    exit 2
  }
else
  wake_date=${mode}
fi

if [[ ! "${wake_date}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
    || [[ $(TZ="${timezone}" date --date="${wake_date}" +%F 2>/dev/null) != "${wake_date}" ]]; then
  echo "Invalid wake date (expected YYYY-MM-DD): ${wake_date}" >&2
  exit 2
fi

night_dir=${archive_root}/${wake_date}
if ! mkdir -p "${night_dir}"; then
  echo "Unable to create nightly archive: ${night_dir}" >&2
  exit 2
fi
chmod 700 "${night_dir}"
acceptance_log=${night_dir}/acceptance.log
summary_json=${night_dir}/summary.json
summary_text=${night_dir}/summary.txt
query_json=$(mktemp "${night_dir}/.summary-query.XXXXXX") || exit 2
trap 'rm -f "${query_json}"' EXIT

echo "SleepAgent nightly check: wake_date=${wake_date} (${timezone})"
echo "Running read-only acceptance..."
"${acceptance}" "${wake_date}" "${acceptance_log}" >/dev/null
acceptance_status=$?

query_status=0
"${psql_bin}" -X -Atq "${SLEEPAGENT_ONE_NIGHT_READ_DSN}" \
  -v list_mode=0 -v wake_date="${wake_date}" \
  -f "${nightly_sql}" >"${query_json}" || query_status=$?

if [[ ${query_status} -eq 0 ]]; then
  "${python_bin}" "${renderer}" summary \
    --input "${query_json}" \
    --summary-json "${summary_json}" \
    --summary-text "${summary_text}" \
    --acceptance-exit-code "${acceptance_status}"
  render_status=$?
else
  render_status=2
  printf '%s\n' \
    "Night: ${wake_date}" \
    "Summary query: failed (exit ${query_status})" \
    "Acceptance: $([[ ${acceptance_status} -eq 0 ]] && echo passed || echo failed) (exit ${acceptance_status})" \
    >"${summary_text}"
  chmod 600 "${summary_text}"
  printf '{"schema_version":"sleepagent.nightly_summary.v1","wake_date":"%s","summary_query":{"status":"failed","exit_code":%d},"acceptance":{"status":"%s","exit_code":%d}}\n' \
    "${wake_date}" "${query_status}" \
    "$([[ ${acceptance_status} -eq 0 ]] && echo passed || echo failed)" \
    "${acceptance_status}" >"${summary_json}"
  chmod 600 "${summary_json}"
  cat "${summary_text}"
fi

if [[ ${acceptance_status} -ne 0 ]]; then
  echo "Acceptance: FAILED (exit ${acceptance_status})"
else
  echo "Acceptance: PASS"
fi
echo
echo "Full log:"
echo "${acceptance_log}"
echo
echo "Summary:"
echo "${summary_text}"

if [[ ${acceptance_status} -ne 0 ]]; then
  exit "${acceptance_status}"
fi
if [[ ${query_status} -ne 0 ]]; then
  exit "${query_status}"
fi
exit "${render_status}"
