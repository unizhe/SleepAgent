from __future__ import annotations

import os
import time
import uuid
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from sleepagent.integrations.perceptor.signing import (
    DEFAULT_SIGNING_PATH,
    DEFAULT_SIGNATURE_METHOD,
    sign_parameters,
)
from sleepagent.observability import log_event, record_error, record_pull


PERCEPTOR_PROVIDER_MODE_ENV = "PERCEPTOR_PROVIDER_MODE"
PERCEPTOR_BASE_URL_ENV = "PERCEPTOR_BASE_URL"
PERCEPTOR_CLIENT_ID_ENV = "PERCEPTOR_CLIENT_ID"
PERCEPTOR_CLIENT_SECRET_ENV = "PERCEPTOR_CLIENT_SECRET"
PERCEPTOR_DEFAULT_DEVICE_NAME_ENV = "PERCEPTOR_DEFAULT_DEVICE_NAME"
PERCEPTOR_DEFAULT_HOME_ID_ENV = "PERCEPTOR_DEFAULT_HOME_ID"
PERCEPTOR_TIMEOUT_SECONDS_ENV = "PERCEPTOR_TIMEOUT_SECONDS"
PERCEPTOR_TOKEN_REFRESH_MARGIN_SECONDS_ENV = "PERCEPTOR_TOKEN_REFRESH_MARGIN_SECONDS"
PERCEPTOR_TOKEN_DEFAULT_TTL_SECONDS_ENV = "PERCEPTOR_TOKEN_DEFAULT_TTL_SECONDS"
PERCEPTOR_REALTIME_SESSION_TTL_SECONDS_ENV = "PERCEPTOR_REALTIME_SESSION_TTL_SECONDS"
PERCEPTOR_MAX_RETRIES_ENV = "PERCEPTOR_MAX_RETRIES"
PERCEPTOR_RETRY_BACKOFF_SECONDS_ENV = "PERCEPTOR_RETRY_BACKOFF_SECONDS"
PERCEPTOR_SIGNING_PATH_ENV = "PERCEPTOR_SIGNING_PATH"
PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND_ENV = "PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND"

PERCEPTOR_DATA_NAMESPACE_ENV = "SLEEPAGENT_PERCEPTOR_DATA_NAMESPACE"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_TOKEN_REFRESH_MARGIN_SECONDS = 60.0
DEFAULT_TOKEN_TTL_SECONDS = 3600.0
DEFAULT_REALTIME_SESSION_TTL_SECONDS = 300.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25


class PerceptorConfigurationError(ValueError):
    """Raised when live Perceptor configuration is incomplete."""


class PerceptorAPIError(RuntimeError):
    """Raised when the Perceptor API boundary returns an unusable response."""


class PerceptorRealtimeSessionError(PerceptorAPIError):
    """Raised when realtime polling is attempted without an active session."""


class PerceptorHTTPClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class PerceptorToken:
    access_token: str
    expires_at: float

    @property
    def expires_at_epoch_seconds(self) -> float:
        return self.expires_at

    def is_valid(
        self,
        *,
        now_epoch_seconds: float,
        refresh_margin_seconds: float,
    ) -> bool:
        return now_epoch_seconds < self.expires_at - refresh_margin_seconds


@dataclass(frozen=True)
class PerceptorRealtimeSession:
    device_name: str
    home_id: str | int
    started_at: float
    expires_at: float

    def is_valid(self, *, now_epoch_seconds: float) -> bool:
        return now_epoch_seconds < self.expires_at

    def matches(self, *, device_name: str, home_id: str | int) -> bool:
        return self.device_name == device_name and str(self.home_id) == str(home_id)


