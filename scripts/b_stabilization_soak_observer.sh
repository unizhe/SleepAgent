#!/usr/bin/env bash
set -euo pipefail

duration_seconds=${1:-1500}
interval_seconds=${2:-60}
repo=${SLEEPAGENT_REPOSITORY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
runtime_root=${SLEEPAGENT_RUNTIME_ROOT:-/var/lib/sleepagent}
output=${3:-${runtime_root}/logs/b-stabilization/soak-current.log}
log_root=${runtime_root}/logs/b-stabilization
psql_bin=${runtime_root}/pg16/bin/psql
b0_count_sql=${SLEEPAGENT_B0_COUNT_SQL:?SLEEPAGENT_B0_COUNT_SQL is required}

umask 077
touch "${output}"
chmod 600 "${output}"
exec >>"${output}" 2>&1

set -a
source "${runtime_root}/one-night-real-report.env"
set +a

started_epoch=$(date +%s)
deadline_epoch=$((started_epoch + duration_seconds))
echo "soak_start|$(date -Ins)|duration_seconds=${duration_seconds}|interval_seconds=${interval_seconds}"

while (( $(date +%s) <= deadline_epoch )); do
  echo "sample_start|$(date -Ins)"
  "${psql_bin}" -X "${SLEEPAGENT_ONE_NIGHT_READ_DSN}" \
    -f "${b0_count_sql}"
  tmux list-windows -t sleepagent-night \
    -F 'component|#{window_name}|pid=#{pane_pid}|command=#{pane_current_command}|dead=#{pane_dead}|pipe=#{pane_pipe}'
  curl -sS -o /dev/null --max-time 5 \
    -w 'health_local|status=%{http_code}|elapsed=%{time_total}\n' \
    http://127.0.0.1:18184/livez || echo 'health_local|failed'
  curl -sS -o /dev/null --max-time 8 \
    -w 'health_public|status=%{http_code}|elapsed=%{time_total}\n' \
    https://sjtu-hradar.cpolar.io/livez || echo 'health_public|failed'
  /usr/sbin/logrotate -s "${log_root}/logrotate.state" \
    "${repo}/scripts/b_stabilization_logrotate.conf" || echo 'logrotate|failed'
  echo "sample_end|$(date -Ins)"
  remaining=$((deadline_epoch - $(date +%s)))
  (( remaining <= 0 )) && break
  sleep_for=${interval_seconds}
  (( sleep_for > remaining )) && sleep_for=${remaining}
  sleep "${sleep_for}"
done

echo "soak_end|$(date -Ins)"
