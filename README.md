# SleepAgent

SleepAgent is a sleep-observation and care-coordination product for older adults, families, and clinicians. It explains overnight evidence, supports bounded conversation, proposes low-risk care steps, and follows outcomes. It does not diagnose, prescribe, or replace emergency care.

## Architecture

The production agent roster is closed: `SleepCareAgent`, `EvidenceReasoningAgent`, `CareStrategyAgent`, and conditionally invoked `SafetyReviewAgent`. `ProductEpisodeRunner` is the facade over one `ProductEpisodeRuntime` lifecycle. Agents own decisions; tools and services own computation, retrieval, persistence, rendering, authorization, and external effects.

The PostgreSQL-backed backend flow is:

`replay/live ingress → committed NightEpisode → product_runtime → role projection → sleep_api/product_api`

`backend.main:app` is the only deployed ASGI composition root. API, Demo,
Internal, Migration, and Worker processes use disjoint profiles and database
roles. API lifespans never run durable workers in-process.

See the [documentation index](docs/README.md) and [architecture overview](docs/architecture/overview.md).

## Repository layout

- `backend/` — ASGI composition entrypoint
- `docker/` — container support
- `docs/` — current architecture, product, development, references, and audits
- `frontend/` — Next.js user interface
- `reference_client/` — server-independent external Sleep API reference client
- `requirements/` — hash-locked environments
- `sleepagent/` — production Python packages
- `tests/` — unit, integration, architecture, end-to-end, fixture, benchmark, and support code

## Development

Requires CPython 3.11 and Node.js.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements/dev.lock
python -m pip install --no-deps -e .
cd frontend && npm ci && cd ..
```

Copy `.env.example` to `.env` for legacy diagnostics. The canonical replay
backend uses `.env.test.example` as a template and requires PostgreSQL; it never
falls back to SQLite or starts without an explicit profile.

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 18000
cd frontend && npm run dev
```

Supported public endpoints are under `/api/v1/*` and `/product/sleep/*`.
Only `/livez` is unauthenticated operationally; readiness, dependency status,
metrics, and reconciliation are exposed on the separately deployed,
credential-protected `/internal/*` surface. Demo routes are replay/test-only.

For a fresh replay backend:

```bash
cp .env.test.example .env.test
docker compose --env-file .env.test up -d --wait postgres
docker compose --env-file .env.test run --rm migrate apply
docker compose --env-file .env.test run --rm test-bootstrap
docker compose --env-file .env.test up -d --wait api demo-api worker
scripts/verify_backend_first_slice.sh
```

With the replay services running, the Phase-1 terminal Product demo can run
the three allowlisted canonical scenarios through the Demo and Product HTTP
APIs. Configure `SLEEPAGENT_DEMO_CONTROLLER_TOKEN`,
`SLEEPAGENT_DEMO_SERVICE_CREDENTIAL`, and an absolute 0600 Ed25519 private-key
path in `SLEEPAGENT_DEMO_ACTOR_PRIVATE_KEY`, then run:

```bash
sleepagent-demo show normal-one-night
sleepagent-demo show worsening-vital-trend --trace
sleepagent-demo show urgent-zero-model --trace
```

Run each scenario against a clean isolated replay deployment, matching the
existing backend proof scripts; ScenarioClock advancement deliberately fails
closed when more than one completed replay authority is visible.

The command presents only public committed responses. If a Safety, Evidence,
or Agent-internal detail is absent from the public contract, it reports that
limitation instead of reading verifier or database state.

See the [backend operations runbook](docs/runbooks/backend-operations.md) for
migrations, queue recovery, retention/reset, permissions, and troubleshooting.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
PYTHONDONTWRITEBYTECODE=1 python -m mypy sleepagent backend reference_client
python -m compileall -q sleepagent backend reference_client
python scripts/generate_openapi_snapshots.py --check
cd frontend && npm run typecheck && npm run build
```

Urgent symptoms such as chest pain, severe breathing difficulty, altered consciousness, or falls must be escalated to appropriate in-person or emergency care.
