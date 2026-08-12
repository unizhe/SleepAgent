#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIFY_MODE="${1:-all}"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SERVICE_CREDENTIAL="${SLEEPAGENT_E2E_SERVICE_CREDENTIAL:-dI7xb-fTREaiNl53eu-iQa64zLVg-RAPKYusbzYnVzQ=}"
DEMO_TOKEN="${SLEEPAGENT_E2E_DEMO_TOKEN:-test-only-demo-controller-token-do-not-use}"

case "${VERIFY_MODE}" in
  all)
    CASES=(multi-night quality urgent)
    ;;
  multi-night|quality|urgent)
    CASES=("${VERIFY_MODE}")
    ;;
  *)
    echo "usage: $0 [all|multi-night|quality|urgent]" >&2
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
    /tmp/sleepagent-backend-stage3.*)
      rm -rf -- "${E2E_TMP}"
      ;;
  esac
  E2E_TMP=""
  COMPOSE_ARGS=()
}

trap cleanup_current_run EXIT INT TERM

cd "${REPOSITORY_ROOT}"
for proof_case in "${CASES[@]}"; do
  case "${proof_case}" in
    multi-night)
      scenario="worsening-vital-trend"
      subject_id="synthetic-subject-lin-001"
      proof_kind="success"
      extra_args=(
        --expected-night-count 4
        --advance-seconds 604800
      )
      ;;
    quality)
      scenario="device-abnormal"
      subject_id="synthetic-subject-device-001"
      proof_kind="quality"
      extra_args=(--expected-error-code quality_insufficient)
      ;;
    urgent)
      scenario="urgent-zero-model"
      subject_id="synthetic-subject-urgent-001"
      proof_kind="urgent"
      extra_args=(--expected-error-code unexpected_urgent_route)
      ;;
  esac

  E2E_TMP="$(mktemp -d /tmp/sleepagent-backend-stage3.XXXXXX)"
  chmod 700 "${E2E_TMP}"
  PRIVATE_KEY="${E2E_TMP}/actor-private.pem"
  PUBLIC_KEY="${E2E_TMP}/actor-public.pem"
  ENV_FILE="${E2E_TMP}/compose.env"
  PROJECT_NAME="sleepagent-stage3-${proof_case//[^a-z0-9]/-}-$$"

  umask 077
  openssl genpkey -algorithm Ed25519 -out "${PRIVATE_KEY}"
  openssl pkey -in "${PRIVATE_KEY}" -pubout -out "${PUBLIC_KEY}"
  chmod 600 "${PRIVATE_KEY}"
  chmod 644 "${PUBLIC_KEY}"
  PUBLIC_KEY_SHA256="$(sha256sum "${PUBLIC_KEY}" | awk '{print $1}')"

  cp .env.test.example "${ENV_FILE}"
  {
    printf '\nSLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_PATH=%s\n' "${PUBLIC_KEY}"
    printf 'SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256=%s\n' \
      "${PUBLIC_KEY_SHA256}"
    printf 'SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES=replay:%s\n' "${scenario}"
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
  docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api worker

  VERIFICATION_OUTPUT="$( \
    SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
    SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL="${SERVICE_CREDENTIAL}" \
    SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY="${PRIVATE_KEY}" \
    "${PYTHON_BIN}" -m sleepagent.demo_cli \
    --base-url http://127.0.0.1:18001 \
    verify backend \
    --suite read-models-abnormal \
    --scenario "${scenario}" \
    --model deterministic \
    --mode clean \
    --wait-seconds 240 \
    --product-base-url http://127.0.0.1:18000 \
    --subject-id "${subject_id}" \
    "${extra_args[@]}")"
  printf '%s\n' "${VERIFICATION_OUTPUT}"

  PROOF_JSON="$(printf '%s' "${VERIFICATION_OUTPUT}" | \
    "${PYTHON_BIN}" -c \
      'import json,sys; p=json.load(sys.stdin); kind=sys.argv[1]; subject=sys.argv[2]; stage3=p.get("stage3") or {}; print(json.dumps({"proof_kind":kind,"operation_id":p["operation_id"],"subject_id":subject,"error_code":p.get("error_code"),"expected_night_count":stage3.get("expected_night_count",1),"advance_operation_id":stage3.get("advance_operation_id"),"stage3_schema_version":stage3.get("schema_version")},separators=(",",":")))' \
      "${proof_kind}" "${subject_id}")"
  SLEEPAGENT_STAGE3_PROOF_JSON="${PROOF_JSON}" \
  SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
    "${PYTHON_BIN}" -m pytest \
      tests/integration/test_backend_stage3_postgres.py -m postgres -q

  cleanup_current_run
  trap cleanup_current_run EXIT INT TERM
done

trap - EXIT INT TERM
