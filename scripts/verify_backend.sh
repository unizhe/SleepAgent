#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SUITE="${1:-all}"
SERVICE_CREDENTIAL="${SLEEPAGENT_E2E_SERVICE_CREDENTIAL:-dI7xb-fTREaiNl53eu-iQa64zLVg-RAPKYusbzYnVzQ=}"
DEMO_TOKEN="${SLEEPAGENT_E2E_DEMO_TOKEN:-test-only-demo-controller-token-do-not-use}"
FAULT_ADMIN_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test"
FAULT_WORKER_DSN="postgresql://sleepagent_test_worker:test-only-worker-password@127.0.0.1:15432/sleepagent_replay_test"

cd "${REPOSITORY_ROOT}"
if [[ "${SUITE}" != "fault-static" && "${SUITE}" != "fault-process" ]]; then
  "${PYTHON_BIN}" -m sleepagent.persistence.migrate check
fi

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
    fault-static)
      "${PYTHON_BIN}" -m pytest -q \
        tests/unit/test_backend_process_fault_probe.py \
        tests/integration/test_backend_postgres_foundation.py \
        -m "not postgres"
      "${PYTHON_BIN}" -m py_compile scripts/backend_process_fault_probe.py
      ;;
    *)
      echo "usage: $0 [core|read-models|delivery-recovery|retention|fault-static|fault-process|all]" >&2
      return 2
      ;;
  esac
}

if [[ "${SUITE}" == "all" ]]; then
  for item in core read-models delivery-recovery retention fault-static; do
    run_suite "${item}"
  done
elif [[ "${SUITE}" != "fault-process" ]]; then
  run_suite "${SUITE}"
  exit
fi

if ! command -v docker >/dev/null || ! command -v openssl >/dev/null \
  || ! "${PYTHON_BIN}" -c "import cryptography, psycopg" >/dev/null 2>&1 \
  || ! docker info >/dev/null 2>&1; then
  printf '%s\n' '{"proof":"backend_process_faults","status":"ENV_BLOCKED","reason":"docker daemon or required Python process-proof dependencies unavailable"}' >&2
  exit 77
fi

E2E_TMP=""
COMPOSE_ARGS=()
HELPER_PID=""
VERIFIER_PID=""

