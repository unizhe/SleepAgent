#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SERVICE_CREDENTIAL="${SLEEPAGENT_E2E_SERVICE_CREDENTIAL:-dI7xb-fTREaiNl53eu-iQa64zLVg-RAPKYusbzYnVzQ=}"
DEMO_TOKEN="${SLEEPAGENT_E2E_DEMO_TOKEN:-test-only-demo-controller-token-do-not-use}"

command -v docker >/dev/null
command -v openssl >/dev/null
"${PYTHON_BIN}" -c "import cryptography, psycopg" >/dev/null

E2E_TMP="$(mktemp -d /tmp/sleepagent-backend-stage4.XXXXXX)"
PROJECT_NAME="sleepagent-stage4-$$"
PRIVATE_KEY="${E2E_TMP}/actor-private.pem"
PUBLIC_KEY="${E2E_TMP}/actor-public.pem"
ENV_FILE="${E2E_TMP}/compose.env"
COMPOSE_ARGS=(--project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}")
LOCK_HOLDER_PID=""
VERIFIER_PID=""

cleanup() {
  if [[ -n "${VERIFIER_PID}" ]] && kill -0 "${VERIFIER_PID}" 2>/dev/null; then
    kill "${VERIFIER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${LOCK_HOLDER_PID}" ]] && kill -0 "${LOCK_HOLDER_PID}" 2>/dev/null; then
    kill "${LOCK_HOLDER_PID}" 2>/dev/null || true
  fi
  docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
  case "${E2E_TMP}" in
    /tmp/sleepagent-backend-stage4.*) rm -rf -- "${E2E_TMP}" ;;
  esac
}
trap cleanup EXIT INT TERM

cd "${REPOSITORY_ROOT}"
chmod 700 "${E2E_TMP}"
umask 077
openssl genpkey -algorithm Ed25519 -out "${PRIVATE_KEY}"
openssl pkey -in "${PRIVATE_KEY}" -pubout -out "${PUBLIC_KEY}"
chmod 600 "${PRIVATE_KEY}"
chmod 644 "${PUBLIC_KEY}"
PUBLIC_KEY_SHA256="$(sha256sum "${PUBLIC_KEY}" | awk '{print $1}')"

cp .env.test.example "${ENV_FILE}"
{
  printf '\nSLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_PATH=%s\n' "${PUBLIC_KEY}"
  printf 'SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256=%s\n' "${PUBLIC_KEY_SHA256}"
  # Start without delivery so the verifier can create an intent before the
  # fault supervisor locks its deterministic external-effect boundary.
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_QUEUES=ingestion,fast_path,product_agent,sleep_command,product_interaction,demo_advance,replay_journey,induction,reconciliation'
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_LEASE_SECONDS=6'
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_HEARTBEAT_SECONDS=2'
} >>"${ENV_FILE}"

if docker compose "${COMPOSE_ARGS[@]}" config | grep -F "${PRIVATE_KEY}" \
    >/dev/null; then
  echo "private verifier key leaked into server compose configuration" >&2
  exit 1
fi

docker compose "${COMPOSE_ARGS[@]}" build
docker compose "${COMPOSE_ARGS[@]}" up -d --wait postgres
docker compose "${COMPOSE_ARGS[@]}" run --rm migrate
docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api worker

FAULT_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test"
LOCK_STATE="${E2E_TMP}/delivery-lock.json"
LOCK_RELEASE="${E2E_TMP}/delivery-lock.release"
VERIFICATION_FILE="${E2E_TMP}/verification.json"
export SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN="${FAULT_DSN}"

"${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
  hold-delivery-effect-lock \
  --ready-file "${LOCK_STATE}" \
  --release-file "${LOCK_RELEASE}" \
  --timeout-seconds 240 &
LOCK_HOLDER_PID=$!

(
  SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
  SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL="${SERVICE_CREDENTIAL}" \
  SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY="${PRIVATE_KEY}" \
  "${PYTHON_BIN}" -m sleepagent.demo_cli \
  --base-url http://127.0.0.1:18001 \
  verify backend \
  --suite effects-reconciliation \
  --model deterministic \
  --mode clean \
  --wait-seconds 240 \
  --product-base-url http://127.0.0.1:18000 \
  >"${VERIFICATION_FILE}"
) &
VERIFIER_PID=$!

FAULT_WAIT_START=${SECONDS}
while [[ ! -s "${LOCK_STATE}" ]]; do
  if ! kill -0 "${LOCK_HOLDER_PID}" 2>/dev/null; then
    wait "${LOCK_HOLDER_PID}"
    echo "delivery fault lock holder exited before acquiring the lock" >&2
    exit 1
  fi
  if ! kill -0 "${VERIFIER_PID}" 2>/dev/null; then
    wait "${VERIFIER_PID}"
    echo "Stage-4 verifier exited before creating a delivery intent" >&2
    exit 1
  fi
  if ((SECONDS - FAULT_WAIT_START > 240)); then
    echo "timed out waiting for the deterministic delivery fault lock" >&2
    exit 1
  fi
  sleep 0.1
done

printf '%s\n' 'SLEEPAGENT_TEST_WORKER_QUEUES=ingestion,fast_path,product_agent,sleep_command,product_interaction,demo_advance,replay_journey,induction,delivery:replay_care_notification,reconciliation' \
  >>"${ENV_FILE}"
docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate --wait worker

"${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
  wait-delivery-send-started \
  --state-file "${LOCK_STATE}" \
  --timeout-seconds 60
docker compose "${COMPOSE_ARGS[@]}" kill -s SIGKILL worker
: >"${LOCK_RELEASE}"
wait "${LOCK_HOLDER_PID}"
LOCK_HOLDER_PID=""
docker compose "${COMPOSE_ARGS[@]}" up -d --wait worker

wait "${VERIFIER_PID}"
VERIFIER_PID=""
VERIFICATION_OUTPUT="$(<"${VERIFICATION_FILE}")"
printf '%s\n' "${VERIFICATION_OUTPUT}"

ROOT_OPERATION_ID="$(printf '%s' "${VERIFICATION_OUTPUT}" | \
  "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["operation_id"])')"
STAGE4_OPERATION_IDS="$(printf '%s' "${VERIFICATION_OUTPUT}" | \
  "${PYTHON_BIN}" -c \
    'import json,sys; print(json.dumps(json.load(sys.stdin)["stage2"]["operation_ids"],separators=(",",":")))')"
FAULT_DELIVERY_INTENT_ID="$( \
  "${PYTHON_BIN}" -c \
    'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["delivery_intent_id"])' \
    "${LOCK_STATE}")"

SLEEPAGENT_STAGE4_ROOT_OPERATION_ID="${ROOT_OPERATION_ID}" \
SLEEPAGENT_STAGE4_OPERATION_IDS="${STAGE4_OPERATION_IDS}" \
SLEEPAGENT_STAGE4_FAULT_DELIVERY_INTENT_ID="${FAULT_DELIVERY_INTENT_ID}" \
SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="${FAULT_DSN}" \
  "${PYTHON_BIN}" -m pytest \
    tests/integration/test_backend_stage4_postgres.py -m postgres -q
