FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SLEEPAGENT_DATA_STORE_DIR=/tmp/sleepagent_stage9_api

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY scripts ./scripts
COPY sleepagent ./sleepagent

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[postgres]"

RUN adduser --disabled-password --gecos "" sleepagent \
    && chown -R sleepagent:sleepagent /app /tmp

USER sleepagent

EXPOSE 18000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "18000"]
