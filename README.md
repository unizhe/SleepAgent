# SleepAgent

SleepAgent is a sleep-observation and care-coordination product for older adults, families, and clinicians. It explains overnight evidence, supports bounded conversation, proposes low-risk care steps, and follows outcomes. It does not diagnose, prescribe, or replace emergency care.

## Architecture

The production agent roster is closed: `SleepCareAgent`, `EvidenceReasoningAgent`, `CareStrategyAgent`, and conditionally invoked `SafetyReviewAgent`. `ProductEpisodeRunner` is the facade over one `ProductEpisodeRuntime` lifecycle. Agents own decisions; tools and services own computation, retrieval, persistence, rendering, authorization, and external effects.

The main flow is:

`product_device` / `integrations.perceptor` → `sleep_domain` → `product_runtime` → `sleep_api` / `product_api`

See the [documentation index](docs/README.md) and [architecture overview](docs/architecture/overview.md).

## Repository layout

- `backend/` — ASGI composition entrypoint
- `docker/` — container support
- `docs/` — current architecture, product, development, references, and audits
- `frontend/` — Next.js user interface
- `reference_client/` — external Sleep API reference client
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

Copy `.env.example` to `.env`. Development diagnostics require explicit development mode and credentials; production requires PostgreSQL and never falls back to `/tmp` SQLite.

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 18000
cd frontend && npm run dev
```

Supported public sleep-domain endpoints are under `/api/v1/*`. `/product/radar/*` and `/radar-agent/*` are bounded development diagnostics backed by the same canonical product episode runtime. Perceptor ingestion uses `/integrations/perceptor/webhook`.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
PYTHONDONTWRITEBYTECODE=1 python -m mypy sleepagent backend reference_client
python -m compileall -q sleepagent backend reference_client
cd frontend && npm run typecheck && npm run build
```

Urgent symptoms such as chest pain, severe breathing difficulty, altered consciousness, or falls must be escalated to appropriate in-person or emergency care.
