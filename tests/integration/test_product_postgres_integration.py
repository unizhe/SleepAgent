from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.requests import Request

from reference_client.sleep_api_v1_client import Ed25519ActorSigner
from sleepagent.api.postgres import (
    PostgresAssertionReplayStore,
    PostgresAuthorityStore,
    PostgresProductBackend,
    PostgresProductIdentityResolver,
    _lock_l2_subject,
)
from sleepagent.api.product import ProductApiError, ProductApiService
from sleepagent.api.product_contracts import (
    ProductReportRunRequest,
    ProductReportState,
    ProductRole,
)
from sleepagent.api.public_auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    FailClosedRoleBindingResolver,
    HttpsBearerServicePrincipalVerifier,
    RotatingServiceCredential,
    SleepApiAuthenticator,
)

from sleepagent.config import (
    DataMode as BackendDataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ReportPipelineMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.contracts import AgentId, SourceScopeKind, stable_hash
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    MemoryPurpose,
    ProvenanceType,
    SensitivityClass,
)
from sleepagent.domain.habit import HabitFact, HabitOperation
from sleepagent.application.product_data import public_product_subject_ref
from sleepagent.application.night_finalization import (
    NightFinalizationPolicy,
    NightFinalizationService,
    NightFinalizationState,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    FastPathHandler,
    FastPathLease,
    default_sleep_slice_policy,
)
from sleepagent.workers.product import (
    PRODUCT_ELDER_NARRATIVE_OPERATION,
    PRODUCT_REPORT_RUN_OPERATION,
    PRODUCT_SHARED_ANALYSIS_OPERATION,
    PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST,
    PostgresProductAgentRepository,
    PreparedProductAgentArtifact,
    ProductAgentConflict,
    ProductAgentLease,
    ProductAgentLeaseLost,
    ProductAgentProcessor,
    ProductAgentWorkHandlerAdapter,
)
from sleepagent.runtime.factory import (
    ProductRuntimeBundle,
    build_deterministic_product_runtime_bundle,
)
from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    ConfidenceValue,
    CurrentRisk,
    DataMode,
    DataSufficiency,
    DeterministicQualityAssessment,
    DeterministicSourceScope,
    MissingState,
    MissingnessState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    QualityState,
    RiskState,
    SleepObservation,
    SourceKind,
    TimezoneStatus,
)
from sleepagent.domain.episodes import (
    NightEpisodeV2,
    UUID7Generator,
    finalize_episode_date,
    open_episode_v2,
)
from sleepagent.workers.runtime import (
    LeaseClaim,
    PostgresDurableWorkStore,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkResult,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


def _deterministic_runtime_bundle() -> ProductRuntimeBundle:
    return build_deterministic_product_runtime_bundle(
        model=DeterministicReplayStructuredAgentModel()
    )


class _IdentityVariantDeterministicModel(
    DeterministicReplayStructuredAgentModel
):
    def __init__(self, model_id: str) -> None:
        super().__init__()
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id


@dataclass(frozen=True, slots=True)
class _ProductSeed:
    namespace_id: str
    run_id: str
    arm_id: str
    subject_id: str
    operation_id: str
    night_episode_id: str
    night_episode_revision_id: str
    quality_assessment_id: str
    current_risk_id: str
    policy_sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedSharedCase:
    seed: _ProductSeed
    provider: PsycopgPoolProvider
    factory: UnitOfWorkFactory[object]
    store: PostgresDurableWorkStore
    bundle: ProductRuntimeBundle
    processor: ProductAgentProcessor
    scope: UowScope
    lease: ProductAgentLease
    source: object
    artifact: PreparedProductAgentArtifact


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def _canonical_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _quality() -> ObservationQuality:
    return ObservationQuality(
        missing_state=MissingState.PRESENT,
        confidence=ConfidenceValue(
            state=AvailabilityState.KNOWN,
            value=1.0,
        ),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.KNOWN,
            value="synthetic-replay-v1",
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
        completeness=1.0,
        processing_steps=("synthetic_replay_normalization.v1",),
        limitations=("not_a_diagnosis",),
    )


def _seed_product_scope(
    psycopg: object,
    *,
    admin_dsn: str,
    worker_principal: str,
    care_required: bool = False,
) -> _ProductSeed:
    suffix = uuid4().hex
    namespace_id = f"replay:product-{suffix}"
    run_id = f"run-{suffix}"
    arm_id = f"arm-{suffix}"
    subject_id = f"subject-{suffix}"
    provider_account_id = f"account-{suffix}"
    device_binding_id = f"binding-{suffix}"
    device_id = f"device-{suffix}"
    raw_id = f"raw-{suffix}"
    candidate_id = f"candidate-{suffix}"
    observation_id = f"observation-{suffix}"
    quality_id = f"quality-{suffix}"
    risk_id = f"risk-{suffix}"
    policy_sha256 = hashlib.sha256(f"policy:{suffix}".encode()).hexdigest()
    ids = UUID7Generator()
    now = datetime.now(tz=UTC)
    collection_start = now - timedelta(hours=10)
    wake_at = collection_start + timedelta(hours=8)
    committed_at = wake_at + timedelta(seconds=1)
    opening_identity = f"synthetic-opening:{suffix}"

    opened = open_episode_v2(
        namespace_id=namespace_id,
        namespace_generation=1,
        data_mode=DataMode.REPLAY,
        run_id=run_id,
        arm_id=arm_id,
        subject_id=subject_id,
        opening_source_idempotency_identity=opening_identity,
        timezone_name="Asia/Shanghai",
        boundary_policy_version="boundary.synthetic.v1",
        collection_start_at=collection_start,
        deterministic_close_deadline_at=collection_start + timedelta(hours=12),
        bed_at=collection_start,
        id_generator=ids,
    )
    finalized = finalize_episode_date(
        opened,
        committed_at=committed_at,
        wake_at=wake_at,
    )
    episode = NightEpisodeV2.model_validate(
        {
            **finalized.model_dump(mode="python"),
            "current_revision": 1,
        }
    )
    revision_id = ids(committed_at)
    operation_id = ids(now)
    observation = SleepObservation(
        observation_id=observation_id,
        data_mode=DataMode.REPLAY,
        observation_type=ObservationType.BED_PRESENCE,
        payload=BedPresencePayload(state=BedPresenceState.OUT_OF_BED),
        subject_id=subject_id,
        device_id=device_id,
        device_binding_id=device_binding_id,
        binding_version=1,
        measurement_at=wake_at,
        event_occurred_at=wake_at,
        received_at=wake_at + timedelta(seconds=1),
        timezone_status=TimezoneStatus.KNOWN,
        source_kind=SourceKind.DEVICE_MEASURED,
        quality=_quality(),
        provenance=ObservationProvenance(
            provider_id="synthetic-replay",
            provider_account_id=provider_account_id,
            adapter_id="synthetic-replay-adapter",
            adapter_version="1.0.0",
            raw_ingress_record_id=raw_id,
            raw_payload_sha256=hashlib.sha256(raw_id.encode()).hexdigest(),
        ),
        source_key=f"source:{suffix}",
        idempotency_key=f"observation:{suffix}",
    )
    source_scope = DeterministicSourceScope(
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=revision_id,
        observation_ids=(observation_id,),
        observation_types=(ObservationType.BED_PRESENCE,),
        device_binding_ids=(device_binding_id,),
        window_start_at=collection_start,
        window_end_at=wake_at + timedelta(seconds=1),
    )
    quality = DeterministicQualityAssessment(
        assessment_id=quality_id,
        data_mode=DataMode.REPLAY,
        subject_id=subject_id,
        night_episode_id=episode.night_episode_id,
        quality_state=QualityState.SUFFICIENT,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        missingness_state=MissingnessState.COMPLETE,
        coverage_ratio=1.0,
        expected_bin_count=1,
        covered_bin_count=1,
        explicit_missing_interval_count=0,
        invalid_observation_count=0,
        stale=False,
        offline=False,
        clock_invalid=False,
        latest_observed_at=wake_at,
        source_scope=source_scope,
        policy_version="quality.synthetic.v1",
        reason_codes=("synthetic_complete",),
        assessed_at=committed_at,
    )
    risk = CurrentRisk(
        current_risk_id=risk_id,
        data_mode=DataMode.REPLAY,
        subject_id=subject_id,
        night_episode_id=episode.night_episode_id,
        risk_state=(
            RiskState.REVIEWED_SIGNAL
            if care_required
            else RiskState.NO_REVIEWED_SIGNAL
        ),
        data_sufficiency=DataSufficiency.SUFFICIENT,
        source_scope=source_scope,
        policy_version="risk.synthetic.v1",
        observed_at=wake_at,
        reason_codes=(
            ("approved_vendor_alert",)
            if care_required
            else ("no_reviewed_signal",)
        ),
        health_escalation_allowed=care_required,
        updated_at=committed_at,
    )
    workload = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": worker_principal,
        "namespace_id": namespace_id,
        "namespace_generation": 1,
        "data_mode": "replay",
        "run_id": run_id,
        "arm_id": arm_id,
        "subject_id": subject_id,
        "purpose": "worker",
        "allowed_handler": "product_agent",
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_policy_epoch": 1,
    }
    operation_payload = {
        "schema_version": "backend_operation.v2",
        "trigger": "synthetic_fast_path_nonurgent",
        "night_episode_id": episode.night_episode_id,
        "night_episode_revision_id": revision_id,
        "quality_assessment_id": quality_id,
        "current_risk_id": risk_id,
        "authorization_snapshot": workload,
    }
    revision_payload = {
        "schema_version": "night_episode_revision.v2",
        "night_episode_revision_id": revision_id,
        "night_episode_id": episode.night_episode_id,
        "revision_number": 1,
        "observation_ids": [observation_id],
        "policy_versions": {
            "boundary": episode.boundary_policy_version,
            "quality": quality.policy_version,
            "risk": risk.policy_version,
        },
    }
    semantic_key = hashlib.sha256(
        f"product:{revision_id}:{policy_sha256}".encode()
    ).hexdigest()

    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_namespaces (
                  namespace_id, data_mode, current_generation, status,
                  synthetic_non_release
                ) VALUES (%s, 'replay', 1, 'active', TRUE)
                """,
                (namespace_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_namespace_generations (
                  namespace_id, data_mode, generation, status,
                  configuration_sha256
                ) VALUES (%s, 'replay', 1, 'active', %s)
                """,
                (
                    namespace_id,
                    hashlib.sha256(namespace_id.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_replay_runs (
                  run_id, namespace_id, data_mode, namespace_generation,
                  scenario_id, scenario_sha256, generation, status,
                  synthetic_non_release
                ) VALUES (%s, %s, 'replay', 1, %s, %s, 1, 'active', TRUE)
                """,
                (
                    run_id,
                    namespace_id,
                    f"scenario-{suffix}",
                    hashlib.sha256(f"scenario:{suffix}".encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_replay_arms (
                  arm_id, run_id, namespace_id, data_mode,
                  namespace_generation, arm_name, configuration_sha256
                ) VALUES (%s, %s, %s, 'replay', 1, 'baseline', %s)
                """,
                (
                    arm_id,
                    run_id,
                    namespace_id,
                    hashlib.sha256(f"arm:{suffix}".encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subjects (
                  namespace_id, data_mode, subject_id, timezone_name, status
                ) VALUES (%s, 'replay', %s, 'Asia/Shanghai', 'active')
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subject_epochs (
                  namespace_id, data_mode, subject_id, authorization_epoch,
                  privacy_epoch, retrieval_policy_epoch
                ) VALUES (%s, 'replay', %s, 1, 1, 1)
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (
                  %s, %s, %s, 'replay', 'worker', '[]'::jsonb,
                  '["product_agent"]'::jsonb, 1, 'active',
                  clock_timestamp() - interval '1 minute'
                )
                """,
                (f"grant-{suffix}", worker_principal, namespace_id),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_provider_accounts (
                  namespace_id, data_mode, provider_account_id, provider_id,
                  configuration_fingerprint, status, account_metadata_json,
                  created_at
                ) VALUES (
                  %s, 'replay', %s, 'synthetic-replay', %s, 'active',
                  '{}'::jsonb, %s
                )
                """,
                (namespace_id, provider_account_id, policy_sha256, collection_start),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_device_bindings (
                  device_binding_id, namespace_id, data_mode, binding_version,
                  device_id, provider_id, provider_account_id, subject_id,
                  timezone_name, effective_from, status, binding_json,
                  recorded_at
                ) VALUES (
                  %s, %s, 'replay', 1, %s, 'synthetic-replay', %s, %s,
                  'Asia/Shanghai', %s, 'active', '{}'::jsonb, %s
                )
                """,
                (
                    device_binding_id,
                    namespace_id,
                    device_id,
                    provider_account_id,
                    subject_id,
                    collection_start - timedelta(days=1),
                    collection_start - timedelta(days=1),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_raw_inbox (
                  raw_ingress_record_id, namespace_id, data_mode, provider_id,
                  provider_account_id, event_type, measurement_at,
                  event_occurred_at, received_at, signature_verification,
                  idempotency_identity, idempotency_version,
                  pre_normalization_payload_sha256, encrypted_payload,
                  encryption_key_id, encrypted_at, content_type,
                  payload_size_bytes, retention_until, raw_metadata_json
                ) VALUES (
                  %s, %s, 'replay', 'synthetic-replay', %s, 'bed_presence',
                  %s, %s, %s, 'verified', %s, 'v1', %s, %s, 'test-key',
                  %s, 'application/json', 1, %s, '{}'::jsonb
                )
                """,
                (
                    raw_id,
                    namespace_id,
                    provider_account_id,
                    wake_at,
                    wake_at,
                    wake_at + timedelta(seconds=1),
                    f"raw:{suffix}",
                    hashlib.sha256(raw_id.encode()).hexdigest(),
                    b"x",
                    wake_at + timedelta(seconds=1),
                    wake_at + timedelta(days=30),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_adapter_candidates (
                  candidate_id, namespace_id, data_mode,
                  raw_ingress_record_id, provider_account_id, source_key,
                  idempotency_key, observation_type, candidate_json,
                  received_at, created_at
                ) VALUES (
                  %s, %s, 'replay', %s, %s, %s, %s, 'bed_presence',
                  '{}'::jsonb, %s, %s
                )
                """,
                (
                    candidate_id,
                    namespace_id,
                    raw_id,
                    provider_account_id,
                    observation.source_key,
                    f"candidate:{suffix}",
                    observation.received_at,
                    observation.received_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_canonical_observations (
                  observation_id, namespace_id, data_mode, candidate_id,
                  raw_ingress_record_id, subject_id, device_id,
                  device_binding_id, binding_version, observation_type,
                  source_key, idempotency_key, observation_json,
                  measurement_at, event_occurred_at, received_at, created_at
                ) VALUES (
                  %s, %s, 'replay', %s, %s, %s, %s, %s, 1,
                  'bed_presence', %s, %s, %s::jsonb, %s, %s, %s, %s
                )
                """,
                (
                    observation_id,
                    namespace_id,
                    candidate_id,
                    raw_id,
                    subject_id,
                    device_id,
                    device_binding_id,
                    observation.source_key,
                    observation.idempotency_key,
                    _canonical_json(observation),
                    observation.measurement_at,
                    observation.event_occurred_at,
                    observation.received_at,
                    observation.received_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episodes (
                  night_episode_id, namespace_id, data_mode, subject_id,
                  night_key, state, current_revision_id,
                  current_revision_number, cas_version, episode_json,
                  created_at, updated_at, protocol_version, id_scheme,
                  namespace_generation, run_id, arm_id, episode_anchor_key,
                  opening_source_idempotency_identity, timezone_name,
                  boundary_policy_version, collection_start_at,
                  deterministic_close_deadline_at, bed_at, wake_at,
                  bed_local_date, wake_local_date, episode_local_date,
                  assignment_basis, date_confidence, assignment_estimated,
                  date_state, date_finalized_at, bed_utc_offset_seconds,
                  wake_utc_offset_seconds, bed_fold, wake_fold,
                  date_conflict
                ) VALUES (
                  %(episode_id)s, %(namespace)s, 'replay', %(subject)s,
                  %(night_key)s, 'closed', %(revision_id)s, 1, 1,
                  %(episode_json)s::jsonb, %(created_at)s, %(updated_at)s,
                  2, 'uuidv7', 1, %(run_id)s, %(arm_id)s, %(anchor)s,
                  %(opening_identity)s, %(timezone_name)s, %(boundary)s,
                  %(collection_start)s, %(deadline)s, %(bed_at)s, %(wake_at)s,
                  %(bed_date)s, %(wake_date)s, %(episode_date)s,
                  %(assignment_basis)s, %(date_confidence)s, FALSE,
                  'finalized', %(finalized_at)s, %(bed_offset)s,
                  %(wake_offset)s, %(bed_fold)s, %(wake_fold)s, FALSE
                )
                """,
                {
                    "episode_id": episode.night_episode_id,
                    "namespace": namespace_id,
                    "subject": subject_id,
                    "night_key": f"night:{suffix}",
                    "revision_id": revision_id,
                    "episode_json": _canonical_json(episode),
                    "created_at": episode.created_at,
                    "updated_at": episode.updated_at,
                    "run_id": run_id,
                    "arm_id": arm_id,
                    "anchor": episode.episode_anchor_key,
                    "opening_identity": opening_identity,
                    "timezone_name": episode.timezone_name,
                    "boundary": episode.boundary_policy_version,
                    "collection_start": episode.collection_start_at,
                    "deadline": episode.deterministic_close_deadline_at,
                    "bed_at": episode.bed_at,
                    "wake_at": episode.wake_at,
                    "bed_date": episode.bed_local_date,
                    "wake_date": episode.wake_local_date,
                    "episode_date": episode.episode_local_date,
                    "assignment_basis": episode.assignment_basis.value,
                    "date_confidence": episode.date_confidence.value,
                    "finalized_at": committed_at,
                    "bed_offset": episode.bed_utc_offset_seconds,
                    "wake_offset": episode.wake_utc_offset_seconds,
                    "bed_fold": episode.bed_fold,
                    "wake_fold": episode.wake_fold,
                },
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episode_revisions (
                  night_episode_revision_id, namespace_id, data_mode,
                  night_episode_id, subject_id, revision_number,
                  revision_json, created_at, protocol_version, id_scheme,
                  namespace_generation, run_id, arm_id, episode_anchor_key,
                  timezone_name, boundary_policy_version, bed_local_date,
                  wake_local_date, episode_local_date, assignment_basis,
                  date_confidence, assignment_estimated, date_state,
                  date_conflict, episode_schema_version
                ) VALUES (
                  %(revision_id)s, %(namespace)s, 'replay', %(episode_id)s,
                  %(subject)s, 1, %(revision_json)s::jsonb, %(created_at)s,
                  2, 'uuidv7', 1, %(run_id)s, %(arm_id)s, %(anchor)s,
                  %(timezone_name)s, %(boundary)s, %(bed_date)s,
                  %(wake_date)s, %(episode_date)s, %(assignment_basis)s,
                  %(date_confidence)s, FALSE, 'finalized', FALSE,
                  'night_episode.v2'
                )
                """,
                {
                    "revision_id": revision_id,
                    "namespace": namespace_id,
                    "episode_id": episode.night_episode_id,
                    "subject": subject_id,
                    "revision_json": _canonical_json(revision_payload),
                    "created_at": committed_at,
                    "run_id": run_id,
                    "arm_id": arm_id,
                    "anchor": episode.episode_anchor_key,
                    "timezone_name": episode.timezone_name,
                    "boundary": episode.boundary_policy_version,
                    "bed_date": episode.bed_local_date,
                    "wake_date": episode.wake_local_date,
                    "episode_date": episode.episode_local_date,
                    "assignment_basis": episode.assignment_basis.value,
                    "date_confidence": episode.date_confidence.value,
                },
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_quality_assessments (
                  assessment_id, namespace_id, data_mode, subject_id,
                  night_episode_id, assessed_at, policy_version,
                  assessment_json
                ) VALUES (%s, %s, 'replay', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    quality_id,
                    namespace_id,
                    subject_id,
                    episode.night_episode_id,
                    quality.assessed_at,
                    quality.policy_version,
                    _canonical_json(quality),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_current_quality (
                  namespace_id, data_mode, subject_id, night_episode_id,
                  assessment_id, cas_version, assessment_json, updated_at
                ) VALUES (%s, 'replay', %s, %s, %s, 1, %s::jsonb, %s)
                """,
                (
                    namespace_id,
                    subject_id,
                    episode.night_episode_id,
                    quality_id,
                    _canonical_json(quality),
                    quality.assessed_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_risk_assessments (
                  current_risk_id, namespace_id, data_mode, subject_id,
                  night_episode_id, observed_at, policy_version, risk_json
                ) VALUES (%s, %s, 'replay', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    risk_id,
                    namespace_id,
                    subject_id,
                    episode.night_episode_id,
                    risk.observed_at,
                    risk.policy_version,
                    _canonical_json(risk),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_current_risk (
                  namespace_id, data_mode, subject_id, night_episode_id,
                  current_risk_id, cas_version, risk_json, updated_at
                ) VALUES (%s, 'replay', %s, %s, %s, 1, %s::jsonb, %s)
                """,
                (
                    namespace_id,
                    subject_id,
                    episode.night_episode_id,
                    risk_id,
                    _canonical_json(risk),
                    risk.updated_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_operations (
                  operation_id, namespace_id, data_mode, operation_type,
                  subject_id, service_principal_id, actor_id,
                  target_resource_id, target_resource_key, idempotency_key,
                  request_sha256, status, attempt_count, cas_version,
                  operation_json, created_at, updated_at, protocol_version,
                  namespace_generation, run_id, arm_id, id_scheme,
                  origin_kind, semantic_key, queue_name, priority,
                  available_at, max_attempts,
                  workload_authorization_snapshot_json, policy_sha256
                ) VALUES (
                  %s, %s, 'replay', 'product_agent', %s, %s, NULL,
                  %s, %s, %s, %s, 'pending', 0, 0, %s::jsonb,
                  %s, %s, 2, 1, %s, %s, 'uuidv7', 'system', %s,
                  'product_agent', 0, %s, 5, %s::jsonb, %s
                )
                """,
                (
                    operation_id,
                    namespace_id,
                    subject_id,
                    worker_principal,
                    episode.night_episode_id,
                    revision_id,
                    semantic_key,
                    policy_sha256,
                    _canonical_json(operation_payload),
                    now - timedelta(seconds=1),
                    now - timedelta(seconds=1),
                    run_id,
                    arm_id,
                    semantic_key,
                    now - timedelta(seconds=1),
                    _canonical_json(workload),
                    policy_sha256,
                ),
            )

    return _ProductSeed(
        namespace_id=namespace_id,
        run_id=run_id,
        arm_id=arm_id,
        subject_id=subject_id,
        operation_id=operation_id,
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=revision_id,
        quality_assessment_id=quality_id,
        current_risk_id=risk_id,
        policy_sha256=policy_sha256,
    )


def _worker_runtime(
    *,
    worker_dsn: str,
    worker_principal: str,
    namespace_id: str,
    pool_max_size: int = 2,
    worker_queues: tuple[str, ...] = ("product_agent",),
) -> tuple[PsycopgPoolProvider, UnitOfWorkFactory[object], PostgresDurableWorkStore]:
    settings = SleepBackendSettings(
        profile="product-postgres-integration",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=BackendDataMode.REPLAY,
        database_dsn=worker_dsn,
        database_identity="integration-database",
        database_role="integration-worker",
        service_principal_id=worker_principal,
        database_scope=BackendDataMode.REPLAY,
        namespace_prefixes=(namespace_id,),
        worker_queues=worker_queues,
        model_mode=ModelMode.DETERMINISTIC,
        service_credential_ref="test:worker-service",
        signing_key_ref="test:worker-signing",
        encryption_key_ref="test:worker-encryption",
        pool_min_size=1,
        pool_max_size=pool_max_size,
    )
    provider = PsycopgPoolProvider.from_dsn(
        worker_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=pool_max_size),
        application_name="sleepagent-product-postgres-integration",
    )
    provider.open()
    factory: UnitOfWorkFactory[object] = UnitOfWorkFactory(provider)
    return provider, factory, PostgresDurableWorkStore(settings, factory)


def _clone_pending_product_operations(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
    count: int,
) -> None:
    ids = UUID7Generator()
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            for index in range(count):
                operation_id = ids()
                semantic_key = hashlib.sha256(
                    f"load:{seed.operation_id}:{index}".encode()
                ).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_operations (
                      operation_id, namespace_id, data_mode, operation_type,
                      subject_id, service_principal_id, actor_id,
                      target_resource_id, target_resource_key, idempotency_key,
                      request_sha256, status, attempt_count, cas_version,
                      operation_json, created_at, updated_at, protocol_version,
                      namespace_generation, run_id, arm_id, id_scheme,
                      origin_kind, semantic_key, queue_name, priority,
                      available_at, max_attempts,
                      workload_authorization_snapshot_json, policy_sha256
                    )
                    SELECT
                      %s, namespace_id, data_mode, operation_type,
                      subject_id, service_principal_id, NULL,
                      target_resource_id, target_resource_key || %s,
                      idempotency_key || %s, request_sha256,
                      'pending', 0, 0,
                      operation_json || jsonb_build_object(
                        'operation_id', %s::text
                      ),
                      clock_timestamp(), clock_timestamp(), protocol_version,
                      namespace_generation, run_id, arm_id, id_scheme,
                      origin_kind, %s, queue_name, priority,
                      clock_timestamp(), max_attempts,
                      workload_authorization_snapshot_json, policy_sha256
                    FROM public.sleep_domain_operations
                    WHERE operation_id = %s
                    """,
                    (
                        operation_id,
                        f":load:{index}",
                        f":load:{index}",
                        operation_id,
                        semantic_key,
                        seed.operation_id,
                    ),
                )
                assert cursor.rowcount == 1


def _claim_product_work(
    store: PostgresDurableWorkStore,
    *,
    worker_instance: str,
    operation_type: str = "product_agent",
) -> LeaseClaim:
    claim = store.claim(
        queue="product_agent",
        worker_instance=worker_instance,
        lease_seconds=300,
    )
    assert claim is not None
    assert claim.metadata == {
        "work_kind": "operation",
        "lease_seconds": 300,
        "operation_type": operation_type,
        "queue_name": "product_agent",
    }
    return claim


def _convert_seed_to_report_request(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
) -> None:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET operation_type = %s,
                    operation_json = operation_json || jsonb_build_object(
                      'command_type', %s::text,
                      'wake_date', (
                        SELECT episode_local_date::text
                        FROM public.sleep_domain_night_episodes
                        WHERE night_episode_id = %s
                      )
                    )
                WHERE operation_id = %s AND status = 'pending'
                """,
                (
                    PRODUCT_REPORT_RUN_OPERATION,
                    PRODUCT_REPORT_RUN_OPERATION,
                    seed.night_episode_id,
                    seed.operation_id,
                ),
            )
            assert cursor.rowcount == 1


def _clone_report_request(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
) -> str:
    operation_id = str(UUID7Generator()())
    suffix = uuid4().hex
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_operations (
                  operation_id, namespace_id, data_mode, operation_type,
                  subject_id, service_principal_id, actor_id,
                  target_resource_id, target_resource_key, idempotency_key,
                  request_sha256, status, attempt_count, cas_version,
                  operation_json, created_at, updated_at, protocol_version,
                  namespace_generation, run_id, arm_id, id_scheme,
                  origin_kind, semantic_key, queue_name, priority,
                  available_at, max_attempts,
                  workload_authorization_snapshot_json, policy_sha256
                )
                SELECT %s, namespace_id, data_mode, operation_type,
                       subject_id, service_principal_id, actor_id,
                       target_resource_id, target_resource_key || %s,
                       idempotency_key || %s, request_sha256,
                       'pending', 0, 0,
                       operation_json || jsonb_build_object(
                         'request_variant', %s::text
                       ),
                       clock_timestamp(), clock_timestamp(), protocol_version,
                       namespace_generation, run_id, arm_id, id_scheme,
                       origin_kind, %s, queue_name, priority,
                       clock_timestamp(), max_attempts,
                       workload_authorization_snapshot_json, policy_sha256
                FROM public.sleep_domain_operations
                WHERE operation_id = %s
                """,
                (
                    operation_id,
                    f":{suffix}",
                    f":{suffix}",
                    suffix,
                    hashlib.sha256(f"report:{suffix}".encode()).hexdigest(),
                    seed.operation_id,
                ),
            )
            assert cursor.rowcount == 1
    return operation_id


def _replace_current_risk(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
    urgent: bool,
    update_operation: bool,
) -> str:
    risk_id = f"risk-gate-{uuid4().hex}"
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT risk_json
                FROM public.sleep_domain_current_risk
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_id = %s
                """,
                (seed.namespace_id, seed.subject_id, seed.night_episode_id),
            )
            current = CurrentRisk.model_validate(cursor.fetchone()[0])
            replacement = current.model_copy(
                update={
                    "current_risk_id": risk_id,
                    "risk_state": (
                        RiskState.REVIEWED_SIGNAL
                        if urgent
                        else RiskState.NO_REVIEWED_SIGNAL
                    ),
                    "reason_codes": (
                        "acceptance_urgent"
                        if urgent
                        else "acceptance_nonurgent",
                    ),
                    "health_escalation_allowed": urgent,
                    "updated_at": datetime.now(tz=UTC),
                }
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_risk_assessments (
                  current_risk_id, namespace_id, data_mode, subject_id,
                  night_episode_id, observed_at, policy_version, risk_json
                ) VALUES (%s, %s, 'replay', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    risk_id,
                    seed.namespace_id,
                    seed.subject_id,
                    seed.night_episode_id,
                    replacement.observed_at,
                    replacement.policy_version,
                    replacement.model_dump_json(),
                ),
            )
            cursor.execute(
                """
                UPDATE public.sleep_domain_current_risk
                SET current_risk_id = %s, cas_version = cas_version + 1,
                    risk_json = %s::jsonb, updated_at = %s
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_id = %s
                """,
                (
                    risk_id,
                    replacement.model_dump_json(),
                    replacement.updated_at,
                    seed.namespace_id,
                    seed.subject_id,
                    seed.night_episode_id,
                ),
            )
            assert cursor.rowcount == 1
            if update_operation:
                cursor.execute(
                    """
                    UPDATE public.sleep_domain_operations
                    SET operation_json = jsonb_set(
                      operation_json, '{current_risk_id}', to_jsonb(%s::text)
                    )
                    WHERE operation_id = %s
                    """,
                    (risk_id, seed.operation_id),
                )
                assert cursor.rowcount == 1
    return risk_id


def _replace_current_quality(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
    usable: bool,
    update_operation: bool,
) -> str:
    quality_id = f"quality-gate-{uuid4().hex}"
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT assessment_json
                FROM public.sleep_domain_current_quality
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_id = %s
                """,
                (seed.namespace_id, seed.subject_id, seed.night_episode_id),
            )
            current = DeterministicQualityAssessment.model_validate(
                cursor.fetchone()[0]
            )
            replacement = current.model_copy(
                update={
                    "assessment_id": quality_id,
                    "quality_state": (
                        QualityState.SUFFICIENT
                        if usable
                        else QualityState.DATA_INSUFFICIENT
                    ),
                    "data_sufficiency": (
                        DataSufficiency.SUFFICIENT
                        if usable
                        else DataSufficiency.DATA_INSUFFICIENT
                    ),
                    "coverage_ratio": 1.0 if usable else 0.0,
                    "covered_bin_count": (
                        current.expected_bin_count if usable else 0
                    ),
                    "reason_codes": (
                        "acceptance_usable"
                        if usable
                        else "acceptance_unusable",
                    ),
                    "assessed_at": datetime.now(tz=UTC),
                }
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_quality_assessments (
                  assessment_id, namespace_id, data_mode, subject_id,
                  night_episode_id, assessed_at, policy_version,
                  assessment_json
                ) VALUES (%s, %s, 'replay', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    quality_id,
                    seed.namespace_id,
                    seed.subject_id,
                    seed.night_episode_id,
                    replacement.assessed_at,
                    replacement.policy_version,
                    replacement.model_dump_json(),
                ),
            )
            cursor.execute(
                """
                UPDATE public.sleep_domain_current_quality
                SET assessment_id = %s, cas_version = cas_version + 1,
                    assessment_json = %s::jsonb, updated_at = %s
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_id = %s
                """,
                (
                    quality_id,
                    replacement.model_dump_json(),
                    replacement.assessed_at,
                    seed.namespace_id,
                    seed.subject_id,
                    seed.night_episode_id,
                ),
            )
            assert cursor.rowcount == 1
            if update_operation:
                cursor.execute(
                    """
                    UPDATE public.sleep_domain_operations
                    SET operation_json = jsonb_set(
                      operation_json, '{quality_assessment_id}',
                      to_jsonb(%s::text)
                    )
                    WHERE operation_id = %s
                    """,
                    (quality_id, seed.operation_id),
                )
                assert cursor.rowcount == 1
    return quality_id


def _cancel_pending_product_work(
    psycopg: object,
    *,
    admin_dsn: str,
    namespace_id: str,
) -> None:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET status = 'cancelled', outcome_class = 'cancelled',
                    lease_owner = NULL, lease_expires_at = NULL,
                    fencing_token = NULL, worker_instance = NULL,
                    heartbeat_at = NULL, updated_at = clock_timestamp()
                WHERE namespace_id = %s AND queue_name = 'product_agent'
                  AND status IN ('pending', 'retry', 'running')
                """,
                (namespace_id,),
            )


def _prepare_shared_acceptance_case(
    psycopg: object,
    *,
    admin_dsn: str,
    worker_dsn: str,
    worker_principal: str,
    report_pipeline_mode: ReportPipelineMode = ReportPipelineMode.SHARED_COMPAT,
) -> _PreparedSharedCase:
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    _convert_seed_to_report_request(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )
    bundle = _deterministic_runtime_bundle()
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    processor = ProductAgentProcessor(
        factory,
        runtime_bundle=bundle,
        report_pipeline_mode=report_pipeline_mode,
    )
    report_claim = _claim_product_work(
        store,
        worker_instance=f"l2-report-{uuid4().hex}",
        operation_type=PRODUCT_REPORT_RUN_OPERATION,
    )
    routed = processor.route_report_request(
        store.uow_scope_for_claim(report_claim),
        _lease_for_claim(report_claim),
    )
    assert routed.state == "pending"
    assert routed.shared_operation_created is True

    shared_claim = _claim_product_work(
        store,
        worker_instance=f"l2-shared-{uuid4().hex}",
        operation_type=PRODUCT_SHARED_ANALYSIS_OPERATION,
    )
    scope = store.uow_scope_for_claim(shared_claim)
    lease = _lease_for_claim(shared_claim)
    source = processor.load_source(scope, lease)
    artifact = processor.prepare_shared(
        scope=scope,
        source=source,
        lease=lease,
        prepared_at=datetime.now(tz=UTC),
    )
    _persist_prepared(factory, scope, lease, artifact)
    return _PreparedSharedCase(
        seed=seed,
        provider=provider,
        factory=factory,
        store=store,
        bundle=bundle,
        processor=processor,
        scope=scope,
        lease=lease,
        source=source,
        artifact=artifact,
    )


def test_shadow_mode_persists_structured_parity_without_legacy_publication() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    case = _prepare_shared_acceptance_case(
        psycopg,
        admin_dsn=admin_dsn,
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        report_pipeline_mode=ReportPipelineMode.SHADOW,
    )

    comparison = case.artifact.shadow_comparison
    assert comparison is not None
    assert comparison.authoritative_path == "shared"
    assert comparison.external_side_effects_permitted is False
    assert comparison.mismatch_categories == ()
    assert set(comparison.category_material_sha256) == {
        "fact_identities",
        "metric_values_units",
        "quality_status",
        "risk_classification",
        "care_candidate_semantics",
        "role_visible_fact_sets",
        "source_references",
    }
    assert (
        comparison.category_material_sha256["metric_values_units"]["legacy"]
        == comparison.category_material_sha256["metric_values_units"]["shared"]
    )

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_json #>> '{shadow_comparison,comparison_sha256}'
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s
                """,
                (case.artifact.product_attempt_id,),
            )
            assert cursor.fetchone() == (comparison.comparison_sha256,)
            cursor.execute(
                """
                SELECT count(*)
                FROM public.sleep_domain_analysis_revisions
                WHERE namespace_id = %s
                """,
                (case.seed.namespace_id,),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                """
                SELECT count(*)
                FROM public.sleep_domain_analysis_role_views
                WHERE namespace_id = %s
                """,
                (case.seed.namespace_id,),
            )
            assert cursor.fetchone() == (0,)
    _cancel_pending_product_work(
        psycopg,
        admin_dsn=admin_dsn,
        namespace_id=case.seed.namespace_id,
    )


def test_shared_only_commit_creates_one_shared_result_without_legacy_bridge() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    case = _prepare_shared_acceptance_case(
        psycopg,
        admin_dsn=admin_dsn,
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        report_pipeline_mode=ReportPipelineMode.SHARED_ONLY,
    )
    case.processor.persist_and_commit(
        case.scope,
        case.lease,
        case.artifact,
        source=case.source,  # type: ignore[arg-type]
    )

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  count(*) FILTER (
                    WHERE operation_type = 'product.report.run.v1'
                  ),
                  count(*) FILTER (
                    WHERE operation_type = 'product.shared_analysis.v1'
                  ),
                  count(*) FILTER (
                    WHERE operation_type = 'product_agent'
                       OR queue_name = 'product_agent_compatibility'
                  )
                FROM public.sleep_domain_operations
                WHERE namespace_id = %s AND subject_id = %s
                """,
                (case.seed.namespace_id, case.seed.subject_id),
            )
            assert cursor.fetchone() == (1, 1, 0)
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT view_json #>> '{role}')
                FROM public.sleep_domain_analysis_role_views
                WHERE namespace_id = %s AND subject_id = %s
                """,
                (case.seed.namespace_id, case.seed.subject_id),
            )
            assert cursor.fetchone() == (3, 3)
    case.provider.close()
    _cancel_pending_product_work(
        psycopg,
        admin_dsn=admin_dsn,
        namespace_id=case.seed.namespace_id,
    )


def test_normalization_and_finalization_handoffs_converge_on_one_shared_analysis() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    default_policy = default_sleep_slice_policy()
    fast_path_policy = replace(
        default_policy,
        quality=default_policy.quality.model_copy(
            update={"coverage_cadence_seconds": 86_400}
        ),
    )
    normalization_workload = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": worker_principal,
        "namespace_id": seed.namespace_id,
        "namespace_generation": 1,
        "data_mode": "replay",
        "run_id": seed.run_id,
        "arm_id": seed.arm_id,
        "subject_id": seed.subject_id,
        "purpose": "worker",
        "allowed_handler": "fast_path",
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_policy_epoch": 1,
    }
    normalization_operation = {
        "schema_version": "backend_operation.v2",
        "trigger": "normalization",
        "night_episode_id": seed.night_episode_id,
        "night_episode_revision_id": seed.night_episode_revision_id,
        "authorization_snapshot": normalization_workload,
    }
    normalization_semantic = hashlib.sha256(
        (
            "normalization-fast-path:"
            f"{seed.namespace_id}:{seed.night_episode_revision_id}"
        ).encode()
    ).hexdigest()
    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET operation_type = 'fast_path',
                  idempotency_key = %s, request_sha256 = %s,
                  operation_json = %s::jsonb, semantic_key = %s,
                  queue_name = 'fast_path', priority = 0,
                  workload_authorization_snapshot_json = %s::jsonb,
                  policy_sha256 = %s
                WHERE operation_id = %s AND status = 'pending'
                """,
                (
                    normalization_semantic,
                    normalization_semantic,
                    json.dumps(normalization_operation),
                    normalization_semantic,
                    json.dumps(normalization_workload),
                    fast_path_policy.policy_sha256,
                    seed.operation_id,
                ),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                UPDATE public.backend_principal_grants
                SET allowed_handlers_json = '["fast_path","product_agent"]'::jsonb
                WHERE namespace_id = %s AND principal_id = %s
                  AND purpose = 'worker'
                """,
                (seed.namespace_id, worker_principal),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                UPDATE public.sleep_domain_night_episode_revisions
                SET revision_json = jsonb_set(
                  revision_json,
                  '{policy_versions}',
                  %s::jsonb
                )
                WHERE night_episode_revision_id = %s
                """,
                (
                    json.dumps(fast_path_policy.policy_versions),
                    seed.night_episode_revision_id,
                ),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                SELECT deterministic_close_deadline_at
                FROM public.sleep_domain_night_episodes
                WHERE night_episode_id = %s
                """,
                (seed.night_episode_id,),
            )
            close_deadline = cursor.fetchone()[0]

    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
        worker_queues=("fast_path", "product_agent"),
    )
    try:
        finalization_scope = UowScope(
            namespace_id=seed.namespace_id,
            data_mode="replay",
            process_role="worker",
            purpose="worker",
            service_principal_id=worker_principal,
            namespace_generation=1,
            run_id=seed.run_id,
            arm_id=seed.arm_id,
            subject_id=seed.subject_id,
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
            worker_instance="first-finalization-test",
        )
        finalizer = NightFinalizationService(
            factory,
            policy=NightFinalizationPolicy(minimum_observation_count=0),
        )
        soft_at = close_deadline + timedelta(hours=3)
        first = finalizer.finalize(
            finalization_scope,
            night_episode_id=seed.night_episode_id,
            evaluated_at=soft_at,
        )
        hard_at = close_deadline + timedelta(hours=25)
        hard = finalizer.finalize(
            finalization_scope,
            night_episode_id=seed.night_episode_id,
            evaluated_at=hard_at,
        )
        replayed = finalizer.finalize(
            finalization_scope,
            night_episode_id=seed.night_episode_id,
            evaluated_at=hard_at,
        )
        assert replayed == hard
        assert first.finalization_revision_number == 1
        assert first.parent_finalization_revision_id is None
        assert first.reanalysis_operation_id is not None
        assert first.state is NightFinalizationState.SOFT_FINALIZED
        assert hard.finalization_revision_number == 2
        assert (
            hard.parent_finalization_revision_id
            == first.night_finalization_revision_id
        )
        assert hard.source_night_episode_revision_id == (
            first.source_night_episode_revision_id
        )
        assert hard.reanalysis_operation_id is not None
        assert hard.state is NightFinalizationState.HARD_FINALIZED
        # The synthetic seed's deterministic close deadline is in the future;
        # make all three independently committed handoffs due in a fixed order.
        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE public.sleep_domain_operations
                    SET available_at = clock_timestamp(),
                      priority = CASE operation_id
                        WHEN %s THEN 300
                        WHEN %s THEN 200
                        WHEN %s THEN 100
                      END
                    WHERE operation_id IN (%s, %s, %s)
                      AND status = 'pending'
                    """,
                    (
                        seed.operation_id,
                        first.reanalysis_operation_id,
                        hard.reanalysis_operation_id,
                        seed.operation_id,
                        first.reanalysis_operation_id,
                        hard.reanalysis_operation_id,
                    ),
                )
                assert cursor.rowcount == 3

        report_operation_ids: list[str] = []
        for index, expected_operation_id in enumerate(
            (
                seed.operation_id,
                first.reanalysis_operation_id,
                hard.reanalysis_operation_id,
            )
        ):
            fast_claim = store.claim(
                queue="fast_path",
                worker_instance=f"convergent-fast-path-{index}",
                lease_seconds=300,
            )
            assert fast_claim is not None
            assert fast_claim.work_id == expected_operation_id
            fast_result = FastPathHandler(
                factory,
                policy=fast_path_policy,
                emit_legacy_report_compatibility=False,
            ).process(
                store.uow_scope_for_claim(fast_claim),
                FastPathLease(
                    operation_id=fast_claim.work_id,
                    lease_generation=fast_claim.lease_generation,
                    fencing_token=fast_claim.fencing_token,
                    worker_instance=fast_claim.worker_instance,
                ),
            )
            assert fast_result.report_operation_id is not None
            assert fast_result.product_agent_operation_id is None
            report_operation_ids.append(fast_result.report_operation_id)
        assert len(set(report_operation_ids)) == 1
        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.sleep_domain_operations SET priority = 10000 "
                    "WHERE operation_id = %s",
                    (report_operation_ids[0],),
                )
                assert cursor.rowcount == 1

        processor = ProductAgentProcessor(
            factory,
            runtime_bundle=_deterministic_runtime_bundle(),
            report_pipeline_mode=ReportPipelineMode.SHARED_ONLY,
        )
        report_claim = _claim_product_work(
            store,
            worker_instance="first-finalization-report",
            operation_type=PRODUCT_REPORT_RUN_OPERATION,
        )
        routed = processor.route_report_request(
            store.uow_scope_for_claim(report_claim),
            _lease_for_claim(report_claim),
        )
        assert routed.shared_operation_created is True
        assert routed.shared_operation_id is not None
        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.sleep_domain_operations SET priority = 20000 "
                    "WHERE operation_id = %s",
                    (routed.shared_operation_id,),
                )
                assert cursor.rowcount == 1

        shared_claim = _claim_product_work(
            store,
            worker_instance="first-finalization-shared",
            operation_type=PRODUCT_SHARED_ANALYSIS_OPERATION,
        )
        shared_scope = store.uow_scope_for_claim(shared_claim)
        shared_lease = _lease_for_claim(shared_claim)
        source = processor.load_source(shared_scope, shared_lease)
        artifact = processor.prepare_shared(
            scope=shared_scope,
            source=source,
            lease=shared_lease,
            prepared_at=datetime.now(tz=UTC),
        )
        processor.persist_and_commit(
            shared_scope,
            shared_lease,
            artifact,
            source=source,
        )

        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                      count(*) FILTER (
                        WHERE operation_type = 'fast_path'
                      ),
                      count(*) FILTER (
                        WHERE operation_type = 'product.report.run.v1'
                      ),
                      count(*) FILTER (
                        WHERE operation_type = 'product.shared_analysis.v1'
                      ),
                      count(*) FILTER (
                        WHERE operation_type = 'product_agent'
                           OR queue_name = 'product_agent_compatibility'
                      )
                    FROM public.sleep_domain_operations
                    WHERE namespace_id = %s AND subject_id = %s
                    """,
                    (seed.namespace_id, seed.subject_id),
                )
                assert cursor.fetchone() == (3, 1, 1, 0)
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.sleep_domain_analysis_revisions
                    WHERE namespace_id = %s AND subject_id = %s
                    """,
                    (seed.namespace_id, seed.subject_id),
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    """
                    SELECT count(*), count(DISTINCT view_json #>> '{role}')
                    FROM public.sleep_domain_analysis_role_views
                    WHERE namespace_id = %s AND subject_id = %s
                    """,
                    (seed.namespace_id, seed.subject_id),
                )
                assert cursor.fetchone() == (3, 3)
    finally:
        _cancel_pending_product_work(
            psycopg,
            admin_dsn=admin_dsn,
            namespace_id=seed.namespace_id,
        )
        provider.close()


def _lease_for_claim(claim: LeaseClaim) -> ProductAgentLease:
    return ProductAgentLease(
        operation_id=claim.work_id,
        attempt_sequence=claim.attempt,
        lease_generation=claim.lease_generation,
        fencing_token=claim.fencing_token,
        worker_instance=claim.worker_instance,
    )


def _admin_execute(
    psycopg: object,
    admin_dsn: str,
    sql: str,
    params: tuple[object, ...],
) -> None:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(sql, params)


def _seed_l2_personalization(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
) -> tuple[HabitFact, GovernedMemoryItemV2]:
    now = datetime.now(tz=UTC)
    unsigned_habit = HabitFact(
        fact_id=f"habit-fact:{uuid4().hex}",
        fact_hash="0" * 64,
        revision=1,
        operation=HabitOperation.REMEMBER,
        subject_id=seed.subject_id,
        concept_id="habit.delivery_modality_preference",
        concept_version="1.0.0",
        value="语音",
        value_hash="b" * 64,
        confirmed_at=now,
        valid_until=now + timedelta(days=90),
        confirmation_ref=f"confirmation:habit:{uuid4().hex}",
        change_id=f"habit-change:{uuid4().hex}",
    )
    habit = unsigned_habit.model_copy(
        update={
            "fact_hash": stable_hash(
                unsigned_habit.model_dump(
                    mode="json",
                    exclude={"fact_hash"},
                )
            )
        }
    )
    memory = GovernedMemoryItemV2(
        memory_id=f"memory:{uuid4().hex}",
        subject_id=seed.subject_id,
        memory_type="communication_preference",
        concept_id="sleep.preference.care_delivery",
        value_schema_id="enum.v1",
        typed_value="morning_voice",
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        source_ref=f"user_report:{uuid4().hex}",
        source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        version=1,
        recorded_at=now,
        valid_from=now,
        sensitivity_class=SensitivityClass.PERSONAL,
        allowed_roles=(AgentId.CARE_STRATEGY,),
        allowed_purposes=(MemoryPurpose.CARE_PREFERENCE_CONTEXT,),
        confirmation_ref=f"confirmation:memory:{uuid4().hex}",
        retention_policy_version="sleepagent-retention.v1",
    )
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_habit_profile_revisions_v2 (
                  fact_id, namespace_id, data_mode, namespace_generation,
                  run_id, arm_id, subject_id, profile_version, concept_id,
                  concept_version, operation, fact_sha256, fact_json,
                  confirmation_ref, committed_at
                ) VALUES (
                  %s, %s, 'replay', 1, %s, %s, %s, 1, %s, %s, 'remember',
                  %s, %s::jsonb, %s, %s
                )
                """,
                (
                    habit.fact_id,
                    seed.namespace_id,
                    seed.run_id,
                    seed.arm_id,
                    seed.subject_id,
                    habit.concept_id,
                    habit.concept_version,
                    habit.fact_hash,
                    habit.model_dump_json(),
                    habit.confirmation_ref,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_governed_memory_revisions_v2 (
                  revision_ref, namespace_id, data_mode, namespace_generation,
                  run_id, arm_id, subject_id, state_version, memory_id,
                  memory_version, concept_id, status, revision_sha256,
                  revision_json, confirmation_ref, committed_at
                ) VALUES (
                  %s, %s, 'replay', 1, %s, %s, %s, 1, %s, 1, %s,
                  'active', %s, %s::jsonb, %s, %s
                )
                """,
                (
                    memory.revision_ref,
                    seed.namespace_id,
                    seed.run_id,
                    seed.arm_id,
                    seed.subject_id,
                    memory.memory_id,
                    memory.concept_id,
                    stable_hash(memory),
                    memory.model_dump_json(),
                    memory.confirmation_ref,
                    now,
                ),
            )
    return habit, memory


def _append_l2_corrections(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
    prior_habit: HabitFact,
    prior_memory: GovernedMemoryItemV2,
) -> tuple[HabitFact, GovernedMemoryItemV2]:
    now = datetime.now(tz=UTC)
    unsigned_habit = HabitFact(
        fact_id=f"habit-fact:{uuid4().hex}",
        fact_hash="0" * 64,
        revision=2,
        operation=HabitOperation.CORRECT,
        subject_id=seed.subject_id,
        concept_id=prior_habit.concept_id,
        concept_version=prior_habit.concept_version,
        value="灯光",
        value_hash="c" * 64,
        confirmed_at=now,
        valid_until=now + timedelta(days=90),
        confirmation_ref=f"confirmation:habit:{uuid4().hex}",
        change_id=f"habit-change:{uuid4().hex}",
        replaces_fact_id=prior_habit.fact_id,
    )
    habit = unsigned_habit.model_copy(
        update={
            "fact_hash": stable_hash(
                unsigned_habit.model_dump(
                    mode="json",
                    exclude={"fact_hash"},
                )
            )
        }
    )
    memory_values = prior_memory.model_dump(mode="python")
    memory_values.update(
        typed_value="evening_light",
        value_hash=None,
        version=2,
        recorded_at=now,
        valid_from=now,
        supersedes_ref=prior_memory.revision_ref,
        confirmation_ref=f"confirmation:memory:{uuid4().hex}",
    )
    memory = GovernedMemoryItemV2.model_validate(memory_values)
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_habit_profile_revisions_v2 (
                  fact_id, namespace_id, data_mode, namespace_generation,
                  run_id, arm_id, subject_id, profile_version, concept_id,
                  concept_version, operation, fact_sha256, fact_json,
                  confirmation_ref, committed_at
                ) VALUES (
                  %s, %s, 'replay', 1, %s, %s, %s, 2, %s, %s, 'correct',
                  %s, %s::jsonb, %s, %s
                )
                """,
                (
                    habit.fact_id,
                    seed.namespace_id,
                    seed.run_id,
                    seed.arm_id,
                    seed.subject_id,
                    habit.concept_id,
                    habit.concept_version,
                    habit.fact_hash,
                    habit.model_dump_json(),
                    habit.confirmation_ref,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_governed_memory_revisions_v2 (
                  revision_ref, namespace_id, data_mode, namespace_generation,
                  run_id, arm_id, subject_id, state_version, memory_id,
                  memory_version, concept_id, status, revision_sha256,
                  revision_json, confirmation_ref, committed_at
                ) VALUES (
                  %s, %s, 'replay', 1, %s, %s, %s, 2, %s, 2, %s,
                  'active', %s, %s::jsonb, %s, %s
                )
                """,
                (
                    memory.revision_ref,
                    seed.namespace_id,
                    seed.run_id,
                    seed.arm_id,
                    seed.subject_id,
                    memory.memory_id,
                    memory.concept_id,
                    stable_hash(memory),
                    memory.model_dump_json(),
                    memory.confirmation_ref,
                    now,
                ),
            )
    return habit, memory


def _expire_operation(psycopg: object, admin_dsn: str, operation_id: str) -> None:
    _admin_execute(
        psycopg,
        admin_dsn,
        "UPDATE public.sleep_domain_operations SET lease_expires_at = "
        "clock_timestamp() - interval '1 second' WHERE operation_id = %s",
        (operation_id,),
    )


def _persist_prepared(
    factory: UnitOfWorkFactory[object],
    scope: UowScope,
    lease: ProductAgentLease,
    artifact: PreparedProductAgentArtifact,
) -> None:
    with factory.begin(scope) as uow:
        repository = PostgresProductAgentRepository(uow.connection, scope)
        repository.persist_prepared(lease, artifact)
        uow.commit()


class _PreparedProcessCrash(RuntimeError):
    pass


class _PersistPreparedThenCrashProcessor(ProductAgentProcessor):
    persisted_artifact: PreparedProductAgentArtifact | None = None

    def persist_and_commit(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
        *,
        source: object | None = None,
    ) -> NoReturn:
        self.persisted_artifact = artifact
        _persist_prepared(self.uow_factory, scope, lease, artifact)
        raise _PreparedProcessCrash


def _prepared_attempt_snapshot(
    psycopg: object,
    *,
    admin_dsn: str,
    product_attempt_id: str,
) -> tuple[object, int, int, str]:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT to_jsonb(attempt) - ARRAY[
                         'attempt_sequence', 'lease_generation', 'fencing_token'
                       ],
                       attempt_sequence, lease_generation, fencing_token
                FROM public.backend_product_attempts AS attempt
                WHERE product_attempt_id = %s
                """,
                (product_attempt_id,),
            )
            row = cursor.fetchone()
    assert row is not None
    return row[0], int(row[1]), int(row[2]), str(row[3])


def _invocation_snapshot(
    psycopg: object,
    *,
    admin_dsn: str,
    operation_id: str,
) -> tuple[tuple[object, ...], ...]:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT invocation.invocation_id, invocation.invocation_key,
                       invocation.request_sha256, invocation.current_state,
                       invocation.response_sha256, journal.sequence,
                       journal.to_state, journal.lease_generation,
                       journal.fencing_token, journal.event_json
                FROM public.backend_invocations AS invocation
                JOIN public.backend_invocation_journal AS journal
                  ON journal.invocation_id = invocation.invocation_id
                WHERE invocation.operation_id = %s
                ORDER BY journal.sequence
                """,
                (operation_id,),
            )
            rows = cursor.fetchall()
    assert rows
    return tuple(tuple(row) for row in rows)


def _assert_committed_closure(
    psycopg: object,
    *,
    admin_dsn: str,
    seed: _ProductSeed,
    analysis_revision_id: str,
    product_attempt_id: str,
    analysis_status: str,
) -> None:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_state, query_visible,
                       committed_at IS NOT NULL,
                       night_episode_revision_id, policy_sha256,
                       (SELECT count(*)
                        FROM public.backend_product_attempts AS all_attempts
                        WHERE all_attempts.operation_id = %s)
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s AND operation_id = %s
                """,
                (seed.operation_id, product_attempt_id, seed.operation_id),
            )
            assert cursor.fetchone() == (
                "committed",
                True,
                True,
                seed.night_episode_revision_id,
                seed.policy_sha256,
                1,
            )
            cursor.execute(
                """
                SELECT status, outcome_class,
                       operation_json -> 'result' ->> 'analysis_revision_id'
                FROM public.sleep_domain_operations
                WHERE operation_id = %s
                """,
                (seed.operation_id,),
            )
            assert cursor.fetchone() == (
                "succeeded",
                "succeeded",
                analysis_revision_id,
            )
            cursor.execute(
                """
                SELECT night_episode_id, night_episode_revision_id, subject_id,
                       revision_number,
                       (SELECT count(*)
                        FROM public.sleep_domain_analysis_revisions AS revisions
                        WHERE revisions.night_episode_revision_id = %s)
                FROM public.sleep_domain_analysis_revisions
                WHERE analysis_revision_id = %s
                """,
                (seed.night_episode_revision_id, analysis_revision_id),
            )
            assert cursor.fetchone() == (
                seed.night_episode_id,
                seed.night_episode_revision_id,
                seed.subject_id,
                1,
                1,
            )
            cursor.execute(
                """
                SELECT role, analysis_revision_id,
                       night_episode_revision_id, protocol_version,
                       namespace_generation, run_id, arm_id,
                       source_state_version, authorization_epoch,
                       privacy_epoch, retrieval_policy_epoch, policy_sha256
                FROM public.sleep_domain_analysis_role_views
                WHERE analysis_revision_id = %s
                ORDER BY role
                """,
                (analysis_revision_id,),
            )
            role_rows = cursor.fetchall()
            assert {row[0] for row in role_rows} == {"elder", "family", "doctor"}
            assert len(role_rows) == 3
            assert all(
                row[1:] == (
                    analysis_revision_id,
                    seed.night_episode_revision_id,
                    2,
                    1,
                    seed.run_id,
                    seed.arm_id,
                    1,
                    1,
                    1,
                    1,
                    seed.policy_sha256,
                )
                for row in role_rows
            )
            cursor.execute(
                """
                SELECT role, subject_id, view_json, public_today_json
                FROM public.sleep_domain_analysis_role_views
                WHERE analysis_revision_id = %s
                ORDER BY role
                """,
                (analysis_revision_id,),
            )
            public_rows = cursor.fetchall()
            assert len(public_rows) == 3
            for role, subject_id, internal_view, public_view in public_rows:
                assert internal_view["subject_id"] == subject_id
                assert public_view["subject_ref"] == public_product_subject_ref(
                    subject_id
                )
                assert public_view["subject_ref"] != subject_id
                assert set(public_view) == PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST
                assert "source_refs" not in public_view
                assert "claim_refs" not in public_view
                assert "product_agent_episode_id" not in public_view
                if role == "doctor":
                    assert public_view["content"]["evidence_refs"] == []
                else:
                    assert "evidence_refs" not in public_view["content"]
            cursor.execute(
                """
                SELECT event_type, aggregate_type, aggregate_id, status,
                       protocol_version, namespace_generation, run_id, arm_id
                FROM public.sleep_domain_domain_outbox
                WHERE operation_id = %s
                  AND aggregate_type = 'AnalysisRevision'
                """,
                (seed.operation_id,),
            )
            expected_event_type = (
                "AGENT_ANALYSIS_READY"
                if analysis_status == "ready"
                else "AGENT_ANALYSIS_DEGRADED"
            )
            assert cursor.fetchall() == [
                (
                    expected_event_type,
                    "AnalysisRevision",
                    analysis_revision_id,
                    "committed",
                    2,
                    1,
                    seed.run_id,
                    seed.arm_id,
                )
            ]


def test_product_worker_claim_commits_exact_three_role_views() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"product-worker-{uuid4().hex}",
        )
        adapter = ProductAgentWorkHandlerAdapter(
            processor=ProductAgentProcessor(
                factory,
                runtime_bundle=_deterministic_runtime_bundle(),
            )
        )
        result = adapter(WorkContext(claim, store, threading.Event()))

        assert result.disposition == WorkDisposition.SUCCEEDED
        assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
        assert len(result.result["role_view_ids"]) == 3
        assert result.result["analysis_status"] in {"ready", "degraded"}
    finally:
        provider.close()

    _assert_committed_closure(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        analysis_revision_id=result.result["analysis_revision_id"],
        product_attempt_id=result.result["product_attempt_id"],
        analysis_status=result.result["analysis_status"],
    )


