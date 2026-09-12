#!/usr/bin/env bash
set -euo pipefail

component=${1:?component is required}
action=${2:-run}
runtime_root=${SLEEPAGENT_RUNTIME_ROOT:-/var/lib/sleepagent}
python_bin=${runtime_root}/py311/bin/python

case "${component}" in
  api)
    set -a
    source "${runtime_root}/one-night-real-api.env"
    set +a
    export SLEEPAGENT_BACKEND_PROFILE=one-night-real-api-b-stabilization
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MIN=31
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MAX=31
    export SLEEPAGENT_BACKEND_MODEL_MODE=disabled
    # The Product HTTP projection validates this request-schema selector even
    # though API roles cannot load a model. Backend MODEL_MODE remains disabled,
    # no Product worker queue exists, and the provider credential is removed.
    export SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE=live
    unset DEEPSEEK_API_KEY
    exec "${python_bin}" -m uvicorn sleepagent.app:app \
      --host 127.0.0.1 --port 18184
    ;;
  ingestion-realtime|ingestion-repair|acquisition-worker)
    set -a
    source "${runtime_root}/one-night-real-worker.env"
    set +a
    export SLEEPAGENT_BACKEND_PROFILE="one-night-real-${component}-b-stabilization"
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MIN=31
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MAX=31
    export SLEEPAGENT_BACKEND_MODEL_MODE=disabled
    export SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE=disabled
    export SLEEPAGENT_BACKEND_LIVE_DELIVERY_ENABLED=false
    export SLEEPAGENT_BACKEND_OUTCOME_EVALUATION_ENABLED=false
    unset DEEPSEEK_API_KEY
    case "${component}" in
      ingestion-realtime)
        export SLEEPAGENT_BACKEND_WORKER_QUEUES=ingestion_realtime
        export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=false
        worker_args=(--lease-seconds 60 --heartbeat-seconds 10)
        ;;
      ingestion-repair)
        export SLEEPAGENT_BACKEND_WORKER_QUEUES=ingestion_repair
        export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=false
        worker_args=(--lease-seconds 1800 --heartbeat-seconds 300)
        ;;
      acquisition-worker)
        export SLEEPAGENT_BACKEND_WORKER_QUEUES=perceptor.history_overlap_pull,perceptor.sleep_report_pull,night.finalization_scan
        export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=true
        worker_args=(--lease-seconds 120 --heartbeat-seconds 20)
        ;;
    esac
    if [[ "${action}" == healthcheck ]]; then
      exec "${python_bin}" -m sleepagent.bootstrap.worker healthcheck
    fi
    exec "${python_bin}" -m sleepagent.bootstrap.worker run "${worker_args[@]}"
    ;;
  scheduler)
    set -a
    source "${runtime_root}/one-night-real-worker.env"
    set +a
    export SLEEPAGENT_BACKEND_PROFILE=one-night-real-scheduler-b-stabilization
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MIN=31
    export SLEEPAGENT_BACKEND_SUPPORTED_SCHEMA_MAX=31
    export SLEEPAGENT_BACKEND_WORKER_QUEUES=perceptor.history_overlap_pull,perceptor.sleep_report_pull,night.finalization_scan
    export SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=true
    export SLEEPAGENT_BACKEND_MODEL_MODE=disabled
    export SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE=disabled
    export SLEEPAGENT_BACKEND_LIVE_DELIVERY_ENABLED=false
    export SLEEPAGENT_BACKEND_OUTCOME_EVALUATION_ENABLED=false
    unset DEEPSEEK_API_KEY
    exec "${python_bin}" -m sleepagent.bootstrap.scheduler "${action}" \
      --worker-instance acquisition-scheduler-b-stabilization \
      --limit 20 --poll-seconds 5
    ;;
  *)
    echo "unsupported component" >&2
    exit 2
    ;;
esac
