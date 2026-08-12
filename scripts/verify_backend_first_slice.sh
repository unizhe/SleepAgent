#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIFY_MODE="${1:-all}"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SERVICE_CREDENTIAL="${SLEEPAGENT_E2E_SERVICE_CREDENTIAL:-dI7xb-fTREaiNl53eu-iQa64zLVg-RAPKYusbzYnVzQ=}"
DEMO_TOKEN="${SLEEPAGENT_E2E_DEMO_TOKEN:-test-only-demo-controller-token-do-not-use}"

case "${VERIFY_MODE}" in
  all)
    MODES=(
      clean
      restart-worker-before-product-commit
      restart-postgres-during-root
    )
    ;;
  clean|restart-worker-before-product-commit|restart-postgres-during-root)
    MODES=("${VERIFY_MODE}")
    ;;
  *)
    echo "usage: $0 [all|clean|restart-worker-before-product-commit|restart-postgres-during-root]" >&2
    exit 2
    ;;
esac

command -v docker >/dev/null
command -v openssl >/dev/null
"${PYTHON_BIN}" -c "import cryptography, psycopg" >/dev/null

E2E_TMP=""
COMPOSE_ARGS=()

cleanup_current_run() {
  if ((${#COMPOSE_ARGS[@]})); then
    docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans \
      >/dev/null 2>&1 || true
  fi
  case "${E2E_TMP}" in
    /tmp/sleepagent-backend-e2e.*)
      rm -rf -- "${E2E_TMP}"
      ;;
  esac
  E2E_TMP=""
  COMPOSE_ARGS=()
}

trap cleanup_current_run EXIT INT TERM

cd "${REPOSITORY_ROOT}"
for mode in "${MODES[@]}"; do
  E2E_TMP="$(mktemp -d /tmp/sleepagent-backend-e2e.XXXXXX)"
  chmod 700 "${E2E_TMP}"
  PRIVATE_KEY="${E2E_TMP}/actor-private.pem"
  PUBLIC_KEY="${E2E_TMP}/actor-public.pem"
  ENV_FILE="${E2E_TMP}/compose.env"
  PROJECT_NAME="sleepagent-e2e-${mode//[^a-z0-9]/-}-$$"

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
  } >>"${ENV_FILE}"

  COMPOSE_ARGS=(
    --project-name "${PROJECT_NAME}"
    --env-file "${ENV_FILE}"
  )
  if docker compose "${COMPOSE_ARGS[@]}" config | grep -F "${PRIVATE_KEY}" \
      >/dev/null; then
    echo "private verifier key leaked into server compose configuration" >&2
    exit 1
  fi

  docker compose "${COMPOSE_ARGS[@]}" build
  docker compose "${COMPOSE_ARGS[@]}" up -d --wait postgres
  docker compose "${COMPOSE_ARGS[@]}" run --rm migrate
  if [[ "${mode}" == "restart-postgres-during-root" ]]; then
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api
  else
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api worker
  fi

  CLI_MODE="${mode}"
  SEEDED_ROOT_OPERATION_ID=""
  if [[ "${mode}" == "restart-postgres-during-root" ]]; then
    CLI_MODE="clean"
    SEED_OUTPUT="$( \
      SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
      "${PYTHON_BIN}" -m sleepagent.demo_cli \
      --base-url http://127.0.0.1:18001 \
      seed normal-one-night \
      --artifact-family canonical-replay-fixtures \
      --batch-size 100)"
    SEEDED_ROOT_OPERATION_ID="$(printf '%s' "${SEED_OUTPUT}" | \
      "${PYTHON_BIN}" -c \
        'import json,sys; print(json.load(sys.stdin)["operation_id"])')"
    ROOT_FILE="${E2E_TMP}/active-root-operation-id"
    printf '%s\n' "${SEEDED_ROOT_OPERATION_ID}" >"${ROOT_FILE}"
    # Keep the root at a deterministic, progressed WAIT boundary while the
    # database restarts. A full queue set could finish this tiny scenario
    # before the supervisor observes its fault window on a fast host.
    printf '%s\n' 'SLEEPAGENT_TEST_WORKER_QUEUES=replay_journey' \
      >>"${ENV_FILE}"
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait worker
    SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
      "${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
      wait-root-active \
      --root-file "${ROOT_FILE}" \
      --require-progress \
      --timeout-seconds 120
    docker compose "${COMPOSE_ARGS[@]}" restart postgres
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait \
      postgres api demo-api worker
    SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
      "${PYTHON_BIN}" scripts/backend_process_fault_probe.py \
      wait-root-active \
      --root-file "${ROOT_FILE}" \
      --timeout-seconds 120
    printf '%s\n' 'SLEEPAGENT_TEST_WORKER_QUEUES=ingestion,fast_path,product_agent,sleep_command,product_interaction,demo_advance,replay_journey,reconciliation' \
      >>"${ENV_FILE}"
    docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate --wait worker
  fi

  unset SLEEPAGENT_VERIFIER_RESTART_WORKER_COMMANDS || true
  if [[ "${mode}" == "restart-worker-before-product-commit" ]]; then
    SLEEPAGENT_VERIFIER_RESTART_WORKER_COMMANDS="$(printf \
      '[["docker","compose","--project-name","%s","--env-file","%s","kill","-s","SIGKILL","worker"],["docker","compose","--project-name","%s","--env-file","%s","up","-d","--wait","worker"]]' \
      "${PROJECT_NAME}" "${ENV_FILE}" "${PROJECT_NAME}" "${ENV_FILE}")"
    export SLEEPAGENT_VERIFIER_RESTART_WORKER_COMMANDS
  fi

  STAGE2_ARGS=(--stage2)
  if [[ "${mode}" == "restart-worker-before-product-commit" ]]; then
    STAGE2_ARGS+=(--restart-stage2-worker)
  fi

  VERIFICATION_OUTPUT="$( \
    SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
    SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL="${SERVICE_CREDENTIAL}" \
    SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY="${PRIVATE_KEY}" \
    "${PYTHON_BIN}" -m sleepagent.demo_cli \
    --base-url http://127.0.0.1:18001 \
    verify backend \
    --model deterministic \
    --mode "${CLI_MODE}" \
    --wait-seconds 180 \
    --product-base-url http://127.0.0.1:18000 \
    "${STAGE2_ARGS[@]}")"
  printf '%s\n' "${VERIFICATION_OUTPUT}"
  ROOT_OPERATION_ID="$(printf '%s' "${VERIFICATION_OUTPUT}" | \
    "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["operation_id"])')"
  if [[ -n "${SEEDED_ROOT_OPERATION_ID}" ]] && \
      [[ "${ROOT_OPERATION_ID}" != "${SEEDED_ROOT_OPERATION_ID}" ]]; then
    echo "PostgreSQL restart verification did not resume the seeded root" >&2
    exit 1
  fi
  STAGE2_OPERATION_IDS="$(printf '%s' "${VERIFICATION_OUTPUT}" | \
    "${PYTHON_BIN}" -c \
      'import json,sys; print(json.dumps(json.load(sys.stdin)["stage2"]["operation_ids"], separators=(",", ":")))')"

  SLEEPAGENT_FIRST_SLICE_ROOT_OPERATION_ID="${ROOT_OPERATION_ID}" \
  SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
    "${PYTHON_BIN}" -m pytest \
      tests/integration/test_backend_first_slice_postgres.py -m postgres -q
  SLEEPAGENT_STAGE2_OPERATION_IDS="${STAGE2_OPERATION_IDS}" \
  SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
    "${PYTHON_BIN}" -m pytest \
      tests/integration/test_backend_stage2_postgres.py -m postgres -q

  cleanup_current_run
  trap cleanup_current_run EXIT INT TERM
done

trap - EXIT INT TERM