def test_product_worker_pins_and_consumes_durable_l2_personalization() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
        care_required=True,
    )
    habit, memory = _seed_l2_personalization(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"product-worker-l2-{uuid4().hex}",
        )
        first_result = ProductAgentWorkHandlerAdapter(
            processor=ProductAgentProcessor(
                factory,
                runtime_bundle=_deterministic_runtime_bundle(),
            )
        )(WorkContext(claim, store, threading.Event()))
        assert first_result.disposition == WorkDisposition.SUCCEEDED
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_json
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s
                """,
                (first_result.result["product_attempt_id"],),
            )
            first_attempt = cursor.fetchone()[0]

    corrected_habit, corrected_memory = _append_l2_corrections(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        prior_habit=habit,
        prior_memory=memory,
    )
    _clone_pending_product_operations(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        count=1,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"product-worker-l2-v2-{uuid4().hex}",
        )
        second_result = ProductAgentWorkHandlerAdapter(
            processor=ProductAgentProcessor(
                factory,
                runtime_bundle=_deterministic_runtime_bundle(),
            )
        )(WorkContext(claim, store, threading.Event()))
        assert second_result.disposition == WorkDisposition.SUCCEEDED
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT attempt_json
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s
                """,
                (second_result.result["product_attempt_id"],),
            )
            second_attempt = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT requesting_agent,
                       jsonb_array_length(receipt_json -> 'items')
                FROM public.backend_memory_read_receipts_v2
                WHERE namespace_id = %s AND data_mode = 'replay'
                  AND run_id = %s AND arm_id = %s AND subject_id = %s
                ORDER BY requesting_agent, receipt_id
                """,
                (
                    seed.namespace_id,
                    seed.run_id,
                    seed.arm_id,
                    seed.subject_id,
                ),
            )
            receipt_rows = cursor.fetchall()

    def assert_pinned_attempt(
        attempt: dict[str, Any],
        *,
        expected_habit: HabitFact,
        expected_memory: GovernedMemoryItemV2,
        expected_version: int,
        expected_preference: str,
    ) -> None:
        role_runs = attempt["role_runs"]
        assert isinstance(role_runs, list) and len(role_runs) == 3
        for role_run in role_runs:
            assert isinstance(role_run, dict)
            pinned = role_run["personalization"]
            snapshot = role_run["fact_snapshot"]
            assert isinstance(pinned, dict) and isinstance(snapshot, dict)
            assert pinned["habit_profile_version"] == expected_version
            assert pinned["habit_facts"][0]["fact_id"] == expected_habit.fact_id
            assert pinned["memory_state_version"] == expected_version
            receipt_ids = [
                item["receipt_id"] for item in pinned["memory_read_receipts"]
            ]
            handle_ids = [
                handle["handle_id"]
                for receipt in pinned["memory_read_receipts"]
                for handle in receipt["handles"]
            ]
            assert snapshot["memory_read_receipt_refs"] == receipt_ids
            assert expected_habit.fact_id in snapshot["source_refs"]
            assert set(handle_ids).issubset(snapshot["source_refs"])
            care_receipt = next(
                item
                for item in pinned["memory_read_receipts"]
                if item["requesting_agent"] == "care_strategy"
            )
            assert care_receipt["items"][0]["revision_ref"] == (
                expected_memory.revision_ref
            )
            accepted = role_run["result"]["accepted_work_products"]
            evidence = next(
                item for item in accepted
                if item["agent_id"] == "evidence_reasoning"
            )
            assert any(
                claim["source_kind"] == "confirmed_habit"
                and expected_habit.fact_id in claim["evidence_refs"]
                for claim in evidence["payload"]["claims"]
            )
            care = next(
                item for item in accepted
                if item["agent_id"] == "care_strategy"
            )
            assert care["payload"]["primary_action"]["parameters"] == {
                "tolerance_minutes": 30,
            }
            assert care["payload"]["primary_action"]["title"] == {
                "morning_voice": "按早晨语音偏好，保持较稳定的起床安排",
                "evening_light": "按晚间灯光偏好，保持较稳定的起床安排",
            }[expected_preference]

    assert_pinned_attempt(
        first_attempt,
        expected_habit=habit,
        expected_memory=memory,
        expected_version=1,
        expected_preference="morning_voice",
    )
    assert_pinned_attempt(
        second_attempt,
        expected_habit=corrected_habit,
        expected_memory=corrected_memory,
        expected_version=2,
        expected_preference="evening_light",
    )

    first_episode_ids = {
        item["product_episode_id"] for item in first_attempt["role_runs"]
    }
    second_episode_ids = {
        item["product_episode_id"] for item in second_attempt["role_runs"]
    }
    assert first_episode_ids.isdisjoint(second_episode_ids)

    assert receipt_rows == [
        ("care_strategy", 1),
        ("care_strategy", 1),
        ("care_strategy", 1),
        ("care_strategy", 1),
        ("care_strategy", 1),
        ("care_strategy", 1),
        ("evidence_reasoning", 0),
        ("evidence_reasoning", 0),
        ("evidence_reasoning", 0),
        ("evidence_reasoning", 0),
        ("evidence_reasoning", 0),
        ("evidence_reasoning", 0),
    ]

    # The immutable first attempt stays pinned to v1 after durable v2 exists.
    for role_run in first_attempt["role_runs"]:
        pinned = role_run["personalization"]
        assert pinned["habit_profile_version"] == 1
        assert pinned["memory_state_version"] == 1


def test_prepared_product_attempt_is_not_query_visible_before_commit() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"prepare-worker-{uuid4().hex}",
        )
        scope = store.uow_scope_for_claim(claim)
        lease = ProductAgentLease(
            operation_id=claim.work_id,
            attempt_sequence=claim.attempt,
            lease_generation=claim.lease_generation,
            fencing_token=claim.fencing_token,
            worker_instance=claim.worker_instance,
        )
        processor = ProductAgentProcessor(
            factory,
            runtime_bundle=_deterministic_runtime_bundle(),
        )
        with factory.begin(scope) as uow:
            repository = PostgresProductAgentRepository(uow.connection, scope)
            source = repository.load_source(lease)
            uow.commit()
        prepared_at = datetime.now(tz=UTC)
        artifact = processor.prepare(
            scope=scope,
            source=source,
            lease=lease,
            prepared_at=prepared_at,
        )
        with factory.begin(scope) as uow:
            repository = PostgresProductAgentRepository(uow.connection, scope)
            repository.persist_prepared(lease, artifact)
            uow.commit()

        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt_state, query_visible, committed_at
                    FROM public.backend_product_attempts
                    WHERE operation_id = %s
                    """,
                    (seed.operation_id,),
                )
                assert cursor.fetchone() == ("prepared", False, None)
                cursor.execute(
                    """
                    SELECT
                      (SELECT count(*)
                       FROM public.backend_product_attempts
                       WHERE operation_id = %s AND query_visible),
                      (SELECT count(*)
                       FROM public.sleep_domain_analysis_revisions
                       WHERE night_episode_revision_id = %s),
                      (SELECT count(*)
                       FROM public.sleep_domain_analysis_role_views
                       WHERE night_episode_revision_id = %s),
                      (SELECT count(*)
                       FROM public.sleep_domain_domain_outbox
                       WHERE operation_id = %s),
                      (SELECT status
                       FROM public.sleep_domain_operations
                       WHERE operation_id = %s)
                    """,
                    (
                        seed.operation_id,
                        seed.night_episode_revision_id,
                        seed.night_episode_revision_id,
                        seed.operation_id,
                        seed.operation_id,
                    ),
                )
                assert cursor.fetchone() == (0, 0, 0, 0, "running")

        with factory.begin(scope) as uow:
            repository = PostgresProductAgentRepository(uow.connection, scope)
            committed = repository.commit_prepared(
                lease,
                artifact,
                committed_at=datetime.now(tz=UTC),
            )
            uow.commit()
    finally:
        provider.close()

    _assert_committed_closure(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        analysis_revision_id=committed.analysis_revision_id,
        product_attempt_id=committed.product_attempt_id,
        analysis_status=committed.analysis_status,
    )


