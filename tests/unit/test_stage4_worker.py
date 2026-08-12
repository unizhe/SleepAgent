from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.stage4_worker import (
    DELIVERY_QUEUE,
    DeterministicReplayDeliverySink,
    DeliveryReconciliationHandler,
    InductionWorkHandler,
    ReplayDeliveryWorkHandler,
    Stage4ReconciliationRouter,
    build_stage4_worker_handlers,
)
from sleepagent.worker_runtime import (
    InvocationKind,
    InvocationRecord,
    InvocationState,
    LeaseClaim,
    TerminalWorkError,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:normal-one-night",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-1",
        arm_id="arm-1",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def _snapshot(handler: str) -> dict[str, object]:
    scope = _scope()
    return {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": handler,
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }


class _Store:
    def __init__(self, factory: object | None = None) -> None:
        if factory is not None:
            self.uow_factory = factory

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        scope = _scope()
        assert claim.namespace_id == scope.namespace_id
        return scope


def _operation_claim(
    *,
    operation_type: str,
    payload: Mapping[str, object],
    version: int = 0,
) -> LeaseClaim:
    return LeaseClaim(
        work_id=f"{operation_type}-operation-1",
        operation_id=f"{operation_type}-operation-1",
        queue="induction" if operation_type == "induction" else "reconciliation",
        namespace_id="replay:normal-one-night",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=version,
        lease_generation=2,
        fencing_token="f" * 64,
        worker_instance="worker-1",
        attempt=1,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload=dict(payload),
        authorization_snapshot=_snapshot(
            "induction" if operation_type == "induction" else "reconciliation"
        ),
        metadata={
            "work_kind": "operation",
            "operation_type": operation_type,
            "queue_name": (
                "induction" if operation_type == "induction" else "reconciliation"
            ),
        },
    )


class _Uow:
    def __init__(self, cursor: object) -> None:
        self.connection = type("Connection", (), {"cursor": lambda _self: cursor})()
        self.committed = False

    def __enter__(self) -> "_Uow":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, cursor: object) -> None:
        self.uow = _Uow(cursor)

    def begin(self, scope: UowScope) -> _Uow:
        assert scope == _scope()
        return self.uow


class _InductionCursor:
    def __init__(self, manifest: Mapping[str, object]) -> None:
        self.manifest = dict(manifest)
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.row: tuple[Any, ...] | None = None
        self.rowcount = 1

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((query, params))
        if "JOIN public.backend_induction_manifests_v2" in query:
            self.row = (
                "manifest-1",
                _sha256(self.manifest),
                "analysis-1",
                "episode-revision-1",
                "a" * 64,
                "b" * 64,
                self.manifest,
                "ready",
                "committed",
                True,
                "b" * 64,
            )
        elif "array_agg(role ORDER BY role)" in query:
            self.row = (3, ["doctor", "elder", "family"])
        elif "FROM public.backend_personalization_profiles_v2" in query:
            self.row = None
        elif "RETURNING cas_version" in query:
            self.row = (1,)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def test_induction_commits_one_profile_revision_and_receipt_atomically() -> None:
    manifest = {
        "schema_version": "induction_manifest.v1",
        "source_fact_snapshot_sha256": "a" * 64,
        "policy_sha256": "b" * 64,
    }
    payload = {
        "schema_version": "induction_operation.v1",
        "manifest_id": "manifest-1",
        "manifest_sha256": _sha256(manifest),
        "analysis_revision_id": "analysis-1",
        "night_episode_revision_id": "episode-revision-1",
        "projector_version": "deterministic_personalization.v1",
        "authorization_snapshot": _snapshot("induction"),
    }
    cursor = _InductionCursor(manifest)
    factory = _Factory(cursor)
    ids = iter(f"id-{index}" for index in range(20))

    result = InductionWorkHandler(id_generator=lambda: next(ids))(
        WorkContext(
            _operation_claim(operation_type="induction", payload=payload),
            _Store(factory),
            threading.Event(),
        )
    )

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result["profile_version"] == 1
    assert factory.uow.committed is True
    statements = [query for query, _params in cursor.executions]
    assert any("backend_personalization_profile_revisions_v2" in q for q in statements)
    assert any("backend_induction_receipts_v2" in q for q in statements)
    assert any(
        "UPDATE public.sleep_domain_operations" in q and "status = 'succeeded'" in q
        for q in statements
    )