@dataclass(frozen=True)
class PerceptorConfig:
    base_url: str
    client_id: str
    client_secret: str
    default_device_name: str | None = None
    default_home_id: str | int | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    token_refresh_margin_seconds: float = DEFAULT_TOKEN_REFRESH_MARGIN_SECONDS
    token_default_ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS
    realtime_session_ttl_seconds: float = DEFAULT_REALTIME_SESSION_TTL_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    signing_path: str = DEFAULT_SIGNING_PATH
    append_ampersand_to_signing_secret: bool = True

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "PerceptorConfig":
        values = env or os.environ
        return cls(
            base_url=_require_env(values, PERCEPTOR_BASE_URL_ENV).rstrip("/"),
            client_id=_require_env(values, PERCEPTOR_CLIENT_ID_ENV),
            client_secret=_require_env(values, PERCEPTOR_CLIENT_SECRET_ENV),
            default_device_name=_optional_env(values, PERCEPTOR_DEFAULT_DEVICE_NAME_ENV),
            default_home_id=_optional_env(values, PERCEPTOR_DEFAULT_HOME_ID_ENV),
            timeout_seconds=_float_env(
                values,
                PERCEPTOR_TIMEOUT_SECONDS_ENV,
                DEFAULT_TIMEOUT_SECONDS,
            ),
            token_refresh_margin_seconds=_float_env(
                values,
                PERCEPTOR_TOKEN_REFRESH_MARGIN_SECONDS_ENV,
                DEFAULT_TOKEN_REFRESH_MARGIN_SECONDS,
            ),
            token_default_ttl_seconds=_float_env(
                values,
                PERCEPTOR_TOKEN_DEFAULT_TTL_SECONDS_ENV,
                DEFAULT_TOKEN_TTL_SECONDS,
            ),
            realtime_session_ttl_seconds=_float_env(
                values,
                PERCEPTOR_REALTIME_SESSION_TTL_SECONDS_ENV,
                DEFAULT_REALTIME_SESSION_TTL_SECONDS,
            ),
            max_retries=max(
                _int_env(
                    values,
                    PERCEPTOR_MAX_RETRIES_ENV,
                    DEFAULT_MAX_RETRIES,
                ),
                0,
            ),
            retry_backoff_seconds=max(
                _float_env(
                    values,
                    PERCEPTOR_RETRY_BACKOFF_SECONDS_ENV,
                    DEFAULT_RETRY_BACKOFF_SECONDS,
                ),
                0.0,
            ),
            signing_path=values.get(PERCEPTOR_SIGNING_PATH_ENV, DEFAULT_SIGNING_PATH),
            append_ampersand_to_signing_secret=_bool_env(
                values,
                PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND_ENV,
                True,
            ),
        )