def test_source_load_releases_operation_lock_before_concurrent_heartbeat() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    load_started = threading.Event()
    heartbeat_finished = threading.Event()
    heartbeat_result: list[bool] = []

    class ConcurrentLoadRepository(PostgresProductAgentRepository):
        def load_source(self, lease: ProductAgentLease):
            load_started.set()
            assert heartbeat_finished.wait(timeout=5)
            return super().load_source(lease)

    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"source-heartbeat-worker-{uuid4().hex}",
        )
        scope = store.uow_scope_for_claim(claim)
        lease = _lease_for_claim(claim)

        def heartbeat() -> None:
            assert load_started.wait(timeout=5)
            heartbeat_result.append(store.heartbeat(claim, lease_seconds=30))
            heartbeat_finished.set()

        heartbeat_thread = threading.Thread(target=heartbeat)
        heartbeat_thread.start()
        processor = ProductAgentProcessor(
            factory,
            runtime_bundle=_deterministic_runtime_bundle(),
            repository_factory=lambda connection, current_scope: (
                ConcurrentLoadRepository(connection, current_scope)
            ),
        )
        try:
            source = processor.load_source(scope, lease)
        finally:
            heartbeat_thread.join(timeout=5)

        assert heartbeat_thread.is_alive() is False
        assert heartbeat_result == [True]
        assert source.operation_id == claim.work_id
        assert source.night_episode_revision_id == seed.night_episode_revision_id
    finally:
        provider.close()


