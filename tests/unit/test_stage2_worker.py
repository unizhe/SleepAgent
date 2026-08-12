from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

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
from sleepagent.stage2_worker import (
    PreparedStage2Command,
    Stage2AuthorizationRevoked,
    Stage2CommandProcessor,
    Stage2WorkHandler,
    build_stage2_worker_handlers,
)
from sleepagent.worker_runtime import (
    InvocationKind,
    InvocationRecord,
    InvocationState,
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


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


def _claim(command_type: str = "interaction.start") -> LeaseClaim:
    scope = _scope()
    return LeaseClaim(
        work_id="01987654-3210-7abc-8def-0123456789ab",
        operation_id="01987654-3210-7abc-8def-0123456789ab",
        queue="product_interaction",
        namespace_id=scope.namespace_id,
        data_mode=scope.data_mode,
        namespace_generation=scope.namespace_generation,
        run_id=scope.run_id,
        arm_id=scope.arm_id,
        subject_id=scope.subject_id or "subject-1",
        operation_version=0,
        lease_generation=1,
        fencing_token="f" * 64,
        worker_instance=scope.worker_instance or "worker-1",
        attempt=1,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={
            "schema_version": "backend_operation.v2",
            "command_type": command_type,
        },
        authorization_snapshot={
            "schema_version": "authorization_snapshot.v1",
            "principal_id": "sleepagent-bff-test",
            "actor_id": "actor-1",
            "binding_id": "binding-1",
            "role": "elder",
            "effective_scopes": ["product:sleep:interaction:write"],
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "data_mode": scope.data_mode,
            "run_id": scope.run_id,
            "arm_id": scope.arm_id,
            "subject_id": scope.subject_id,
            "purpose": "sleep_care",
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
            "policy_sha256": "a" * 64,
        },
        metadata={
            "work_kind": "operation",
            "operation_type": command_type,
            "queue_name": "product_interaction",
        },
    )


class Store:
    def __init__(self) -> None:
        self.uow_factory = object()
        self.invocation: InvocationRecord | None = None
        self.send_started = 0

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        del claim
        return _scope()

    def reserve_invocation(self, claim, *, invocation_kind, invocation_key, request_sha256):
        self.invocation = InvocationRecord(
            invocation_id="invocation-1",
            invocation_key=invocation_key,
            invocation_kind=invocation_kind,
            work_id=claim.work_id,
            lease_generation=claim.lease_generation,
            request_sha256=request_sha256,
            state=InvocationState.RESERVED,
        )
        return self.invocation

    def mark_invocation_send_started(self, claim, record) -> bool:
        del claim, record
        self.send_started += 1
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
        del claim, error_code
        self.invocation = record.model_copy(
            update={
                "state": state,
                "provider_request_id": provider_request_id,
                "response": dict(response or {}),
            }
        )
        return True


class Processor:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.commits = []

    def prepare(self, scope: UowScope, claim: LeaseClaim) -> PreparedStage2Command:
        if self.failure is not None:
            raise self.failure
        return PreparedStage2Command(
            operation_id=claim.work_id,
            command_type="interaction.start",
            queue="product_interaction",
            payload={"intent": "morning_review"},
            target_id="episode-revision-1",
            authorization_snapshot=dict(claim.authorization_snapshot),
            fact_snapshot={
                "policy_sha256": "a" * 64,
                "source": {"night_episode_revision_id": "episode-revision-1"},
            },
            fact_snapshot_sha256="b" * 64,
            source_state_version=1,
        )

    def commit(self, scope, claim, prepared, *, model_response):
        self.commits.append((scope, claim, prepared, model_response))
        return {
            "schema_version": "product_interaction_result.v1",
            "interaction_id": "interaction-1",
            "public_state": "waiting_for_input",
            "answer_handle": "opaque-handle-1",
        }


def test_product_interaction_uses_invocation_journal_and_handler_owned_commit() -> None:
    store = Store()
    processor = Processor()
    handler = Stage2WorkHandler(
        queue="product_interaction",
        processor_factory=lambda factory: processor,
    )

    result = handler(WorkContext(_claim(), store, threading.Event()))

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert store.send_started == 1
    assert store.invocation is not None
    assert store.invocation.invocation_kind == InvocationKind.MODEL
    assert store.invocation.state == InvocationState.RESPONSE_RECEIVED
    assert processor.commits[0][3]["next_state"] == "waiting_user"


def test_revoked_queued_command_fails_without_model_dispatch() -> None:
    store = Store()
    handler = Stage2WorkHandler(
        queue="product_interaction",
        processor_factory=lambda factory: Processor(
            failure=Stage2AuthorizationRevoked("revoked")
        ),
    )

    result = handler(WorkContext(_claim(), store, threading.Event()))

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "stage2_authorization_revoked"
    assert store.invocation is None


def test_stage2_composition_has_real_handlers_for_each_enabled_queue() -> None:
    settings = SleepBackendSettings(
        profile="test-stage2-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:",),
        worker_queues=("sleep_command", "product_interaction"),
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )

    handlers = build_stage2_worker_handlers(settings)

    assert set(handlers) == {"sleep_command", "product_interaction"}
    assert all(isinstance(item, Stage2WorkHandler) for item in handlers.values())


def test_care_followup_inserts_complete_v2_event_without_mutating_outbox() -> None:
    occurred_at = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)

    class Cursor:
        def __init__(self) -> None:
            self.statements: list[tuple[str, tuple[object, ...] | None]] = []

        def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
            self.statements.append((query, params))

        def fetchone(self):
            query = self.statements[-1][0]
            if query.startswith("SELECT state"):
                return None
            if query == "SELECT clock_timestamp()":
                return (occurred_at,)
            if query.startswith("SELECT nextval"):
                return (17,)
            raise AssertionError(f"unexpected fetch for {query}")

    cursor = Cursor()
    claim = _claim("interaction.confirm")
    prepared = PreparedStage2Command(
        operation_id=claim.work_id,
        command_type="interaction.confirm",
        queue="product_interaction",
        payload={},
        target_id="interaction-1",
        authorization_snapshot=dict(claim.authorization_snapshot),
        fact_snapshot={},
        fact_snapshot_sha256="b" * 64,
        source_state_version=3,
        interaction_id="interaction-1",
    )

    Stage2CommandProcessor._insert_care_followup(
        cursor,
        scope=_scope(),
        claim=claim,
        prepared=prepared,
        night_episode_id="episode-1",
        care_action_id="care-action-1",
        source_event_id="source-event-1",
    )

    assert not any(
        query.startswith("UPDATE public.sleep_domain_domain_outbox")
        for query, _ in cursor.statements
    )
    outbox_query, outbox_params = cursor.statements[-1]
    assert outbox_query.startswith("INSERT INTO public.sleep_domain_domain_outbox")
    assert outbox_params is not None
    assert outbox_params[0] == 17
    event = json.loads(str(outbox_params[8]))
    assert event["delivery_offset"] == 17
    assert event["event_type"] == "CARE_FOLLOWUP_PENDING"
