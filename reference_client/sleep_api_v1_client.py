"""Standalone HTTPS/OpenAPI client for the public Sleep Domain API v1.

This module deliberately has no dependency on the server implementation.  It
persists opaque cursors and event ids, because delivery is at-least-once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


UTC = timezone.utc


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse: ...


class HttpsJsonTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> HttpResponse:
        if urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise ValueError("the reference client only sends credentials over HTTPS")
        request = urllib.request.Request(
            url,
            data=body if body else None,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                body=exc.read(),
            )


class ApiError(RuntimeError):
    def __init__(self, status_code: int, payload: Mapping[str, Any]) -> None:
        message = str(payload.get("message", "Sleep API request failed"))
        details = payload.get("details")
        super().__init__(message if not details else f"{message} ({details})")
        self.status_code = status_code
        self.payload = dict(payload)
        self.code = str(payload.get("code", "UNKNOWN_ERROR"))


@dataclass(frozen=True)
class Ed25519ActorSigner:
    issuer: str
    audience: str
    key_id: str
    actor_id: str
    subject_id: str
    role: str
    scopes: tuple[str, ...]
    private_key: ed25519.Ed25519PrivateKey
    lifetime: timedelta = timedelta(minutes=2)

    @classmethod
    def from_pem(
        cls,
        *,
        private_key_pem: bytes,
        **kwargs: Any,
    ) -> "Ed25519ActorSigner":
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ValueError("the reference signer requires an Ed25519 private key")
        return cls(private_key=key, **kwargs)

    def sign(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        now: datetime,
    ) -> str:
        issued = int(now.timestamp())
        header = {"alg": "EdDSA", "kid": self.key_id, "typ": "JWT"}
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "jti": uuid4().hex,
            "actor_id": self.actor_id,
            "subject_id": self.subject_id,
            "role": self.role,
            "scope": list(self.scopes),
            "iat": issued,
            "exp": int((now + self.lifetime).timestamp()),
            "nonce": uuid4().hex,
            "method": method.upper(),
            "path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        encoded_header = _b64url(_canonical_json(header))
        encoded_claims = _b64url(_canonical_json(claims))
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        return (
            f"{encoded_header}.{encoded_claims}."
            f"{_b64url(self.private_key.sign(signing_input))}"
        )


class FileEventStateStore:
    """Small restart-safe store for opaque cursors, event ids and snapshots."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "sleep_client_state.v1", "subjects": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "sleep_client_state.v1":
            raise ValueError("unsupported reference-client state schema")
        return value

    def save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class SleepApiV1Client:
    def __init__(
        self,
        *,
        base_url: str,
        service_credential: str,
        signer: Ed25519ActorSigner,
        event_state_store: FileEventStateStore,
        transport: HttpTransport | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTPS URL")
        self.base_url = base_url.rstrip("/")
        self.service_credential = service_credential
        self.signer = signer
        self.event_state_store = event_state_store
        self.transport = transport or HttpsJsonTransport()
        self.now_factory = now_factory

    def activate(
        self,
        *,
        device_binding_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "activate_monitoring_request.v1"
        }
        if device_binding_id is not None:
            payload["device_binding_id"] = device_binding_id
        return self._command(
            f"/api/v1/subjects/{self.signer.subject_id}/monitoring/activate",
            payload,
            idempotency_key,
        )

    def deactivate(self, *, idempotency_key: str) -> dict[str, Any]:
        return self._command(
            f"/api/v1/subjects/{self.signer.subject_id}/monitoring/deactivate",
            {"schema_version": "deactivate_monitoring_request.v1"},
            idempotency_key,
        )

    def submit_feedback(
        self,
        *,
        night_episode_id: str,
        event_at: datetime,
        idempotency_key: str,
        source_text: str | None = None,
        structured_answer: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        route_role = "elder" if self.signer.role == "elder" else "family"
        payload = {
            "schema_version": "feedback_request.v1",
            "night_episode_id": night_episode_id,
            "event_at": event_at.isoformat(),
            "source_text": source_text,
            "structured_answer": structured_answer,
        }
        return self._command(
            f"/api/v1/subjects/{self.signer.subject_id}/feedback/{route_role}",
            payload,
            idempotency_key,
        )

    def request_reanalysis(
        self,
        *,
        night_episode_id: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._command(
            f"/api/v1/subjects/{self.signer.subject_id}/night-episodes/"
            f"{night_episode_id}/reanalysis",
            {"schema_version": "reanalysis_request.v1", "reason": reason},
            idempotency_key,
        )

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/operations/{operation_id}")

    def await_operation(
        self,
        operation_id: str,
        *,
        timeout_seconds: float = 60,
        interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            operation = self.get_operation(operation_id)
            if operation["status"] in {"succeeded", "failed", "cancelled"}:
                return operation
            if time.monotonic() >= deadline:
                raise TimeoutError(f"operation {operation_id} did not finish")
            time.sleep(interval_seconds)

    def get_lifecycle(self) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/subjects/{self.signer.subject_id}/lifecycle"
        )

    def get_current_risk(self) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/v1/subjects/{self.signer.subject_id}/risk"
        )

    def list_night_episodes(self, *, limit: int = 20) -> dict[str, Any]:
        path = f"/api/v1/subjects/{self.signer.subject_id}/night-episodes"
        return self._request("GET", path, query={"limit": str(limit)})

    def get_role_view(self, night_episode_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/subjects/{self.signer.subject_id}/night-episodes/"
            f"{night_episode_id}/view",
        )

    def poll_events(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        state = self.event_state_store.load()
        subject_state = state["subjects"].setdefault(
            self.signer.subject_id,
            {"cursor": None, "event_ids": [], "snapshots": {}},
        )
        query = {"limit": str(limit)}
        if subject_state.get("cursor"):
            query["cursor"] = str(subject_state["cursor"])
        path = f"/api/v1/subjects/{self.signer.subject_id}/events"
        try:
            response = self._request("GET", path, query=query)
        except ApiError as exc:
            if exc.code != "CURSOR_RESYNC_REQUIRED":
                raise
            tombstone = exc.payload.get("tombstone")
            subject_state["cursor"] = None
            if tombstone is not None:
                subject_state["event_ids"] = []
                subject_state["snapshots"] = {}
                subject_state["revocation"] = {
                    "event_id": tombstone["event_id"],
                    "remote_recall_possible": False,
                    "local_authorized_cache_deleted": True,
                }
                self.event_state_store.save(state)
                return ()
            self._refresh_snapshots(subject_state)
            self.event_state_store.save(state)
            response = self._request("GET", path, query={"limit": str(limit)})

        seen = set(subject_state.get("event_ids", []))
        delivered = tuple(
            event for event in response["events"] if event["event_id"] not in seen
        )
        seen.update(event["event_id"] for event in response["events"])
        subject_state["event_ids"] = sorted(seen)
        subject_state["cursor"] = response["next_cursor"]
        self.event_state_store.save(state)
        return delivered

    def _refresh_snapshots(self, subject_state: dict[str, Any]) -> None:
        snapshots: dict[str, Any] = {}
        for name, loader in (
            ("lifecycle", self.get_lifecycle),
            ("risk", self.get_current_risk),
            ("episodes", self.list_night_episodes),
        ):
            try:
                snapshots[name] = loader()
            except ApiError as exc:
                if exc.code not in {"DATA_INSUFFICIENT", "RESOURCE_NOT_FOUND"}:
                    raise
        episodes = snapshots.get("episodes", {}).get("items", [])
        if episodes:
            try:
                snapshots["role_view"] = self.get_role_view(
                    episodes[0]["night_episode_id"]
                )
            except ApiError as exc:
                if exc.code not in {
                    "DATA_INSUFFICIENT",
                    "RESOURCE_NOT_FOUND",
                    "RESULT_PENDING",
                }:
                    raise
        subject_state["snapshots"] = snapshots

    def _command(
        self,
        path: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            payload=payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        body = b"" if payload is None else _canonical_json(payload)
        assertion = self.signer.sign(
            method=method,
            path=path,
            body=body,
            now=self.now_factory(),
        )
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Authorization": f"Bearer {self.service_credential}",
            "X-Sleep-Actor-Assertion": assertion,
            "Accept": "application/json",
        }
        if body:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        response = self.transport.request(
            method=method,
            url=url,
            headers=headers,
            body=body,
        )
        decoded: dict[str, Any] = (
            {} if not response.body else json.loads(response.body)
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise ApiError(response.status_code, decoded)
        return decoded


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


__all__ = [
    "ApiError",
    "Ed25519ActorSigner",
    "FileEventStateStore",
    "HttpResponse",
    "HttpsJsonTransport",
    "SleepApiV1Client",
]