def test_reclaimed_prepared_artifact_is_taken_over_without_reinvocation() -> None:
    """A journaled immutable artifact survives its original Product owner."""

    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        model = DeterministicReplayStructuredAgentModel()
        counted_generate = Mock(wraps=model.generate)
        model.generate = counted_generate  # type: ignore[method-assign]

        first_claim = _claim_product_work(
            store,
            worker_instance=f"prepared-stale-worker-{uuid4().hex}",
        )
        first_scope = store.uow_scope_for_claim(first_claim)
        first_lease = _lease_for_claim(first_claim)
        first_processor = _PersistPreparedThenCrashProcessor(
            factory,
            runtime_bundle=build_deterministic_product_runtime_bundle(model=model),
        )
        first_adapter = ProductAgentWorkHandlerAdapter(processor=first_processor)
        with pytest.raises(_PreparedProcessCrash):
            first_adapter(WorkContext(first_claim, store, threading.Event()))
        first_artifact = first_processor.persisted_artifact
        assert first_artifact is not None
        first_model_call_count = counted_generate.call_count
        assert first_model_call_count > 0

        before_attempt = _prepared_attempt_snapshot(
            psycopg,
            admin_dsn=admin_dsn,
            product_attempt_id=first_artifact.product_attempt_id,
        )
        before_invocation = _invocation_snapshot(
            psycopg,
            admin_dsn=admin_dsn,
            operation_id=seed.operation_id,
        )
        assert {row[6] for row in before_invocation} == {
            "reserved",
            "send_started",
            "response_received",
        }

        _expire_operation(psycopg, admin_dsn, seed.operation_id)

        second_claim = _claim_product_work(
            store,
            worker_instance=f"prepared-takeover-worker-{uuid4().hex}",
        )
        assert second_claim.work_id == first_claim.work_id
        assert second_claim.attempt == first_claim.attempt
        assert second_claim.lease_generation == first_claim.lease_generation + 1
        assert second_claim.fencing_token != first_claim.fencing_token
        second_scope = store.uow_scope_for_claim(second_claim)
        second_lease = _lease_for_claim(second_claim)
        second_processor = _PersistPreparedThenCrashProcessor(
            factory,
            runtime_bundle=build_deterministic_product_runtime_bundle(model=model),
        )
        second_adapter = ProductAgentWorkHandlerAdapter(processor=second_processor)
        with pytest.raises(_PreparedProcessCrash):
            second_adapter(WorkContext(second_claim, store, threading.Event()))
        second_artifact = second_processor.persisted_artifact
        assert second_artifact is not None

        assert counted_generate.call_count == first_model_call_count
        assert second_artifact.model_dump(mode="json") == first_artifact.model_dump(
            mode="json"
        )
        assert second_artifact.attempt_sha256 == first_artifact.attempt_sha256
        assert (
            second_artifact.night_episode_revision_id
            == first_artifact.night_episode_revision_id
        )
        first_agent_identity = tuple(
            (
                role_run.role.value,
                invocation.invocation_id,
                invocation.skill_lock_hash,
            )
            for role_run in first_artifact.role_runs
            for invocation in role_run.result.agent_invocations
        )
        second_agent_identity = tuple(
            (
                role_run.role.value,
                invocation.invocation_id,
                invocation.skill_lock_hash,
            )
            for role_run in second_artifact.role_runs
            for invocation in role_run.result.agent_invocations
        )
        assert first_agent_identity
        assert second_agent_identity == first_agent_identity

        after_attempt = _prepared_attempt_snapshot(
            psycopg,
            admin_dsn=admin_dsn,
            product_attempt_id=first_artifact.product_attempt_id,
        )
        assert after_attempt[0] == before_attempt[0]
        assert before_attempt[1:] == (
            first_claim.attempt,
            first_claim.lease_generation,
            first_claim.fencing_token,
        )
        assert after_attempt[1:] == (
            second_claim.attempt,
            second_claim.lease_generation,
            second_claim.fencing_token,
        )
        assert (
            _invocation_snapshot(
                psycopg,
                admin_dsn=admin_dsn,
                operation_id=seed.operation_id,
            )
            == before_invocation
        )

        with pytest.raises(ProductAgentLeaseLost, match="fence"):
            with factory.begin(first_scope) as uow:
                repository = PostgresProductAgentRepository(uow.connection, first_scope)
                repository.commit_prepared(
                    first_lease,
                    first_artifact,
                    committed_at=datetime.now(tz=UTC),
                )

        _persist_prepared(factory, second_scope, second_lease, second_artifact)
        assert (
            _prepared_attempt_snapshot(
                psycopg,
                admin_dsn=admin_dsn,
                product_attempt_id=first_artifact.product_attempt_id,
            )
            == after_attempt
        )

        with factory.begin(second_scope) as uow:
            repository = PostgresProductAgentRepository(
                uow.connection,
                second_scope,
            )
            committed = repository.commit_prepared(
                second_lease,
                second_artifact,
                committed_at=datetime.now(tz=UTC),
            )
            uow.commit()
    finally:
        provider.close()

    _assert_committed_closure(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        analysis_revision_id=committed.analysis_revision_id,
        product_attempt_id=committed.product_attempt_id,
        analysis_status=committed.analysis_status,
    )


