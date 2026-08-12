#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SLEEPAGENT_E2E_PYTHON:-python}"
SERVICE_CREDENTIAL="${SLEEPAGENT_E2E_SERVICE_CREDENTIAL:-dI7xb-fTREaiNl53eu-iQa64zLVg-RAPKYusbzYnVzQ=}"
DEMO_TOKEN="${SLEEPAGENT_E2E_DEMO_TOKEN:-test-only-demo-controller-token-do-not-use}"

command -v docker >/dev/null
command -v openssl >/dev/null
"${PYTHON_BIN}" -c "import cryptography, psycopg" >/dev/null

E2E_TMP="$(mktemp -d /tmp/sleepagent-backend-stage5.XXXXXX)"
PROJECT_NAME="sleepagent-stage5-$$"
PRIVATE_KEY="${E2E_TMP}/actor-private.pem"
PUBLIC_KEY="${E2E_TMP}/actor-public.pem"
ENV_FILE="${E2E_TMP}/compose.env"
COMPOSE_ARGS=(--project-name "${PROJECT_NAME}" --env-file "${ENV_FILE}")

cleanup() {
  docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans \
    >/dev/null 2>&1 || true
  case "${E2E_TMP}" in
    /tmp/sleepagent-backend-stage5.*) rm -rf -- "${E2E_TMP}" ;;
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
  printf '%s\n' 'SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES=replay:worsening-vital-trend'
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_QUEUES=ingestion,fast_path,product_agent,demo_advance,replay_journey,retention,demo_reset,reconciliation'
  # Releasing the full seven-day staged batch is one fenced transaction.  Keep
  # the lease comfortably above the transaction's slow-CI wall time; a short
  # lease would be rejected by the staged-fact trigger rather than partially
  # committing, but would make the proof needlessly retry-bound.
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_LEASE_SECONDS=120'
  printf '%s\n' 'SLEEPAGENT_TEST_WORKER_HEARTBEAT_SECONDS=30'
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

VERIFICATION_OUTPUT="$( \
  SLEEPAGENT_DEMO_CONTROLLER_TOKEN="${DEMO_TOKEN}" \
  SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL="${SERVICE_CREDENTIAL}" \
  SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY="${PRIVATE_KEY}" \
  "${PYTHON_BIN}" -m sleepagent.demo_cli \
  --base-url http://127.0.0.1:18001 \
  verify backend \
  --suite bounded-retention \
  --scenario worsening-vital-trend \
  --model deterministic \
  --mode clean \
  --wait-seconds 300 \
  --product-base-url http://127.0.0.1:18000 \
  --subject-id synthetic-subject-lin-001)"
printf '%s\n' "${VERIFICATION_OUTPUT}"

SLEEPAGENT_STAGE5_PROOF_JSON="${VERIFICATION_OUTPUT}" \
SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN="postgresql://sleepagent_test_migration:test-only-migration-password@127.0.0.1:15432/sleepagent_replay_test" \
  "${PYTHON_BIN}" -m pytest \
    tests/integration/test_backend_stage5_postgres.py -m postgres -q