def test_induction_rejects_embedded_authority_drift_before_database_access() -> None:
    payload = {
        "schema_version": "induction_operation.v1",
        "manifest_id": "manifest-1",
        "manifest_sha256": "a" * 64,
        "analysis_revision_id": "analysis-1",
        "night_episode_revision_id": "episode-revision-1",
        "projector_version": "deterministic_personalization.v1",
        "authorization_snapshot": {**_snapshot("induction"), "privacy_epoch": 2},
    }
    cursor = _InductionCursor({})

    result = InductionWorkHandler()(
        WorkContext(
            _operation_claim(operation_type="induction", payload=payload),
            _Store(_Factory(cursor)),
            threading.Event(),
        )
    )

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "induction_invariant_violation"
    assert cursor.executions == []


class _InvocationStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.invocation_key: str | None = None
        self.state = InvocationState.RESERVED

    def reserve_invocation(
        self, claim, *, invocation_kind, invocation_key, request_sha256
    ) -> InvocationRecord:
        self.invocation_key = invocation_key
        return InvocationRecord(
            invocation_id="invocation-1",
            invocation_key=invocation_key,
            invocation_kind=invocation_kind,
            work_id=claim.work_id,
            lease_generation=claim.lease_generation,
            request_sha256=request_sha256,
            state=self.state,
        )

    def mark_invocation_send_started(self, claim, record) -> bool:
        del claim, record
        return True

    def finalize_invocation(
        self,
        claim,
        record,
        *,
        state,
        provider_request_id,
        response,
        error_code,
    ) -> bool:
        del claim, record, provider_request_id, response, error_code
        self.state = state
        return True


class _Sink:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[Mapping[str, str]] = []

    def send(self, context, *, scope, intent):
        del context, scope
        self.calls.append(dict(intent))
        if self.failure is not None:
            raise self.failure
        return {"status": "delivered", "effect_id": "effect-1"}, "effect-1"


def _delivery_claim() -> LeaseClaim:
    payload = {
        "schema_version": "delivery_intent.v2",
        "care_action_id": "care-action-1",
        "human_decision_id": "decision-1",
        "recipient_actor_id": "actor-1",
        "recipient_binding_id": "binding-1",
        "recipient_role": "elder",
        "synthetic_non_release": True,
    }
    return LeaseClaim(
        work_id="delivery-1",
        operation_id="source-operation-1",
        queue=DELIVERY_QUEUE,
        namespace_id="replay:normal-one-night",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=0,
        lease_generation=2,
        fencing_token="d" * 64,
        worker_instance="worker-1",
        attempt=1,
        max_attempts=8,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload=payload,
        authorization_snapshot={
            "authorization_epoch": 1,
            "privacy_epoch": 1,
            "retrieval_policy_epoch": 1,
        },
        metadata={
            "work_kind": "delivery",
            "handler_name": "deterministic_replay_sink",
            "destination": "replay_care_notification",
            "semantic_effect_key": "effect-key-1",
        },
    )


def test_delivery_uses_stable_invocation_and_real_sink() -> None:
    store = _InvocationStore()
    sink = _Sink()

    result = ReplayDeliveryWorkHandler(sink=sink)(
        WorkContext(_delivery_claim(), store, threading.Event())
    )

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert store.invocation_key == "replay-delivery:effect-key-1:v1"
    assert store.state == InvocationState.RESPONSE_RECEIVED
    assert len(sink.calls) == 1


class _DeliverySinkCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.row: tuple[Any, ...] | None = None

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((query, params))
        payload_sha256 = _sha256(_delivery_claim().payload)
        effect_id = "replay-effect:" + hashlib.sha256(
            b"effect-key-1"
        ).hexdigest()
        response = {
            "schema_version": "replay_delivery_sink_result.v1",
            "effect_id": effect_id,
            "semantic_effect_key": "effect-key-1",
            "status": "delivered",
        }
        if "sleepagent_stage4_delivery_authority_allows" in query:
            self.row = (True,)
        elif "FROM public.backend_delivery_intents" in query:
            self.row = (payload_sha256, "effect-key-1", "CareAction", "care-action-1", 1)
        elif "FROM public.backend_replay_delivery_effects_v2" in query:
            self.row = (
                effect_id,
                "delivery-1",
                payload_sha256,
                _sha256(response),
                response,
            )
        elif "UPDATE public.backend_care_actions_v2" in query:
            self.row = ("active",)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def test_replay_delivery_sink_activates_public_care_projection() -> None:
    cursor = _DeliverySinkCursor()
    factory = _Factory(cursor)
    context = WorkContext(
        _delivery_claim(),
        _Store(factory),
        threading.Event(),
    )
    intent = {
        "care_action_id": "care-action-1",
        "human_decision_id": "decision-1",
        "delivery_intent_id": "delivery-1",
        "semantic_effect_key": "effect-key-1",
        "payload_sha256": _sha256(_delivery_claim().payload),
    }

    response, _ = DeterministicReplayDeliverySink().send(
        context,
        scope=_scope(),
        intent=intent,
    )

    assert response["status"] == "delivered"
    assert factory.uow.committed is True
    assert any(
        "UPDATE public.backend_care_actions_v2" in query
        for query, _params in cursor.executions
    )
    lock_index = next(
        index
        for index, (query, _params) in enumerate(cursor.executions)
        if "pg_advisory_xact_lock" in query
    )
    effect_index = next(
        index
        for index, (query, _params) in enumerate(cursor.executions)
        if "INSERT INTO public.backend_replay_delivery_effects_v2" in query
    )
    assert lock_index < effect_index
    assert cursor.executions[lock_index][1] == (
        "sleepagent:replay-delivery-effect:effect-key-1",
    )