def test_prepared_artifact_takeover_conflicts_fail_closed() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        first_claim = _claim_product_work(
            store,
            worker_instance=f"conflict-first-worker-{uuid4().hex}",
        )
        first_scope = store.uow_scope_for_claim(first_claim)
        first_lease = _lease_for_claim(first_claim)
        processor = ProductAgentProcessor(
            factory,
            runtime_bundle=_deterministic_runtime_bundle(),
        )
        source = processor.load_source(first_scope, first_lease)
        artifact = processor.prepare(
            scope=first_scope,
            source=source,
            lease=first_lease,
            prepared_at=datetime.now(tz=UTC),
        )
        _persist_prepared(factory, first_scope, first_lease, artifact)
        _expire_operation(psycopg, admin_dsn, seed.operation_id)
        second_claim = _claim_product_work(
            store,
            worker_instance=f"conflict-second-worker-{uuid4().hex}",
        )
        second_scope = store.uow_scope_for_claim(second_claim)
        second_lease = _lease_for_claim(second_claim)

        first_invocations = artifact.role_runs[0].result.agent_invocations
        assert first_invocations
        changed_hash = "f" * 64 if artifact.attempt_sha256 != "f" * 64 else "e" * 64
        changed_skill_lock = (
            "f" * 64
            if first_invocations[0].skill_lock_hash != "f" * 64
            else "e" * 64
        )
        immutable_drifts = (
            (
                "UPDATE public.backend_product_attempts SET attempt_sha256 = %s "
                "WHERE product_attempt_id = %s",
                (changed_hash, artifact.product_attempt_id),
            ),
            (
                "UPDATE public.backend_product_attempts SET attempt_json = "
                "attempt_json || '{\"tampered\": true}'::jsonb "
                "WHERE product_attempt_id = %s",
                (artifact.product_attempt_id,),
            ),
            (
                "UPDATE public.backend_product_attempts SET attempt_json = "
                "jsonb_set(attempt_json, "
                "'{role_runs,0,result,agent_invocations,0,skill_lock_hash}', "
                "to_jsonb(%s::text)) WHERE product_attempt_id = %s",
                (changed_skill_lock, artifact.product_attempt_id),
            ),
        )
        for drift_sql, drift_params in immutable_drifts:
            _admin_execute(psycopg, admin_dsn, drift_sql, drift_params)
            with pytest.raises(ProductAgentConflict):
                _persist_prepared(factory, second_scope, second_lease, artifact)
            _admin_execute(
                psycopg,
                admin_dsn,
                "UPDATE public.backend_product_attempts SET attempt_sha256 = %s, "
                "attempt_json = %s::jsonb WHERE product_attempt_id = %s",
                (
                    artifact.attempt_sha256,
                    artifact.model_dump_json(),
                    artifact.product_attempt_id,
                ),
            )

        changed_revision = f"revision-drift-{uuid4().hex}"
        revision_drift = artifact.model_copy(
            update={
                "night_episode_revision_id": changed_revision,
                "analysis": artifact.analysis.model_copy(
                    update={"night_episode_revision_id": changed_revision}
                ),
                "role_runs": tuple(
                    role_run.model_copy(
                        update={
                            "role_view": role_run.role_view.model_copy(
                                update={
                                    "night_episode_revision_id": changed_revision
                                }
                            )
                        }
                    )
                    for role_run in artifact.role_runs
                ),
            }
        )
        revision_drift = PreparedProductAgentArtifact.model_validate(
            revision_drift.model_dump(mode="json")
        )
        with pytest.raises(ProductAgentConflict):
            _persist_prepared(factory, second_scope, second_lease, revision_drift)
        assert _prepared_attempt_snapshot(
            psycopg,
            admin_dsn=admin_dsn,
            product_attempt_id=artifact.product_attempt_id,
        )[1:] == (
            first_claim.attempt,
            first_claim.lease_generation,
            first_claim.fencing_token,
        )

        ownership_drifts = (
            (
                second_claim.lease_generation,
                first_claim.fencing_token,
            ),
            (
                first_claim.lease_generation,
                second_claim.fencing_token,
            ),
        )
        for prepared_generation, prepared_token in ownership_drifts:
            _admin_execute(
                psycopg,
                admin_dsn,
                "UPDATE public.backend_product_attempts "
                "SET lease_generation = %s, fencing_token = %s "
                "WHERE product_attempt_id = %s",
                (
                    prepared_generation,
                    prepared_token,
                    artifact.product_attempt_id,
                ),
            )
            with pytest.raises(ProductAgentConflict):
                _persist_prepared(factory, second_scope, second_lease, artifact)
            _admin_execute(
                psycopg,
                admin_dsn,
                "UPDATE public.backend_product_attempts "
                "SET lease_generation = %s, fencing_token = %s "
                "WHERE product_attempt_id = %s",
                (
                    first_claim.lease_generation,
                    first_claim.fencing_token,
                    artifact.product_attempt_id,
                ),
            )

        assert store.finalize(
            second_claim,
            WorkResult(
                disposition=WorkDisposition.RETRYABLE,
                error_code="prepared_artifact_business_retry",
                retry_after_seconds=0,
            ),
        ) is True
        business_retry_claim = _claim_product_work(
            store,
            worker_instance=f"conflict-business-retry-{uuid4().hex}",
        )
        assert business_retry_claim.attempt == second_claim.attempt + 1
        assert (
            business_retry_claim.lease_generation
            == second_claim.lease_generation + 1
        )
        business_retry_scope = store.uow_scope_for_claim(business_retry_claim)
        business_retry_lease = _lease_for_claim(business_retry_claim)
        with pytest.raises(ProductAgentConflict):
            _persist_prepared(
                factory,
                business_retry_scope,
                business_retry_lease,
                artifact,
            )

        other_id = f"product-attempt-conflict-{uuid4().hex}"
        other_artifact = artifact.model_copy(update={"product_attempt_id": other_id})
        _admin_execute(
            psycopg,
            admin_dsn,
            "UPDATE public.backend_product_attempts SET product_attempt_id = %s, "
            "attempt_sequence = %s, lease_generation = %s, fencing_token = %s, "
            "attempt_sha256 = %s, attempt_json = %s::jsonb "
            "WHERE product_attempt_id = %s",
            (
                other_id,
                business_retry_claim.attempt,
                business_retry_claim.lease_generation,
                business_retry_claim.fencing_token,
                other_artifact.attempt_sha256,
                other_artifact.model_dump_json(),
                artifact.product_attempt_id,
            ),
        )
        with pytest.raises(ProductAgentConflict):
            _persist_prepared(
                factory,
                business_retry_scope,
                business_retry_lease,
                artifact,
            )

        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT product_attempt_id, attempt_sequence,
                           lease_generation, fencing_token
                    FROM public.backend_product_attempts
                    WHERE operation_id = %s
                    """,
                    (seed.operation_id,),
                )
                owners = {
                    str(row[0]): (int(row[1]), int(row[2]), str(row[3]))
                    for row in cursor.fetchall()
                }
        assert owners == {
            other_id: (
                business_retry_claim.attempt,
                business_retry_claim.lease_generation,
                business_retry_claim.fencing_token,
            ),
        }
    finally:
        provider.close()


def test_worker_epoch_lock_privilege_is_column_scoped_and_rls_bounded() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"epoch-lock-worker-{uuid4().hex}",
        )
        scope = store.uow_scope_for_claim(claim)

        with factory.begin(scope) as uow:
            repository = PostgresProductAgentRepository(uow.connection, scope)
            assert repository._lock_and_validate_epochs() == (1, 1, 1)
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT
                      has_table_privilege(
                        current_user, 'public.backend_subject_epochs', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'namespace_id', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'authorization_epoch', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'privacy_epoch', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'retrieval_policy_epoch', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'cas_version', 'UPDATE'
                      ),
                      has_column_privilege(
                        current_user, 'public.backend_subject_epochs',
                        'updated_at', 'UPDATE'
                      )
                    """
                )
                assert cursor.fetchone() == (
                    False,
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                )
            finally:
                cursor.close()
            uow.commit()

        with factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute(
                        """
                        UPDATE public.backend_subject_epochs
                        SET authorization_epoch = authorization_epoch + 1
                        WHERE namespace_id = %s AND data_mode = %s
                          AND subject_id = %s
                        """,
                        (scope.namespace_id, scope.data_mode, scope.subject_id),
                    )
            finally:
                cursor.close()

        with factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cursor.execute(
                        """
                        UPDATE public.backend_subject_epochs
                        SET namespace_id = %s
                        WHERE namespace_id = %s AND data_mode = %s
                          AND subject_id = %s
                        """,
                        (
                            f"replay:forbidden-{uuid4().hex}",
                            scope.namespace_id,
                            scope.data_mode,
                            scope.subject_id,
                        ),
                    )
            finally:
                cursor.close()

        assert store.finalize(
            claim,
            WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="permission_invariant_complete",
            ),
        ) is True
    finally:
        provider.close()

    with psycopg.connect(api_dsn) as api:
        assert api.execute(
            """
            SELECT
              has_table_privilege(
                current_user, 'public.backend_subject_epochs', 'UPDATE'
              ),
              has_column_privilege(
                current_user, 'public.backend_subject_epochs',
                'namespace_id', 'UPDATE'
              )
            """
        ).fetchone() == (False, False)

    with psycopg.connect(admin_dsn) as admin:
        assert admin.execute(
            """
            SELECT authorization_epoch, privacy_epoch,
                   retrieval_policy_epoch, namespace_id
            FROM public.backend_subject_epochs
            WHERE namespace_id = %s AND data_mode = 'replay'
              AND subject_id = %s
            """,
            (seed.namespace_id, seed.subject_id),
        ).fetchone() == (1, 1, 1, seed.namespace_id)


