from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sleepagent.backend_settings import (
    DataMode as BackendDataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.product_runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.product_runtime.postgres_worker import (
    PostgresProductAgentRepository,
    ProductAgentLease,
    ProductAgentProcessor,
    ProductAgentWorkHandlerAdapter,
)
from sleepagent.product_runtime.runtime_factory import (
    ProductRuntimeBundle,
    build_deterministic_product_runtime_bundle,
)
from sleepagent.sleep_domain.contracts import (
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
from sleepagent.sleep_domain.episode_v2 import (
    NightEpisodeV2,
    UUID7Generator,
    finalize_episode_date,
    open_episode_v2,
)
from sleepagent.worker_runtime import (
    LeaseClaim,
    PostgresDurableWorkStore,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


def _deterministic_runtime_bundle() -> ProductRuntimeBundle:
    return build_deterministic_product_runtime_bundle(
        model=DeterministicReplayStructuredAgentModel()
    )


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
        risk_state=RiskState.NO_REVIEWED_SIGNAL,
        data_sufficiency=DataSufficiency.SUFFICIENT,
        source_scope=source_scope,
        policy_version="risk.synthetic.v1",
        observed_at=wake_at,
        reason_codes=("no_reviewed_signal",),
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
        worker_queues=("product_agent",),
        model_mode=ModelMode.DETERMINISTIC,
        service_credential_ref="test:worker-service",
        signing_key_ref="test:worker-signing",
        encryption_key_ref="test:worker-encryption",
        pool_min_size=1,
        pool_max_size=2,
    )
    provider = PsycopgPoolProvider.from_dsn(
        worker_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name="sleepagent-product-postgres-integration",
    )
    provider.open()
    factory: UnitOfWorkFactory[object] = UnitOfWorkFactory(provider)
    return provider, factory, PostgresDurableWorkStore(settings, factory)


def _claim_product_work(
    store: PostgresDurableWorkStore,
    *,
    worker_instance: str,
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
        "operation_type": "product_agent",
        "queue_name": "product_agent",
    }
    return claim


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
                       night_episode_revision_id, policy_sha256
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s AND operation_id = %s
                """,
                (product_attempt_id, seed.operation_id),
            )
            assert cursor.fetchone() == (
                "committed",
                True,
                True,
                seed.night_episode_revision_id,
                seed.policy_sha256,
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
                       revision_number
                FROM public.sleep_domain_analysis_revisions
                WHERE analysis_revision_id = %s
                """,
                (analysis_revision_id,),
            )
            assert cursor.fetchone() == (
                seed.night_episode_id,
                seed.night_episode_revision_id,
                seed.subject_id,
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
            assert cursor.fetchone() == (
                expected_event_type,
                "AnalysisRevision",
                analysis_revision_id,
                "committed",
                2,
                1,
                seed.run_id,
                seed.arm_id,
            )


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