def test_delivery_revocation_is_terminal_and_never_reported_as_success() -> None:
    store = _InvocationStore()
    sink = _Sink(TerminalWorkError("delivery_authority_revoked"))

    result = ReplayDeliveryWorkHandler(sink=sink)(
        WorkContext(_delivery_claim(), store, threading.Event())
    )

    assert result.disposition == WorkDisposition.OUTCOME_UNKNOWN
    assert result.error_code == "connector_outcome_unknown"


class _ReconciliationCursor:
    def __init__(self, effect: tuple[Any, ...] | None) -> None:
        self.effect = effect
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.row: tuple[Any, ...] | None = None
        self.rowcount = 1

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((query, params))
        if "FROM public.sleep_domain_operations AS operation" in query:
            self.row = (
                "replay_care_notification",
                "deterministic_replay_sink",
                "effect-key-1",
                "a" * 64,
                "outcome_unknown",
                2,
                "d" * 64,
                "b" * 64,
                "invocation-1",
                "outcome_unknown",
                "c" * 64,
                3,
                2,
                "d" * 64,
            )
        elif "FROM public.backend_replay_delivery_effects_v2" in query:
            self.row = self.effect
        elif "MAX(sequence)" in query:
            self.row = (4,)
        elif "RETURNING cas_version" in query:
            self.row = (4,)
        elif "RETURNING status" in query:
            self.row = (params[0],)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def _reconciliation_claim() -> LeaseClaim:
    payload = {
        "schema_version": "delivery_reconciliation_operation.v1",
        "delivery_intent_id": "delivery-1",
        "source_operation_id": "source-operation-1",
        "destination": "replay_care_notification",
        "handler_name": "deterministic_replay_sink",
        "semantic_effect_key": "effect-key-1",
        "payload_sha256": "a" * 64,
        "invocation_key": "replay-delivery:effect-key-1:v1",
        "trigger_error_code": "prior_send_requires_reconciliation",
        "authorization_snapshot": _snapshot("reconciliation"),
    }
    return _operation_claim(
        operation_type="delivery_reconciliation", payload=payload, version=3
    )


@pytest.mark.parametrize(
    ("effect", "resolution", "delivery_state"),
    [
        (
            (
                "effect-1",
                "delivery-1",
                "a" * 64,
                "e" * 64,
                {"status": "delivered"},
            ),
            "known_delivered",
            "delivered",
        ),
        (None, "known_not_delivered", "retry"),
        (
            (
                "effect-conflict",
                "another-delivery",
                "a" * 64,
                "e" * 64,
                {"status": "delivered"},
            ),
            "unknown",
            "dead_letter",
        ),
    ],
)
def test_delivery_reconciliation_records_query_resolution(
    effect: tuple[Any, ...] | None,
    resolution: str,
    delivery_state: str,
) -> None:
    cursor = _ReconciliationCursor(effect)
    factory = _Factory(cursor)
    ids = iter(f"id-{index}" for index in range(30))

    result = DeliveryReconciliationHandler(id_generator=lambda: next(ids))(
        WorkContext(
            _reconciliation_claim(),
            _Store(factory),
            threading.Event(),
        )
    )

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result["resolution"] == resolution
    assert result.result["delivery_state"] == delivery_state
    assert factory.uow.committed is True
    receipt_inserts = [
        params
        for query, params in cursor.executions
        if "backend_delivery_reconciliation_receipts_v2" in query
    ]
    assert receipt_inserts and receipt_inserts[0][10] == resolution


def test_stage4_composition_registers_only_real_enabled_handlers() -> None:
    settings = SleepBackendSettings(
        profile="test-stage4-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:",),
        worker_queues=("induction", DELIVERY_QUEUE, "reconciliation"),
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )

    handlers = build_stage4_worker_handlers(settings)

    assert isinstance(handlers["induction"], InductionWorkHandler)
    assert isinstance(handlers[DELIVERY_QUEUE], ReplayDeliveryWorkHandler)
    assert isinstance(handlers["reconciliation"], Stage4ReconciliationRouter)