def test_namespace_capacity_and_fairness_hold_under_concurrent_claim_load() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seeds = [
        _seed_product_scope(
            psycopg,
            admin_dsn=admin_dsn,
            worker_principal=worker_principal,
        )
        for _index in range(4)
    ]
    for seed in seeds:
        _clone_pending_product_operations(
            psycopg,
            admin_dsn=admin_dsn,
            seed=seed,
            count=7,
        )
    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                "UPDATE public.backend_namespaces "
                "SET max_worker_concurrency = 1 "
                "WHERE namespace_id = ANY(%s)",
                ([seed.namespace_id for seed in seeds],),
            )
            assert cursor.rowcount == 4
            # The postgres marker is order-independent even when it shares one
            # disposable database. Keep reclaimable work left by another test
            # outside this load window so every claim belongs to this four-
            # namespace experiment.
            cursor.execute(
                "UPDATE public.sleep_domain_operations "
                "SET available_at = clock_timestamp() + interval '1 hour' "
                "WHERE data_mode = 'replay' AND queue_name = 'product_agent' "
                "AND namespace_id <> ALL(%s) "
                "AND status IN ('pending', 'retry', 'running')",
                ([seed.namespace_id for seed in seeds],),
            )

    provider, _factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id="replay:",
        pool_max_size=8,
    )
    barrier = threading.Barrier(8)

    def claim(index: int) -> LeaseClaim | None:
        barrier.wait(timeout=10)
        return store.claim(
            queue="product_agent",
            worker_instance=f"fairness-worker-{index}",
            lease_seconds=30,
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(claim, range(8)))
        claimed = [item for item in claims if item is not None]
        # A heavily contended first pass may skip an advisory-locked namespace;
        # subsequent claims must still reach every namespace and never exceed
        # its capacity of one running item.
        for index in range(8, 16):
            if len(claimed) == len(seeds):
                break
            item = store.claim(
                queue="product_agent",
                worker_instance=f"fairness-worker-{index}",
                lease_seconds=30,
            )
            if item is not None:
                claimed.append(item)
    finally:
        provider.close()

    claimed_namespaces = [item.namespace_id for item in claimed]
    assert len(claimed) == 4
    assert set(claimed_namespaces) == {seed.namespace_id for seed in seeds}
    assert len(set(claimed_namespaces)) == len(claimed_namespaces)

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT namespace_id, count(*) FROM "
                "public.sleep_domain_operations "
                "WHERE namespace_id = ANY(%s) AND status = 'running' "
                "AND lease_expires_at > clock_timestamp() "
                "GROUP BY namespace_id ORDER BY namespace_id",
                ([seed.namespace_id for seed in seeds],),
            )
            assert cursor.fetchall() == sorted(
                (seed.namespace_id, 1) for seed in seeds
            )
            cursor.execute(
                "SELECT namespace_id, claim_count FROM "
                "public.backend_queue_namespace_fairness "
                "WHERE queue_name = 'product_agent' "
                "AND namespace_id = ANY(%s) ORDER BY namespace_id",
                ([seed.namespace_id for seed in seeds],),
            )
            assert cursor.fetchall() == sorted(
                (seed.namespace_id, 1) for seed in seeds
            )


