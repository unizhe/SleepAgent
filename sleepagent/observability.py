from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter, deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


LOG_LEVEL_ENV = "SLEEPAGENT_LOG_LEVEL"
MAX_RECENT_ERRORS = 8
MAX_RECENT_EVENTS = 20

SENSITIVE_KEY_PATTERN = re.compile(
    r"(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|secret|password|signature|sign|cookie)",
    re.IGNORECASE,
)
RAW_PAYLOAD_KEYS = {
    "payload",
    "raw_payload",
    "raw_payload_json",
    "raw_data",
    "data_payload",
    "data_payload_json",
    "request_body",
    "response_body",
    "messages",
    "content",
    "user_message",
}
IDENTIFIER_KEY_PATTERN = re.compile(
    r"(device[-_]?id|device[-_]?name|home[-_]?id|message[-_]?id|"
    r"product[-_]?id|raw[-_]?event[-_]?id|imei)",
    re.IGNORECASE,
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"(?i)(api[-_]?key|access[-_]?token|refresh[-_]?token|"
        r"client[-_]?secret|secret|password|signature|sign)"
        r"\s*[:=]\s*[^,\s]+"
    ),
)


LOGGER = logging.getLogger("sleepagent.observability")
LOGGER.setLevel(os.getenv(LOG_LEVEL_ENV, "INFO").upper())
if not LOGGER.handlers and not logging.getLogger().handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)


