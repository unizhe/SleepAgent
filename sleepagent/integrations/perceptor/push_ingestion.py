"""Production-shaped Perceptor push intake over the unified sleep-domain store."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from sleepagent.integrations.perceptor.signing import sign_parameters
from sleepagent.sleep_domain import (
    AdapterCapability,
    AdapterExecutionStatus,
    AdapterInputEnvelope,
    CandidatePromotionService,
    ControlledAdapterRegistry,
    DataMode,
    DomainNamespace,
    IdempotencyConflictError,
    IngressReplayGuard,
    ProcessingOutcome,
    ProcessingReceipt,
    ProcessingStage,
    QuarantineReason,
    RawIngressRecord,
    ReplayConflictError,
    SignatureVerificationState,
    SleepDomainRepository,
    TerminalRawQuarantine,
)


PROFILE_HEADER = "x-perceptor-compatibility-profile"
SIGNATURE_HEADER = "x-perceptor-signature"
SIGNATURE_METHOD_HEADER = "x-perceptor-signature-method"
TIMESTAMP_HEADER = "x-perceptor-timestamp"
NONCE_HEADER = "x-perceptor-nonce"
MESSAGE_ID_HEADER = "x-perceptor-message-id"
ALLOWED_SIGNATURE_ALGORITHMS = frozenset({"HMAC-SHA1", "HMAC-SHA256"})


class PerceptorPushError(RuntimeError):
    pass


class PerceptorPushConfigurationError(PerceptorPushError):
    pass


class PerceptorPushAuthenticationError(PerceptorPushError):
    pass


class PerceptorPushReplayError(PerceptorPushError):
    pass


class PerceptorPushRequestTooLarge(PerceptorPushError):
    pass


class PerceptorPushRateLimitError(PerceptorPushError):
    pass


class SignatureMode(str, Enum):
    RAW_BODY = "raw_body"
    CANONICAL_PARAMETERS = "canonical_parameters"


@dataclass(frozen=True)
class PerceptorPushCompatibilityProfile:
    profile_id: str
    provider_account_id: str
    signing_secret: str = field(repr=False)
    signature_algorithm: str
    signature_mode: SignatureMode
    signing_path: str
    append_ampersand_to_secret: bool
    timestamp_window: timedelta
    future_clock_skew: timedelta
    environment: str
    capability_status: str = "pending"

    def __post_init__(self) -> None:
        if not self.profile_id or not self.provider_account_id:
            raise PerceptorPushConfigurationError(
                "compatibility profile and provider account are required"
            )
        if not self.signing_secret:
            raise PerceptorPushConfigurationError("signing secret is required")
        if self.signature_algorithm not in ALLOWED_SIGNATURE_ALGORITHMS:
            raise PerceptorPushConfigurationError(
                "compatibility profile uses an unsupported signature algorithm"
            )
        expected = {
            SignatureMode.RAW_BODY: "HMAC-SHA256",
            SignatureMode.CANONICAL_PARAMETERS: "HMAC-SHA1",
        }[self.signature_mode]
        if self.signature_algorithm != expected:
            raise PerceptorPushConfigurationError(
                "signature mode and algorithm are inconsistent"
            )
        if self.timestamp_window <= timedelta(0):
            raise PerceptorPushConfigurationError(
                "timestamp window must be positive"
            )
        if self.future_clock_skew < timedelta(0):
            raise PerceptorPushConfigurationError(
                "future clock skew cannot be negative"
            )
        if self.capability_status != "pending":
            raise PerceptorPushConfigurationError(
                "unconfirmed push profiles must remain PENDING"
            )


@dataclass(frozen=True)
class PerceptorPushHttpLimits:
    max_body_bytes: int = 1_048_576
    max_header_count: int = 64
    max_header_bytes: int = 16_384

    def __post_init__(self) -> None:
        if min(
            self.max_body_bytes,
            self.max_header_count,
            self.max_header_bytes,
        ) <= 0:
            raise ValueError("HTTP ingress limits must be positive")


@dataclass(frozen=True)
class PerceptorRawHttpRequest:
    method: str
    path: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    client_key: str

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode("ascii")
        values = [
            value.decode("latin-1")
            for key, value in self.raw_headers
            if key.lower() == wanted
        ]
        if not values:
            return None
        if len(values) != 1:
            raise PerceptorPushAuthenticationError(
                f"duplicate security header: {name}"
            )
        return values[0].strip()


@dataclass(frozen=True)
class PerceptorPushIngestResult:
    accepted: bool
    duplicate: bool
    collision: bool
    raw_ingress_record_id: str
    work_id: str
    message_id: str | None = field(repr=False)
    event_type: str
    normalization_status: str
    compatibility_profile_id: str

    def to_response_payload(self) -> dict[str, Any]:
        return {
            "code": 200,
            "success": True,
            "message": "OK",
            "data": {
                "accepted": self.accepted,
                "duplicate": self.duplicate,
                "collision": self.collision,
                "event_type": self.event_type,
                "normalization_status": self.normalization_status,
                "compatibility_profile_id": self.compatibility_profile_id,
            },
        }


class PerceptorPushRateLimiter:
    """Small pre-parse process guard; deployment ingress remains the outer limit."""

    def __init__(self, *, requests: int, window: timedelta) -> None:
        if requests <= 0 or window <= timedelta(0):
            raise ValueError("rate limit and window must be positive")
        self.requests = requests
        self.window = window
        self._seen: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def check(self, client_key: str, now: datetime) -> None:
        with self._lock:
            threshold = now - self.window
            current = [
                item
                for item in self._seen.get(client_key, [])
                if item >= threshold
            ]
            if len(current) >= self.requests:
                raise PerceptorPushRateLimitError(
                    "Perceptor push rate limit exceeded"
                )
            current.append(now)
            self._seen[client_key] = current


class PerceptorPushIngestionService:
    def __init__(
        self,
        *,
        namespace: DomainNamespace,
        repository: SleepDomainRepository,
        registry: ControlledAdapterRegistry,
        profiles: Mapping[str, PerceptorPushCompatibilityProfile],
        http_limits: PerceptorPushHttpLimits | None = None,
        rate_limiter: PerceptorPushRateLimiter | None = None,
    ) -> None:
        if not profiles:
            raise PerceptorPushConfigurationError(
                "at least one explicit compatibility profile is required"
            )
        self.namespace = namespace
        self.repository = repository
        self.registry = registry
        self.profiles = dict(profiles)
        self.http_limits = http_limits or PerceptorPushHttpLimits()
        self.rate_limiter = rate_limiter
        self._adapter_resolution_lock = threading.Lock()

    def ingest(
        self,
        request: PerceptorRawHttpRequest,
        *,
        received_at: datetime,
    ) -> PerceptorPushIngestResult:
        self._validate_pre_parse_limits(request)
        if self.rate_limiter is not None:
            self.rate_limiter.check(request.client_key, received_at)
        profile = self._profile(request)
        request_signed_at = _parse_signed_time(
            _required_header(request, TIMESTAMP_HEADER)
        )
        if request_signed_at < received_at - profile.timestamp_window:
            raise PerceptorPushAuthenticationError(
                "Perceptor push timestamp is stale"
            )
        if request_signed_at > received_at + profile.future_clock_skew:
            raise PerceptorPushAuthenticationError(
                "Perceptor push timestamp is in the future"
            )
        nonce = _required_header(request, NONCE_HEADER)
        if len(nonce.encode("utf-8")) > 256:
            raise PerceptorPushAuthenticationError("Perceptor nonce is oversized")
        self._verify_signature(request, profile)

        payload_hash = hashlib.sha256(request.body).hexdigest()
        parsed, parse_error = _try_parse_json_object(request.body)
        try:
            message_id = (
                _opaque_text(
                    None if parsed is None else parsed.get("message_id")
                )
                or _opaque_text(
                    None if parsed is None else parsed.get("messageId")
                )
                or _opaque_text(request.header(MESSAGE_ID_HEADER))
            )
            event_type = (
                _opaque_text(None if parsed is None else parsed.get("type"))
                or "UnknownEvent"
            )
        except ValueError:
            message_id = _opaque_text(request.header(MESSAGE_ID_HEADER))
            event_type = "UnknownEvent"
            parse_error = "external_identifier_not_opaque_string"
        if message_id is not None:
            idempotency_identity = f"message-id.v1:{message_id}"
            idempotency_version = "provider-message-id.v1"
        else:
            idempotency_identity = f"payload-sha256.v1:{payload_hash}"
            idempotency_version = "full-payload-sha256.v1"
        raw_id = _stable_id(
            "raw",
            profile.provider_account_id,
            idempotency_identity,
            payload_hash,
        )
        lock = self._resolve_adapter_lock(profile, received_at=received_at)
        record = RawIngressRecord(
            raw_ingress_record_id=raw_id,
            data_mode=self.namespace.data_mode,
            provider_id="perceptor",
            provider_account_id=profile.provider_account_id,
            event_type=event_type,
            message_id=message_id,
            request_signed_at=request_signed_at,
            measurement_at=None,
            event_occurred_at=None,
            received_at=received_at,
            signature_profile=profile.profile_id,
            signature_verification=SignatureVerificationState.VERIFIED,
            idempotency_identity=idempotency_identity,
            idempotency_version=idempotency_version,
            pre_normalization_payload_sha256=payload_hash,
            encrypted_payload_reference=(
                f"db:sleep_domain_raw_inbox:{raw_id}"
            ),
            content_type=request.header("content-type") or "application/json",
            payload_size_bytes=len(request.body),
            retention_deadline=(
                received_at + self.repository.raw_payload_policy.retention_period
            ),
        )
        work_id = f"normalize:{raw_id}"
        guard = IngressReplayGuard(
            provider_id="perceptor",
            provider_account_id=profile.provider_account_id,
            compatibility_profile_id=profile.profile_id,
            nonce=nonce,
            idempotency_identity=idempotency_identity,
            pre_normalization_payload_sha256=payload_hash,
            request_signed_at=request_signed_at,
        )
        terminal_quarantine = None
        if parse_error is not None:
            terminal_quarantine = TerminalRawQuarantine(
                quarantine_id=f"quarantine:malformed:{raw_id}",
                receipt=_raw_quarantine_receipt(
                    raw_id=raw_id,
                    data_mode=self.namespace.data_mode,
                    receipt_id=f"receipt:malformed:{raw_id}",
                    reason=QuarantineReason.MALFORMED_PAYLOAD,
                    detail_code=parse_error,
                    occurred_at=received_at,
                ),
                detail={
                    "parse_error": parse_error,
                    "compatibility_profile_id": profile.profile_id,
                },
            )
        try:
            intake = self.repository.intake_raw(
                self.namespace,
                record,
                raw_payload=request.body,
                work_id=work_id,
                work_generation=1,
                work_json={
                    "adapter_resolution_lock_id": (
                        lock.adapter_resolution_lock_id
                    ),
                    "compatibility_profile_id": profile.profile_id,
                    "environment": profile.environment,
                    "ingress_channel": "push",
                },
                processing_intent_id=f"processing-intent:{raw_id}",
                processing_intent_json={
                    "event_type": "RAW_ACCEPTED",
                    "raw_headers_sha256": _headers_sha256(
                        request.raw_headers
                    ),
                },
                created_at=received_at,
                replay_guard=guard,
                terminal_quarantine=terminal_quarantine,
            )
        except IdempotencyConflictError:
            return self._persist_collision(
                request=request,
                profile=profile,
                request_signed_at=request_signed_at,
                nonce=nonce,
                message_id=message_id,
                event_type=event_type,
                payload_hash=payload_hash,
                received_at=received_at,
                lock_id=lock.adapter_resolution_lock_id,
            )
        except ReplayConflictError as exc:
            raise PerceptorPushReplayError(str(exc)) from exc

        if not intake.created:
            return PerceptorPushIngestResult(
                accepted=True,
                duplicate=True,
                collision=False,
                raw_ingress_record_id=intake.raw_ingress_record_id,
                work_id=intake.work_id,
                message_id=message_id,
                event_type=event_type,
                normalization_status=(
                    "quarantined" if parse_error is not None else "pending"
                ),
                compatibility_profile_id=profile.profile_id,
            )
        if parse_error is not None:
            return PerceptorPushIngestResult(
                accepted=True,
                duplicate=False,
                collision=False,
                raw_ingress_record_id=raw_id,
                work_id=work_id,
                message_id=message_id,
                event_type=event_type,
                normalization_status="quarantined",
                compatibility_profile_id=profile.profile_id,
            )
        return PerceptorPushIngestResult(
            accepted=True,
            duplicate=False,
            collision=False,
            raw_ingress_record_id=raw_id,
            work_id=work_id,
            message_id=message_id,
            event_type=event_type,
            normalization_status="pending",
            compatibility_profile_id=profile.profile_id,
        )

    def _profile(
        self,
        request: PerceptorRawHttpRequest,
    ) -> PerceptorPushCompatibilityProfile:
        profile_id = _required_header(request, PROFILE_HEADER)
        try:
            profile = self.profiles[profile_id]
        except KeyError as exc:
            raise PerceptorPushAuthenticationError(
                "unsupported Perceptor compatibility profile"
            ) from exc
        algorithm = _required_header(request, SIGNATURE_METHOD_HEADER).upper()
        if (
            algorithm not in ALLOWED_SIGNATURE_ALGORITHMS
            or algorithm != profile.signature_algorithm
        ):
            raise PerceptorPushAuthenticationError(
                "unsupported Perceptor signature method"
            )
        if request.path != profile.signing_path:
            raise PerceptorPushAuthenticationError(
                "Perceptor request path does not match the signing profile"
            )
        return profile

    def _resolve_adapter_lock(
        self,
        profile: PerceptorPushCompatibilityProfile,
        *,
        received_at: datetime,
    ) -> Any:
        lock_id = f"perceptor-push-lock:{profile.profile_id}"
        with self._adapter_resolution_lock:
            retained = self.repository.load_adapter_resolution_lock(
                self.namespace,
                adapter_resolution_lock_id=lock_id,
            )
            if retained is not None:
                if (
                    retained.provider_id != "perceptor"
                    or retained.provider_account_id
                    != profile.provider_account_id
                    or retained.environment != profile.environment
                    or not any(
                        item.capability == AdapterCapability.VITAL_PUSH
                        for item in retained.capabilities
                    )
                ):
                    raise PerceptorPushConfigurationError(
                        "retained Perceptor Adapter lock does not match "
                        "the compatibility profile"
                    )
                return retained
            return self.registry.resolve(
                provider_id="perceptor",
                provider_account_id=profile.provider_account_id,
                environment=profile.environment,
                required_capabilities=(AdapterCapability.VITAL_PUSH,),
                adapter_resolution_lock_id=lock_id,
                resolution_request_id=(
                    f"perceptor-push-resolution:{profile.profile_id}"
                ),
                resolved_at=received_at,
                adapter_id="perceptor-v1",
                version="1.0.0",
                require_verified=False,
            )

    def _verify_signature(
        self,
        request: PerceptorRawHttpRequest,
        profile: PerceptorPushCompatibilityProfile,
    ) -> None:
        supplied = _required_header(request, SIGNATURE_HEADER)
        if profile.signature_mode == SignatureMode.RAW_BODY:
            expected = hmac.new(
                profile.signing_secret.encode("utf-8"),
                request.body,
                hashlib.sha256,
            ).hexdigest()
        else:
            parsed, error = _try_parse_json_object(request.body)
            if error is not None or parsed is None:
                raise PerceptorPushAuthenticationError(
                    "canonical-parameter profile requires a JSON object"
                )
            expected = sign_parameters(
                {
                    str(key): _canonical_signing_value(value)
                    for key, value in parsed.items()
                },
                client_secret=profile.signing_secret,
                append_ampersand=profile.append_ampersand_to_secret,
                signing_path=profile.signing_path,
            )
        if not hmac.compare_digest(supplied, expected):
            raise PerceptorPushAuthenticationError(
                "invalid Perceptor push signature"
            )

    def _persist_collision(
        self,
        *,
        request: PerceptorRawHttpRequest,
        profile: PerceptorPushCompatibilityProfile,
        request_signed_at: datetime,
        nonce: str,
        message_id: str | None,
        event_type: str,
        payload_hash: str,
        received_at: datetime,
        lock_id: str,
    ) -> PerceptorPushIngestResult:
        if message_id is None:
            raise PerceptorPushReplayError(
                "payload-hash identity cannot produce a message-id collision"
            )
        identity = f"message-id-collision.v1:{message_id}:{payload_hash}"
        raw_id = _stable_id(
            "collision",
            profile.provider_account_id,
            identity,
            payload_hash,
        )
        work_id = f"normalize:{raw_id}"
        record = RawIngressRecord(
            raw_ingress_record_id=raw_id,
            data_mode=self.namespace.data_mode,
            provider_id="perceptor",
            provider_account_id=profile.provider_account_id,
            event_type=event_type,
            message_id=message_id,
            request_signed_at=request_signed_at,
            received_at=received_at,
            signature_profile=profile.profile_id,
            signature_verification=SignatureVerificationState.VERIFIED,
            idempotency_identity=identity,
            idempotency_version="message-id-collision.v1",
            pre_normalization_payload_sha256=payload_hash,
            encrypted_payload_reference=(
                f"db:sleep_domain_raw_inbox:{raw_id}"
            ),
            content_type=request.header("content-type") or "application/json",
            payload_size_bytes=len(request.body),
            retention_deadline=(
                received_at + self.repository.raw_payload_policy.retention_period
            ),
        )
        terminal_quarantine = TerminalRawQuarantine(
            quarantine_id=f"quarantine:collision:{raw_id}",
            receipt=_raw_quarantine_receipt(
                raw_id=raw_id,
                data_mode=self.namespace.data_mode,
                receipt_id=f"receipt:collision:{raw_id}",
                reason=QuarantineReason.MESSAGE_ID_COLLISION,
                detail_code="same_message_id_different_payload_hash",
                occurred_at=received_at,
            ),
            detail={
                "message_id_hash": hashlib.sha256(
                    message_id.encode("utf-8")
                ).hexdigest(),
                "payload_sha256": payload_hash,
            },
        )
        try:
            intake = self.repository.intake_raw(
                self.namespace,
                record,
                raw_payload=request.body,
                work_id=work_id,
                work_generation=1,
                work_json={
                    "adapter_resolution_lock_id": lock_id,
                    "compatibility_profile_id": profile.profile_id,
                    "environment": profile.environment,
                    "ingress_channel": "push",
                },
                processing_intent_id=f"processing-intent:{raw_id}",
                processing_intent_json={
                    "event_type": "MESSAGE_ID_COLLISION"
                },
                created_at=received_at,
                replay_guard=IngressReplayGuard(
                    provider_id="perceptor",
                    provider_account_id=profile.provider_account_id,
                    compatibility_profile_id=profile.profile_id,
                    nonce=nonce,
                    idempotency_identity=identity,
                    pre_normalization_payload_sha256=payload_hash,
                    request_signed_at=request_signed_at,
                ),
                processing_intent_event_type="MESSAGE_ID_COLLISION",
                terminal_quarantine=terminal_quarantine,
            )
        except ReplayConflictError as exc:
            raise PerceptorPushReplayError(str(exc)) from exc
        if not intake.created:
            return PerceptorPushIngestResult(
                accepted=True,
                duplicate=True,
                collision=True,
                raw_ingress_record_id=intake.raw_ingress_record_id,
                work_id=intake.work_id,
                message_id=message_id,
                event_type=event_type,
                normalization_status="quarantined",
                compatibility_profile_id=profile.profile_id,
            )
        return PerceptorPushIngestResult(
            accepted=True,
            duplicate=False,
            collision=True,
            raw_ingress_record_id=intake.raw_ingress_record_id,
            work_id=intake.work_id,
            message_id=message_id,
            event_type=event_type,
            normalization_status="quarantined",
            compatibility_profile_id=profile.profile_id,
        )

    def _validate_pre_parse_limits(
        self,
        request: PerceptorRawHttpRequest,
    ) -> None:
        if request.method.upper() != "POST":
            raise PerceptorPushAuthenticationError(
                "Perceptor push requires POST"
            )
        if len(request.body) > self.http_limits.max_body_bytes:
            raise PerceptorPushRequestTooLarge("Perceptor push body is oversized")
        if len(request.raw_headers) > self.http_limits.max_header_count:
            raise PerceptorPushRequestTooLarge(
                "Perceptor push has too many headers"
            )
        header_bytes = sum(
            len(key) + len(value) for key, value in request.raw_headers
        )
        if header_bytes > self.http_limits.max_header_bytes:
            raise PerceptorPushRequestTooLarge(
                "Perceptor push headers are oversized"
            )


class PerceptorNormalizationWorker:
    def __init__(
        self,
        *,
        namespace: DomainNamespace,
        repository: SleepDomainRepository,
        registry: ControlledAdapterRegistry,
        promotion_service: CandidatePromotionService,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        self.namespace = namespace
        self.repository = repository
        self.registry = registry
        self.promotion_service = promotion_service
        self.worker_id = worker_id
        self.lease_duration = lease_duration

    def run_once(self, *, now: datetime) -> bool:
        lease = self.repository.lease_normalization_work(
            self.namespace,
            worker_id=self.worker_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if lease is None:
            return False
        raw_record = self.repository.get_raw_record(
            self.namespace,
            raw_ingress_record_id=lease.raw_ingress_record_id,
        )
        if raw_record is None:
            raise RuntimeError("leased raw record is missing")
        raw_payload = self.repository.load_raw_payload(
            self.namespace,
            raw_ingress_record_id=lease.raw_ingress_record_id,
        )
        work = json.loads(lease.work_json)
        adapter_input = AdapterInputEnvelope(
            data_mode=self.namespace.data_mode,
            adapter_resolution_lock_id=str(
                work["adapter_resolution_lock_id"]
            ),
            environment=str(work["environment"]),
            raw_record=raw_record,
            raw_payload=raw_payload,
        )
        execution = self.registry.execute(
            execution_receipt_id=(
                f"adapter-execution:{raw_record.raw_ingress_record_id}"
            ),
            adapter_input=adapter_input,
        )
        if execution.receipt.status != AdapterExecutionStatus.SUCCEEDED:
            reason = (
                execution.receipt.quarantine_reason
                or QuarantineReason.UNKNOWN
            )
            receipt = _raw_quarantine_receipt(
                raw_id=raw_record.raw_ingress_record_id,
                data_mode=self.namespace.data_mode,
                receipt_id=(
                    f"receipt:adapter:{raw_record.raw_ingress_record_id}"
                ),
                reason=reason,
                detail_code=(
                    execution.receipt.detail_code
                    or execution.receipt.status.value
                ),
                occurred_at=raw_record.received_at,
                stage=ProcessingStage.NORMALIZATION,
            )
            self.repository.quarantine_normalization_work(
                self.namespace,
                work_id=lease.work_id,
                worker_id=self.worker_id,
                quarantine_id=(
                    f"quarantine:adapter:{raw_record.raw_ingress_record_id}"
                ),
                receipt=receipt,
                detail={
                    "adapter_status": execution.receipt.status.value,
                    "detail_code": execution.receipt.detail_code,
                },
                completed_at=now,
                require_active_lease=True,
            )
            return True
        for candidate in execution.candidates:
            acquisition_channel = str(
                work.get("ingress_channel", "unknown")
            )
            self.promotion_service.promote(
                self.namespace,
                candidate,
                attempt_id=(
                    f"{acquisition_channel}:{raw_record.raw_ingress_record_id}:"
                    f"{candidate.candidate_id}"
                ),
                actor_id=self.worker_id,
                occurred_at=raw_record.received_at,
                acquisition_channel=acquisition_channel,
            )
        if not self.repository.complete_normalization_work(
            self.namespace,
            work_id=lease.work_id,
            worker_id=self.worker_id,
            completed_at=now,
        ):
            raise RuntimeError("normalization work lease was lost before completion")
        return True


def sign_perceptor_push_request(
    body: bytes,
    *,
    profile: PerceptorPushCompatibilityProfile,
) -> str:
    if profile.signature_mode == SignatureMode.RAW_BODY:
        return hmac.new(
            profile.signing_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    parsed, error = _try_parse_json_object(body)
    if error is not None or parsed is None:
        raise ValueError("canonical parameter signing requires a JSON object")
    return sign_parameters(
        {
            str(key): _canonical_signing_value(value)
            for key, value in parsed.items()
        },
        client_secret=profile.signing_secret,
        append_ampersand=profile.append_ampersand_to_secret,
        signing_path=profile.signing_path,
    )


async def read_bounded_starlette_request(
    request: Any,
    *,
    limits: PerceptorPushHttpLimits,
) -> PerceptorRawHttpRequest:
    raw_headers = tuple(request.scope.get("headers", ()))
    if len(raw_headers) > limits.max_header_count:
        raise PerceptorPushRequestTooLarge("Perceptor push has too many headers")
    header_bytes = sum(len(key) + len(value) for key, value in raw_headers)
    if header_bytes > limits.max_header_bytes:
        raise PerceptorPushRequestTooLarge(
            "Perceptor push headers are oversized"
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise PerceptorPushRequestTooLarge(
                "invalid Perceptor Content-Length"
            ) from exc
        if declared > limits.max_body_bytes:
            raise PerceptorPushRequestTooLarge(
                "Perceptor push body is oversized"
            )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limits.max_body_bytes:
            raise PerceptorPushRequestTooLarge(
                "Perceptor push body is oversized"
            )
        chunks.append(bytes(chunk))
    client = request.client
    client_key = "unknown" if client is None else str(client.host)
    return PerceptorRawHttpRequest(
        method=request.method,
        path=request.url.path,
        raw_headers=raw_headers,
        body=b"".join(chunks),
        client_key=client_key,
    )


def _raw_quarantine_receipt(
    *,
    raw_id: str,
    data_mode: DataMode,
    receipt_id: str,
    reason: QuarantineReason,
    detail_code: str,
    occurred_at: datetime,
    stage: ProcessingStage = ProcessingStage.INTAKE,
) -> ProcessingReceipt:
    return ProcessingReceipt(
        receipt_id=receipt_id,
        raw_ingress_record_id=raw_id,
        data_mode=data_mode,
        stage=stage,
        outcome=ProcessingOutcome.QUARANTINED,
        occurred_at=occurred_at,
        actor_id="perceptor-push-ingestion",
        processor_id="perceptor-push-ingestion",
        processor_version="1.0.0",
        quarantine_reason=reason,
        detail_code=detail_code,
    )


def _try_parse_json_object(
    body: bytes,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json_body"
    if not isinstance(parsed, dict):
        return None, "json_body_not_object"
    return parsed, None


def _canonical_signing_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return value


def _parse_signed_time(value: str) -> datetime:
    stripped = value.strip()
    try:
        if stripped.replace(".", "", 1).isdigit():
            number = float(stripped)
            seconds = number / 1000 if abs(number) >= 10_000_000_000 else number
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except (OverflowError, OSError, ValueError) as exc:
        raise PerceptorPushAuthenticationError(
            "invalid Perceptor push timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PerceptorPushAuthenticationError(
            "Perceptor push timestamp requires a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _required_header(request: PerceptorRawHttpRequest, name: str) -> str:
    value = request.header(name)
    if not value:
        raise PerceptorPushAuthenticationError(
            f"missing Perceptor security header: {name}"
        )
    return value


def _opaque_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "Perceptor external identifiers must be opaque strings"
        )
    return value if value else None


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _headers_sha256(headers: tuple[tuple[bytes, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for key, value in headers:
        digest.update(len(key).to_bytes(4, "big"))
        digest.update(key)
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.hexdigest()


__all__ = [
    "ALLOWED_SIGNATURE_ALGORITHMS",
    "MESSAGE_ID_HEADER",
    "NONCE_HEADER",
    "PROFILE_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_METHOD_HEADER",
    "TIMESTAMP_HEADER",
    "PerceptorNormalizationWorker",
    "PerceptorPushAuthenticationError",
    "PerceptorPushCompatibilityProfile",
    "PerceptorPushConfigurationError",
    "PerceptorPushError",
    "PerceptorPushHttpLimits",
    "PerceptorPushIngestResult",
    "PerceptorPushIngestionService",
    "PerceptorPushRateLimitError",
    "PerceptorPushRateLimiter",
    "PerceptorPushReplayError",
    "PerceptorPushRequestTooLarge",
    "PerceptorRawHttpRequest",
    "SignatureMode",
    "read_bounded_starlette_request",
    "sign_perceptor_push_request",
]
