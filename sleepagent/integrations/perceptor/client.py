"""Read-only YunYun Platform API client frozen by the P4-D2-A golden.

This client is intentionally narrower than the historical diagnostic. It owns
only token acquisition and read-only product/device discovery. The outbound
Platform contract is fixed to ``/v2`` plus ``ClientSecret + "&"`` and never
tries an alternative. Incoming Push verification remains a separate module.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sleepagent.domain.contracts import ProviderDeviceIdentity
from sleepagent.integrations.perceptor.signing import (
    PLATFORM_SIGNING_KEY_MODE,
    PLATFORM_SIGNING_PATH,
    sign_parameters,
)


UTC = timezone.utc
DEFAULT_PLATFORM_BASE_URL = "https://openapi.perceptor.cn/v2"
TOKEN_ENDPOINT = "/token/get"
PRODUCT_LIST_ENDPOINT = "/product/getList"
DEVICE_LIST_ENDPOINT = "/device/getList"
DEVICE_DETAIL_ENDPOINT = "/device/detail"
GET_CURRENT_ENDPOINT = "/vitalSigns/getCurrent"
REALTIME_START_ENDPOINT = "/vitalSigns/start"
REALTIME_READ_ENDPOINT = "/vitalSigns/getRealTimes"
HISTORY_ENDPOINT = "/vitalSigns/getHistoryData"
SLEEP_REPORT_ENDPOINT = "/vitalSigns/getSleepReport"
READ_ONLY_ENDPOINTS = frozenset(
    {
        PRODUCT_LIST_ENDPOINT,
        DEVICE_LIST_ENDPOINT,
        DEVICE_DETAIL_ENDPOINT,
        GET_CURRENT_ENDPOINT,
        REALTIME_START_ENDPOINT,
        REALTIME_READ_ENDPOINT,
        HISTORY_ENDPOINT,
        SLEEP_REPORT_ENDPOINT,
    }
)


class PlatformApiError(RuntimeError):
    """A deliberately redacted Platform API failure."""

    def __init__(
        self,
        category: str,
        *,
        endpoint: str,
        http_status: int | None = None,
        vendor_code: str | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.endpoint = endpoint
        self.http_status = http_status
        self.vendor_code = vendor_code


@dataclass(frozen=True, slots=True)
class PlatformAccessToken:
    access_token: str = field(repr=False)
    token_type: str
    expiry_selector: int | None
    issued_at: datetime
    expires_at: datetime | None

    def is_usable(self, now: datetime, *, refresh_margin: timedelta) -> bool:
        _require_aware(now, "now")
        return self.expires_at is not None and now + refresh_margin < self.expires_at


@dataclass(frozen=True, slots=True)
class PlatformRawResponseEvidence:
    endpoint: str
    requested_at: datetime
    received_at: datetime
    http_status: int
    raw_body: bytes = field(repr=False)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_body).hexdigest()


ReadDataT = TypeVar("ReadDataT")


@dataclass(frozen=True, slots=True)
class PlatformEvidencedRead(Generic[ReadDataT]):
    """One parsed read and the exact raw response that produced it."""

    endpoint: str
    data: ReadDataT
    evidence: PlatformRawResponseEvidence

    def __post_init__(self) -> None:
        if self.endpoint == TOKEN_ENDPOINT:
            raise ValueError("token response cannot be represented as a data read")
        if self.endpoint not in READ_ONLY_ENDPOINTS:
            raise ValueError("endpoint is outside the read-only allowlist")
        if self.endpoint != self.evidence.endpoint:
            raise ValueError("read endpoint does not match raw response evidence")


class PlatformProductRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    product_id: str
    product_name: str | None = None
    product_model: str | None = None
    device_amount: int | None = Field(default=None, ge=0)

    @field_validator("product_id", mode="before")
    @classmethod
    def stringify_product_id(cls, value: object) -> str:
        return _identifier_text(value, "product_id")


class PlatformDeviceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    device_id: str | None = None
    device_name: str
    product_id: str | None = None
    home_id: str | None = None
    project_id: str | None = None
    device_status: str | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    device_imei: str | None = None
    device_imsi: str | None = None
    device_iccid: str | None = None

    @field_validator(
        "device_id",
        "device_name",
        "product_id",
        "home_id",
        "project_id",
        mode="before",
    )
    @classmethod
    def stringify_identity(cls, value: object, info: Any) -> str | None:
        if value is None:
            return None
        return _identifier_text(value, info.field_name)

    def provider_identity(self) -> ProviderDeviceIdentity:
        native_keys = {
            key: value
            for key, value in (
                ("device_imei", self.device_imei),
                ("device_imsi", self.device_imsi),
                ("device_iccid", self.device_iccid),
            )
            if value
        }
        return ProviderDeviceIdentity(
            provider_device_id=self.device_id,
            provider_device_name=self.device_name,
            product_id=self.product_id,
            home_id=self.home_id,
            project_id=self.project_id,
            native_keys=native_keys,
        )


class PlatformDevicePage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    page_count: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    current_page: int = Field(ge=1)
    total: int = Field(ge=0)
    total_records: int = Field(ge=0)
    devices: tuple[PlatformDeviceRecord, ...] = Field(alias="list")


class PerceptorPlatformClient:
    """Synchronous bounded client for authenticated read-only API calls."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = DEFAULT_PLATFORM_BASE_URL,
        http_client: httpx.Client | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        nonce_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        refresh_margin: timedelta = timedelta(minutes=5),
        response_observer: Callable[[PlatformRawResponseEvidence], None] | None = None,
    ) -> None:
        if not client_id.strip() or not client_secret:
            raise ValueError("Platform API credentials are required")
        normalized_base = base_url.rstrip("/")
        if not normalized_base.startswith("https://"):
            raise ValueError("Platform API base URL must use HTTPS")
        if refresh_margin < timedelta(0):
            raise ValueError("token refresh margin must be non-negative")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = normalized_base
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(15.0), follow_redirects=False
        )
        self._owns_http_client = http_client is None
        self._now_factory = now_factory
        self._nonce_factory = nonce_factory
        self._refresh_margin = refresh_margin
        self._response_observer = response_observer
        self._token: PlatformAccessToken | None = None
        self._token_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "PerceptorPlatformClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def token(self, *, force_refresh: bool = False) -> PlatformAccessToken:
        now = self._now()
        if (
            not force_refresh
            and self._token is not None
            and self._token.is_usable(now, refresh_margin=self._refresh_margin)
        ):
            return self._token
        with self._token_lock:
            now = self._now()
            if (
                not force_refresh
                and self._token is not None
                and self._token.is_usable(
                    now, refresh_margin=self._refresh_margin
                )
            ):
                return self._token
            envelope, _evidence = self._post_json(
                TOKEN_ENDPOINT,
                {"client_id": self._client_id, "client_secret": self._client_secret},
                access_token=None,
            )
            data = _response_data(envelope, endpoint=TOKEN_ENDPOINT)
            token = data.get("access_token")
            token_type = data.get("token_type")
            if not isinstance(token, str) or not token:
                raise PlatformApiError("TOKEN_SHAPE_INVALID", endpoint=TOKEN_ENDPOINT)
            if token_type != "Bearer":
                raise PlatformApiError("TOKEN_TYPE_UNSUPPORTED", endpoint=TOKEN_ENDPOINT)
            selector = _expiry_selector(data)
            expires_at = (
                now + timedelta(days=1 if selector == 1 else 30)
                if selector is not None
                else None
            )
            self._token = PlatformAccessToken(
                access_token=token,
                token_type=token_type,
                expiry_selector=selector,
                issued_at=now,
                expires_at=expires_at,
            )
            return self._token

    def product_list(self) -> tuple[PlatformProductRecord, ...]:
        data = self._signed_read(PRODUCT_LIST_ENDPOINT, {})
        return tuple(
            PlatformProductRecord.model_validate(item)
            for item in _object_list(data, "list", endpoint=PRODUCT_LIST_ENDPOINT)
        )

    def device_list(
        self,
        *,
        page_current: int = 1,
        page_size: int = 50,
        product_id: str | int | None = None,
        device_name: str | None = None,
    ) -> tuple[PlatformDeviceRecord, ...]:
        return self.device_page(
            page_current=page_current,
            page_size=page_size,
            product_id=product_id,
            device_name=device_name,
        ).devices

    def device_page(
        self,
        *,
        page_current: int = 1,
        page_size: int = 50,
        product_id: str | int | None = None,
        device_name: str | None = None,
    ) -> PlatformDevicePage:
        if page_current < 1:
            raise ValueError("page_current must be positive")
        if not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")
        params: dict[str, str | int] = {
            "page_current": page_current,
            "page_size": page_size,
        }
        if product_id is not None:
            params["product_id"] = _identifier_text(product_id, "product_id")
        if device_name is not None:
            params["device_name"] = _identifier_text(device_name, "device_name")
        data = self._signed_read(DEVICE_LIST_ENDPOINT, params)
        try:
            return PlatformDevicePage.model_validate(data)
        except ValueError as exc:
            raise PlatformApiError(
                "DEVICE_PAGE_SHAPE_INVALID", endpoint=DEVICE_LIST_ENDPOINT
            ) from exc

    def device_detail(self, *, device_name: str) -> PlatformDeviceRecord:
        normalized_name = _identifier_text(device_name, "device_name")
        data = self._signed_read(
            DEVICE_DETAIL_ENDPOINT,
            {"device_name": normalized_name},
        )
        return PlatformDeviceRecord.model_validate(data)

    def get_current(
        self, *, device_name: str, home_id: str | int
    ) -> Mapping[str, Any]:
        return self.get_current_evidenced(
            device_name=device_name,
            home_id=home_id,
        ).data

    def get_current_evidenced(
        self, *, device_name: str, home_id: str | int
    ) -> PlatformEvidencedRead[Mapping[str, Any]]:
        return self._signed_read_evidenced(
            GET_CURRENT_ENDPOINT,
            {
                "device_name": _identifier_text(device_name, "device_name"),
                "home_id": _long_identifier(home_id, "home_id"),
            },
        )

    def start_realtime(
        self, *, device_name: str, home_id: str | int
    ) -> Mapping[str, Any]:
        return self.start_realtime_evidenced(
            device_name=device_name,
            home_id=home_id,
        ).data

    def start_realtime_evidenced(
        self, *, device_name: str, home_id: str | int
    ) -> PlatformEvidencedRead[Mapping[str, Any]]:
        return self._signed_read_evidenced(
            REALTIME_START_ENDPOINT,
            {
                "device_name": _identifier_text(device_name, "device_name"),
                "home_id": _long_identifier(home_id, "home_id"),
            },
        )

    def get_realtime(
        self, *, device_name: str, home_id: str | int
    ) -> Mapping[str, Any]:
        return self.get_realtime_evidenced(
            device_name=device_name,
            home_id=home_id,
        ).data

    def get_realtime_evidenced(
        self, *, device_name: str, home_id: str | int
    ) -> PlatformEvidencedRead[Mapping[str, Any]]:
        return self._signed_read_evidenced(
            REALTIME_READ_ENDPOINT,
            {
                "device_name": _identifier_text(device_name, "device_name"),
                "home_id": _long_identifier(home_id, "home_id"),
            },
        )

    def get_history(
        self,
        *,
        device_names: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[Mapping[str, Any], ...]:
        return self.get_history_evidenced(
            device_names=device_names,
            start_at=start_at,
            end_at=end_at,
        ).data

    def get_history_evidenced(
        self,
        *,
        device_names: tuple[str, ...],
        start_at: datetime,
        end_at: datetime,
    ) -> PlatformEvidencedRead[tuple[Mapping[str, Any], ...]]:
        if not 1 <= len(device_names) <= 20:
            raise ValueError("history requires between 1 and 20 devices")
        _require_aware(start_at, "start_at")
        _require_aware(end_at, "end_at")
        if end_at <= start_at or end_at - start_at > timedelta(hours=1):
            raise ValueError("history window must be positive and at most one hour")
        devices = tuple(
            _identifier_text(value, "device_name") for value in device_names
        )
        compact_devices = json.dumps(
            devices, ensure_ascii=False, separators=(",", ":")
        )
        read = self._signed_read_payload_evidenced(
            HISTORY_ENDPOINT,
            {
                "devices": list(devices),
                "start_time": int(start_at.timestamp()),
                "end_time": int(end_at.timestamp()),
            },
            signing_overrides={"devices": compact_devices},
        )
        payload = read.data
        if not isinstance(payload, list) or any(
            not isinstance(item, Mapping) for item in payload
        ):
            raise PlatformApiError("HISTORY_SHAPE_INVALID", endpoint=HISTORY_ENDPOINT)
        return PlatformEvidencedRead(
            endpoint=read.endpoint,
            data=tuple(payload),
            evidence=read.evidence,
        )

    def get_sleep_report(
        self,
        *,
        device_name: str,
        home_id: str | int,
        report_date: date,
    ) -> Mapping[str, Any]:
        return self.get_sleep_report_evidenced(
            device_name=device_name,
            home_id=home_id,
            report_date=report_date,
        ).data

    def get_sleep_report_evidenced(
        self,
        *,
        device_name: str,
        home_id: str | int,
        report_date: date,
    ) -> PlatformEvidencedRead[Mapping[str, Any]]:
        if not isinstance(report_date, date) or isinstance(report_date, datetime):
            raise ValueError("report_date must be a date")
        return self._signed_read_evidenced(
            SLEEP_REPORT_ENDPOINT,
            {
                "device_name": _identifier_text(device_name, "device_name"),
                "home_id": _long_identifier(home_id, "home_id"),
                "date": report_date.isoformat(),
            },
        )

    def _signed_read(
        self,
        endpoint: str,
        business_parameters: Mapping[str, object],
    ) -> Mapping[str, Any]:
        return self._signed_read_evidenced(endpoint, business_parameters).data

    def _signed_read_evidenced(
        self,
        endpoint: str,
        business_parameters: Mapping[str, object],
    ) -> PlatformEvidencedRead[Mapping[str, Any]]:
        read = self._signed_read_payload_evidenced(endpoint, business_parameters)
        payload = read.data
        if not isinstance(payload, Mapping):
            raise PlatformApiError("DATA_SHAPE_INVALID", endpoint=endpoint)
        return PlatformEvidencedRead(
            endpoint=read.endpoint,
            data=payload,
            evidence=read.evidence,
        )

    def _signed_read_payload(
        self,
        endpoint: str,
        business_parameters: Mapping[str, object],
        *,
        signing_overrides: Mapping[str, str | int | float] | None = None,
    ) -> object:
        return self._signed_read_payload_evidenced(
            endpoint,
            business_parameters,
            signing_overrides=signing_overrides,
        ).data

    def _signed_read_payload_evidenced(
        self,
        endpoint: str,
        business_parameters: Mapping[str, object],
        *,
        signing_overrides: Mapping[str, str | int | float] | None = None,
    ) -> PlatformEvidencedRead[object]:
        if endpoint not in READ_ONLY_ENDPOINTS:
            raise ValueError("endpoint is outside the read-only allowlist")
        lease = self.token()
        now = self._now()
        parameters: dict[str, object] = {
            "client_id": self._client_id,
            "version": "2.0",
            "timestamp": str(int(now.timestamp())),
            "sign_version": "2.0",
            "sign_nonce": self._nonce_factory(),
            "sign_method": "HMAC-SHA1",
            **business_parameters,
        }
        signing_parameters = {**parameters, **(signing_overrides or {})}
        parameters["sign"] = sign_parameters(
            signing_parameters,
            client_secret=self._client_secret,
            signing_path=PLATFORM_SIGNING_PATH,
            key_mode=PLATFORM_SIGNING_KEY_MODE,
        )
        envelope, evidence = self._post_json(
            endpoint,
            parameters,
            access_token=lease.access_token,
        )
        return PlatformEvidencedRead(
            endpoint=endpoint,
            data=_response_payload(envelope, endpoint=endpoint),
            evidence=evidence,
        )

    def _post_json(
        self,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        access_token: str | None,
    ) -> tuple[Mapping[str, Any], PlatformRawResponseEvidence]:
        headers = (
            {"Authorization": f"Bearer {access_token}"}
            if access_token is not None
            else {}
        )
        requested_at = self._now()
        try:
            response = self._http.post(
                f"{self._base_url}{endpoint}", json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise PlatformApiError("TIMEOUT", endpoint=endpoint) from exc
        except httpx.HTTPError as exc:
            raise PlatformApiError("TRANSPORT_ERROR", endpoint=endpoint) from exc
        received_at = self._now()
        evidence = PlatformRawResponseEvidence(
            endpoint=endpoint,
            requested_at=requested_at,
            received_at=received_at,
            http_status=response.status_code,
            raw_body=response.content,
        )
        if self._response_observer is not None:
            try:
                self._response_observer(evidence)
            except Exception as exc:
                raise PlatformApiError(
                    "EVIDENCE_CAPTURE_FAILED", endpoint=endpoint
                ) from exc
        if response.status_code != 200:
            raise PlatformApiError(
                "HTTP_ERROR", endpoint=endpoint, http_status=response.status_code
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise PlatformApiError("INVALID_JSON", endpoint=endpoint) from exc
        if not isinstance(value, Mapping):
            raise PlatformApiError("INVALID_ENVELOPE", endpoint=endpoint)
        return value, evidence

    def _now(self) -> datetime:
        value = self._now_factory()
        _require_aware(value, "now_factory result")
        return value


def _response_data(
    envelope: Mapping[str, Any], *, endpoint: str
) -> Mapping[str, Any]:
    data = _response_payload(envelope, endpoint=endpoint)
    if not isinstance(data, Mapping):
        raise PlatformApiError("DATA_SHAPE_INVALID", endpoint=endpoint)
    return data


def _response_payload(envelope: Mapping[str, Any], *, endpoint: str) -> object:
    code = str(envelope.get("code", "MISSING"))
    if envelope.get("success") is not True or code != "200":
        raise PlatformApiError(
            _vendor_error_category(code), endpoint=endpoint, vendor_code=code
        )
    data = envelope.get("data")
    if data is None:
        raise PlatformApiError("DATA_MISSING", endpoint=endpoint)
    return data


def _object_list(
    data: Mapping[str, Any], key: str, *, endpoint: str
) -> list[Mapping[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise PlatformApiError("LIST_SHAPE_INVALID", endpoint=endpoint)
    return list(value)


def _expiry_selector(data: Mapping[str, Any]) -> int | None:
    for key in ("expires_time", "expires_in"):
        value = data.get(key)
        if value in (1, "1"):
            return 1
        if value in (2, "2"):
            return 2
    return None


def _vendor_error_category(code: str) -> str:
    return {
        "400": "BAD_REQUEST",
        "401": "ILLEGAL_REQUEST",
        "403": "UNAUTHORIZED",
        "404": "ENDPOINT_NOT_FOUND",
        "405": "METHOD_NOT_ALLOWED",
        "500": "SERVER_ERROR",
        "1000": "CLIENT_ID_NOT_FOUND",
        "1001": "CLIENT_ID_DISABLED",
        "1002": "CLIENT_SECRET_INCORRECT",
        "1003": "SIGNATURE_ERROR",
        "2000": "CHANNEL_NOT_FOUND",
        "2001": "CHANNEL_PRODUCT_NOT_FOUND",
        "2002": "CHANNEL_DEVICE_NOT_FOUND",
        "4000": "PRODUCT_NOT_FOUND",
        "6000": "DEVICE_NOT_FOUND",
        "6001": "DEVICE_OFFLINE",
    }.get(code, "VENDOR_ERROR")


def _identifier_text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a string or integer")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must be non-empty")
    return text


def _long_identifier(value: object, name: str) -> int:
    text = _identifier_text(value, name)
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"{name} must use the documented integer representation")
    return int(text)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "DEFAULT_PLATFORM_BASE_URL",
    "DEVICE_DETAIL_ENDPOINT",
    "DEVICE_LIST_ENDPOINT",
    "GET_CURRENT_ENDPOINT",
    "HISTORY_ENDPOINT",
    "PRODUCT_LIST_ENDPOINT",
    "REALTIME_READ_ENDPOINT",
    "REALTIME_START_ENDPOINT",
    "SLEEP_REPORT_ENDPOINT",
    "PlatformAccessToken",
    "PlatformApiError",
    "PlatformDevicePage",
    "PlatformDeviceRecord",
    "PlatformEvidencedRead",
    "PlatformProductRecord",
    "PlatformRawResponseEvidence",
    "PerceptorPlatformClient",
    "READ_ONLY_ENDPOINTS",
    "TOKEN_ENDPOINT",
]