cleanup_process_case() {
  if [[ -n "${VERIFIER_PID}" ]] && kill -0 "${VERIFIER_PID}" 2>/dev/null; then
    kill "${VERIFIER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${HELPER_PID}" ]] && kill -0 "${HELPER_PID}" 2>/dev/null; then
    kill "${HELPER_PID}" 2>/dev/null || true
  fi
  if ((${#COMPOSE_ARGS[@]})); then
    docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans \
      >/dev/null 2>&1 || true
  fi
  case "${E2E_TMP}" in
    /tmp/sleepagent-backend-fault.*) rm -rf -- "${E2E_TMP}" ;;
  esac
  E2E_TMP=""
  COMPOSE_ARGS=()
  HELPER_PID=""
  VERIFIER_PID=""
}
trap cleanup_process_case EXIT INT TERM

append_worker_queues() {
  printf 'SLEEPAGENT_TEST_WORKER_QUEUES=%s\n' "$1" >>"${ENV_FILE}"
}

start_case() {
  local proof_case="$1" queues="$2"
  E2E_TMP="$(mktemp -d /tmp/sleepagent-backend-fault.XXXXXX)"
  chmod 700 "${E2E_TMP}"
  PRIVATE_KEY="${E2E_TMP}/actor-private.pem"
  PUBLIC_KEY="${E2E_TMP}/actor-public.pem"
  ENV_FILE="${E2E_TMP}/compose.env"
  PROJECT_NAME="sleepagent-fault-${proof_case//[^a-z0-9]/-}-$$"
  umask 077
  openssl genpkey -algorithm Ed25519 -out "${PRIVATE_KEY}" >/dev/null 2>&1
  openssl pkey -in "${PRIVATE_KEY}" -pubout -out "${PUBLIC_KEY}" \
    >/dev/null 2>&1
  chmod 600 "${PRIVATE_KEY}"
  chmod 644 "${PUBLIC_KEY}"
  cp .env.test.example "${ENV_FILE}"
  {
    printf '\nSLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_PATH=%s\n' "${PUBLIC_KEY}"
    printf 'SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256=%s\n' \
      "$(sha256sum "${PUBLIC_KEY}" | awk '{print $1}')"
    printf 'SLEEPAGENT_TEST_WORKER_LEASE_SECONDS=10\n'
    printf 'SLEEPAGENT_TEST_WORKER_HEARTBEAT_SECONDS=2\n'
  } >>"${ENV_FILE}"
  append_worker_queues "${queues}"
  COMPOSE_ARGS=(--project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}")
  if docker compose "${COMPOSE_ARGS[@]}" config | grep -F "${PRIVATE_KEY}" \
      >/dev/null; then
    echo "private verifier key leaked into compose configuration" >&2
    exit 1
  fi
  docker compose "${COMPOSE_ARGS[@]}" build
  docker compose "${COMPOSE_ARGS[@]}" up -d --wait postgres
  docker compose "${COMPOSE_ARGS[@]}" run --rm migrate
  docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api worker
  export SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN="${FAULT_ADMIN_DSN}"
  export SLEEPAGENT_FAULT_PROBE_WORKER_DSN="${FAULT_WORKER_DSN}"
}

wait_for_file() {
  local path="$1" pid="$2" label="$3" started=${SECONDS}
  while [[ ! -s "${path}" ]]; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}"
      echo "${label} exited before publishing state" >&2
      exit 1
    fi
    if ((SECONDS - started > 300)); then
      echo "timed out waiting for ${label}" >&2
      exit 1
    fi
    sleep 0.1
  done
}

run_cli_verifier() {
  local output_file="$1"
  shift
  SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
  SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL="${SERVICE_CREDENTIAL}" \
  SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY="${PRIVATE_KEY}" \
    "${PYTHON_BIN}" -m sleepagent.simulation.cli \
      --base-url http://127.0.0.1:18001 verify backend \
      --model deterministic --mode clean --wait-seconds 300 \
      --product-base-url http://127.0.0.1:18000 "$@" >"${output_file}"
}

root_from_output() {
  "${PYTHON_BIN}" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["operation_id"])' "$1"
}

assert_first_slice() {
  SLEEPAGENT_FIRST_SLICE_ROOT_OPERATION_ID="$1" \
  SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="${FAULT_ADMIN_DSN}" \
    "${PYTHON_BIN}" -m pytest -q \
      tests/integration/test_backend_first_slice_postgres.py -m postgres
}

FULL_QUEUES="ingestion,fast_path,product_agent,sleep_command,product_interaction,demo_advance,replay_journey,reconciliation"

start_case clean "${FULL_QUEUES}"
CLEAN_OUTPUT="${E2E_TMP}/clean.json"
run_cli_verifier "${CLEAN_OUTPUT}"
cat "${CLEAN_OUTPUT}"
assert_first_slice "$(root_from_output "${CLEAN_OUTPUT}")"
cleanup_process_case

start_case product-reclaim "${FULL_QUEUES}"
PRODUCT_READY="${E2E_TMP}/product-ready.json"
PRODUCT_PREPARED="${E2E_TMP}/product-prepared.json"
PRODUCT_TAKEOVER="${E2E_TMP}/product-takeover.json"
PRODUCT_RELEASE="${E2E_TMP}/product-release"
PRODUCT_OUTPUT="${E2E_TMP}/product.json"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py hold-product-commit \
  --ready-file "${PRODUCT_READY}" --prepared-file "${PRODUCT_PREPARED}" \
  --takeover-file "${PRODUCT_TAKEOVER}" --release-file "${PRODUCT_RELEASE}" &
