"""Production YunYun Push HTTP-to-durable-ingress adapter for P4-D."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sleepagent.config import (
    BackendKeyProvider,
    ObservationSemanticsVersion,
    SleepBackendSettings,
)
from sleepagent.domain.contracts import (
    DataMode,
    DeviceBinding,
    bind_adapter_candidate,
)
from sleepagent.domain.episodes import UUID7Generator
from sleepagent.infrastructure.postgres_sleep_slice import (
    NormalizationLease,
    RawPayloadCipher,
    SleepSliceInvariantError,
    SleepSliceLeaseLost,
)
from sleepagent.integrations.perceptor.push import (
    ADAPTER_VERSION,
    PARSER_VERSION,
    SUPPORTED_EVENT_TYPES,
    FieldState,
    ParsedPushEnvelope,
    PushContractError,
    canonicalize_push_candidates_v2,
    normalize_push_envelope,
    parse_push_envelope,
    push_request_signed_at,
    verify_push_envelope_signature,
)
from sleepagent.integrations.perceptor.reconciliation import (
    PerceptorObservationReconciler,
    PerceptorReconciliationResult,
    namespace_generation_scoped_candidate,
    semantic_surface_for_push,
)
from sleepagent.integrations.perceptor.signing import (
    SigningRepresentationUnresolved,
)
from sleepagent.observability import log_event, record_backend_signal
from sleepagent.persistence.uow import (
    ExternalIngressScope,
    TransactionBoundConnection,
    UnitOfWorkFactory,
    UowScope,
)


UTC = timezone.utc
WEBHOOK_PATH = "/integrations/perceptor/webhook"
SIGNATURE_PROFILE = "perceptor-push-empty-path-secret-ampersand.v1"


class PerceptorWebhookError(RuntimeError):
    def __init__(self, status_code: int, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PerceptorIngressResult:
    disposition: str
    raw_ingress_record_id: str | None
    normalization_work_id: str | None
    subject_id: str | None
    device_binding_id: str | None
    duplicate: bool


@dataclass(frozen=True, slots=True)
class PerceptorNormalizationResult:
    work_id: str
    raw_ingress_record_id: str
    canonical_observation_ids: tuple[str, ...]
    quarantined: bool = False
    canonical_created_count: int = 0
    push_pull_overlap_count: int = 0
    conflict_created_count: int = 0


class PerceptorWebhookService:
    """Authenticate one Push and atomically hand it to PostgreSQL."""

    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        client_secret: str | None = None,
        cipher: RawPayloadCipher | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        id_generator: Callable[[datetime | None], str] | None = None,
    ) -> None:
        if settings.perceptor_namespace_id is None:
            raise ValueError("Perceptor namespace is not configured")
        if settings.perceptor_provider_account_id is None:
            raise ValueError("Perceptor provider account is not configured")
        if client_secret is None:
            reference = settings.perceptor_client_secret_ref
            if reference is None:
                raise ValueError("Perceptor client secret reference is not configured")
            secret_bytes = BackendKeyProvider(settings.deployment_mode).secret(
                reference,
                purpose="Perceptor incoming Push",
                minimum_bytes=1,
            )
            try:
                client_secret = secret_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValueError("Perceptor client secret must be UTF-8") from exc
        if not client_secret:
            raise ValueError("Perceptor client secret is empty")
        if cipher is None:
            key = BackendKeyProvider(settings.deployment_mode).encryption_key(
                settings.encryption_key_ref
            )
            cipher = RawPayloadCipher(key, key_id=settings.encryption_key_ref)
        self.settings = settings
        self.uow_factory = uow_factory
        self._client_secret = client_secret
        self.cipher = cipher
        self.now_factory = now_factory
        self.id_generator = id_generator or UUID7Generator()

    def accept(
        self,
        raw_body: bytes,
        *,
        content_type: str,
        request_path: str,
        received_at: datetime | None = None,
    ) -> PerceptorIngressResult:
        if request_path != WEBHOOK_PATH:
            raise PerceptorWebhookError(404, "not_found")
        if _media_type(content_type) != "application/json":
            raise PerceptorWebhookError(415, "unsupported_content_type")
        if not raw_body:
            raise PerceptorWebhookError(400, "empty_request_body")
        effective_received_at = received_at or self.now_factory()
        _require_aware(effective_received_at, "received_at")
        record_backend_signal(category="perceptor_webhook", outcome="received")
        try:
            envelope = parse_push_envelope(
                raw_body,
                allow_unknown_event=True,
            )
        except PushContractError as exc:
            record_backend_signal(category="perceptor_signature", outcome="rejected")
            raise PerceptorWebhookError(400, "invalid_push_contract") from exc

        signed_at = push_request_signed_at(envelope)
        fresh = signed_at is not None and abs(
            (effective_received_at - signed_at).total_seconds()
        ) <= self.settings.perceptor_freshness_seconds
        try:
            signature_valid = verify_push_envelope_signature(
                envelope,
                client_secret=self._client_secret,
            )
        except SigningRepresentationUnresolved as exc:
            record_backend_signal(category="perceptor_signature", outcome="rejected")
            raise PerceptorWebhookError(
                400, "unsupported_signing_representation"
            ) from exc
        if not signature_valid:
            record_backend_signal(category="perceptor_signature", outcome="rejected")
            raise PerceptorWebhookError(401, "invalid_signature")
        record_backend_signal(category="perceptor_signature", outcome="verified")
        provider_device_identity = (
            _provider_device_id(envelope) or envelope.device_name
        )
        log_event(
            "perceptor_webhook_authenticated",
            client_id_sha256=_sha256_text(envelope.client_id),
            provider_device_identity_sha256=_sha256_text(
                provider_device_identity
            ),
            event_type=envelope.event_type,
        )

        raw_id = self.id_generator(effective_received_at)
        encrypted = self.cipher.encrypt(
            raw_body,
            aad=_perceptor_raw_aad(
                self.settings.perceptor_namespace_id,
                self.settings.perceptor_namespace_generation,
                raw_id,
            ),
        )
        result = self._commit(
            envelope,
            raw_id=raw_id,
            encrypted=encrypted,
            content_type=content_type,
            request_path=request_path,
            received_at=effective_received_at,
            request_signed_at=signed_at,
            fresh=fresh,
        )
        if result.disposition == "stale":
            record_backend_signal(category="perceptor_webhook", outcome="stale")
            raise PerceptorWebhookError(400, "stale_request")
        metric = (
            "duplicate"
            if result.duplicate
            else "unbound"
            if result.disposition == "quarantined_unbound"
            else "committed"
        )
        record_backend_signal(category="perceptor_webhook", outcome=metric)
        log_event(
            "perceptor_webhook_durable_result",
            disposition=result.disposition,
            duplicate=result.duplicate,
            raw_ingress_record_id=result.raw_ingress_record_id,
            normalization_work_id=result.normalization_work_id,
            raw_sha256=envelope.raw_payload_sha256,
        )
        return result

    def _commit(
        self,
        envelope: ParsedPushEnvelope,
        *,
        raw_id: str,
        encrypted: bytes,
        content_type: str,
        request_path: str,
        received_at: datetime,
        request_signed_at: datetime | None,
        fresh: bool,
    ) -> PerceptorIngressResult:
        namespace = cast(str, self.settings.perceptor_namespace_id)
        provider_account = cast(str, self.settings.perceptor_provider_account_id)
        scope = ExternalIngressScope(
            namespace_id=namespace,
            namespace_generation=self.settings.perceptor_namespace_generation,
            service_principal_id=self.settings.service_principal_id,
            authorization_epoch=self.settings.perceptor_authorization_epoch,
        )
        ids = tuple(self.id_generator(received_at) for _ in range(4))
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_ingest_perceptor_push("
                    + ",".join(["%s"] * 26)
                    + ")",
                    (
                        namespace,
                        self.settings.perceptor_namespace_generation,
                        provider_account,
                        _sha256_text(envelope.client_id),
                        _provider_device_id(envelope),
                        envelope.device_name,
                        envelope.event_type,
                        _safe_message_identity(envelope),
                        request_signed_at,
                        received_at,
                        _generation_scoped_push_idempotency_identity(
                            namespace,
                            self.settings.perceptor_namespace_generation,
                            envelope.idempotency_identity,
                        ),
                        envelope.raw_payload_sha256,
                        encrypted,
                        self.cipher.key_id,
                        _media_type(content_type),
                        len(envelope.raw_body),
                        received_at
                        + timedelta(seconds=self.settings.raw_retention_seconds),
                        SIGNATURE_PROFILE,
                        fresh,
                        envelope.event_type in SUPPORTED_EVENT_TYPES,
                        raw_id,
                        ids[0],
                        ids[1],
                        ids[2],
                        ids[3],
                        json.dumps(
                            {
                                "request_path": request_path,
                                "raw_sha256": envelope.raw_payload_sha256,
                                "parser_version": PARSER_VERSION,
                                "adapter_version": ADAPTER_VERSION,
                                "event_type": envelope.event_type,
                                "data_representation": envelope.data_representation.value,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise RuntimeError("Perceptor ingress function returned no result")
            uow.commit()
        return PerceptorIngressResult(
            disposition=str(row[0]),
            raw_ingress_record_id=None if row[1] is None else str(row[1]),
            normalization_work_id=None if row[2] is None else str(row[2]),
            subject_id=None if row[3] is None else str(row[3]),
            device_binding_id=None if row[4] is None else str(row[4]),
            duplicate=bool(row[5]),
        )


class PerceptorNormalizationProcessor:
    """Normalize one claimed live Push into canonical observations only."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        cipher: RawPayloadCipher,
        id_generator: Callable[[datetime | None], str] | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        observation_semantics_version: ObservationSemanticsVersion = (
            ObservationSemanticsVersion.V1
        ),
    ) -> None:
        self.uow_factory = uow_factory
        self.cipher = cipher
        self.id_generator = id_generator or UUID7Generator()
        self.now_factory = now_factory
        self.observation_semantics_version = observation_semantics_version
        self.reconciler = PerceptorObservationReconciler()

    def process(
        self,
        scope: UowScope,
        lease: NormalizationLease,
    ) -> PerceptorNormalizationResult:
        if scope.data_mode != "live" or scope.subject_id is None:
            raise SleepSliceInvariantError(
                "Perceptor normalization requires exact live subject scope"
            )
        committed_at = self.now_factory()
        _require_aware(committed_at, "committed_at")
        with self.uow_factory.begin(scope) as uow:
            loaded = self._load(uow.connection, scope, lease)
            try:
                raw = self.cipher.decrypt(
                    loaded["encrypted_payload"],
                    aad=_perceptor_raw_aad(
                        scope.namespace_id,
                        scope.namespace_generation,
                        loaded["raw_ingress_record_id"],
                    ),
                )
                if hashlib.sha256(raw).hexdigest() != loaded["payload_sha256"]:
                    raise SleepSliceInvariantError("raw payload hash mismatch")
                envelope = parse_push_envelope(raw)
                candidates = tuple(
                    namespace_generation_scoped_candidate(
                        candidate,
                        namespace_id=scope.namespace_id,
                        namespace_generation=scope.namespace_generation,
                    )
                    for candidate in normalize_push_envelope(
                        envelope,
                        provider_account_id=loaded["provider_account_id"],
                        data_mode=DataMode.LIVE,
                        received_at=loaded["received_at"],
                        raw_ingress_record_id=loaded["raw_ingress_record_id"],
                    )
                )
                canonical_semantics: tuple[Any | None, ...]
                if self.observation_semantics_version is ObservationSemanticsVersion.V2:
                    canonical_semantics = canonicalize_push_candidates_v2(candidates)
                    candidates = tuple(
                        item.compatibility_candidate
                        for item in canonical_semantics
                    )
                else:
                    canonical_semantics = tuple(None for _ in candidates)
                binding = DeviceBinding.model_validate(loaded["binding_json"])
                observations = tuple(
                    bind_adapter_candidate(candidate, binding)
                    for candidate in candidates
                )
            except (PushContractError, ValueError, SleepSliceInvariantError) as exc:
                self._quarantine(
                    uow.connection,
                    scope,
                    lease,
                    raw_ingress_record_id=loaded["raw_ingress_record_id"],
                    committed_at=committed_at,
                    detail_code=type(exc).__name__,
                )
                uow.commit()
                return PerceptorNormalizationResult(
                    work_id=lease.work_id,
                    raw_ingress_record_id=loaded["raw_ingress_record_id"],
                    canonical_observation_ids=(),
                    quarantined=True,
                )
            reconciliations = self._persist(
                uow.connection,
                scope,
                lease,
                raw_ingress_record_id=loaded["raw_ingress_record_id"],
                candidates=candidates,
                observations=observations,
                canonical_semantics=canonical_semantics,
                committed_at=committed_at,
            )
            uow.commit()
        record_backend_signal(category="perceptor_normalization", outcome="succeeded")
        _emit_push_reconciliation_observability(reconciliations)
        log_event(
            "perceptor_normalization_succeeded",
            work_id=lease.work_id,
            raw_ingress_record_id=loaded["raw_ingress_record_id"],
            canonical_observation_count=sum(
                item.canonical_created for item in reconciliations
            ),
            push_pull_overlap_count=sum(
                item.push_pull_overlap for item in reconciliations
            ),
            conflict_created_count=sum(
                item.conflict_created_count for item in reconciliations
            ),
        )
        return PerceptorNormalizationResult(
            work_id=lease.work_id,
            raw_ingress_record_id=loaded["raw_ingress_record_id"],
            canonical_observation_ids=tuple(
                item.canonical_observation_id for item in reconciliations
            ),
            canonical_created_count=sum(
                item.canonical_created for item in reconciliations
            ),
            push_pull_overlap_count=sum(
                item.push_pull_overlap for item in reconciliations
            ),
            conflict_created_count=sum(
                item.conflict_created_count for item in reconciliations
            ),
        )

    def _load(
        self,
        connection: TransactionBoundConnection,
        scope: UowScope,
        lease: NormalizationLease,
    ) -> dict[str, Any]:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT raw.raw_ingress_record_id, raw.encrypted_payload,
                       raw.encryption_key_id,
                       raw.pre_normalization_payload_sha256,
                       raw.provider_account_id, raw.received_at,
                       work.work_json, binding.binding_json
                FROM public.sleep_domain_normalization_work AS work
                JOIN public.sleep_domain_raw_inbox AS raw
                  ON raw.raw_ingress_record_id = work.raw_ingress_record_id
                 AND raw.namespace_id = work.namespace_id
                 AND raw.data_mode = work.data_mode
                 AND raw.subject_id = work.subject_id
                JOIN public.sleep_domain_device_bindings AS binding
                  ON binding.device_binding_id = work.work_json ->> 'device_binding_id'
                 AND binding.namespace_id = work.namespace_id
                 AND binding.data_mode = work.data_mode
                 AND binding.subject_id = work.subject_id
                WHERE work.work_id = %s AND work.namespace_id = %s
                  AND work.data_mode = 'live'
                  AND work.namespace_generation = %s AND work.subject_id = %s
                  AND work.work_json ->> 'normalizer' = 'perceptor_push'
                  AND raw.signature_verification = 'verified'
                  AND raw.signature_profile = %s
                  AND raw.encryption_protocol_version = 1
                  AND work.status = 'running' AND work.lease_generation = %s
                  AND work.fencing_token = %s AND work.worker_instance = %s
                  AND work.lease_expires_at > clock_timestamp()
                FOR UPDATE OF work
                """,
                (
                    lease.work_id,
                    scope.namespace_id,
                    scope.namespace_generation,
                    scope.subject_id,
                    SIGNATURE_PROFILE,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                ),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise SleepSliceLeaseLost("Perceptor normalization fence was rejected")
        if str(row[2]) != self.cipher.key_id:
            raise SleepSliceInvariantError("raw payload encryption key is unavailable")
        return {
            "raw_ingress_record_id": str(row[0]),
            "encrypted_payload": bytes(row[1]),
            "payload_sha256": str(row[3]),
            "provider_account_id": str(row[4]),
            "received_at": row[5],
            "work_json": _json_mapping(row[6]),
            "binding_json": _json_mapping(row[7]),
        }

    def _persist(
        self,
        connection: TransactionBoundConnection,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        raw_ingress_record_id: str,
        candidates: tuple[Any, ...],
        observations: tuple[Any, ...],
        canonical_semantics: tuple[Any | None, ...],
        committed_at: datetime,
    ) -> tuple[PerceptorReconciliationResult, ...]:
        cursor = connection.cursor()
        try:
            reconciliations: list[PerceptorReconciliationResult] = []
            for candidate, observation, semantics in zip(
                candidates,
                observations,
                canonical_semantics,
                strict=True,
            ):
                reconciliations.append(
                    self.reconciler.reconcile(
                        cursor,
                        scope,
                        raw_ingress_record_id=raw_ingress_record_id,
                        candidate=candidate,
                        observation=observation,
                        canonical_semantics=semantics,
                        semantic_surface=semantic_surface_for_push(observation),
                        committed_at=committed_at,
                    )
                )
            receipt_id = self.id_generator(committed_at)
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_processing_receipts (
                  receipt_id, namespace_id, data_mode, raw_ingress_record_id,
                  stage, outcome, receipt_json, occurred_at
                ) VALUES (%s, %s, 'live', %s, 'normalization', 'succeeded', %s::jsonb, %s)
                """,
                (
                    receipt_id,
                    scope.namespace_id,
                    raw_ingress_record_id,
                    json.dumps(
                        {
                            "schema_version": "processing_receipt.v2",
                            "receipt_id": receipt_id,
                            "raw_ingress_record_id": raw_ingress_record_id,
                            "stage": "normalization",
                            "outcome": "succeeded",
                            "canonical_observation_ids": [
                                item.canonical_observation_id
                                for item in reconciliations
                            ],
                            "canonical_created_count": sum(
                                item.canonical_created for item in reconciliations
                            ),
                            "push_pull_overlap_count": sum(
                                item.push_pull_overlap for item in reconciliations
                            ),
                            "conflict_created_count": sum(
                                item.conflict_created_count
                                for item in reconciliations
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    committed_at,
                ),
            )
            self._finalize_work(
                cursor,
                scope,
                lease,
                status="succeeded",
                error_code=None,
                committed_at=committed_at,
            )
            return tuple(reconciliations)
        finally:
            cursor.close()

    def _quarantine(
        self,
        connection: TransactionBoundConnection,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        raw_ingress_record_id: str,
        committed_at: datetime,
        detail_code: str,
    ) -> None:
        receipt_id = self.id_generator(committed_at)
        quarantine_id = self.id_generator(committed_at)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_processing_receipts (
                  receipt_id, namespace_id, data_mode, raw_ingress_record_id,
                  stage, outcome, quarantine_reason, receipt_json, occurred_at
                ) VALUES (
                  %s, %s, 'live', %s, 'normalization', 'quarantined',
                  'malformed_payload', %s::jsonb, %s
                )
                """,
                (
                    receipt_id,
                    scope.namespace_id,
                    raw_ingress_record_id,
                    json.dumps(
                        {
                            "schema_version": "processing_receipt.v2",
                            "receipt_id": receipt_id,
                            "raw_ingress_record_id": raw_ingress_record_id,
                            "stage": "normalization",
                            "outcome": "quarantined",
                            "quarantine_reason": "malformed_payload",
                            "detail_code": detail_code,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    committed_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_quarantine (
                  quarantine_id, namespace_id, data_mode, raw_ingress_record_id,
                  reason, detail_code, receipt_id, quarantine_json, quarantined_at
                ) VALUES (%s, %s, 'live', %s, 'malformed_payload', %s, %s, %s::jsonb, %s)
                """,
                (
                    quarantine_id,
                    scope.namespace_id,
                    raw_ingress_record_id,
                    detail_code,
                    receipt_id,
                    json.dumps(
                        {
                            "schema_version": "perceptor_quarantine.v1",
                            "raw_ingress_record_id": raw_ingress_record_id,
                            "reason": "malformed_payload",
                            "detail_code": detail_code,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    committed_at,
                ),
            )
            self._finalize_work(
                cursor,
                scope,
                lease,
                status="quarantined",
                error_code="perceptor_normalization_quarantined",
                committed_at=committed_at,
            )
        finally:
            cursor.close()
        record_backend_signal(category="perceptor_normalization", outcome="failed")

    @staticmethod
    def _finalize_work(
        cursor: Any,
        scope: UowScope,
        lease: NormalizationLease,
        *,
        status: str,
        error_code: str | None,
        committed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE public.sleep_domain_normalization_work
            SET status = %s, updated_at = %s, last_error_code = %s,
                lease_owner = NULL, lease_expires_at = NULL,
                fencing_token = NULL, worker_instance = NULL, heartbeat_at = NULL
            WHERE work_id = %s AND namespace_id = %s AND data_mode = 'live'
              AND namespace_generation = %s AND subject_id = %s
              AND status = 'running' AND lease_generation = %s
              AND fencing_token = %s AND worker_instance = %s
              AND lease_expires_at > clock_timestamp()
            """,
            (
                status,
                committed_at,
                error_code,
                lease.work_id,
                scope.namespace_id,
                scope.namespace_generation,
                scope.subject_id,
                lease.lease_generation,
                lease.fencing_token,
                lease.worker_instance,
            ),
        )
        if cursor.rowcount != 1:
            raise SleepSliceLeaseLost(
                "Perceptor normalization fence expired before atomic commit"
            )


def create_perceptor_router(service: PerceptorWebhookService) -> APIRouter:
    router = APIRouter()

    @router.post(WEBHOOK_PATH)
    async def webhook(request: Request) -> JSONResponse:
        content_encoding = getattr(
            request.state,
            "sleepagent_transport_content_encoding",
            "identity",
        )
        if content_encoding not in {"", "identity"}:
            return _error_response(request, 415, "unsupported_content_encoding")
        raw = getattr(request.state, "sleepagent_transport_request_body", None)
        if not isinstance(raw, bytes):
            raw = getattr(request.state, "sleepagent_validated_request_body", None)
        if not isinstance(raw, bytes):
            raw = await request.body()
        try:
            accepted = service.accept(
                raw,
                content_type=request.headers.get("content-type", ""),
                request_path=request.url.path,
            )
        except PerceptorWebhookError as exc:
            return _error_response(
                request,
                exc.status_code,
                exc.code,
                retryable=exc.retryable,
            )
        except Exception as exc:
            log_event(
                "perceptor_webhook_persistence_failed",
                level=logging.ERROR,
                error_type=type(exc).__name__,
            )
            record_backend_signal(category="perceptor_webhook", outcome="db_failed")
            return _error_response(
                request,
                503,
                "durable_ingress_unavailable",
                retryable=True,
            )
        now = datetime.now(tz=UTC)
        return JSONResponse(
            status_code=200,
            content={
                "request_id": str(uuid.uuid4()).upper(),
                "success": True,
                "code": "200",
                "message": (
                    "Duplicate accepted"
                    if accepted.duplicate
                    else "Durably accepted"
                ),
                "timestamp": int(now.timestamp() * 1000),
                "data": {},
            },
        )

    return router


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    *,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "schema_version": "perceptor_webhook_error.v1",
            "success": False,
            "code": code,
            "message": code.replace("_", " "),
            "retryable": retryable,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
        )


def _emit_push_reconciliation_observability(
    reconciliations: tuple[PerceptorReconciliationResult, ...],
) -> None:
    """Emit the same cross-channel signals regardless of arrival order."""

    for event, outcome, count in (
        (
            "push_pull_overlap",
            "overlap",
            sum(item.push_pull_overlap for item in reconciliations),
        ),
        (
            "push_pull_conflict",
            "conflict",
            sum(item.conflict_created_count for item in reconciliations),
        ),
    ):
        if count:
            record_backend_signal(category="reconciliation", outcome=outcome)
            log_event(event, endpoint=WEBHOOK_PATH, count=count)


def _perceptor_raw_aad(
    namespace_id: str,
    namespace_generation: int,
    raw_ingress_record_id: str,
) -> bytes:
    return "\0".join(
        (
            "perceptor-push-raw.v1",
            namespace_id,
            str(namespace_generation),
            raw_ingress_record_id,
        )
    ).encode("utf-8")


def _provider_device_id(envelope: ParsedPushEnvelope) -> str | None:
    if envelope.device_id.state != FieldState.PRESENT:
        return None
    value = envelope.device_id.value
    return None if isinstance(value, bool) else str(value)


def _safe_message_identity(envelope: ParsedPushEnvelope) -> str | None:
    if envelope.message_id.state != FieldState.PRESENT:
        return None
    value = envelope.message_id.value
    if isinstance(value, bool):
        return None
    return "sha256:" + _sha256_text(str(value))


def _generation_scoped_push_idempotency_identity(
    namespace_id: str,
    namespace_generation: int,
    source_identity: str,
) -> str:
    material = "\0".join(
        (
            "perceptor-push-idempotency.v2",
            namespace_id,
            str(namespace_generation),
            source_identity,
        )
    )
    return "perceptor-push-generation.v2:" + _sha256_text(material)


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _json_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise SleepSliceInvariantError("persisted JSON value is not an object")
    return dict(value)


__all__ = [
    "PerceptorIngressResult",
    "PerceptorNormalizationProcessor",
    "PerceptorNormalizationResult",
    "PerceptorWebhookError",
    "PerceptorWebhookService",
    "SIGNATURE_PROFILE",
    "WEBHOOK_PATH",
    "create_perceptor_router",
]
