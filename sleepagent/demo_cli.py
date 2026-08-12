"""HTTP-only demo controller and external backend verifier CLI."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from sleepagent.simulation.seed_registry import (
    ReplaySeedDefinition,
    ReplaySeedRegistryError,
    load_replay_seed_registry,
)


MAX_RESPONSE_BYTES = 1_048_576
PRODUCT_DEMO_SCENARIOS = (
    "normal-one-night",
    "worsening-vital-trend",
    "urgent-zero-model",
)


class DemoCliError(RuntimeError):
    pass


@dataclass
class DemoHttpClient:
    base_url: str
    demo_token: str
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url += "?" + urlencode(query)
        body = None
        headers = {
            "Accept": "application/json",
            "X-Demo-Controller-Token": self.demo_token,
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            raise DemoCliError(
                f"backend returned HTTP {exc.code}: {_safe_error_code(raw)}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DemoCliError(
                f"backend request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DemoCliError("backend response exceeded verifier limit")
        if status < 200 or status >= 300:
            raise DemoCliError(f"backend returned HTTP {status}")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoCliError("backend returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DemoCliError("backend response must be a JSON object")
        return value


@dataclass(frozen=True, slots=True)
class ActorAssertionSigner:
    private_key: Ed25519PrivateKey
    issuer: str = "sleepagent-bff-v1"
    audience: str = "sleepagent-backend"
    key_id: str = "primary"

    @classmethod
    def from_private_file(
        cls,
        path: str,
        *,
        issuer: str,
        audience: str,
        key_id: str,
    ) -> "ActorAssertionSigner":
        key_path = Path(path)
        if not key_path.is_absolute() or key_path.is_symlink():
            raise DemoCliError(
                "actor private-key path must be absolute and not a symlink"
            )
        try:
            metadata = key_path.stat()
            raw = key_path.read_bytes()
        except OSError as exc:
            raise DemoCliError("actor private key is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DemoCliError("actor private key must be a 0600 regular file")
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as exc:
            raise DemoCliError("actor private key is invalid PEM") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise DemoCliError("actor private key must be Ed25519")
        return cls(
            private_key=key,
            issuer=issuer,
            audience=audience,
            key_id=key_id,
        )

    def sign(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: str,
        scope: tuple[str, ...],
        method: str,
        path: str,
        body: bytes = b"",
        authorization_epoch: int = 1,
        privacy_epoch: int = 1,
        retrieval_policy_epoch: int = 1,
    ) -> str:
        now = int(time.time())
        header = {"alg": "EdDSA", "kid": self.key_id, "typ": "JWT"}
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "jti": str(uuid4()),
            "actor_id": actor_id,
            "subject_id": subject_id,
            "role": role,
            "scope": list(scope),
            "iat": now,
            "exp": now + 120,
            "nonce": f"nonce-{uuid4()}",
            "method": method.upper(),
            "path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "authorization_epoch": authorization_epoch,
            "privacy_epoch": privacy_epoch,
            "retrieval_policy_epoch": retrieval_policy_epoch,
        }
        protected = _b64url(_canonical_json(header))
        payload = _b64url(_canonical_json(claims))
        signing_input = f"{protected}.{payload}".encode("ascii")
        signature = _b64url(self.private_key.sign(signing_input))
        return f"{protected}.{payload}.{signature}"


@dataclass
class ProductHttpClient:
    base_url: str
    service_credential: str
    signer: ActorAssertionSigner
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen
    authorization_epoch: int = 1
    privacy_epoch: int = 1
    retrieval_policy_epoch: int = 1

    def today(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: str,
    ) -> dict[str, Any]:
        value, _, _ = self.signed_json(
            method="GET",
            path="/product/sleep/today",
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            scope=("product:sleep:today:read",),
            accepted_statuses=(200,),
        )
        return value

    def night_episodes(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: str,
    ) -> tuple[dict[str, Any], Mapping[str, str]]:
        value, headers, _ = self.signed_json(
            method="GET",
            path=f"/api/v1/subjects/{subject_id}/night-episodes",
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            scope=("sleep:episode:read",),
            accepted_statuses=(200,),
        )
        return value, headers

    def current_risk(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: str,
    ) -> tuple[dict[str, Any], Mapping[str, str], int]:
        value, headers, status = self.signed_json(
            method="GET",
            path=f"/api/v1/subjects/{subject_id}/risk",
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            scope=("sleep:risk:read",),
            accepted_statuses=(200, 403, 404),
        )
        return value, headers, status

    def read_model(
        self,
        *,
        kind: str,
        actor_id: str,
        subject_id: str,
        role: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"trends", "care", "records"}:
            raise DemoCliError("unknown Product read model")
        query = {"limit": str(limit)}
        if cursor is not None:
            query["cursor"] = cursor
        value, _, _ = self.signed_json(
            method="GET",
            path=f"/product/sleep/{kind}",
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            scope=(f"product:sleep:{kind}:read",),
            query=query,
            accepted_statuses=(200,),
        )
        return value

    def signed_json(
        self,
        *,
        method: str,
        path: str,
        actor_id: str,
        subject_id: str,
        role: str,
        scope: tuple[str, ...],
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        query: Mapping[str, str] | None = None,
        accepted_statuses: tuple[int, ...] = (200,),
        assertion_override: str | None = None,
    ) -> tuple[dict[str, Any], Mapping[str, str], int]:
        body = b"" if payload is None else _canonical_json(payload)
        assertion = assertion_override or self.signer.sign(
            actor_id=actor_id,
            subject_id=subject_id,
            role=role,
            scope=scope,
            method=method,
            path=path,
            body=body,
            authorization_epoch=self.authorization_epoch,
            privacy_epoch=self.privacy_epoch,
            retrieval_policy_epoch=self.retrieval_policy_epoch,
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.service_credential}",
            "X-Sleep-Actor-Assertion": assertion,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            data=body if payload is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
                response_headers = dict(response.headers.items())
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = int(exc.code)
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            if status not in accepted_statuses:
                raise DemoCliError(
                    f"Product API returned HTTP {status}: {_safe_error_code(raw)}"
                ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DemoCliError(
                f"Product API request failed: {type(exc).__name__}"
            ) from exc
        if status not in accepted_statuses or len(raw) > MAX_RESPONSE_BYTES:
            raise DemoCliError("Product API returned an invalid response")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoCliError("Product API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DemoCliError("Product response must be a JSON object")
        return value, response_headers, status


def verify_backend(
    client: DemoHttpClient,
    *,
    scenario_id: str,
    model: str,
    wait_seconds: float,
    product_client: ProductHttpClient | Any | None = None,
    subject_id: str = "synthetic-subject-normal-001",
    actor_ids: Mapping[str, str] | None = None,
    mode: str = "clean",
    restart_worker: Callable[[], None] | None = None,
    include_stage2: bool = False,
    restart_stage2_worker: bool = False,
    include_stage3: bool = False,
    include_stage4: bool = False,
    stage3_expected_night_count: int = 1,
    stage3_advance_seconds: int = 0,
) -> dict[str, Any]:
    if model != "deterministic":
        raise DemoCliError("only deterministic verification is supported")
    if mode not in {"clean", "restart-worker-before-product-commit"}:
        raise DemoCliError("unsupported backend verification mode")
    if mode == "restart-worker-before-product-commit" and restart_worker is None:
        raise DemoCliError("restart verification requires a Worker supervisor")
    live = client.request("GET", "/livez")
    if live.get("status") != "alive":
        raise DemoCliError("backend liveness check failed")
    accepted = client.request(
        "POST",
        "/demo/v1/seed",
        payload={
            "artifact_family": "canonical-replay-fixtures",
            "scenario_id": scenario_id,
            "batch_size": 100,
        },
        idempotency_key=f"verify-{scenario_id}-{uuid4()}",
    )
    _require_replay_watermark(accepted)
    # A fresh replay database has no active scenario clock until the first
    # allowlisted seed reserves its namespace/generation.  Attest the clock
    # immediately after that short reservation transaction.
    clock = client.request("GET", "/demo/v1/clock")
    _require_replay_watermark(clock)
    operation_id = str(accepted.get("operation_id") or "")
    if not operation_id:
        raise DemoCliError("seed response omitted operation_id")
    deadline = time.monotonic() + wait_seconds
    terminal_entry: Mapping[str, Any] | None = None
    terminal_operation: Mapping[str, Any] | None = None
    restart_performed = False
    while True:
        trace = client.request(
            "GET",
            "/demo/v1/trace",
            query={"operation_id": operation_id, "limit": "200"},
        )
        _require_replay_watermark(trace)
        for entry in trace.get("entries", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("operation_id") != operation_id:
                continue
            if (
                mode == "restart-worker-before-product-commit"
                and not restart_performed
                and entry.get("state") == "waiting_product"
            ):
                assert restart_worker is not None
                restart_worker()
                restart_performed = True
            if entry.get("state") in {
                "succeeded",
                "failed",
                "blocked",
                "reconciliation_required",
            }:
                terminal_entry = entry
        operation = client.request("GET", f"/demo/v1/operations/{operation_id}")
        _require_replay_watermark(operation)
        if operation.get("state") in {
            "succeeded",
            "failed",
            "blocked",
            "reconciliation_required",
        }:
            terminal_operation = operation
        if terminal_entry is not None and terminal_operation is not None:
            break
        if time.monotonic() >= deadline:
            raise DemoCliError("seed operation did not reach a terminal state")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    if (
        terminal_entry.get("state") != "succeeded"
        or terminal_operation.get("state") != "succeeded"
    ):
        raise DemoCliError(
            f"seed operation ended as {terminal_entry.get('state', 'unknown')}"
        )
    if mode == "restart-worker-before-product-commit" and not restart_performed:
        raise DemoCliError("root completed without the required Worker restart")
    result = terminal_operation.get("result")
    if not isinstance(result, dict):
        raise DemoCliError("succeeded root omitted its committed result")
    if result.get("subject_ref") != subject_id:
        raise DemoCliError("root result subject does not match verifier scope")
    analysis_revision_id = str(result.get("analysis_revision_id") or "")
    if not analysis_revision_id:
        raise DemoCliError("root result omitted analysis_revision_id")
    public_projections: dict[str, str] = {}
    stage2_result: dict[str, Any] | None = None
    stage3_result: dict[str, Any] | None = None
    stage4_result: dict[str, Any] | None = None
    if product_client is not None:
        selected_actor_ids = actor_ids or {
            "elder": "replay-elder-normal-001",
            "family": "replay-family-normal-001",
            "doctor": "replay-doctor-normal-001",
        }
        if set(selected_actor_ids) != {"elder", "family", "doctor"}:
            raise DemoCliError("verifier requires exactly three Product actors")
        night_episodes, episode_headers = product_client.night_episodes(
            actor_id=selected_actor_ids["elder"],
            subject_id=subject_id,
            role="elder",
        )
        _verify_episode_page(
            night_episodes,
            headers=episode_headers,
            subject_id=subject_id,
            night_episode_id=str(result.get("night_episode_id") or ""),
            night_episode_revision_id=str(
                result.get("night_episode_revision_id") or ""
            ),
        )
        for role in ("elder", "family", "doctor"):
            today = product_client.today(
                actor_id=selected_actor_ids[role],
                subject_id=subject_id,
                role=role,
            )
            _verify_today_projection(
                today,
                role=role,
                subject_id=subject_id,
                analysis_revision_id=analysis_revision_id,
            )
            public_projections[role] = str(today["projection_id"])
        if include_stage2:
            stage2_result = verify_stage2_backend(
                product_client,
                subject_id=subject_id,
                actor_ids=selected_actor_ids,
                night_episode_id=str(result.get("night_episode_id") or ""),
                night_episode_revision_id=str(
                    result.get("night_episode_revision_id") or ""
                ),
                wait_seconds=wait_seconds,
                restart_worker=(
                    restart_worker if restart_stage2_worker else None
                ),
            )
        if include_stage4:
            if stage2_result is None:
                raise DemoCliError("Stage-4 verification requires Stage-2 evidence")
            stage4_result = verify_effects_reconciliation_backend(
                product_client,
                subject_id=subject_id,
                actor_ids=selected_actor_ids,
                stage2_result=stage2_result,
                wait_seconds=wait_seconds,
            )
        if include_stage3:
            stage3_result = verify_stage3_backend(
                client,
                product_client,
                subject_id=subject_id,
                actor_ids=selected_actor_ids,
                expected_night_count=stage3_expected_night_count,
                advance_seconds=stage3_advance_seconds,
                wait_seconds=wait_seconds,
            )
    return {
        "schema_version": "backend_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "model": model,
        "mode": mode,
        "scenario_id": scenario_id,
        "operation_id": operation_id,
        "night_episode_id": str(result.get("night_episode_id") or ""),
        "night_episode_revision_id": str(
            result.get("night_episode_revision_id") or ""
        ),
        "analysis_revision_id": analysis_revision_id,
        "role_projection_ids": public_projections,
        "stage2": stage2_result,
        "stage3": stage3_result,
        "stage4": stage4_result,
    }


def show_product_demo(
    demo_client: DemoHttpClient | Any,
    product_client: ProductHttpClient | Any,
    *,
    scenario_id: str,
    wait_seconds: float,
    include_trace: bool,
) -> dict[str, Any]:
    """Run one allowlisted Product demo using public HTTP surfaces only."""

    if scenario_id not in PRODUCT_DEMO_SCENARIOS:
        raise DemoCliError(
            "show supports only " + ", ".join(PRODUCT_DEMO_SCENARIOS)
        )
    if wait_seconds <= 0:
        raise DemoCliError("show wait time must be greater than zero")
    try:
        registry = load_replay_seed_registry()
        seed = registry.lookup("canonical-replay-fixtures", scenario_id)
    except ReplaySeedRegistryError as exc:
        raise DemoCliError(f"packaged replay registry is unavailable: {exc}") from exc

    live = demo_client.request("GET", "/livez")
    if live.get("status") != "alive":
        raise DemoCliError("backend liveness check failed")
    accepted = demo_client.request(
        "POST",
        "/demo/v1/seed",
        payload={
            "artifact_family": "canonical-replay-fixtures",
            "scenario_id": scenario_id,
            "batch_size": 100,
        },
        idempotency_key=f"show-{scenario_id}-{uuid4()}",
    )
    _require_replay_watermark(accepted)
    operation_id = _required_public_text(
        accepted.get("operation_id"), "show seed Operation"
    )
    root_operation = _poll_product_demo_operation(
        demo_client,
        operation_id=operation_id,
        wait_seconds=wait_seconds,
    )
    root_state = str(root_operation.get("state") or "")
    if scenario_id == "urgent-zero-model":
        if (
            root_state != "failed"
            or root_operation.get("error_code") != "unexpected_urgent_route"
        ):
            raise DemoCliError(
                "urgent demo did not terminate on its public zero-model boundary"
            )
    elif root_state != "succeeded":
        raise DemoCliError(f"show seed Operation ended as {root_state or 'unknown'}")

    root_result = root_operation.get("result")
    if root_state == "succeeded":
        if not isinstance(root_result, Mapping):
            raise DemoCliError("successful show seed omitted its public result")
        if root_result.get("subject_ref") != seed.subject_id:
            raise DemoCliError("show seed result subject does not match replay registry")

    advance_operation: dict[str, Any] | None = None
    if seed.night_count > 1:
        advance_seconds = int(
            (seed.last_received_at - seed.scenario_clock_start).total_seconds()
        )
        if advance_seconds < 1 or advance_seconds > 604_800:
            raise DemoCliError("replay registry requires an invalid demo clock advance")
        advance_accepted = demo_client.request(
            "POST",
            "/demo/v1/advance",
            payload={"seconds": advance_seconds},
            idempotency_key=f"show-advance-{scenario_id}-{uuid4()}",
        )
        _require_replay_watermark(advance_accepted)
        advance_operation = _poll_product_demo_operation(
            demo_client,
            operation_id=_required_public_text(
                advance_accepted.get("operation_id"), "show advance Operation"
            ),
            wait_seconds=wait_seconds,
        )
        if advance_operation.get("state") != "succeeded":
            raise DemoCliError(
                "show clock advance ended as "
                f"{advance_operation.get('state', 'unknown')}"
            )
        _wait_for_product_demo_trends(
            product_client,
            seed=seed,
            wait_seconds=wait_seconds,
        )

    public = _read_product_demo_public_views(product_client, seed=seed)
    trace = (
        _read_product_demo_trace(demo_client, operation_id=operation_id)
        if include_trace
        else None
    )
    return {
        "schema_version": "terminal_product_demo.v1",
        "scenario": {
            "scenario_id": seed.scenario_id,
            "night_count": seed.night_count,
            "observation_count": seed.observation_count,
            "first_received_at": seed.first_received_at.isoformat(),
            "last_received_at": seed.last_received_at.isoformat(),
            "data_mode": "replay",
            "synthetic_non_release": True,
        },
        "root_operation": root_operation,
        "advance_operation": advance_operation,
        "public": public,
        "trace": trace,
    }


def _poll_product_demo_operation(
    client: DemoHttpClient | Any,
    *,
    operation_id: str,
    wait_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        value = client.request("GET", f"/demo/v1/operations/{operation_id}")
        _require_replay_watermark(value)
        state = str(value.get("state") or "")
        if state in {"succeeded", "failed", "blocked", "reconciliation_required"}:
            return value
        if time.monotonic() >= deadline:
            raise DemoCliError("show Operation did not reach a terminal state")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _wait_for_product_demo_trends(
    client: ProductHttpClient | Any,
    *,
    seed: ReplaySeedDefinition,
    wait_seconds: float,
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        value = client.read_model(
            kind="trends",
            actor_id=seed.actor_aliases["elder"],
            subject_id=seed.subject_id,
            role="elder",
            limit=90,
        )
        _verify_read_model_page(
            value,
            kind="trends",
            role="elder",
            subject_id=seed.subject_id,
        )
        if len(value["items"]) >= seed.night_count:
            return
        if time.monotonic() >= deadline:
            raise DemoCliError("Product trends did not reach the scenario night count")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _read_product_demo_public_views(
    client: ProductHttpClient | Any,
    *,
    seed: ReplaySeedDefinition,
) -> dict[str, Any]:
    episodes, episode_headers = client.night_episodes(
        actor_id=seed.actor_aliases["elder"],
        subject_id=seed.subject_id,
        role="elder",
    )
    _require_v1_replay_headers(episode_headers)
    if (
        episodes.get("schema_version") != "night_episode_page_response.v1"
        or not isinstance(episodes.get("items"), list)
        or any(
            not isinstance(item, Mapping)
            or item.get("subject_id") != seed.subject_id
            for item in episodes.get("items", ())
        )
    ):
        raise DemoCliError("NightEpisode page violated its public contract")

    today: dict[str, dict[str, Any]] = {}
    for role in ("elder", "family", "doctor"):
        value = client.today(
            actor_id=seed.actor_aliases[role],
            subject_id=seed.subject_id,
            role=role,
        )
        _verify_product_demo_today(
            value,
            role=role,
            subject_id=seed.subject_id,
        )
        today[role] = value

    care = client.read_model(
        kind="care",
        actor_id=seed.actor_aliases["elder"],
        subject_id=seed.subject_id,
        role="elder",
        limit=100,
    )
    _verify_read_model_page(
        care,
        kind="care",
        role="elder",
        subject_id=seed.subject_id,
    )

    trends: dict[str, Any] | None = None
    if seed.night_count > 1:
        trends = client.read_model(
            kind="trends",
            actor_id=seed.actor_aliases["elder"],
            subject_id=seed.subject_id,
            role="elder",
            limit=90,
        )
        _verify_read_model_page(
            trends,
            kind="trends",
            role="elder",
            subject_id=seed.subject_id,
        )

    risk, risk_headers, risk_status = client.current_risk(
        actor_id=seed.actor_aliases["elder"],
        subject_id=seed.subject_id,
        role="elder",
    )
    if risk_status == 200:
        _require_v1_replay_headers(risk_headers)
        if (
            risk.get("schema_version") != "current_risk_response.v1"
            or risk.get("subject_id") != seed.subject_id
        ):
            raise DemoCliError("current-risk response violated its public contract")
    return {
        "night_episodes": episodes,
        "today": today,
        "trends": trends,
        "care": care,
        "risk": {"http_status": risk_status, "body": risk},
    }


def _verify_product_demo_today(
    value: Mapping[str, Any],
    *,
    role: str,
    subject_id: str,
) -> None:
    _require_replay_watermark(value)
    if (
        value.get("schema_version") != "product_sleep_today.v1"
        or value.get("role") != role
        or value.get("subject_ref") != subject_id
        or value.get("state") not in {"ready", "degraded", "blocked", "no_data"}
    ):
        raise DemoCliError(f"{role} /today violated its public contract")
    content = value.get("content")
    if value.get("state") == "no_data":
        if content is not None:
            raise DemoCliError(f"{role} no-data /today exposed content")
    elif not isinstance(content, Mapping) or content.get("audience") != role:
        raise DemoCliError(f"{role} /today content violated its role contract")
    forbidden = {
        "claim_refs",
        "source_refs",
        "failure_codes",
        "product_agent_episode_id",
        "execution_mode",
        "prompt",
        "tool",
        "model_versions",
        "policy_sha256",
    }
    if forbidden.intersection(_recursive_keys(value)):
        raise DemoCliError(f"{role} /today leaked internal fields")


def _read_product_demo_trace(
    client: DemoHttpClient | Any,
    *,
    operation_id: str,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    generation: object = None
    while True:
        query = {"operation_id": operation_id, "limit": "200"}
        if cursor is not None:
            query["cursor"] = cursor
        page = client.request("GET", "/demo/v1/trace", query=query)
        _require_replay_watermark(page)
        generation = page.get("generation")
        page_entries = page.get("entries")
        if not isinstance(page_entries, list) or any(
            not isinstance(item, dict) for item in page_entries
        ):
            raise DemoCliError("Demo trace violated its public contract")
        entries.extend(page_entries)
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        cursor = _required_public_text(next_cursor, "trace cursor")
    return {
        "schema_version": "demo_trace.v1",
        "data_mode": "replay",
        "synthetic_non_release": True,
        "generation": generation,
        "entries": entries,
    }


def render_product_demo(value: Mapping[str, Any]) -> str:
    """Render already-committed public DTOs without deriving Product decisions."""

    scenario = _mapping(value.get("scenario"))
    public = _mapping(value.get("public"))
    root = _mapping(value.get("root_operation"))
    episodes = _mapping(public.get("night_episodes"))
    episode_items = _mapping_items(episodes.get("items"))
    today = _mapping(public.get("today"))
    care = _mapping(public.get("care"))
    risk_envelope = _mapping(public.get("risk"))
    risk = _mapping(risk_envelope.get("body"))

    lines = [
        "SleepAgent Terminal Product Demo",
        "================================",
        f"Scenario: {scenario.get('scenario_id', 'unavailable')}",
        "Data: synthetic replay; non-clinical; non-release",
        (
            f"Input summary: {scenario.get('night_count', 'unavailable')} night(s), "
            f"{scenario.get('observation_count', 'unavailable')} observations"
        ),
        (
            f"Observation window: {scenario.get('first_received_at', 'unavailable')} "
            f"-> {scenario.get('last_received_at', 'unavailable')}"
        ),
        "",
        "NightEpisode",
        "------------",
    ]
    if not episode_items:
        lines.append("Current public contract returned no NightEpisode.")
    for item in episode_items:
        flags = item.get("quality_flags")
        quality_flags = (
            ", ".join(str(flag) for flag in flags)
            if isinstance(flags, list)
            else "none"
        )
        lines.extend(
            [
                (
                    f"- {item.get('episode_local_date') or item.get('local_sleep_date')}: "
                    f"state={item.get('lifecycle_state', 'unavailable')}, "
                    f"quality={item.get('data_sufficiency', 'unavailable')}"
                ),
                (
                    f"  revision={item.get('current_revision_number', 'unavailable')} "
                    f"assignment={item.get('assignment_basis', 'unavailable')} "
                    f"flags={quality_flags}"
                ),
            ]
        )

    lines.extend(["", "Analysis and risk", "-----------------"])
    elder = _mapping(today.get("elder"))
    lines.append(f"Product analysis state: {elder.get('state', 'unavailable')}")
    risk_status = risk_envelope.get("http_status")
    if risk_status == 200:
        lines.extend(
            [
                f"Risk state: {risk.get('risk_state', 'unavailable')}",
                f"Data sufficiency: {risk.get('data_sufficiency', 'unavailable')}",
                "Reason codes: " + _joined(risk.get("reason_codes")),
                (
                    "Health escalation allowed: "
                    f"{risk.get('health_escalation_allowed', 'unavailable')}"
                ),
            ]
        )
    else:
        lines.append(
            "Risk detail: current public authorization/contract did not provide it "
            f"(HTTP {risk_status or 'unavailable'})."
        )

    lines.extend(["", "Evidence, Care, Safety", "----------------------"])
    doctor = _mapping(today.get("doctor"))
    doctor_content = _mapping(doctor.get("content"))
    evidence_refs = doctor_content.get("evidence_refs")
    if isinstance(evidence_refs, list) and evidence_refs:
        lines.append("Evidence references: " + ", ".join(map(str, evidence_refs)))
    elif isinstance(evidence_refs, list):
        lines.append("Evidence references: public doctor projection returned none.")
    else:
        lines.append("Evidence detail: current public contract does not provide it.")
    care_items = _mapping_items(care.get("items"))
    if care_items:
        for item in care_items:
            lines.append(
                f"Care: {item.get('record_type', 'record')} "
                f"state={item.get('state', 'unavailable')}"
            )
    else:
        lines.append("Care: no public confirmed Care records.")
    if (
        scenario.get("scenario_id") == "urgent-zero-model"
        and root.get("state") == "failed"
        and root.get("error_code") == "unexpected_urgent_route"
    ):
        lines.append(
            "Safety: deterministic urgent boundary; zero-model Product path "
            "(public root error=unexpected_urgent_route)."
        )
    else:
        lines.append("Safety detail: current public contract does not provide it.")

    lines.extend(["", "Role Product outputs", "--------------------"])
    for role in ("elder", "family", "doctor"):
        projection = _mapping(today.get(role))
        content = _mapping(projection.get("content"))
        lines.append(f"[{role}] state={projection.get('state', 'unavailable')}")
        summary = content.get("summary_text")
        notice = content.get("context_notice")
        if isinstance(summary, str) and summary:
            lines.append(f"  {summary}")
            lines.append(f"  Context: {notice or 'unavailable'}")
        else:
            lines.append("  Current public contract provides no Product output.")

    trends = public.get("trends")
    if isinstance(trends, Mapping):
        lines.extend(["", "Trends", "------"])
        trend_items = _mapping_items(trends.get("items"))
        if not trend_items:
            lines.append("No public trend points.")
        for item in reversed(trend_items):
            lines.append(
                f"- {item.get('episode_local_date', 'unavailable')}: "
                f"state={item.get('projection_state', 'unavailable')}, "
                f"sleep_window_minutes={item.get('sleep_window_minutes', 'unavailable')}"
            )

    trace = value.get("trace")
    if isinstance(trace, Mapping):
        lines.extend(["", "Public execution trace", "----------------------"])
        trace_items = _mapping_items(trace.get("entries"))
        if not trace_items:
            lines.append("No public trace entries.")
        for entry in trace_items:
            refs = [
                f"episode={entry['night_episode_revision_id']}"
                if entry.get("night_episode_revision_id")
                else "",
                f"fast_path={entry['fast_path_operation_id']}"
                if entry.get("fast_path_operation_id")
                else "",
                f"product={entry['product_operation_id']}"
                if entry.get("product_operation_id")
                else "",
                f"analysis={entry['analysis_revision_id']}"
                if entry.get("analysis_revision_id")
                else "",
            ]
            suffix = (
                " " + " ".join(item for item in refs if item)
                if any(refs)
                else ""
            )
            lines.append(
                f"{entry.get('sequence', '?'):>3} "
                f"{entry.get('event_type', 'event')} -> "
                f"{entry.get('state', 'unavailable')}{suffix}"
            )
        if (
            scenario.get("scenario_id") == "urgent-zero-model"
            and root.get("error_code") == "unexpected_urgent_route"
        ):
            lines.append(
                "Chain: replay seed -> normalization -> NightEpisode -> "
                "deterministic urgent fast path; Product Runtime and role "
                "projections were not invoked."
            )
        else:
            lines.append(
                "Chain: replay seed -> normalization -> NightEpisode -> "
                "Product Runtime -> role projections, as exposed by Demo trace."
            )

    lines.append("")
    return "\n".join(lines)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _joined(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(str(item) for item in value)


def verify_stage2_backend(
    client: ProductHttpClient | Any,
    *,
    subject_id: str,
    actor_ids: Mapping[str, str],
    night_episode_id: str,
    night_episode_revision_id: str,
    wait_seconds: float,
    restart_worker: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Prove Stage-2 commands through only signed public HTTP surfaces."""

    if set(actor_ids) != {"elder", "family", "doctor"}:
        raise DemoCliError("Stage-2 verifier requires exactly three Product actors")
    if not night_episode_id or not night_episode_revision_id:
        raise DemoCliError("Stage-2 verifier requires the committed Episode identity")
    operation_ids: dict[str, str] = {}

    care_before = client.read_model(
        kind="care",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        limit=100,
    )
    _verify_read_model_page(
        care_before,
        kind="care",
        role="elder",
        subject_id=subject_id,
    )
    if care_before.get("items"):
        raise DemoCliError("Stage-2 fresh scenario exposed Care before confirmation")

    def submit_product(
        name: str,
        *,
        path: str,
        role: str,
        scope: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[str, str]:
        key = idempotency_key or f"stage2-{name}-{uuid4()}"
        value, _, status = client.signed_json(
            method="POST",
            path=path,
            actor_id=actor_ids[role],
            subject_id=subject_id,
            role=role,
            scope=(scope,),
            payload=payload,
            idempotency_key=key,
            accepted_statuses=(202,),
        )
        _require_replay_watermark(value)
        if status != 202 or value.get("state") != "accepted":
            raise DemoCliError(f"Stage-2 {name} was not accepted")
        operation_id = str(value.get("operation_id") or "")
        if not operation_id:
            raise DemoCliError(f"Stage-2 {name} omitted operation_id")
        operation_ids[name] = operation_id
        return operation_id, key

    def poll_product(
        name: str,
        *,
        operation_id: str,
        role: str,
        expected_state: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        while True:
            value, _, status = client.signed_json(
                method="GET",
                path=f"/product/sleep/interactions/status/{operation_id}",
                actor_id=actor_ids[role],
                subject_id=subject_id,
                role=role,
                scope=("product:sleep:operation:read",),
                accepted_statuses=(200,),
            )
            _require_replay_watermark(value)
            state = str(value.get("state") or "")
            if status == 200 and state == expected_state:
                return value
            if state in {
                "failed",
                "blocked",
                "reconciliation_required",
            }:
                raise DemoCliError(f"Stage-2 {name} ended as {state}")
            if time.monotonic() >= deadline:
                raise DemoCliError(f"Stage-2 {name} did not reach {expected_state}")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def submit_sleep(
        name: str,
        *,
        path: str,
        role: str,
        scope: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        value, headers, status = client.signed_json(
            method="POST",
            path=path,
            actor_id=actor_ids[role],
            subject_id=subject_id,
            role=role,
            scope=(scope,),
            payload=payload,
            idempotency_key=f"stage2-{name}-{uuid4()}",
            accepted_statuses=(202,),
        )
        _require_v1_replay_headers(headers)
        operation_id = str(value.get("operation_id") or "")
        if status != 202 or value.get("status") != "pending" or not operation_id:
            raise DemoCliError(f"Stage-2 {name} was not accepted")
        operation_ids[name] = operation_id
        deadline = time.monotonic() + wait_seconds
        while True:
            operation, operation_headers, _ = client.signed_json(
                method="GET",
                path=f"/api/v1/operations/{operation_id}",
                actor_id=actor_ids[role],
                subject_id=subject_id,
                role=role,
                scope=("sleep:operation:read",),
                accepted_statuses=(200,),
            )
            _require_v1_replay_headers(operation_headers)
            state = str(operation.get("status") or "")
            if state == "succeeded":
                return operation
            if state in {"failed", "cancelled"}:
                raise DemoCliError(f"Stage-2 {name} ended as {state}")
            if time.monotonic() >= deadline:
                raise DemoCliError(f"Stage-2 {name} did not succeed")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    start_payload = {
        "intent": "review_last_night",
        "episode_revision_id": night_episode_revision_id,
    }
    start_id, start_key = submit_product(
        "confirm_start",
        path="/product/sleep/interactions/start",
        role="elder",
        scope="product:sleep:interaction:write",
        payload=start_payload,
    )
    replayed, _, replay_status = client.signed_json(
        method="POST",
        path="/product/sleep/interactions/start",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        scope=("product:sleep:interaction:write",),
        payload=start_payload,
        idempotency_key=start_key,
        accepted_statuses=(202,),
    )
    if replay_status != 202 or replayed.get("operation_id") != start_id:
        raise DemoCliError("Stage-2 same-key replay did not return the original Operation")
    conflict, _, conflict_status = client.signed_json(
        method="POST",
        path="/product/sleep/interactions/start",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        scope=("product:sleep:interaction:write",),
        payload={**start_payload, "intent": "different_intent"},
        idempotency_key=start_key,
        accepted_statuses=(409,),
    )
    if conflict_status != 409 or conflict.get("code") != "idempotency_conflict":
        raise DemoCliError("Stage-2 same-key/different-body did not conflict")
    started = poll_product(
        "confirm_start",
        operation_id=start_id,
        role="elder",
        expected_state="waiting_for_input",
    )
    confirm_interaction_id = _required_public_text(
        started.get("interaction_id"), "confirm interaction"
    )

    ask_id, _ = submit_product(
        "confirm_ask",
        path=f"/product/sleep/interactions/{confirm_interaction_id}/ask",
        role="elder",
        scope="product:sleep:interaction:write",
        payload={"message": "What should I focus on?"},
    )
    asked = poll_product(
        "confirm_ask",
        operation_id=ask_id,
        role="elder",
        expected_state="waiting_for_input",
    )
    answer_handle = _required_public_text(
        asked.get("answer_handle"), "answer handle"
    )
    answer_id, _ = submit_product(
        "confirm_answer",
        path=f"/product/sleep/interactions/{confirm_interaction_id}/answer",
        role="elder",
        scope="product:sleep:interaction:answer",
        payload={"answer_handle": answer_handle, "answer": "I woke feeling rested."},
    )
    if restart_worker is not None:
        restart_worker()
    answered = poll_product(
        "confirm_answer",
        operation_id=answer_id,
        role="elder",
        expected_state="waiting_for_input",
    )
    confirmation_handle = _required_public_text(
        answered.get("confirmation_handle"), "confirmation handle"
    )
    confirm_id, _ = submit_product(
        "confirm",
        path=f"/product/sleep/interactions/{confirm_interaction_id}/confirm",
        role="elder",
        scope="product:sleep:care:confirm",
        payload={"confirmation_handle": confirmation_handle, "reason_code": None},
    )
    if restart_worker is not None:
        restart_worker()
    confirmed = poll_product(
        "confirm",
        operation_id=confirm_id,
        role="elder",
        expected_state="succeeded",
    )
    for field in ("human_decision_id", "care_action_id", "delivery_intent_id"):
        _required_public_text(confirmed.get(field), field)
    confirmed_care_action_id = str(confirmed["care_action_id"])
    care_after_confirm = client.read_model(
        kind="care",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        limit=100,
    )
    _verify_confirmed_care_projection(
        care_after_confirm,
        subject_id=subject_id,
        care_action_id=confirmed_care_action_id,
        interaction_id=confirm_interaction_id,
    )

    decline_start_id, _ = submit_product(
        "decline_start",
        path="/product/sleep/interactions/start",
        role="elder",
        scope="product:sleep:interaction:write",
        payload={
            "intent": "decline_example",
            "episode_revision_id": night_episode_revision_id,
        },
    )
    decline_started = poll_product(
        "decline_start",
        operation_id=decline_start_id,
        role="elder",
        expected_state="waiting_for_input",
    )
    decline_interaction_id = _required_public_text(
        decline_started.get("interaction_id"), "decline interaction"
    )
    decline_answer_id, _ = submit_product(
        "decline_answer",
        path=f"/product/sleep/interactions/{decline_interaction_id}/answer",
        role="elder",
        scope="product:sleep:interaction:answer",
        payload={
            "answer_handle": _required_public_text(
                decline_started.get("answer_handle"), "decline answer handle"
            ),
            "answer": "Please show the proposed action.",
        },
    )
    decline_answered = poll_product(
        "decline_answer",
        operation_id=decline_answer_id,
        role="elder",
        expected_state="waiting_for_input",
    )
    decline_id, _ = submit_product(
        "decline",
        path=f"/product/sleep/interactions/{decline_interaction_id}/decline",
        role="elder",
        scope="product:sleep:care:confirm",
        payload={
            "confirmation_handle": _required_public_text(
                decline_answered.get("confirmation_handle"),
                "decline confirmation handle",
            ),
            "reason_code": "not_now",
        },
    )
    declined = poll_product(
        "decline",
        operation_id=decline_id,
        role="elder",
        expected_state="succeeded",
    )
    if declined.get("care_action_id") is not None or declined.get(
        "delivery_intent_id"
    ) is not None:
        raise DemoCliError("Stage-2 decline created a Care or delivery effect")
    care_after_decline = client.read_model(
        kind="care",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        limit=100,
    )
    _verify_confirmed_care_projection(
        care_after_decline,
        subject_id=subject_id,
        care_action_id=confirmed_care_action_id,
        interaction_id=confirm_interaction_id,
    )
    if any(
        isinstance(item, Mapping)
        and item.get("interaction_id") == decline_interaction_id
        for item in care_after_decline.get("items", [])
    ):
        raise DemoCliError("Stage-2 declined candidate leaked into /care")

    event_at = datetime.now(tz=timezone.utc).isoformat()
    product_feedback_id, _ = submit_product(
        "product_feedback",
        path="/product/sleep/interactions/feedback",
        role="elder",
        scope="product:sleep:feedback:write",
        payload={
            "interaction_id": None,
            "episode_revision_id": night_episode_revision_id,
            "feedback": "The summary matched how I felt.",
            "event_at": event_at,
        },
    )
    product_feedback = poll_product(
        "product_feedback",
        operation_id=product_feedback_id,
        role="elder",
        expected_state="succeeded",
    )
    _required_public_text(
        product_feedback.get("product_operation_id"), "Product feedback child"
    )

    sleep_prefix = f"/api/v1/subjects/{subject_id}"
    submit_sleep(
        "monitoring_activate",
        path=f"{sleep_prefix}/monitoring/activate",
        role="elder",
        scope="sleep:monitoring:write",
        payload={
            "schema_version": "activate_monitoring_request.v1",
            "device_binding_id": None,
            "occurred_at": None,
        },
    )
    submit_sleep(
        "monitoring_deactivate",
        path=f"{sleep_prefix}/monitoring/deactivate",
        role="elder",
        scope="sleep:monitoring:write",
        payload={
            "schema_version": "deactivate_monitoring_request.v1",
            "occurred_at": None,
        },
    )
    submit_sleep(
        "elder_feedback",
        path=f"{sleep_prefix}/feedback/elder",
        role="elder",
        scope="sleep:feedback:self",
        payload={
            "schema_version": "feedback_request.v1",
            "night_episode_id": night_episode_id,
            "event_at": event_at,
            "source_text": "I felt rested after waking.",
            "structured_answer": None,
        },
    )
    submit_sleep(
        "family_feedback",
        path=f"{sleep_prefix}/feedback/family",
        role="family",
        scope="sleep:feedback:family",
        payload={
            "schema_version": "feedback_request.v1",
            "night_episode_id": night_episode_id,
            "event_at": event_at,
            "source_text": "The morning routine looked normal.",
            "structured_answer": None,
        },
    )
    submit_sleep(
        "explicit_reanalysis",
        path=f"{sleep_prefix}/night-episodes/{night_episode_id}/reanalysis",
        role="doctor",
        scope="sleep:reanalysis:write",
        payload={
            "schema_version": "reanalysis_request.v1",
            "reason": "verify exact current Episode revision",
        },
    )

    return {
        "schema_version": "backend_stage2_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "confirm_interaction_id": confirm_interaction_id,
        "decline_interaction_id": decline_interaction_id,
        "care_action_id": confirmed_care_action_id,
        "delivery_intent_id": str(confirmed["delivery_intent_id"]),
        "operation_ids": operation_ids,
    }


def verify_effects_reconciliation_backend(
    client: ProductHttpClient | Any,
    *,
    subject_id: str,
    actor_ids: Mapping[str, str],
    stage2_result: Mapping[str, Any],
    wait_seconds: float,
) -> dict[str, Any]:
    """Prove the confirmed delivery effect through the public Care projection."""

    if set(actor_ids) != {"elder", "family", "doctor"}:
        raise DemoCliError("Stage-4 verifier requires exactly three Product actors")
    care_action_id = _required_public_text(
        stage2_result.get("care_action_id"), "Stage-4 CareAction"
    )
    delivery_intent_id = _required_public_text(
        stage2_result.get("delivery_intent_id"), "Stage-4 delivery intent"
    )
    confirm_interaction_id = _required_public_text(
        stage2_result.get("confirm_interaction_id"), "Stage-4 confirm interaction"
    )
    decline_interaction_id = _required_public_text(
        stage2_result.get("decline_interaction_id"), "Stage-4 decline interaction"
    )
    deadline = time.monotonic() + wait_seconds
    while True:
        care = client.read_model(
            kind="care",
            actor_id=actor_ids["elder"],
            subject_id=subject_id,
            role="elder",
            limit=100,
        )
        _verify_read_model_page(
            care,
            kind="care",
            role="elder",
            subject_id=subject_id,
        )
        active_actions = [
            item
            for item in care["items"]
            if isinstance(item, Mapping)
            and item.get("record_type") == "care_action"
            and item.get("care_action_id") == care_action_id
            and item.get("interaction_id") == confirm_interaction_id
            and item.get("state") == "active"
        ]
        declined_actions = [
            item
            for item in care["items"]
            if isinstance(item, Mapping)
            and item.get("record_type") == "care_action"
            and item.get("interaction_id") == decline_interaction_id
        ]
        if len(active_actions) == 1 and not declined_actions:
            break
        if declined_actions:
            raise DemoCliError("Stage-4 decline created a Care delivery effect")
        if time.monotonic() >= deadline:
            raise DemoCliError("Stage-4 delivery effect was not publicly active")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return {
        "schema_version": "backend_stage4_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "care_action_id": care_action_id,
        "delivery_intent_id": delivery_intent_id,
        "public_care_state": "active",
    }


def verify_stage3_backend(
    demo_client: DemoHttpClient | Any,
    product_client: ProductHttpClient | Any,
    *,
    subject_id: str,
    actor_ids: Mapping[str, str],
    expected_night_count: int,
    advance_seconds: int,
    wait_seconds: float,
) -> dict[str, Any]:
    """Prove dedicated read models and durable ScenarioClock via public HTTP."""

    if set(actor_ids) != {"elder", "family", "doctor"}:
        raise DemoCliError("Stage-3 verifier requires exactly three Product actors")
    if expected_night_count < 1 or expected_night_count > 15:
        raise DemoCliError("Stage-3 expected night count is out of bounds")
    if advance_seconds < 0 or advance_seconds > 604_800:
        raise DemoCliError("Stage-3 advance seconds are out of bounds")

    advance_operation_id: str | None = None
    if advance_seconds:
        clock_before = demo_client.request("GET", "/demo/v1/clock")
        _require_replay_watermark(clock_before)
        key = f"stage3-advance-{uuid4()}"
        payload = {"seconds": advance_seconds}
        accepted = demo_client.request(
            "POST",
            "/demo/v1/advance",
            payload=payload,
            idempotency_key=key,
        )
        replayed = demo_client.request(
            "POST",
            "/demo/v1/advance",
            payload=payload,
            idempotency_key=key,
        )
        _require_replay_watermark(accepted)
        _require_replay_watermark(replayed)
        advance_operation_id = _required_public_text(
            accepted.get("operation_id"), "advance Operation"
        )
        if replayed.get("operation_id") != advance_operation_id:
            raise DemoCliError("Stage-3 advance replay changed Operation identity")
        advance_result = _poll_demo_operation(
            demo_client,
            operation_id=advance_operation_id,
            wait_seconds=wait_seconds,
        )
        clock_after = demo_client.request("GET", "/demo/v1/clock")
        _require_replay_watermark(clock_after)
        result = advance_result.get("result")
        if (
            not isinstance(result, Mapping)
            or result.get("generation") != accepted.get("generation")
            or clock_after.get("generation") != clock_before.get("generation")
            or clock_after.get("scenario_time") != result.get("scenario_time")
        ):
            raise DemoCliError("Stage-3 committed ScenarioClock does not match receipt")

    deadline = time.monotonic() + wait_seconds
    pages: dict[str, dict[str, Any]] = {}
    while True:
        pages = {
            kind: product_client.read_model(
                kind=kind,
                actor_id=actor_ids["elder"],
                subject_id=subject_id,
                role="elder",
                limit=100 if kind != "trends" else 90,
            )
            for kind in ("trends", "records", "care")
        }
        for kind, page in pages.items():
            _verify_read_model_page(
                page,
                kind=kind,
                role="elder",
                subject_id=subject_id,
            )
        if (
            len(pages["trends"]["items"]) >= expected_night_count
            and len(pages["records"]["items"]) >= expected_night_count
        ):
            break
        if time.monotonic() >= deadline:
            raise DemoCliError(
                "Stage-3 read models did not reach the expected night count"
            )
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    cursor_proofs: dict[str, str] = {}
    if expected_night_count > 1:
        for kind in ("trends", "records"):
            first = product_client.read_model(
                kind=kind,
                actor_id=actor_ids["elder"],
                subject_id=subject_id,
                role="elder",
                limit=1,
            )
            _verify_read_model_page(
                first,
                kind=kind,
                role="elder",
                subject_id=subject_id,
            )
            cursor = _required_public_text(
                first.get("next_cursor"), f"{kind} cursor"
            )
            second = product_client.read_model(
                kind=kind,
                actor_id=actor_ids["elder"],
                subject_id=subject_id,
                role="elder",
                limit=1,
                cursor=cursor,
            )
            _verify_read_model_page(
                second,
                kind=kind,
                role="elder",
                subject_id=subject_id,
            )
            if not second["items"] or second["items"][0] == first["items"][0]:
                raise DemoCliError(f"Stage-3 {kind} cursor repeated its first item")
            cursor_proofs[kind] = cursor

    return {
        "schema_version": "backend_stage3_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "expected_night_count": expected_night_count,
        "trend_count": len(pages["trends"]["items"]),
        "record_count": len(pages["records"]["items"]),
        "care_count": len(pages["care"]["items"]),
        "advance_operation_id": advance_operation_id,
        "cursor_kinds": sorted(cursor_proofs),
    }


def verify_bounded_retention_backend(
    demo_client: DemoHttpClient | Any,
    product_client: ProductHttpClient,
    *,
    scenario_id: str,
    subject_id: str,
    actor_ids: Mapping[str, str],
    wait_seconds: float,
) -> dict[str, Any]:
    """Prove generation-fenced forget through only public HTTP surfaces."""

    baseline = verify_backend(
        demo_client,
        scenario_id=scenario_id,
        model="deterministic",
        wait_seconds=wait_seconds,
        product_client=product_client,
        subject_id=subject_id,
        actor_ids=actor_ids,
        include_stage3=True,
        stage3_expected_night_count=4,
        stage3_advance_seconds=604_800,
    )
    first_page = product_client.read_model(
        kind="records",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        limit=1,
    )
    old_cursor = _required_public_text(
        first_page.get("next_cursor"), "pre-reset records cursor"
    )
    old_assertion = product_client.signer.sign(
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        scope=("product:sleep:today:read",),
        method="GET",
        path="/product/sleep/today",
        authorization_epoch=product_client.authorization_epoch,
        privacy_epoch=product_client.privacy_epoch,
        retrieval_policy_epoch=product_client.retrieval_policy_epoch,
    )
    reset_key = f"stage5-reset-{uuid4()}"
    reset_payload = {"confirmation": "reset-replay-generation"}
    accepted = demo_client.request(
        "POST", "/demo/v1/reset", payload=reset_payload,
        idempotency_key=reset_key,
    )
    replayed = demo_client.request(
        "POST", "/demo/v1/reset", payload=reset_payload,
        idempotency_key=reset_key,
    )
    _require_replay_watermark(accepted)
    _require_replay_watermark(replayed)
    reset_operation_id = _required_public_text(
        accepted.get("operation_id"), "reset Operation"
    )
    if replayed.get("operation_id") != reset_operation_id:
        raise DemoCliError("reset idempotency replay changed root identity")
    terminal = _poll_demo_operation(
        demo_client,
        operation_id=reset_operation_id,
        wait_seconds=wait_seconds,
    )
    result = terminal.get("result")
    if not isinstance(result, Mapping):
        raise DemoCliError("reset root omitted its final receipt")
    receipt = result.get("receipt")
    if not isinstance(receipt, Mapping):
        raise DemoCliError("reset result omitted the allowlisted receipt")
    epochs = receipt.get("authority_epochs")
    if not isinstance(epochs, Mapping):
        raise DemoCliError("reset receipt omitted target authority epochs")

    stale, _, stale_status = product_client.signed_json(
        method="GET",
        path="/product/sleep/today",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        scope=("product:sleep:today:read",),
        accepted_statuses=(403,),
        assertion_override=old_assertion,
    )
    if stale_status != 403 or stale.get("code") != "stale_actor_assertion":
        raise DemoCliError("pre-reset actor assertion was not epoch-fenced")

    product_client.authorization_epoch = int(epochs["authorization_epoch"])
    product_client.privacy_epoch = int(epochs["privacy_epoch"])
    product_client.retrieval_policy_epoch = int(
        epochs["retrieval_policy_epoch"]
    )
    cursor_error, _, cursor_status = product_client.signed_json(
        method="GET",
        path="/product/sleep/records",
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
        scope=("product:sleep:records:read",),
        query={"limit": "1", "cursor": old_cursor},
        accepted_statuses=(400, 403, 409),
    )
    if cursor_status not in {400, 403, 409} or cursor_error.get("code") not in {
        "cursor_resync_required",
        "generation_fenced",
        "stale_actor_assertion",
    }:
        raise DemoCliError("pre-reset Product cursor was not generation-fenced")
    today = product_client.today(
        actor_id=actor_ids["elder"],
        subject_id=subject_id,
        role="elder",
    )
    _require_replay_watermark(today)
    if today.get("state") != "no_data":
        raise DemoCliError("new reset generation exposed an old Product projection")
    outcomes = receipt.get("domain_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        raise DemoCliError("reset receipt omitted retention-domain outcomes")
    if any(
        isinstance(item, Mapping)
        and set(item).intersection(
            {"subject_id", "raw_ingress_record_id", "encrypted_payload", "plaintext"}
        )
        for item in outcomes
    ):
        raise DemoCliError("reset receipt leaked disallowed retention fields")
    return {
        "schema_version": "backend_stage5_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "baseline_operation_id": baseline["operation_id"],
        "reset_operation_id": reset_operation_id,
        "source_generation": receipt.get("source_generation"),
        "target_generation": receipt.get("target_generation"),
        "domain_outcome_count": len(outcomes),
        "old_assertion_fenced": True,
        "old_cursor_fenced": True,
        "new_generation_today_state": "no_data",
    }


def verify_abnormal_backend(
    client: DemoHttpClient | Any,
    *,
    scenario_id: str,
    expected_error_code: str,
    wait_seconds: float,
    product_client: ProductHttpClient | Any | None = None,
    subject_id: str,
    actor_id: str,
) -> dict[str, Any]:
    """Prove an abnormal facts-only route without internal or DB access."""

    accepted = client.request(
        "POST",
        "/demo/v1/seed",
        payload={
            "artifact_family": "canonical-replay-fixtures",
            "scenario_id": scenario_id,
            "batch_size": 100,
        },
        idempotency_key=f"verify-abnormal-{scenario_id}-{uuid4()}",
    )
    _require_replay_watermark(accepted)
    operation_id = _required_public_text(
        accepted.get("operation_id"), "abnormal root Operation"
    )
    operation = _poll_demo_operation(
        client,
        operation_id=operation_id,
        wait_seconds=wait_seconds,
        expected_state="failed",
    )
    if operation.get("error_code") != expected_error_code:
        raise DemoCliError(
            "abnormal root ended with an unexpected terminal code"
        )
    trace = client.request(
        "GET",
        "/demo/v1/trace",
        query={"operation_id": operation_id, "limit": "200"},
    )
    _require_replay_watermark(trace)
    entries = trace.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DemoCliError("abnormal root omitted its public causation trace")
    if any(
        isinstance(entry, Mapping)
        and (
            entry.get("product_operation_id") is not None
            or entry.get("analysis_revision_id") is not None
        )
        for entry in entries
    ):
        raise DemoCliError("abnormal zero-Product route crossed into Product Runtime")
    if expected_error_code == "unexpected_urgent_route" and not any(
        isinstance(entry, Mapping) and entry.get("fast_path_operation_id")
        for entry in entries
    ):
        raise DemoCliError("urgent route omitted its committed fast-path evidence")
    if product_client is not None:
        today = product_client.today(
            actor_id=actor_id,
            subject_id=subject_id,
            role="elder",
        )
        _require_replay_watermark(today)
        if today.get("state") != "no_data":
            raise DemoCliError("abnormal zero-Product route exposed /today data")
    return {
        "schema_version": "backend_abnormal_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "scenario_id": scenario_id,
        "operation_id": operation_id,
        "error_code": expected_error_code,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sleepagent-demo")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SLEEPAGENT_DEMO_BASE_URL", "http://127.0.0.1:18001"),
    )
    parser.add_argument(
        "--demo-token",
        default=os.environ.get("SLEEPAGENT_DEMO_CONTROLLER_TOKEN", ""),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify")
    verify_subcommands = verify.add_subparsers(dest="target", required=True)
    backend = verify_subcommands.add_parser("backend")
    backend.add_argument("--model", choices=("deterministic",), default="deterministic")
    backend.add_argument(
        "--mode",
        choices=("clean", "restart-worker-before-product-commit"),
        default="clean",
    )
    backend.add_argument("--scenario", default="normal-one-night")
    backend.add_argument("--wait-seconds", type=float, default=30.0)
    backend.add_argument(
        "--stage2",
        action="store_true",
        help="also prove public commands and Product interactions",
    )
    backend.add_argument(
        "--suite",
        choices=(
            "commands-interactions",
            "read-models-abnormal",
            "effects-reconciliation",
            "bounded-retention",
        ),
        default=None,
        help="run one locked post-slice public verification suite",
    )
    backend.add_argument(
        "--expected-error-code",
        choices=("quality_insufficient", "unexpected_urgent_route"),
        default=None,
        help="verify an abnormal facts-only root instead of a successful root",
    )
    backend.add_argument("--expected-night-count", type=int, default=1)
    backend.add_argument("--advance-seconds", type=int, default=0)
    backend.add_argument(
        "--restart-stage2-worker",
        action="store_true",
        help="restart the Worker after answer and confirm reservation",
    )
    backend.add_argument(
        "--product-base-url",
        default=os.environ.get("SLEEPAGENT_PRODUCT_BASE_URL", "http://127.0.0.1:18000"),
    )
    backend.add_argument(
        "--service-credential",
        default=os.environ.get("SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL", ""),
    )
    backend.add_argument(
        "--actor-private-key",
        default=os.environ.get("SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY", ""),
    )
    backend.add_argument("--subject-id", default="synthetic-subject-normal-001")
    backend.add_argument("--elder-actor-id", default=None)
    backend.add_argument("--family-actor-id", default=None)
    backend.add_argument("--doctor-actor-id", default=None)

    show = subcommands.add_parser(
        "show",
        help="run and present one Phase-1 Product replay scenario",
    )
    show.add_argument("scenario", choices=PRODUCT_DEMO_SCENARIOS)
    show.add_argument(
        "--trace",
        action="store_true",
        help="also show the public replay execution trace",
    )
    show.add_argument("--wait-seconds", type=float, default=60.0)
    show.add_argument(
        "--product-base-url",
        default=os.environ.get(
            "SLEEPAGENT_PRODUCT_BASE_URL", "http://127.0.0.1:18000"
        ),
    )
    show.add_argument(
        "--service-credential",
        default=os.environ.get(
            "SLEEPAGENT_DEMO_SERVICE_CREDENTIAL",
            os.environ.get("SLEEPAGENT_VERIFIER_SERVICE_CREDENTIAL", ""),
        ),
    )
    show.add_argument(
        "--actor-private-key",
        default=os.environ.get(
            "SLEEPAGENT_DEMO_ACTOR_PRIVATE_KEY",
            os.environ.get("SLEEPAGENT_VERIFIER_ACTOR_PRIVATE_KEY", ""),
        ),
    )

    seed = subcommands.add_parser("seed")
    seed.add_argument("scenario")
    seed.add_argument("--artifact-family", default="canonical-replay-fixtures")
    seed.add_argument("--batch-size", type=int, default=100)
    advance = subcommands.add_parser("advance")
    advance.add_argument("seconds", type=int)
    subcommands.add_parser("clock")
    subcommands.add_parser("trace")
    subcommands.add_parser("reset")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.demo_token:
        print("sleepagent-demo: demo controller token is required", file=sys.stderr)
        return 2
    client = DemoHttpClient(args.base_url, args.demo_token)
    try:
        if args.command == "show":
            if not args.service_credential or not args.actor_private_key:
                raise DemoCliError(
                    "show requires Product service and actor credentials"
                )
            signer = ActorAssertionSigner.from_private_file(
                args.actor_private_key,
                issuer=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_ISSUER",
                    "sleepagent-bff-v1",
                ),
                audience=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_AUDIENCE",
                    "sleepagent-backend",
                ),
                key_id=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_KEY_ID",
                    "primary",
                ),
            )
            result = show_product_demo(
                client,
                ProductHttpClient(
                    args.product_base_url,
                    args.service_credential,
                    signer,
                ),
                scenario_id=args.scenario,
                wait_seconds=args.wait_seconds,
                include_trace=args.trace,
            )
            print(render_product_demo(result), end="")
            return 0
        if args.command == "verify":
            if args.restart_stage2_worker and not (
                args.stage2
                or args.suite in {
                    "commands-interactions",
                    "effects-reconciliation",
                }
            ):
                raise DemoCliError(
                    "--restart-stage2-worker requires commands-interactions"
                )
            if args.restart_stage2_worker and args.mode == "clean":
                raise DemoCliError(
                    "--restart-stage2-worker requires a restart verification mode"
                )
            if not args.service_credential or not args.actor_private_key:
                raise DemoCliError(
                    "backend verification requires Product service and actor credentials"
                )
            signer = ActorAssertionSigner.from_private_file(
                args.actor_private_key,
                issuer=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_ISSUER",
                    "sleepagent-bff-v1",
                ),
                audience=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_AUDIENCE",
                    "sleepagent-backend",
                ),
                key_id=os.environ.get(
                    "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_KEY_ID",
                    "primary",
                ),
            )
            product_client = ProductHttpClient(
                args.product_base_url,
                args.service_credential,
                signer,
            )
            actor_ids = {
                role: explicit
                or (
                    f"replay-{role}-normal-001"
                    if args.scenario == "normal-one-night"
                    else f"replay-{role}-{args.scenario}"
                )
                for role, explicit in (
                    ("elder", args.elder_actor_id),
                    ("family", args.family_actor_id),
                    ("doctor", args.doctor_actor_id),
                )
            }
            include_stage2 = args.stage2 or args.suite in {
                "commands-interactions",
                "effects-reconciliation",
            }
            include_stage3 = args.suite == "read-models-abnormal"
            include_stage4 = args.suite == "effects-reconciliation"
            if args.restart_stage2_worker and not include_stage2:
                raise DemoCliError(
                    "--restart-stage2-worker requires commands-interactions"
                )
            if args.suite == "bounded-retention":
                result = verify_bounded_retention_backend(
                    client,
                    product_client,
                    scenario_id=args.scenario,
                    subject_id=args.subject_id,
                    actor_ids=actor_ids,
                    wait_seconds=args.wait_seconds,
                )
            elif args.expected_error_code is not None:
                if not include_stage3:
                    raise DemoCliError(
                        "--expected-error-code requires --suite read-models-abnormal"
                    )
                result = verify_abnormal_backend(
                    client,
                    scenario_id=args.scenario,
                    expected_error_code=args.expected_error_code,
                    wait_seconds=args.wait_seconds,
                    product_client=product_client,
                    subject_id=args.subject_id,
                    actor_id=actor_ids["elder"],
                )
            else:
                result = verify_backend(
                    client,
                    scenario_id=args.scenario,
                    model=args.model,
                    wait_seconds=args.wait_seconds,
                    product_client=product_client,
                    subject_id=args.subject_id,
                    actor_ids=actor_ids,
                    mode=args.mode,
                    restart_worker=(
                        _restart_worker_from_environment
                        if args.mode == "restart-worker-before-product-commit"
                        else None
                    ),
                    include_stage2=include_stage2,
                    restart_stage2_worker=args.restart_stage2_worker,
                    include_stage3=include_stage3,
                    include_stage4=include_stage4,
                    stage3_expected_night_count=args.expected_night_count,
                    stage3_advance_seconds=args.advance_seconds,
                )
        elif args.command == "seed":
            result = client.request(
                "POST",
                "/demo/v1/seed",
                payload={
                    "artifact_family": args.artifact_family,
                    "scenario_id": args.scenario,
                    "batch_size": args.batch_size,
                },
                idempotency_key=f"cli-seed-{uuid4()}",
            )
        elif args.command == "advance":
            result = client.request(
                "POST",
                "/demo/v1/advance",
                payload={"seconds": args.seconds},
                idempotency_key=f"cli-advance-{uuid4()}",
            )
        elif args.command == "clock":
            result = client.request("GET", "/demo/v1/clock")
        elif args.command == "trace":
            result = client.request("GET", "/demo/v1/trace")
        else:
            result = client.request(
                "POST",
                "/demo/v1/reset",
                payload={"confirmation": "reset-replay-generation"},
                idempotency_key=f"cli-reset-{uuid4()}",
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except DemoCliError as exc:
        print(f"sleepagent-demo: {exc}", file=sys.stderr)
        return 1


def _require_replay_watermark(value: Mapping[str, Any]) -> None:
    if value.get("data_mode") != "replay" or value.get("synthetic_non_release") is not True:
        raise DemoCliError("backend response lacks replay non-release watermark")


def _require_v1_replay_headers(headers: Mapping[str, str]) -> None:
    normalized = {key.lower(): value.lower() for key, value in headers.items()}
    if (
        normalized.get("x-sleepagent-data-mode") != "replay"
        or normalized.get("x-sleepagent-synthetic-non-release") != "true"
    ):
        raise DemoCliError("Sleep API response lacks replay non-release headers")


def _required_public_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DemoCliError(f"backend response omitted {label}")
    return value


def _poll_demo_operation(
    client: DemoHttpClient | Any,
    *,
    operation_id: str,
    wait_seconds: float,
    expected_state: str = "succeeded",
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    while True:
        value = client.request("GET", f"/demo/v1/operations/{operation_id}")
        _require_replay_watermark(value)
        state = str(value.get("state") or "")
        if state == expected_state:
            return value
        if state in {"succeeded", "failed", "blocked", "reconciliation_required"}:
            raise DemoCliError(
                f"Demo Operation ended as {state}, expected {expected_state}"
            )
        if time.monotonic() >= deadline:
            raise DemoCliError("Demo Operation did not reach its expected state")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _verify_read_model_page(
    value: Mapping[str, Any],
    *,
    kind: str,
    role: str,
    subject_id: str,
) -> None:
    expected_schema = {
        "trends": "product_sleep_trends.v1",
        "records": "product_sleep_records.v1",
        "care": "product_sleep_care.v1",
    }[kind]
    _require_replay_watermark(value)
    if (
        value.get("schema_version") != expected_schema
        or value.get("role") != role
        or value.get("subject_ref") != subject_id
        or not isinstance(value.get("items"), list)
        or (
            value.get("next_cursor") is not None
            and not isinstance(value.get("next_cursor"), str)
        )
    ):
        raise DemoCliError(f"/{kind} violated its dedicated public contract")
    forbidden = {
        "claim_refs",
        "source_refs",
        "failure_codes",
        "execution_mode",
        "prompt",
        "tool",
        "model_versions",
        "policy_sha256",
        "fact_snapshot_json",
    }
    if forbidden.intersection(_recursive_keys(value)):
        raise DemoCliError(f"/{kind} leaked internal fields")


def _verify_confirmed_care_projection(
    value: Mapping[str, Any],
    *,
    subject_id: str,
    care_action_id: str,
    interaction_id: str,
) -> None:
    _verify_read_model_page(
        value,
        kind="care",
        role="elder",
        subject_id=subject_id,
    )
    items = value["items"]
    actions = [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("record_type") == "care_action"
        and item.get("care_action_id") == care_action_id
        and item.get("interaction_id") == interaction_id
        and item.get("state") in {"confirmed_pending_delivery", "active"}
    ]
    followups = [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("record_type") == "care_followup"
        and item.get("state") in {"pending_feedback", "following_up"}
    ]
    if len(actions) != 1 or len(followups) != 1:
        raise DemoCliError(
            "confirmed CareAction/CareFollowup projection is incomplete"
        )


def _verify_today_projection(
    value: Mapping[str, Any],
    *,
    role: str,
    subject_id: str,
    analysis_revision_id: str,
) -> None:
    _require_replay_watermark(value)
    if (
        value.get("schema_version") != "product_sleep_today.v1"
        or value.get("state") != "ready"
        or value.get("role") != role
        or value.get("subject_ref") != subject_id
        or value.get("analysis_revision_id") != analysis_revision_id
        or not value.get("projection_id")
    ):
        raise DemoCliError(f"{role} /today projection violated its public contract")
    content = value.get("content")
    if not isinstance(content, dict) or content.get("audience") != role:
        raise DemoCliError(f"{role} /today content violated its role contract")
    forbidden = {
        "claim_refs",
        "source_refs",
        "failure_codes",
        "product_agent_episode_id",
        "execution_mode",
        "prompt",
        "tool",
        "model_versions",
        "policy_sha256",
    }
    if forbidden.intersection(_recursive_keys(value)):
        raise DemoCliError(f"{role} /today leaked internal fields")


def _verify_episode_page(
    value: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
    subject_id: str,
    night_episode_id: str,
    night_episode_revision_id: str,
) -> None:
    normalized_headers = {key.lower(): item for key, item in headers.items()}
    if (
        normalized_headers.get("x-sleepagent-data-mode") != "replay"
        or normalized_headers.get("x-sleepagent-synthetic-non-release") != "true"
    ):
        raise DemoCliError("Sleep API response lacks replay non-release headers")
    items = value.get("items")
    if (
        value.get("schema_version") != "night_episode_page_response.v1"
        or not isinstance(items, list)
    ):
        raise DemoCliError("Sleep API returned an invalid NightEpisode page")
    matches = [
        item
        for item in items
        if isinstance(item, Mapping)
        and item.get("night_episode_id") == night_episode_id
        and item.get("current_revision_id") == night_episode_revision_id
        and item.get("subject_id") == subject_id
        and item.get("assignment_basis") == "observed_wake"
        and item.get("lifecycle_state") in {
            "awaiting_report",
            "analyzed",
            "revised",
        }
    ]
    if len(matches) != 1:
        raise DemoCliError("Sleep API did not expose the committed root episode")


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {
            *(str(key) for key in value),
            *(
                nested
                for item in value.values()
                for nested in _recursive_keys(item)
            ),
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


def _restart_worker_from_environment() -> None:
    raw = os.environ.get("SLEEPAGENT_VERIFIER_RESTART_WORKER_COMMANDS", "")
    if not raw.strip():
        raise DemoCliError(
            "SLEEPAGENT_VERIFIER_RESTART_WORKER_COMMANDS is required"
        )
    try:
        commands = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DemoCliError("Worker restart commands must be JSON") from exc
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
            for command in commands
        )
    ):
        raise DemoCliError("Worker restart commands must be non-empty argv arrays")
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DemoCliError("Worker supervisor command failed to execute") from exc
        if completed.returncode != 0:
            executable = shlex.quote(command[0])
            raise DemoCliError(
                f"Worker supervisor command {executable} failed"
            )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _safe_error_code(raw: bytes) -> str:
    if len(raw) > MAX_RESPONSE_BYTES:
        return "response_too_large"
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_error_response"
    if isinstance(payload, dict):
        value = payload.get("code") or payload.get("detail")
        if isinstance(value, str) and len(value) <= 100:
            return value
    return "backend_error"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ActorAssertionSigner",
    "DemoCliError",
    "DemoHttpClient",
    "ProductHttpClient",
    "build_parser",
    "main",
    "verify_backend",
    "verify_abnormal_backend",
    "verify_effects_reconciliation_backend",
    "verify_stage2_backend",
    "verify_stage3_backend",
]