HELPER_PID=$!
run_cli_verifier "${PRODUCT_OUTPUT}" &
VERIFIER_PID=$!
wait_for_file "${PRODUCT_READY}" "${HELPER_PID}" "Product commit lock"
wait_for_file "${PRODUCT_PREPARED}" "${HELPER_PID}" "prepared Product artifact"
docker compose "${COMPOSE_ARGS[@]}" kill -s SIGKILL worker
docker compose "${COMPOSE_ARGS[@]}" up -d --wait worker
wait_for_file "${PRODUCT_TAKEOVER}" "${HELPER_PID}" "Product ownership takeover"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
  assert-stale-product-fence --state-file "${PRODUCT_TAKEOVER}"
: >"${PRODUCT_RELEASE}"
wait "${HELPER_PID}"
HELPER_PID=""
wait "${VERIFIER_PID}"
VERIFIER_PID=""
cat "${PRODUCT_OUTPUT}"
PRODUCT_ROOT="$(root_from_output "${PRODUCT_OUTPUT}")"
printf '%s\n' "${PRODUCT_ROOT}" >"${E2E_TMP}/product-root"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py assert-product-recovery \
  --state-file "${PRODUCT_TAKEOVER}" --root-file "${E2E_TMP}/product-root"
assert_first_slice "${PRODUCT_ROOT}"
cleanup_process_case

start_case postgres-restart "replay_journey"
SEED_OUTPUT="$(SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
  "${PYTHON_BIN}" -m sleepagent.simulation.cli --base-url http://127.0.0.1:18001 \
  seed normal-one-night --artifact-family canonical-replay-fixtures --batch-size 100)"
printf '%s\n' "${SEED_OUTPUT}" | "${PYTHON_BIN}" -c \
  'import json,sys; print(json.load(sys.stdin)["operation_id"])' \
  >"${E2E_TMP}/restart-root"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py wait-root-active \
  --root-file "${E2E_TMP}/restart-root" --state-file "${E2E_TMP}/root-before.json"
docker compose "${COMPOSE_ARGS[@]}" restart postgres
docker compose "${COMPOSE_ARGS[@]}" up -d --wait postgres api demo-api worker
append_worker_queues "${FULL_QUEUES}"
docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate --wait worker
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py assert-root-recovery \
  --root-file "${E2E_TMP}/restart-root" --state-file "${E2E_TMP}/root-before.json"
assert_first_slice "$(<"${E2E_TMP}/restart-root")"
cleanup_process_case

DELIVERY_BASE="${FULL_QUEUES},induction"
start_case delivery-ambiguous "${DELIVERY_BASE}"
DELIVERY_STATE="${E2E_TMP}/delivery.json"
DELIVERY_RELEASE="${E2E_TMP}/delivery-release"
DELIVERY_OUTPUT="${E2E_TMP}/delivery-verification.json"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py hold-delivery-effect-lock \
  --ready-file "${DELIVERY_STATE}" --release-file "${DELIVERY_RELEASE}" &
HELPER_PID=$!
run_cli_verifier "${DELIVERY_OUTPUT}" --suite effects-reconciliation &
VERIFIER_PID=$!
wait_for_file "${DELIVERY_STATE}" "${HELPER_PID}" "delivery effect lock"
append_worker_queues "${DELIVERY_BASE},delivery:replay_care_notification"
docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate --wait worker
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
  wait-delivery-send-started --state-file "${DELIVERY_STATE}"
docker compose "${COMPOSE_ARGS[@]}" kill -s SIGKILL worker
: >"${DELIVERY_RELEASE}"
wait "${HELPER_PID}"
HELPER_PID=""
docker compose "${COMPOSE_ARGS[@]}" up -d --wait worker
wait "${VERIFIER_PID}"
VERIFIER_PID=""
cat "${DELIVERY_OUTPUT}"
"${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
  assert-delivery-recovery --state-file "${DELIVERY_STATE}"
assert_first_slice "$(root_from_output "${DELIVERY_OUTPUT}")"
cleanup_process_case
trap - EXIT INT TERM