class ObservabilityState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent_pull_at: str | None = None
        self._recent_push_at: str | None = None
        self._data_freshness: dict[str, Any] = {}
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_ERRORS)
        self._event_counters: Counter[str] = Counter()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT_EVENTS)

    def record_pull(self, *, source: str, endpoint: str | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            self._recent_pull_at = now
            self._event_counters["pull"] += 1
            self._recent_events.append(
                redact(
                    {
                        "timestamp": now,
                        "event": "pull",
                        "source": source,
                        "endpoint": endpoint,
                    }
                )
            )

    def record_push(self, *, source: str, event_type: str | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            self._recent_push_at = now
            self._event_counters["push"] += 1
            self._recent_events.append(
                redact(
                    {
                        "timestamp": now,
                        "event": "push",
                        "source": source,
                        "event_type": event_type,
                    }
                )
            )

    def record_data_freshness(self, freshness: Mapping[str, Any]) -> None:
        with self._lock:
            self._data_freshness = redact(dict(freshness))
            self._event_counters["data_quality"] += 1

    def record_error(
        self,
        *,
        event: str,
        error: BaseException | str,
        source: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        error_type = error.__class__.__name__ if isinstance(error, BaseException) else "Error"
        message = str(error)
        entry = redact(
            {
                "timestamp": utc_now_iso(),
                "event": event,
                "source": source,
                "error_type": error_type,
                "message": redact_text(message),
                "context": dict(context or {}),
            }
        )
        with self._lock:
            self._event_counters["error"] += 1
            self._recent_errors.append(entry)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "recent_pull_at": self._recent_pull_at,
                "recent_push_at": self._recent_push_at,
                "data_freshness": dict(self._data_freshness),
                "errors": {
                    "count": self._event_counters["error"],
                    "recent": list(self._recent_errors),
                },
                "event_counters": dict(self._event_counters),
                "recent_events": list(self._recent_events),
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._recent_pull_at = None
            self._recent_push_at = None
            self._data_freshness = {}
            self._recent_errors.clear()
            self._event_counters.clear()
            self._recent_events.clear()


STATE = ObservabilityState()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    record = redact(
        {
            "timestamp": utc_now_iso(),
            "event": event,
            **fields,
        }
    )
    LOGGER.log(
        level,
        json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
    )


def record_pull(*, source: str, endpoint: str | None = None) -> None:
    STATE.record_pull(source=source, endpoint=endpoint)


def record_push(*, source: str, event_type: str | None = None) -> None:
    STATE.record_push(source=source, event_type=event_type)


def record_data_freshness(freshness: Mapping[str, Any]) -> None:
    STATE.record_data_freshness(freshness)


def record_error(
    *,
    event: str,
    error: BaseException | str,
    source: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> None:
    STATE.record_error(event=event, error=error, source=source, context=context)


def build_status_snapshot(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = env or os.environ
    provider_mode = (
        values.get("PERCEPTOR_PROVIDER_MODE", "").strip().lower()
        or "unconfigured"
    )
    capability_status = {
        "perceptor_live_configured": _all_present(
            values,
            (
                "PERCEPTOR_BASE_URL",
                "PERCEPTOR_CLIENT_ID",
                "PERCEPTOR_CLIENT_SECRET",
            ),
        ),
        "perceptor_default_device_configured": _all_present(
            values,
            (
                "PERCEPTOR_DEFAULT_DEVICE_NAME",
                "PERCEPTOR_DEFAULT_HOME_ID",
            ),
        ),
        "perceptor_webhook_signature_configured": bool(
            _present(values, "SLEEPAGENT_PERCEPTOR_PUSH_SIGNING_SECRET")
        ),
        "perceptor_webhook_unsigned_dev_allowed": False,
        "perceptor_push_enabled": _boolish(
            values.get("SLEEPAGENT_PERCEPTOR_PUSH_ENABLED")
        ),
        "perceptor_push_worker_enabled": _boolish(
            values.get("SLEEPAGENT_PERCEPTOR_PUSH_WORKER_ENABLED")
        ),
        "perceptor_push_capability_status": "pending",
        "product_api_auth_configured": _present(
            values,
            "SLEEPAGENT_PRODUCT_RADAR_API_KEY",
        ),
        "product_llm_configured": _present(values, "DEEPSEEK_API_KEY"),
        "repository_backend_configured": _present(
            values,
            "SLEEPAGENT_REPOSITORY_BACKEND",
        ),
    }
    state = STATE.snapshot()
    return {
        "status": "ok",
        "project": "SleepAgent",
        "stage": "v1_observability",
        "provider_mode": provider_mode,
        "capabilities": capability_status,
        "recent_pull_at": state["recent_pull_at"],
        "recent_push_at": state["recent_push_at"],
        "data_freshness": state["data_freshness"],
        "errors": state["errors"],
        "event_counters": state["event_counters"],
    }


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[max-depth]"
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            normalized_key = key.lower()
            if SENSITIVE_KEY_PATTERN.search(normalized_key):
                redacted[key] = "[redacted]"
            elif normalized_key in RAW_PAYLOAD_KEYS:
                redacted[key] = _payload_summary(raw_item)
            elif IDENTIFIER_KEY_PATTERN.search(normalized_key):
                redacted[key] = _fingerprint_value(raw_item)
            else:
                redacted[key] = redact(raw_item, depth=depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        if len(value) > 20:
            return {
                "type": type(value).__name__,
                "length": len(value),
                "items": [redact(item, depth=depth + 1) for item in value[:20]],
                "truncated": True,
            }
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(_redact_secret_match, redacted)
    if len(redacted) > 500:
        return f"{redacted[:500]}...[truncated]"
    return redacted


def _redact_secret_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if text.lower().startswith("bearer "):
        return "Bearer [redacted]"
    key = re.split(r"\s*[:=]\s*", text, maxsplit=1)[0]
    return f"{key}=[redacted]"


def _payload_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "omitted": True,
            "type": "object",
            "keys": sorted(str(key) for key in value.keys())[:30],
            "key_count": len(value),
        }
    if isinstance(value, (list, tuple)):
        return {
            "omitted": True,
            "type": "array",
            "length": len(value),
        }
    if value is None:
        return {
            "omitted": True,
            "type": "null",
        }
    return {
        "omitted": True,
        "type": type(value).__name__,
    }


def _fingerprint_value(value: Any) -> str | None:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"[hash:{digest}]"


def _present(values: Mapping[str, str], key: str) -> bool:
    value = values.get(key)
    return value is not None and bool(value.strip())


def _all_present(values: Mapping[str, str], keys: tuple[str, ...]) -> bool:
    return all(_present(values, key) for key in keys)


def _boolish(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "y", "on"})
