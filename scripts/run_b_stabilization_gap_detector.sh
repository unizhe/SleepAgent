#!/usr/bin/env bash
set -euo pipefail
repo=${SLEEPAGENT_REPOSITORY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
runtime_root=${SLEEPAGENT_RUNTIME_ROOT:-/var/lib/sleepagent}
log_root=${runtime_root}/logs/b-stabilization
set -a
source "${runtime_root}/one-night-real-report.env"
set +a
umask 077
exec "${runtime_root}/py311/bin/python" \
  "${repo}/scripts/b_stabilization_gap_detector.py" \
  --state "${log_root}/push-gap-state.json" \
  --evidence "${log_root}/push-gap-evidence.jsonl" \
  --interval-seconds 60 --warning-seconds 180 --repair-seconds 300
