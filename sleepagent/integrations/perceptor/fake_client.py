from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sleepagent.integrations.perceptor.client import PerceptorRealtimeSessionError
from sleepagent.observability import log_event, record_pull


class FakePerceptorClient:
    """No-network Perceptor client for tests and local product-device demos."""

    def __init__(
        self,
        *,
        responses: Mapping[str, dict[str, Any]] | None = None,
        default_device_name: str = "fake-device-name",
        default_home_id: str | int = "fake-home-id",
        realtime_session_ttl_seconds: float = 300.0,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.default_device_name = default_device_name
        self.default_home_id = default_home_id
        self.realtime_session_ttl_seconds = realtime_session_ttl_seconds
        self._time_provider = time_provider or time.time
        self.requests: list[dict[str, Any]] = []
        self._realtime_session: dict[str, Any] | None = None

    def get_access_token(self) -> str:
        return "fake-perceptor-token"

    def request_api(
        self,
        endpoint: str,
        biz_params: Mapping[str, Any],
    ) -> dict[str, Any]:
        log_event(
            "vendor_call_start",
            source="perceptor",
            provider_mode="fake",
            endpoint=endpoint,
            payload_keys=sorted(str(key) for key in biz_params.keys()),
        )
        request = {
            "endpoint": endpoint,
            "biz_params": dict(biz_params),
        }
        self.requests.append(request)
        if endpoint in self.responses:
            response = self.responses[endpoint]
        else:
            response = {
                "code": 200,
                "success": True,
                "data": {
                    "endpoint": endpoint,
                    "echo": dict(biz_params),
                },
            }
        record_pull(source="perceptor", endpoint=endpoint)
        log_event(
            "vendor_call_success",
            source="perceptor",
            provider_mode="fake",
            endpoint=endpoint,
            success=response.get("success"),
            response_code=response.get("code"),
        )
        return response

    def start_realtime(
        self,
        *,
        device_name: str | None = None,
        home_id: str | int | None = None,
    ) -> dict[str, Any]:
        params = self._device_params(device_name=device_name, home_id=home_id)
        response = self.request_api("/vitalSigns/start", params)
        if str(response.get("code")) == "200" and response.get("success") is True:
            self._realtime_session = {
                "device_name": str(params["device_name"]),
                "home_id": params["home_id"],
                "expires_at": self._time_provider()
                + max(self.realtime_session_ttl_seconds, 0.0),
            }
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
        from sleepagent.integrations.perceptor.client import (
            _local_report_date_text,
        )

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
        if not device_names or len(device_names) > 20:
            raise ValueError("history batch must contain 1..20 devices")
        if (
            start_at.tzinfo is None
            or end_at.tzinfo is None
            or end_at <= start_at
            or end_at - start_at > timedelta(hours=1)
        ):
            raise ValueError("history window must be aware, positive, and <= 1 hour")
        return self.request_api(
            "/vitalSigns/getHistoryData",
            {
                "device_names": ",".join(device_names),
                "home_id": (
                    home_id if home_id is not None else self.default_home_id
                ),
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
        device_name: str | None,
        home_id: str | int | None,
    ) -> dict[str, Any]:
        return {
            "device_name": device_name or self.default_device_name,
            "home_id": home_id if home_id is not None else self.default_home_id,
        }

    def _require_realtime_session(self, params: Mapping[str, Any]) -> None:
        session = self._realtime_session
        if session is None:
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session has not been started. "
                "Call start_realtime() before get_realtime()."
            )
        if (
            session["device_name"] != str(params["device_name"])
            or str(session["home_id"]) != str(params["home_id"])
        ):
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session was started for a different device. "
                "Call start_realtime() for this configured device before polling."
            )
        if self._time_provider() >= session["expires_at"]:
            self._realtime_session = None
            raise PerceptorRealtimeSessionError(
                "Perceptor realtime session has expired. "
                "Call start_realtime() again before polling."
            )