class PerceptorClient:
    def __init__(
        self,
        config: PerceptorConfig,
        *,
        http_client: PerceptorHTTPClient | None = None,
        time_provider: Callable[[], float] | None = None,
        nonce_factory: Callable[[], str] | None = None,
        sleep_provider: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._http_client = http_client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_http_client = http_client is None
        self._time_provider = time_provider or time.time
        self._nonce_factory = nonce_factory or (lambda: uuid.uuid4().hex)
        self._sleep_provider = sleep_provider or time.sleep
        self._cached_token: PerceptorToken | None = None
        self._realtime_session: PerceptorRealtimeSession | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def get_access_token(self) -> str:
        now = self._time_provider()
        if self._cached_token and self._cached_token.is_valid(
            now_epoch_seconds=now,
            refresh_margin_seconds=self.config.token_refresh_margin_seconds,
        ):
            return self._cached_token.access_token

        response = self._post(
            "/token/get",
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            },
            headers={"Content-Type": "application/json"},
        )
        payload = _response_json(response)
        if str(payload.get("code")) != "200" or payload.get("success") is not True:
            raise PerceptorAPIError("Perceptor token request failed.")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PerceptorAPIError("Perceptor token response missing data object.")
        access_token = data.get("access_token") or data.get("accessToken")
        if not access_token:
            raise PerceptorAPIError("Perceptor token response missing access token.")
        expires_at = _extract_token_expires_at(
            data,
            now_epoch_seconds=self._time_provider(),
            default_ttl_seconds=self.config.token_default_ttl_seconds,
        )
        self._cached_token = PerceptorToken(
            access_token=str(access_token),
            expires_at=expires_at,
        )
        return self._cached_token.access_token

    def build_signed_payload(
        self,
        biz_params: Mapping[str, Any],
        *,
        timestamp: str | int | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        public_params: dict[str, Any] = {
            "client_id": self.config.client_id,
            "version": "2.0",
            "timestamp": str(timestamp or int(self._time_provider())),
            "sign_version": "2.0",
            "sign_nonce": nonce or self._nonce_factory(),
            "sign_method": DEFAULT_SIGNATURE_METHOD,
        }
        signed_payload = {**public_params, **dict(biz_params)}
        signed_payload["sign"] = sign_parameters(
            signed_payload,
            client_secret=self.config.client_secret,
            append_ampersand=self.config.append_ampersand_to_signing_secret,
            signing_path=self.config.signing_path,
        )
        return signed_payload

    def request_api(
        self,
        endpoint: str,
        biz_params: Mapping[str, Any],
    ) -> dict[str, Any]:
        access_token = self.get_access_token()
        payload = self.build_signed_payload(biz_params)
        response = self._post(
            endpoint,
            payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        return _response_json(response)

    def start_realtime(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        params = self._device_params(device_name=device_name, home_id=home_id)
        response = self.request_api("/vitalSigns/start", params)
        if _response_successful(response):
            now = self._time_provider()
            self._realtime_session = PerceptorRealtimeSession(
                device_name=str(params["device_name"]),
                home_id=params["home_id"],
                started_at=now,
                expires_at=_extract_realtime_session_expires_at(
                    response,
                    now_epoch_seconds=now,
                    default_ttl_seconds=self.config.realtime_session_ttl_seconds,
                ),
            )
        else:
            self._realtime_session = None
        return response

    def get_realtime(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        params = self._device_params(device_name=device_name, home_id=home_id)
        self._require_realtime_session(params)
        return self.request_api("/vitalSigns/getRealTimes", params)

    def get_sleep_report(
        self,
        *,
        report_date: date | str,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        params = self._device_params(device_name=device_name, home_id=home_id)
        params["date"] = _local_report_date_text(report_date)
        return self.request_api("/vitalSigns/getSleepReport", params)

    def get_history_data(
        self,
        *,
        device_names: list[str] | tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        names = tuple(str(item).strip() for item in device_names)
        if not names or any(not item for item in names):
            raise PerceptorConfigurationError(
                "getHistoryData requires non-empty device names."
            )
        if len(names) > 20:
            raise PerceptorConfigurationError(
                "getHistoryData accepts at most 20 devices per request."
            )
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise PerceptorConfigurationError(
                "getHistoryData requires timezone-aware bounds."
            )
        if end_at <= start_at or end_at - start_at > timedelta(hours=1):
            raise PerceptorConfigurationError(
                "getHistoryData window must be positive and no longer than one hour."
            )
        resolved_home_id = (
            home_id if home_id is not None else self.config.default_home_id
        )
        if resolved_home_id is None or resolved_home_id == "":
            raise PerceptorConfigurationError("Perceptor home_id is not configured.")
        return self.request_api(
            "/vitalSigns/getHistoryData",
            {
                "device_names": ",".join(names),
                "home_id": resolved_home_id,
                "start_time": start_at.isoformat(),
                "end_time": end_at.isoformat(),
            },
        )

    def get_alarm_list(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        return self.request_api(
            "/alarm/getList",
            self._device_params(device_name=device_name, home_id=home_id),
        )

    def get_device_detail(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        return self.request_api(
            "/device/detail",
            self._device_params(device_name=device_name, home_id=home_id),
        )

    def _device_params(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        resolved_device_name = device_name or self.config.default_device_name
        resolved_home_id = home_id if home_id is not None else self.config.default_home_id
        if not resolved_device_name:
            raise PerceptorConfigurationError("Perceptor device_name is not configured.")
        if resolved_home_id is None or resolved_home_id == "":
            raise PerceptorConfigurationError("Perceptor home_id is not configured.")
        return {
            "device_name": resolved_device_name,
            "home_id": resolved_home_id,
        }

    def _require_realtime_session(self, params: Mapping[str, Any]) -> None:
        session = self._realtime_session
        if session is None:
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session has not been started. "
                "Call start_realtime() before get_realtime()."
            )
        device_name = str(params["device_name"])
        home_id = params["home_id"]
        if not session.matches(device_name=device_name, home_id=home_id):
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session was started for a different device. "
                "Call start_realtime() for this configured device before polling."
            )
        if not session.is_valid(now_epoch_seconds=self._time_provider()):
            self._realtime_session = None
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session has expired. "
                "Call start_realtime() again before polling."
            )

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if not endpoint.startswith("/"):
            raise ValueError("Perceptor endpoint must start with '/'.")
        total_attempts = 1 + max(self.config.max_retries, 0)
        for attempt in range(1, total_attempts + 1):
            started_at = time.perf_counter()
            log_event(
                "vendor_call_start",
                source="perceptor",
                provider_mode="live",
                endpoint=endpoint,
                attempt=attempt,
                total_attempts=total_attempts,
                payload_keys=sorted(payload.keys()),
                header_names=sorted((headers or {}).keys()),
                auth_header_present=bool((headers or {}).get("Authorization")),
            )
            try:
                response = self._http_client.post(
                    f"{self.config.base_url}{endpoint}",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                record_pull(source="perceptor", endpoint=endpoint)
                log_event(
                    "vendor_call_success",
                    source="perceptor",
                    provider_mode="live",
                    endpoint=endpoint,
                    attempt=attempt,
                    status_code=response.status_code,
                    duration_ms=_elapsed_ms(started_at),
                )
                return response
            except httpx.HTTPStatusError as exc:
                retryable = _retryable_status_code(exc.response.status_code)
                log_event(
                    "vendor_call_failure",
                    level=logging.WARNING,
                    source="perceptor",
                    provider_mode="live",
                    endpoint=endpoint,
                    attempt=attempt,
                    status_code=exc.response.status_code,
                    retryable=retryable and attempt < total_attempts,
                    duration_ms=_elapsed_ms(started_at),
                    error_type=exc.__class__.__name__,
                )
                if (
                    attempt >= total_attempts
                    or not retryable
                ):
                    error = PerceptorAPIError(
                        "Perceptor HTTP request failed with "
                        f"status {exc.response.status_code}."
                    )
                    record_error(
                        event="vendor_call_failure",
                        error=error,
                        source="perceptor",
                        context={
                            "endpoint": endpoint,
                            "status_code": exc.response.status_code,
                        },
                    )
                    raise error from exc
            except httpx.RequestError as exc:
                log_event(
                    "vendor_call_failure",
                    level=logging.WARNING,
                    source="perceptor",
                    provider_mode="live",
                    endpoint=endpoint,
                    attempt=attempt,
                    retryable=attempt < total_attempts,
                    duration_ms=_elapsed_ms(started_at),
                    error_type=exc.__class__.__name__,
                )
                if attempt >= total_attempts:
                    error = PerceptorAPIError("Perceptor HTTP request failed.")
                    record_error(
                        event="vendor_call_failure",
                        error=error,
                        source="perceptor",
                        context={"endpoint": endpoint},
                    )
                    raise error from exc
            if self.config.retry_backoff_seconds > 0:
                self._sleep_provider(self.config.retry_backoff_seconds * attempt)
        raise PerceptorAPIError("Perceptor HTTP request failed.")


def build_perceptor_client_from_env(
    env: Mapping[str, str] | None = None,
) -> object:
    values = env or os.environ
    mode = values.get(PERCEPTOR_PROVIDER_MODE_ENV, "").strip().lower()
    if not mode:
        raise PerceptorConfigurationError(
            f"{PERCEPTOR_PROVIDER_MODE_ENV} must be explicitly configured"
        )
    if mode == "fake":
        deployment = values.get("SLEEPAGENT_DEPLOYMENT_MODE", "").strip().lower()
        namespace = values.get(PERCEPTOR_DATA_NAMESPACE_ENV, "").strip()
        if (
            deployment not in {"development", "test"}
            or not namespace.startswith("replay:")
        ):
            raise PerceptorConfigurationError(
                "fake Perceptor requires explicit development/test mode and "
                f"a replay namespace in {PERCEPTOR_DATA_NAMESPACE_ENV}"
            )
        from sleepagent.integrations.perceptor.fake_client import FakePerceptorClient

        return FakePerceptorClient()
    if mode == "live":
        if (
            values.get("SLEEPAGENT_DEPLOYMENT_MODE", "").strip().lower()
            == "production"
            and not values.get(PERCEPTOR_DATA_NAMESPACE_ENV, "")
            .strip()
            .startswith("live:")
        ):
            raise PerceptorConfigurationError(
                "production live Perceptor requires an explicit live data "
                f"namespace in {PERCEPTOR_DATA_NAMESPACE_ENV}"
            )
        return PerceptorClient(PerceptorConfig.from_env(values))
    raise PerceptorConfigurationError(
        f"{PERCEPTOR_PROVIDER_MODE_ENV} must be 'fake' or 'live'."
    )


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PerceptorAPIError("Perceptor response was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PerceptorAPIError("Perceptor response JSON must be an object.")
    return payload


def _local_report_date_text(value: date | str) -> str:
    if isinstance(value, datetime):
        raise PerceptorConfigurationError(
            "report_date must be an explicit local date, not a datetime."
        )
    if isinstance(value, date):
        return value.isoformat()
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise PerceptorConfigurationError(
            "report_date must use ISO YYYY-MM-DD."
        ) from exc
    if str(value) != parsed.isoformat():
        raise PerceptorConfigurationError(
            "report_date must use canonical ISO YYYY-MM-DD."
        )
    return parsed.isoformat()


def _response_successful(response: Mapping[str, Any]) -> bool:
    return str(response.get("code")) == "200" and response.get("success") is True


def _extract_token_expires_at(
    data: Mapping[str, Any],
    *,
    now_epoch_seconds: float,
    default_ttl_seconds: float,
) -> float:
    expires_at = _extract_epoch_seconds(
        data,
        keys=(
            "expires_at",
            "expiresAt",
            "expire_at",
            "expireAt",
            "expiration_time",
            "expirationTime",
        ),
    )
    if expires_at is not None:
        return expires_at
    value = data.get("expires_in") or data.get("expiresIn") or data.get("expire_in")
    if value is None:
        return now_epoch_seconds + max(default_ttl_seconds, 0.0)
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise PerceptorAPIError("Perceptor token expiry is not numeric.") from exc
    return now_epoch_seconds + max(ttl, 0.0)


def _extract_realtime_session_expires_at(
    response: Mapping[str, Any],
    *,
    now_epoch_seconds: float,
    default_ttl_seconds: float,
) -> float:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return now_epoch_seconds + max(default_ttl_seconds, 0.0)
    expires_at = _extract_epoch_seconds(
        data,
        keys=(
            "expires_at",
            "expiresAt",
            "expire_at",
            "expireAt",
            "expire_time",
            "expireTime",
            "end_at",
            "endAt",
            "end_time",
            "endTime",
            "valid_until",
            "validUntil",
            "session_expires_at",
            "sessionExpiresAt",
        ),
    )
    if expires_at is not None:
        return expires_at
    ttl = _extract_duration_seconds(
        data,
        keys=(
            "expires_in",
            "expiresIn",
            "expire_in",
            "duration",
            "duration_seconds",
            "durationSeconds",
            "ttl",
            "ttl_seconds",
            "ttlSeconds",
            "valid_seconds",
            "validSeconds",
        ),
    )
    if ttl is None:
        ttl = default_ttl_seconds
    return now_epoch_seconds + max(ttl, 0.0)


def _extract_epoch_seconds(
    values: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> float | None:
    for key in keys:
        if key not in values:
            continue
        parsed = _parse_epoch_seconds(values[key])
        if parsed is not None:
            return parsed
    return None


def _extract_duration_seconds(
    values: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> float | None:
    for key in keys:
        if key not in values:
            continue
        try:
            return float(values[key])
        except (TypeError, ValueError) as exc:
            raise PerceptorAPIError(
                "Perceptor realtime session duration is not numeric."
            ) from exc
    return None


def _parse_epoch_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return _normalize_numeric_epoch_seconds(float(value))
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return _normalize_numeric_epoch_seconds(float(stripped))
    except ValueError:
        pass
    try:
        normalized = stripped.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PerceptorAPIError(
            "Perceptor realtime session expiry is not a recognized timestamp."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalize_numeric_epoch_seconds(value: float) -> float:
    if value > 10_000_000_000:
        return value / 1000.0
    return value


def _retryable_status_code(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000.0, 3)


def _require_env(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or not value.strip():
        raise PerceptorConfigurationError(f"{key} is required for live Perceptor mode.")
    return value.strip()


def _optional_env(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _float_env(values: Mapping[str, str], key: str, default: float) -> float:
    value = values.get(key)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise PerceptorConfigurationError(f"{key} must be numeric.") from exc


def _int_env(values: Mapping[str, str], key: str, default: int) -> int:
    value = values.get(key)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise PerceptorConfigurationError(f"{key} must be an integer.") from exc


def _bool_env(values: Mapping[str, str], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise PerceptorConfigurationError(f"{key} must be a boolean value.")
