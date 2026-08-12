FROM python:3.11-slim AS production-dependencies

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements/production.lock /tmp/requirements/production.lock
RUN python -m pip install \
    --require-hashes \
    --only-binary=:all: \
    --requirement /tmp/requirements/production.lock


FROM python:3.11-slim AS wheel-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src

COPY requirements/dev.lock /tmp/requirements/dev.lock
RUN python -m pip install \
    --require-hashes \
    --only-binary=:all: \
    --requirement /tmp/requirements/dev.lock

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY reference_client ./reference_client
COPY sleepagent ./sleepagent

RUN python -m build --wheel --no-isolation --outdir /tmp/wheels


FROM production-dependencies AS runtime-base

COPY --from=wheel-builder /tmp/wheels /tmp/wheels
RUN python -m pip install --no-deps /tmp/wheels/sleepagent-*.whl \
    && rm -rf /tmp/wheels /tmp/requirements

RUN adduser --disabled-password --gecos "" sleepagent \
    && chown -R sleepagent:sleepagent /app /tmp

EXPOSE 18000


# Replay fixtures are retained only in the explicit development/test target.
FROM runtime-base AS development

USER sleepagent

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "18000"]


# The production artifact contains neither replay scenario payloads nor replay
# goldens. Demo surfaces must additionally fail closed in runtime settings.
FROM runtime-base AS production

RUN rm -rf -- \
      /usr/local/lib/python3.11/site-packages/sleepagent/simulation/fixtures \
      /usr/local/lib/python3.11/site-packages/sleepagent/simulation/replay \
      /usr/local/lib/python3.11/site-packages/sleepagent/simulation/replay_seed_registry.json

USER sleepagent

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "18000"]