def _report_write_counts(
    cursor: Any,
    *,
    namespace_id: str,
    subject_id: str,
    assertion_issuer: str,
) -> dict[str, int]:
    statements: dict[str, tuple[str, tuple[object, ...]]] = {
        "actor_assertion_replays": (
            "SELECT count(*) FROM public.sleep_api_actor_assertion_replays "
            "WHERE issuer = %s",
            (assertion_issuer,),
        ),
        "memory_read_receipts": (
            "SELECT count(*) FROM public.backend_memory_read_receipts_v2 "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "command_receipts": (
            "SELECT count(*) FROM public.backend_command_receipts "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "operations": (
            "SELECT count(*) FROM public.sleep_domain_operations "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "authorization_audit": (
            "SELECT count(*) FROM public.backend_authorization_audit "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "domain_outbox": (
            "SELECT count(*) FROM public.sleep_domain_domain_outbox "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "product_attempts": (
            "SELECT count(*) FROM public.backend_product_attempts "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "invocations": (
            "SELECT count(*) FROM public.backend_invocations "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "invocation_journal": (
            "SELECT count(*) FROM public.backend_invocation_journal "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "analysis_revisions": (
            "SELECT count(*) FROM public.sleep_domain_analysis_revisions "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "analysis_role_views": (
            "SELECT count(*) FROM public.sleep_domain_analysis_role_views "
            "WHERE namespace_id = %s AND subject_id = %s",
            (namespace_id, subject_id),
        ),
        "api_operation_commands": (
            "SELECT count(*) FROM public.sleep_api_operation_commands "
            "WHERE namespace_id = %s",
            (namespace_id,),
        ),
    }
    counts: dict[str, int] = {}
    for name, (statement, parameters) in statements.items():
        cursor.execute(statement, parameters)
        row = cursor.fetchone()
        assert row is not None
        counts[name] = int(row[0])
    return counts


def test_product_report_exact_reservation_and_stateless_reads_are_postgres_safe(
) -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    api_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL",
        "sleepagent-api-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    suffix = uuid4().hex
    actor_id = f"report-actor-{suffix}"
    binding_id = f"report-binding-{suffix}"
    assertion_issuer = f"report-issuer-{suffix}"
    service_secret = f"report-service-secret-{suffix}"
    scopes = ["product:sleep:today:read", "sleep:reanalysis:write"]
    higher_revision_id = str(UUID7Generator()())

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                "INSERT INTO public.backend_actors "
                "(actor_id, actor_kind, status) "
                "VALUES (%s, 'human', 'active')",
                (actor_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_actor_subject_bindings (
                  binding_id, namespace_id, data_mode, actor_id, subject_id,
                  role, status, purpose_json, scopes_json,
                  authorization_epoch, valid_from
                ) VALUES (
                  %s, %s, 'replay', %s, %s, 'elder', 'active',
                  '["sleep_care"]'::jsonb, %s::jsonb, 1,
                  clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    binding_id,
                    seed.namespace_id,
                    actor_id,
                    seed.subject_id,
                    json.dumps(scopes),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (
                  %s, %s, %s, 'replay', 'sleep_care', %s::jsonb,
                  '["product_agent"]'::jsonb, 1, 'active',
                  clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    f"report-grant-{suffix}",
                    api_principal,
                    seed.namespace_id,
                    json.dumps(scopes),
                ),
            )
            # A larger revision exists but is not the marked current revision.
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episode_revisions (
                  night_episode_revision_id, namespace_id, data_mode,
                  night_episode_id, subject_id, revision_number,
                  revision_json, created_at, protocol_version, id_scheme,
                  namespace_generation, run_id, arm_id, episode_anchor_key,
                  timezone_name, boundary_policy_version, bed_local_date,
                  wake_local_date, episode_local_date, assignment_basis,
                  date_confidence, assignment_estimated, date_state,
                  date_conflict, episode_schema_version
                )
                SELECT %s, namespace_id, data_mode, night_episode_id,
                       subject_id, revision_number + 1,
                       revision_json || jsonb_build_object(
                         'night_episode_revision_id', %s::text,
                         'revision_number', revision_number + 1
                       ),
                       clock_timestamp(), protocol_version, id_scheme,
                       namespace_generation, run_id, arm_id,
                       episode_anchor_key, timezone_name,
                       boundary_policy_version, bed_local_date,
                       wake_local_date, episode_local_date, assignment_basis,
                       date_confidence, assignment_estimated, date_state,
                       date_conflict, episode_schema_version
                FROM public.sleep_domain_night_episode_revisions
                WHERE night_episode_revision_id = %s
                """,
                (
                    higher_revision_id,
                    higher_revision_id,
                    seed.night_episode_revision_id,
                ),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                "SELECT episode_local_date "
                "FROM public.sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (seed.night_episode_id,),
            )
            wake_date_row = cursor.fetchone()
            assert wake_date_row is not None
            report_wake_date = wake_date_row[0]
            assert isinstance(report_wake_date, date)

    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name="sleepagent-product-report-postgres-integration",
    )
    provider.open()
    with psycopg.connect(api_dsn) as api:
        assert api.execute(
            """
            SELECT
              has_table_privilege(
                current_user, 'public.sleep_domain_night_episodes', 'UPDATE'
              ),
              has_column_privilege(
                current_user, 'public.sleep_domain_night_episodes',
                'namespace_id', 'UPDATE'
              ),
              has_column_privilege(
                current_user, 'public.sleep_domain_night_episodes',
                'current_revision_id', 'UPDATE'
              ),
              has_column_privilege(
                current_user, 'public.sleep_domain_night_episodes',
                'date_state', 'UPDATE'
              )
            """
        ).fetchone() == (False, True, False, False)
    now = datetime.now(tz=UTC)
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    settings = SimpleNamespace(
        data_mode=SimpleNamespace(value="replay"),
        service_principal_id=api_principal,
    )
    factory: UnitOfWorkFactory[object] = UnitOfWorkFactory(provider)
    replay_store = PostgresAssertionReplayStore(
        settings,  # type: ignore[arg-type]
        factory,
    )
    authenticator = SleepApiAuthenticator(
        service_verifier=HttpsBearerServicePrincipalVerifier(
            (
                RotatingServiceCredential.from_secret(
                    credential_id=f"report-credential-{suffix}",
                    principal_id=api_principal,
                    secret=service_secret,
                    not_before=now - timedelta(hours=1),
                    not_after=now + timedelta(hours=1),
                    allowed_actor_issuers=frozenset({assertion_issuer}),
                ),
            )
        ),
        actor_verifier=ActorAssertionVerifier(
            (
                ActorVerificationKey(
                    issuer=assertion_issuer,
                    key_id=f"report-key-{suffix}",
                    algorithm="EdDSA",
                    public_key_pem=public_key_pem,
                    not_before=now - timedelta(hours=1),
                    not_after=now + timedelta(hours=1),
                ),
            ),
            audience="sleep-api",
            replay_store=replay_store,
        ),
        role_binding_resolver=FailClosedRoleBindingResolver(),
        now_factory=lambda: now,
    )
    service = ProductApiService(
        identity_resolver=PostgresProductIdentityResolver(
            authenticator=authenticator,
            authority=PostgresAuthorityStore(
                settings,  # type: ignore[arg-type]
                factory,
            ),
        ),
        backend=PostgresProductBackend(
            factory,
            cursor_key=b"r" * 32,
            report_model_mode="deterministic",
            report_deployment_mode="test",
        ),
    )
    signer = Ed25519ActorSigner(
        issuer=assertion_issuer,
        audience="sleep-api",
        key_id=f"report-key-{suffix}",
        actor_id=actor_id,
        subject_id=seed.subject_id,
        role="elder",
        scopes=tuple(scopes),
        private_key=private_key,
    )

    def signed_request(method: str, path: str, body: bytes = b"") -> Request:
        assertion = signer.sign(method=method, path=path, body=body, now=now)
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "server": ("sleepagent.test", 443),
                "headers": [
                    (
                        b"authorization",
                        f"Bearer {service_secret}".encode("ascii"),
                    ),
                    (b"x-sleep-actor-assertion", assertion.encode("ascii")),
                ],
            }
        )

    try:
        payload = ProductReportRunRequest(wake_date=report_wake_date)
        request_body = _canonical_json(payload).encode("utf-8")
        idempotency_key = f"report-run-{suffix}"
        first = service.run_report(
            signed_request(
                "POST", "/product/sleep/reports/run", request_body
            ),
            payload,
            idempotency_key=idempotency_key,
            request_body=request_body,
        )
        second = service.run_report(
            signed_request(
                "POST", "/product/sleep/reports/run", request_body
            ),
            payload,
            idempotency_key=idempotency_key,
            request_body=request_body,
        )
        assert first == second
        assert first.state == "accepted"

        missing_date = report_wake_date - timedelta(days=30)
        missing_payload = ProductReportRunRequest(wake_date=missing_date)
        missing_body = _canonical_json(missing_payload).encode("utf-8")
        with pytest.raises(ProductApiError) as missing_error:
            service.run_report(
                signed_request(
                    "POST", "/product/sleep/reports/run", missing_body
                ),
                missing_payload,
                idempotency_key=f"missing-report-{suffix}",
                request_body=missing_body,
            )
        assert missing_error.value.status_code == 404

        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*),
                           min(operation_json ->> 'night_episode_revision_id'),
                           min(operation_json ->> 'wake_date')
                    FROM public.sleep_domain_operations
                    WHERE namespace_id = %s AND subject_id = %s
                      AND operation_type = 'product.report.run.v1'
                    """,
                    (seed.namespace_id, seed.subject_id),
                )
                operation_row = cursor.fetchone()
                assert operation_row is not None
                operation_count, resolved_revision_id, resolved_date = (
                    operation_row
                )
                assert operation_count == 1
                assert resolved_revision_id == seed.night_episode_revision_id
                assert resolved_revision_id != higher_revision_id
                assert resolved_date == report_wake_date.isoformat()
                cursor.execute(
                    """
                    SELECT count(*)
                    FROM public.backend_command_receipts
                    WHERE namespace_id = %s AND subject_id = %s
                      AND route_template = '/product/sleep/reports/run'
                    """,
                    (seed.namespace_id, seed.subject_id),
                )
                assert cursor.fetchone()[0] == 1
                before_reads = _report_write_counts(
                    cursor,
                    namespace_id=seed.namespace_id,
                    subject_id=seed.subject_id,
                    assertion_issuer=assertion_issuer,
                )

        show_path = "/product/sleep/reports/" + report_wake_date.isoformat()
        reusable_show_request = signed_request("GET", show_path)
        shown = service.show_report(
            reusable_show_request,
            wake_date=report_wake_date,
            trace=True,
            request_body=b"",
        )
        shown_again = service.show_report(
            reusable_show_request,
            wake_date=report_wake_date,
            trace=True,
            request_body=b"",
        )
        listing = service.list_reports(
            signed_request("GET", "/product/sleep/reports"),
            limit=20,
            cursor=None,
            trace=True,
            request_body=b"",
        )
        assert shown == shown_again
        assert shown.state == ProductReportState.PENDING
        assert listing.items[0].wake_date == report_wake_date
        assert listing.items[0].audience == ProductRole.ELDER

        with psycopg.connect(admin_dsn) as admin:
            with admin.cursor() as cursor:
                after_reads = _report_write_counts(
                    cursor,
                    namespace_id=seed.namespace_id,
                    subject_id=seed.subject_id,
                    assertion_issuer=assertion_issuer,
                )
        assert before_reads == after_reads
        # POST consumed three nonces (including the safe missing-date command);
        # repeated show/list signatures consumed none.
        assert after_reads["actor_assertion_replays"] == 3
        assert after_reads["memory_read_receipts"] == 0
    finally:
        provider.close()
    _cancel_pending_product_work(
        psycopg,
        admin_dsn=admin_dsn,
        namespace_id=seed.namespace_id,
    )


def test_shared_publication_cross_identity_rebases_in_real_postgres() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    _convert_seed_to_report_request(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )
    same_request_id = _clone_report_request(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )
    cross_request_id = _clone_report_request(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )

    first_model = _IdentityVariantDeterministicModel("acceptance-model-a")
    second_model = _IdentityVariantDeterministicModel("acceptance-model-b")
    first_generate = Mock(wraps=first_model.generate)
    second_generate = Mock(wraps=second_model.generate)
    first_model.generate = first_generate  # type: ignore[method-assign]
    second_model.generate = second_generate  # type: ignore[method-assign]
    first_processor_bundle = build_deterministic_product_runtime_bundle(
        model=first_model
    )
    second_processor_bundle = build_deterministic_product_runtime_bundle(
        model=second_model
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    first_processor = ProductAgentProcessor(
        factory,
        runtime_bundle=first_processor_bundle,
    )
    second_processor = ProductAgentProcessor(
        factory,
        runtime_bundle=second_processor_bundle,
    )
    try:
        routed_requests = []
        for index, processor in enumerate(
            (first_processor, first_processor, second_processor)
        ):
            claim = _claim_product_work(
                store,
                worker_instance=f"report-route-{index}-{uuid4().hex}",
                operation_type=PRODUCT_REPORT_RUN_OPERATION,
            )
            scope = store.uow_scope_for_claim(claim)
            routed_requests.append(
                (
                    claim.work_id,
                    processor.route_report_request(
                        scope,
                        _lease_for_claim(claim),
                    ),
                )
            )

        assert [item[0] for item in routed_requests] == [
            seed.operation_id,
            same_request_id,
            cross_request_id,
        ]
        first_route = routed_requests[0][1]
        same_route = routed_requests[1][1]
        cross_route = routed_requests[2][1]
        assert first_route.state == same_route.state == cross_route.state == "pending"
        assert first_route.shared_operation_created is True
        assert same_route.shared_operation_created is False
        assert cross_route.shared_operation_created is True
        assert same_route.shared_operation_id == first_route.shared_operation_id
        assert cross_route.shared_operation_id != first_route.shared_operation_id

        prepared: list[
            tuple[
                ProductAgentProcessor,
                UowScope,
                ProductAgentLease,
                object,
                PreparedProductAgentArtifact,
            ]
        ] = []
        for index, processor in enumerate(
            (first_processor, second_processor)
        ):
            claim = _claim_product_work(
                store,
                worker_instance=f"shared-prepare-{index}-{uuid4().hex}",
                operation_type=PRODUCT_SHARED_ANALYSIS_OPERATION,
            )
            scope = store.uow_scope_for_claim(claim)
            lease = _lease_for_claim(claim)
            source = processor.load_source(scope, lease)
            artifact = processor.prepare_shared(
                scope=scope,
                source=source,
                lease=lease,
                prepared_at=datetime.now(tz=UTC),
            )
            assert artifact.schema_version == "product_agent_prepared_attempt.v3"
            _persist_prepared(factory, scope, lease, artifact)
            prepared.append((processor, scope, lease, source, artifact))

        calls_after_prepare = first_generate.call_count + second_generate.call_count
        assert first_generate.call_count > 0
        assert second_generate.call_count > 0

        committed = []
        for processor, scope, lease, source, artifact in prepared:
            committed.append(
                processor.persist_and_commit(
                    scope,
                    lease,
                    artifact,
                    source=source,  # type: ignore[arg-type]
                )
            )
        assert first_generate.call_count + second_generate.call_count == (
            calls_after_prepare
        )
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT analysis_revision_id, revision_number,
                       parent_analysis_revision_id,
                       analysis_json ->> 'desired_analysis_sha256'
                FROM public.sleep_domain_analysis_revisions
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_revision_id = %s
                ORDER BY revision_number
                """,
                (
                    seed.namespace_id,
                    seed.subject_id,
                    seed.night_episode_revision_id,
                ),
            )
            revisions = cursor.fetchall()
            assert len(revisions) == 2
            assert [int(row[1]) for row in revisions] == [1, 2]
            assert revisions[0][2] is None
            assert str(revisions[1][2]) == str(revisions[0][0])
            assert len({str(row[0]) for row in revisions}) == 2
            assert len({str(row[3]) for row in revisions}) == 2

            shared_ids = (
                first_route.shared_operation_id,
                cross_route.shared_operation_id,
            )
            cursor.execute(
                """
                SELECT operation_id, status
                FROM public.sleep_domain_operations
                WHERE operation_id = ANY(%s)
                ORDER BY operation_id
                """,
                (list(shared_ids),),
            )
            assert {str(row[0]): str(row[1]) for row in cursor.fetchall()} == {
                str(shared_ids[0]): "succeeded",
                str(shared_ids[1]): "succeeded",
            }
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT product_attempt_id)
                FROM public.backend_product_attempts
                WHERE operation_id = ANY(%s)
                  AND attempt_state = 'committed' AND query_visible = TRUE
                """,
                (list(shared_ids),),
            )
            assert cursor.fetchone() == (2, 2)
            assert {item.operation_id for item in committed} == set(shared_ids)
    _cancel_pending_product_work(
        psycopg,
        admin_dsn=admin_dsn,
        namespace_id=seed.namespace_id,
    )


@pytest.mark.parametrize(
    ("gate", "stale"),
    (
        ("urgent", False),
        ("urgent", True),
        ("unusable", False),
        ("unusable", True),
    ),
)
def test_report_final_gate_fences_authoritative_drift_in_real_postgres(
    gate: str,
    stale: bool,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
        care_required=gate == "urgent",
    )
    if gate == "unusable":
        _replace_current_quality(
            psycopg,
            admin_dsn=admin_dsn,
            seed=seed,
            usable=False,
            update_operation=True,
        )
    _convert_seed_to_report_request(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
    )

    model = DeterministicReplayStructuredAgentModel()
    counted_generate = Mock(wraps=model.generate)
    model.generate = counted_generate  # type: ignore[method-assign]
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )

    class _DriftingGateProcessor(ProductAgentProcessor):
        def load_source(self, scope: UowScope, lease: ProductAgentLease):
            source = super().load_source(scope, lease)
            if gate == "urgent":
                _replace_current_risk(
                    psycopg,
                    admin_dsn=admin_dsn,
                    seed=seed,
                    urgent=False,
                    update_operation=False,
                )
            else:
                _replace_current_quality(
                    psycopg,
                    admin_dsn=admin_dsn,
                    seed=seed,
                    usable=True,
                    update_operation=False,
                )
            return source

    processor_type = _DriftingGateProcessor if stale else ProductAgentProcessor
    processor = processor_type(
        factory,
        runtime_bundle=build_deterministic_product_runtime_bundle(model=model),
    )
    try:
        claim = _claim_product_work(
            store,
            worker_instance=f"gate-{gate}-{stale}-{uuid4().hex}",
            operation_type=PRODUCT_REPORT_RUN_OPERATION,
        )
        result = processor.route_report_request(
            store.uow_scope_for_claim(claim),
            _lease_for_claim(claim),
        )
    finally:
        provider.close()

    expected_terminal = (
        "urgent_handled" if gate == "urgent" else "unusable_blocked"
    )
    assert result.state == ("pending" if stale else expected_terminal)
    assert counted_generate.call_count == 0
    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, operation_json -> 'report_result',
                       operation_json ->> 'gate_reroute_count'
                FROM public.sleep_domain_operations
                WHERE operation_id = %s
                """,
                (seed.operation_id,),
            )
            operation = cursor.fetchone()
            assert operation is not None
            if stale:
                assert operation == ("retry", None, "1")
            else:
                assert operation[0] == "succeeded"
                assert operation[1]["state"] == expected_terminal
                assert operation[2] is None
            cursor.execute(
                """
                SELECT
                  count(*) FILTER (
                    WHERE operation_type = 'product.shared_analysis.v1'
                  ),
                  (SELECT count(*)
                   FROM public.backend_product_attempts
                   WHERE namespace_id = %s AND subject_id = %s),
                  (SELECT count(*)
                   FROM public.backend_invocations
                   WHERE namespace_id = %s AND subject_id = %s)
                FROM public.sleep_domain_operations
                WHERE namespace_id = %s AND subject_id = %s
                """,
                (
                    seed.namespace_id,
                    seed.subject_id,
                    seed.namespace_id,
                    seed.subject_id,
                    seed.namespace_id,
                    seed.subject_id,
                ),
            )
            assert cursor.fetchone() == (0, 0, 0)
    _cancel_pending_product_work(
        psycopg,
        admin_dsn=admin_dsn,
        namespace_id=seed.namespace_id,
    )


@pytest.mark.parametrize("capability", ("habit", "memory"))
def test_shared_final_commit_serializes_real_l2_writer_lock(
    capability: str,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    case = _prepare_shared_acceptance_case(
        psycopg,
        admin_dsn=admin_dsn,
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
    )
    locks_acquired = threading.Event()
    release_commit = threading.Event()
    writer_started = threading.Event()
    writer_acquired = threading.Event()
    errors: list[BaseException] = []
    results: list[object] = []

    class _PausedFinalCommitRepository(PostgresProductAgentRepository):
        def _lock_final_context_writers(self, cursor: Any) -> None:
            super()._lock_final_context_writers(cursor)
            locks_acquired.set()
            assert release_commit.wait(timeout=5)

    processor = ProductAgentProcessor(
        case.factory,
        runtime_bundle=case.bundle,
        repository_factory=lambda connection, scope: (
            _PausedFinalCommitRepository(connection, scope)
        ),
    )
    writer_context = SimpleNamespace(
        namespace_id=case.seed.namespace_id,
        data_mode="replay",
        namespace_generation=1,
        run_id=case.seed.run_id,
        arm_id=case.seed.arm_id,
        subject_id=case.seed.subject_id,
    )

    def commit_shared() -> None:
        try:
            results.append(
                processor.persist_and_commit(
                    case.scope,
                    case.lease,
                    case.artifact,
                    source=case.source,  # type: ignore[arg-type]
                )
            )
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    def lock_as_api_writer() -> None:
        try:
            with psycopg.connect(api_dsn) as api:
                with api.cursor() as cursor:
                    writer_started.set()
                    _lock_l2_subject(
                        cursor,
                        writer_context,  # type: ignore[arg-type]
                        capability=capability,  # type: ignore[arg-type]
                    )
                    writer_acquired.set()
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    commit_thread = threading.Thread(target=commit_shared)
    commit_thread.start()
    assert locks_acquired.wait(timeout=5)
    writer_thread = threading.Thread(target=lock_as_api_writer)
    writer_thread.start()
    assert writer_started.wait(timeout=5)
    assert writer_acquired.wait(timeout=0.1) is False
    release_commit.set()
    commit_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    try:
        assert errors == []
        assert not commit_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_acquired.is_set()
        assert len(results) == 1
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                """
                SELECT count(*)
                FROM public.sleep_domain_analysis_revisions
                WHERE namespace_id = %s AND subject_id = %s
                  AND night_episode_revision_id = %s
                """,
                (
                    case.seed.namespace_id,
                    case.seed.subject_id,
                    case.seed.night_episode_revision_id,
                ),
            ).fetchone() == (1,)
    finally:
        case.provider.close()
        _cancel_pending_product_work(
            psycopg,
            admin_dsn=admin_dsn,
            namespace_id=case.seed.namespace_id,
        )


def test_elder_narrative_final_commit_serializes_real_context_writer() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    case = _prepare_shared_acceptance_case(
        psycopg,
        admin_dsn=admin_dsn,
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
    )
    case.processor.persist_and_commit(
        case.scope,
        case.lease,
        case.artifact,
        source=case.source,  # type: ignore[arg-type]
    )
    narrative_claim = _claim_product_work(
        case.store,
        worker_instance=f"narrative-{uuid4().hex}",
        operation_type=PRODUCT_ELDER_NARRATIVE_OPERATION,
    )
    narrative_scope = case.store.uow_scope_for_claim(narrative_claim)
    narrative_lease = _lease_for_claim(narrative_claim)
    narrative_source = case.processor.load_source(
        narrative_scope,
        narrative_lease,
    )
    committed_shared = case.processor.load_committed_shared_for_narrative(
        narrative_scope,
        narrative_lease,
        narrative_source,
    )
    narrative_artifact = case.processor.prepare_elder_narrative(
        scope=narrative_scope,
        source=narrative_source,
        lease=narrative_lease,
        committed_shared=committed_shared,
        prepared_at=datetime.now(tz=UTC),
    )
    with case.factory.begin(narrative_scope) as uow:
        PostgresProductAgentRepository(
            uow.connection,
            narrative_scope,
        ).persist_elder_narrative_prepared(
            narrative_lease,
            narrative_artifact,
        )
        uow.commit()

    locks_acquired = threading.Event()
    release_commit = threading.Event()
    writer_started = threading.Event()
    writer_acquired = threading.Event()
    errors: list[BaseException] = []

    class _PausedNarrativeCommitRepository(PostgresProductAgentRepository):
        def _lock_final_context_writers(self, cursor: Any) -> None:
            super()._lock_final_context_writers(cursor)
            locks_acquired.set()
            assert release_commit.wait(timeout=5)

    narrative_processor = ProductAgentProcessor(
        case.factory,
        runtime_bundle=case.bundle,
        repository_factory=lambda connection, scope: (
            _PausedNarrativeCommitRepository(connection, scope)
        ),
    )
    writer_context = SimpleNamespace(
        namespace_id=case.seed.namespace_id,
        data_mode="replay",
        namespace_generation=1,
        run_id=case.seed.run_id,
        arm_id=case.seed.arm_id,
        subject_id=case.seed.subject_id,
    )

    def commit_narrative() -> None:
        try:
            narrative_processor.persist_and_commit_elder_narrative(
                narrative_scope,
                narrative_lease,
                narrative_artifact,
                source=narrative_source,
                committed_shared=committed_shared,
            )
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    def lock_memory_writer() -> None:
        try:
            with psycopg.connect(api_dsn) as api:
                with api.cursor() as cursor:
                    writer_started.set()
                    _lock_l2_subject(
                        cursor,
                        writer_context,  # type: ignore[arg-type]
                        capability="memory",
                    )
                    writer_acquired.set()
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    commit_thread = threading.Thread(target=commit_narrative)
    commit_thread.start()
    assert locks_acquired.wait(timeout=5)
    writer_thread = threading.Thread(target=lock_memory_writer)
    writer_thread.start()
    assert writer_started.wait(timeout=5)
    assert writer_acquired.wait(timeout=0.1) is False
    release_commit.set()
    commit_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    try:
        assert errors == []
        assert not commit_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_acquired.is_set()
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                """
                SELECT operation.status,
                       attempt.attempt_state,
                       attempt.query_visible,
                       (SELECT count(*)
                        FROM public.sleep_domain_analysis_role_views
                        WHERE analysis_revision_id = %s)
                FROM public.sleep_domain_operations AS operation
                JOIN public.backend_product_attempts AS attempt
                  ON attempt.operation_id = operation.operation_id
                WHERE operation.operation_id = %s
                """,
                (
                    committed_shared.analysis.analysis_revision_id,
                    narrative_lease.operation_id,
                ),
            ).fetchone() == ("succeeded", "committed", True, 3)
    finally:
        case.provider.close()
        _cancel_pending_product_work(
            psycopg,
            admin_dsn=admin_dsn,
            namespace_id=case.seed.namespace_id,
        )
