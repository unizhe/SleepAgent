from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Event, Lock, Thread
from typing import Any

import pytest

from sleepagent.radar_agent.product_agent.agent_invocation_coordinator import (
    ProviderInputBudgetLedger,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    InvocationOutcome,
    SourceScope,
    SourceScopeKind,
    ToolEffect,
    ToolReceipt,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.episode_result_finalizer import (
    EpisodeResultFinalizer,
)
from sleepagent.radar_agent.product_agent.governance import AcceptanceError
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ConfirmedActionOutcome,
    PendingConfirmationTarget,
    PendingUserInputTarget,
    ProductContinuationLineage,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    TerminalEpisodeDecision,
    bind_product_episode_checkpoint,
    product_episode_request_hash,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductToolExecutionContext,
    ProductToolResult,
)
from sleepagent.radar_agent.product_agent.registry import (
    RUNTIME_INTERACTION_TOOLS,
    TOOL_DEFINITIONS,
)
from sleepagent.radar_agent.product_agent.tool_execution_coordinator import (
    ToolExecutionCoordinator,
)
from sleepagent.radar_agent.product_agent.tooling import (
    canonical_runtime_interaction_idempotency_key,
    canonical_tool_input_hash,
    canonical_tool_invocation_id,
)


NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)


def _snapshot() -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="snapshot:coordinator",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            authorization_scope=("read_sleep_data",),
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 8, 9),
            date_end=date(2026, 8, 9),
            valid_night_count=1,
        ),
        canonical_data_version="canonical:coordinator",
        source_refs=("night:coordinator",),
        created_at=NOW,
    )


def _tool_result(
    snapshot: FactSnapshot | None = None,
    *,
    tool_name: str = "policy.read",
    arguments: dict[str, Any] | None = None,
    caller: AgentId | str = "runtime",
    episode_id: str = "episode:coordinator",
) -> ProductToolResult:
    bound_snapshot = snapshot or _snapshot()
    bound_arguments = (
        {"purpose": "test"} if arguments is None else arguments
    )
    input_hash = canonical_tool_input_hash(tool_name, bound_arguments)
    definition = TOOL_DEFINITIONS[tool_name]
    caller_name = caller.value if isinstance(caller, AgentId) else caller
    idempotency_key: str | None = None
    if tool_name in RUNTIME_INTERACTION_TOOLS:
        idempotency_key = canonical_runtime_interaction_idempotency_key(
            tool_name,
            bound_arguments,
            episode_id=episode_id,
            fact_snapshot_hash=bound_snapshot.fact_snapshot_hash,
        )
    elif definition.effect is not ToolEffect.READ_ONLY:
        idempotency_key = "test-idempotency"
    receipt = ToolReceipt(
        tool_invocation_id=canonical_tool_invocation_id(
            tool_name,
            input_hash,
        ),
        tool_name=tool_name,
        tool_version=f"{tool_name}.{definition.version}",
        caller=caller_name,
        fact_snapshot_id=bound_snapshot.fact_snapshot_id,
        fact_snapshot_hash=bound_snapshot.fact_snapshot_hash,
        input_hash=input_hash,
        effect=definition.effect,
        outcome=InvocationOutcome.SUCCEEDED,
        observed_at=NOW,
        output={"policy_version": "test"},
        source_refs=["policy:test"],
        idempotency_key=idempotency_key,
    )
    return ProductToolResult(receipt=receipt)


class _RecordingToolExecutor:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        fail_release: bool = False,
    ) -> None:
        self.events = events if events is not None else []
        self.fail_release = fail_release
        self.calls: list[
            tuple[str, dict[str, Any], ProductToolExecutionContext]
        ] = []
        self.released: list[str] = []
        self.active_episodes: set[str] = set()
        self.result: ProductToolResult | None = None
        self.last_result: ProductToolResult | None = None

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ProductToolExecutionContext,
    ) -> ProductToolResult:
        self.events.append("execute")
        self.calls.append((tool_name, arguments, context))
        result = self.result or _tool_result(
            context.fact_snapshot,
            tool_name=tool_name,
            arguments=arguments,
            caller=context.caller,
            episode_id=context.episode_id or "episode:unbound",
        )
        self.last_result = result
        return result

    def release_episode(self, episode_id: str) -> None:
        self.events.append("tool_release")
        self.released.append(episode_id)
        if self.fail_release:
            raise RuntimeError("tool cleanup failed")
        self.active_episodes.discard(episode_id)


class _RecordingRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.tool_calls = 0

    def record_tool_call(self) -> None:
        self.events.append("record")
        self.tool_calls += 1


class _ResultStore:
    def __init__(
        self,
        events: list[str],
        *,
        fail_terminal: bool = False,
    ) -> None:
        self.events = events
        self.fail_terminal = fail_terminal
        self.nonterminal: list[ProductEpisodeRunResult] = []
        self.terminal: list[ProductEpisodeRunResult] = []

    def history(self, episode_id: str) -> list[ProductEpisodeRunResult]:
        return [
            result
            for result in [*self.nonterminal, *self.terminal]
            if result.receipt.episode_id == episode_id
        ]

    def append_nonterminal(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
    ) -> None:
        assert subject_id == "subject-1"
        self.events.append("append_nonterminal")
        self.nonterminal.append(result)

    def append_terminal_bundle(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> object:
        del now
        assert subject_id == "subject-1"
        self.events.append("append_terminal")
        if self.fail_terminal:
            raise RuntimeError("durable append failed")
        self.terminal.append(result)
        return object()


class _TracingBudgetLedger(ProviderInputBudgetLedger):
    def __init__(
        self,
        events: list[str],
        *,
        fail_release: bool = False,
    ) -> None:
        super().__init__()
        self.events = events
        self.fail_release = fail_release

    def release_episode(self, episode_id: str) -> None:
        self.events.append("budget_release")
        if self.fail_release:
            raise RuntimeError("provider budget cleanup failed")
        super().release_episode(episode_id)


def _result(*, terminal: bool) -> ProductEpisodeRunResult:
    snapshot = _snapshot()
    status = EpisodeStatus.COMPLETE if terminal else EpisodeStatus.WAITING_USER
    receipt = EpisodeReceipt(
        episode_id="episode:coordinator",
        episode_type=EpisodeType.MORNING_REVIEW,
        receipt_revision=1,
        terminal=terminal,
        execution_mode=ExecutionMode.INTELLIGENT,
        status=status,
        goal_achieved=terminal,
        fact_snapshot_id=snapshot.fact_snapshot_id,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        source_scope=snapshot.source_scope,
        final_episode_state_revision=1,
        trace_ref="trace:episode:coordinator:1",
    )
    if terminal:
        return ProductEpisodeRunResult(
            registry_hash="r" * 64,
            receipt=receipt,
        )
    return ProductEpisodeRunResult(
        registry_hash="r" * 64,
        continuation_request_hash="c" * 64,
        receipt=receipt,
        pending_user_input=PendingUserInputTarget(
            request_id="request:coordinator",
            question_text="昨晚是否比平时晚睡？",
            why_needed="补齐当前证据缺口。",
            decision_scope="current_night",
            target_role="elder",
            source_agent=AgentId.EVIDENCE_REASONING,
            expires_at=NOW + timedelta(minutes=20),
        ),
    )


def _confirmed_finalization_fixture() -> tuple[
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
]:
    snapshot = _snapshot()
    request = ProductEpisodeRunRequest(
        episode_id="episode:confirmed-finalizer",
        episode_type=EpisodeType.CARE_PLAN,
        objective="Commit one exact frozen action.",
        fact_snapshot=snapshot,
    )
    receipt = EpisodeReceipt(
        episode_id=request.episode_id,
        episode_type=request.episode_type,
        receipt_revision=4,
        terminal=False,
        execution_mode=ExecutionMode.INTELLIGENT,
        status=EpisodeStatus.WAITING_CONFIRMATION,
        goal_achieved=False,
        fact_snapshot_id=snapshot.fact_snapshot_id,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        source_scope=snapshot.source_scope,
        final_episode_state_revision=3,
        trace_ref="trace:confirmed-finalizer",
    )
    frozen = ProductEpisodeRunResult(
        registry_hash="r" * 64,
        continuation_request_hash=product_episode_request_hash(request),
        receipt=receipt,
        pending_confirmations=[
            PendingConfirmationTarget(
                confirmation_id="confirmation:memory:one",
                target_kind="memory",
                candidate_id="memory:one",
                candidate_hash="b" * 64,
                actor_id="actor-1",
                subject_id="subject-1",
                action_scope="commit_memory",
                reason="Commit the reviewed memory candidate.",
                expires_at=NOW + timedelta(minutes=20),
                decision_id="decision:one",
                proposal_id="proposal:one",
            )
        ],
    )
    return request, bind_product_episode_checkpoint(frozen)


def test_provider_input_budget_enforces_16k_48k_and_release() -> None:
    ledger = ProviderInputBudgetLedger()

    assert ledger.reserve(
        "episode-a", AgentId.EVIDENCE_REASONING, 16_000
    ) == 16_000
    assert ledger.reserve(
        "episode-a", AgentId.EVIDENCE_REASONING, 16_000
    ) == 32_000
    assert ledger.reserve(
        "episode-a", AgentId.EVIDENCE_REASONING, 16_000
    ) == 48_000
    with pytest.raises(AcceptanceError, match="budget exhausted"):
        ledger.reserve("episode-a", AgentId.EVIDENCE_REASONING, 1)
    with pytest.raises(AcceptanceError, match="budget exhausted"):
        ledger.reserve("episode-a", AgentId.CARE_STRATEGY, 16_001)

    ledger.reserve("episode-a", AgentId.CARE_STRATEGY, 8_000)
    ledger.reserve("episode-b", AgentId.EVIDENCE_REASONING, 5_000)
    assert ledger.episode_total("episode-a") == 56_000
    ledger.release_episode("episode-a")
    assert ledger.episode_total("episode-a") == 0
    assert ledger.episode_total("episode-b") == 5_000


def test_provider_input_budget_reservation_is_atomic_under_contention() -> None:
    ledger = ProviderInputBudgetLedger(
        per_call_limit=1_000,
        per_agent_episode_limit=5_000,
    )
    contenders = 12
    gate = Barrier(contenders)

    def reserve() -> bool:
        gate.wait(timeout=5)
        try:
            ledger.reserve("episode-race", AgentId.SLEEP_CARE, 1_000)
        except AcceptanceError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        outcomes = list(pool.map(lambda _index: reserve(), range(contenders)))

    assert sum(outcomes) == 5
    assert ledger.episode_total("episode-race", AgentId.SLEEP_CARE) == 5_000


def test_tool_execution_coordinator_records_then_returns_typed_delegate() -> None:
    events: list[str] = []
    executor = _RecordingToolExecutor(events=events)
    coordinator = ToolExecutionCoordinator(executor)
    runtime = _RecordingRuntime(events)
    snapshot = _snapshot()
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=snapshot,
        episode_id="episode:coordinator",
    )
    arguments = {"purpose": "test"}

    returned = coordinator.execute(
        "policy.read",
        arguments,
        context=context,
        runtime=runtime,
        record_call=True,
    )

    assert executor.last_result is not None
    assert returned is not executor.last_result
    assert returned.receipt == executor.last_result.receipt
    assert returned.context_item is not None
    assert returned.context_item.key == "tool:policy.read"
    assert events == ["record", "execute"]
    assert runtime.tool_calls == 1
    assert executor.calls == [("policy.read", arguments, context)]
    with pytest.raises(ValueError, match="requires a Product Episode runtime"):
        coordinator.execute(
            "policy.read",
            {},
            context=context,
            record_call=True,
        )
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tool_name", "profile.read"),
        ("caller", "evidence_reasoning"),
        ("fact_snapshot_id", "snapshot:other"),
        ("fact_snapshot_hash", "f" * 64),
    ),
)
def test_tool_execution_rejects_mismatched_receipt_binding(
    field: str,
    value: str,
) -> None:
    executor = _RecordingToolExecutor()
    base_result = _tool_result(arguments={"purpose": "test"})
    executor.result = ProductToolResult(
        receipt=base_result.receipt.model_copy(update={field: value})
    )
    coordinator = ToolExecutionCoordinator(executor)

    with pytest.raises(ValueError, match="invocation binding"):
        coordinator.execute(
            "policy.read",
            {"purpose": "test"},
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=_snapshot(),
                episode_id="episode:coordinator",
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tool_version", "policy.read.v999"),
        ("input_hash", "b" * 64),
        ("tool_invocation_id", "tool:policy.read:forged"),
        ("idempotency_key", "forged-read-idempotency"),
    ),
)
def test_tool_execution_rejects_tampered_receipt_provenance(
    field: str,
    value: str,
) -> None:
    arguments = {"purpose": "test"}
    executor = _RecordingToolExecutor()
    valid = _tool_result(arguments=arguments)
    executor.result = ProductToolResult(
        receipt=valid.receipt.model_copy(update={field: value})
    )
    coordinator = ToolExecutionCoordinator(executor)

    with pytest.raises(ValueError, match="receipt provenance"):
        coordinator.execute(
            "policy.read",
            arguments,
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=_snapshot(),
                episode_id="episode:coordinator",
            ),
        )


def test_runtime_interaction_receipt_uses_canonical_hash_and_idempotency() -> None:
    tool_name = "questionnaire.select_profile"
    episode_id = "episode:runtime-interaction-provenance"
    arguments = {
        "request": {
            "request_id": "request:one",
            "episode_id": episode_id,
            "subject_id": "subject-1",
            "actor_id": "actor-1",
            "role": "elder",
            "remaining_episode_budget": 3,
        },
        "_now": NOW.isoformat(),
    }
    snapshot = _snapshot()
    valid = _tool_result(
        snapshot,
        tool_name=tool_name,
        arguments=arguments,
        episode_id=episode_id,
    )
    executor = _RecordingToolExecutor()
    executor.result = valid
    coordinator = ToolExecutionCoordinator(executor)
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=snapshot,
        episode_id=episode_id,
    )

    returned = coordinator.execute(
        tool_name,
        arguments,
        context=context,
    )

    expected_input_hash = canonical_tool_input_hash(tool_name, arguments)
    assert expected_input_hash != stable_hash(arguments)
    assert returned.receipt.input_hash == expected_input_hash
    assert returned.receipt.tool_invocation_id == canonical_tool_invocation_id(
        tool_name,
        expected_input_hash,
    )
    assert returned.receipt.idempotency_key == (
        canonical_runtime_interaction_idempotency_key(
            tool_name,
            arguments,
            episode_id=episode_id,
            fact_snapshot_hash=snapshot.fact_snapshot_hash,
        )
    )

    executor.result = valid.model_copy(
        update={
            "receipt": valid.receipt.model_copy(
                update={"idempotency_key": "runtime-interaction:forged"}
            )
        }
    )
    with pytest.raises(ValueError, match="receipt provenance"):
        coordinator.execute(tool_name, arguments, context=context)


def test_unhashable_nonweakref_mapping_adapter_is_registered_by_identity(
) -> None:
    class _SlottedMappingAdapter:
        __slots__ = ("calls", "released")
        __hash__ = None  # type: ignore[assignment]

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.released: list[str] = []

        def __eq__(self, other: object) -> bool:
            return self is other

        def execute(
            self,
            tool_name: str,
            arguments: dict[str, Any],
            *,
            context: ProductToolExecutionContext,
        ) -> dict[str, Any]:
            self.calls.append(arguments)
            return _tool_result(
                context.fact_snapshot,
                tool_name=tool_name,
                arguments=arguments,
                caller=context.caller,
                episode_id=context.episode_id or "episode:unbound",
            ).model_dump(mode="python")

        def release_episode(self, episode_id: str) -> None:
            self.released.append(episode_id)

    episode_id = "episode:slotted-adapter"
    adapter = _SlottedMappingAdapter()
    coordinator = ToolExecutionCoordinator(adapter)  # type: ignore[arg-type]
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=_snapshot(),
        episode_id=episode_id,
    )

    first = coordinator.execute(
        "policy.read",
        {"purpose": "adapter-one"},
        context=context,
    )
    second = coordinator.execute(
        "policy.read",
        {"purpose": "adapter-two"},
        context=context,
    )
    coordinator.release_episode(episode_id)

    assert first.context_item is not None
    assert second.context_item is not None
    assert len(adapter.calls) == 2
    assert adapter.released == [episode_id]


def test_tool_execution_isolates_caller_payload_from_adapter_normalization() -> None:
    class _MutatingExecutor(_RecordingToolExecutor):
        def execute(
            self,
            tool_name: str,
            arguments: dict[str, Any],
            *,
            context: ProductToolExecutionContext,
        ) -> ProductToolResult:
            original = deepcopy(arguments)
            arguments["nested"]["changed"] = True
            self.calls.append((tool_name, deepcopy(arguments), context))
            return _tool_result(
                snapshot=context.fact_snapshot,
                tool_name=tool_name,
                arguments=original,
                caller=context.caller,
                episode_id=context.episode_id or "episode:coordinator",
            )

    executor = _MutatingExecutor()
    coordinator = ToolExecutionCoordinator(executor)
    arguments = {"nested": {"changed": False}}
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=_snapshot(),
        episode_id="episode:coordinator",
    )

    result = coordinator.execute(
        "policy.read",
        arguments,
        context=context,
    )

    assert result.context_item is not None
    assert arguments == {"nested": {"changed": False}}
    assert executor.calls[0][1] == {"nested": {"changed": True}}


def test_tool_execution_validates_failed_result_context() -> None:
    arguments = {"nested": {"changed": False}}
    executor = _RecordingToolExecutor()
    coordinator = ToolExecutionCoordinator(executor)

    valid = _tool_result(arguments=arguments)
    failed_receipt = valid.receipt.model_copy(
        update={
            "outcome": InvocationOutcome.FAILED,
            "error_code": "SyntheticFailure",
        }
    )
    executor.result = ProductToolResult(
        receipt=failed_receipt,
        context_item=ToolExecutionCoordinator.context_item_for_receipt(
            valid.receipt
        ),
    )
    with pytest.raises(ValueError, match="cannot expose trusted context"):
        coordinator.execute(
            "policy.read",
            arguments,
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=_snapshot(),
                episode_id="episode:coordinator",
            ),
        )


def test_tool_execution_same_episode_is_serialized_across_coordinators() -> None:
    first_coordinator = ToolExecutionCoordinator(_RecordingToolExecutor())
    second_coordinator = ToolExecutionCoordinator(_RecordingToolExecutor())
    first_entered = Event()
    second_attempted = Event()
    second_entered = Event()
    release_first = Event()

    def first() -> None:
        with first_coordinator.episode_execution_lease("episode-same"):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second() -> None:
        second_attempted.set()
        with second_coordinator.episode_execution_lease("episode-same"):
            second_entered.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    try:
        assert first_entered.wait(timeout=5)
        second_thread.start()
        assert second_attempted.wait(timeout=5)
        assert not second_entered.wait(timeout=0.05)
    finally:
        release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()
    assert first_coordinator._episode_leases == {}
    assert second_coordinator._episode_leases == {}


def test_tool_execution_different_episodes_can_run_in_parallel() -> None:
    coordinator = ToolExecutionCoordinator(_RecordingToolExecutor())
    both_inside = Barrier(2)

    def enter(episode_id: str) -> None:
        with coordinator.episode_execution_lease(episode_id):
            both_inside.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(enter, "episode-left"),
            pool.submit(enter, "episode-right"),
        ]
        for future in futures:
            future.result(timeout=5)

    assert coordinator._episode_leases == {}


def test_tool_execution_nested_lease_is_reentrant_and_exception_safe() -> None:
    coordinator = ToolExecutionCoordinator(_RecordingToolExecutor())

    with pytest.raises(RuntimeError, match="operation failed"):
        with coordinator.episode_execution_lease("episode-nested"):
            with coordinator.episode_execution_lease("episode-nested"):
                assert (
                    coordinator._episode_leases[
                        "episode-nested"
                    ].references
                    == 2
                )
            raise RuntimeError("operation failed")

    assert coordinator._episode_leases == {}


def test_terminal_release_cleans_every_participating_executor() -> None:
    first_executor = _RecordingToolExecutor()
    second_executor = _RecordingToolExecutor()
    first = ToolExecutionCoordinator(first_executor)
    second = ToolExecutionCoordinator(second_executor)
    episode_id = "episode:cross-bundle-cache-release"
    first_executor.active_episodes.add(episode_id)
    second_executor.active_episodes.add(episode_id)
    first.execute(
        "policy.read",
        {"purpose": "register-first-executor"},
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
            episode_id=episode_id,
        ),
    )

    second.release_episode(episode_id)

    assert first_executor.released == [episode_id]
    assert second_executor.released == [episode_id]
    assert first_executor.active_episodes == set()
    assert second_executor.active_episodes == set()


def test_result_finalizer_nonterminal_preserves_transient_state() -> None:
    events: list[str] = []
    executor = _RecordingToolExecutor(events=events)
    executor.active_episodes.add("episode:coordinator")
    budget = _TracingBudgetLedger(events)
    budget.reserve("episode:coordinator", AgentId.SLEEP_CARE, 10)
    store = _ResultStore(events)
    finalizer = EpisodeResultFinalizer(
        result_store=store,
        tool_execution_coordinator=ToolExecutionCoordinator(executor),
        provider_input_budget_ledger=budget,
    )
    result = _result(terminal=False)

    returned = finalizer.finalize(result, subject_id="subject-1")

    assert returned.continuation_checkpoint_hash is not None
    assert events == ["append_nonterminal"]
    assert store.nonterminal == [returned]
    assert executor.active_episodes == {"episode:coordinator"}
    assert budget.episode_total("episode:coordinator") == 10


def test_result_finalizer_persists_terminal_before_both_cleanups() -> None:
    events: list[str] = []
    executor = _RecordingToolExecutor(events=events)
    executor.active_episodes.add("episode:coordinator")
    budget = _TracingBudgetLedger(events)
    budget.reserve("episode:coordinator", AgentId.SLEEP_CARE, 10)
    store = _ResultStore(events)
    finalizer = EpisodeResultFinalizer(
        result_store=store,
        tool_execution_coordinator=ToolExecutionCoordinator(executor),
        provider_input_budget_ledger=budget,
    )
    result = _result(terminal=True)

    returned = finalizer.finalize(result, subject_id="subject-1")

    assert returned == result
    assert events == ["append_terminal", "tool_release", "budget_release"]
    assert store.terminal == [result]
    assert executor.active_episodes == set()
    assert budget.episode_total("episode:coordinator") == 0


def test_result_finalizer_append_failure_performs_no_cleanup() -> None:
    events: list[str] = []
    executor = _RecordingToolExecutor(events=events)
    executor.active_episodes.add("episode:coordinator")
    budget = _TracingBudgetLedger(events)
    budget.reserve("episode:coordinator", AgentId.SLEEP_CARE, 10)
    store = _ResultStore(events, fail_terminal=True)
    finalizer = EpisodeResultFinalizer(
        result_store=store,
        tool_execution_coordinator=ToolExecutionCoordinator(executor),
        provider_input_budget_ledger=budget,
    )

    with pytest.raises(RuntimeError, match="durable append failed"):
        finalizer.finalize(_result(terminal=True), subject_id="subject-1")

    assert events == ["append_terminal"]
    assert store.terminal == []
    assert executor.active_episodes == {"episode:coordinator"}
    assert budget.episode_total("episode:coordinator") == 10


@pytest.mark.parametrize(
    ("fail_tool_cleanup", "fail_budget_cleanup"),
    [(True, False), (False, True), (True, True)],
)
def test_result_finalizer_cleanup_failure_does_not_undo_durable_terminal(
    fail_tool_cleanup: bool,
    fail_budget_cleanup: bool,
) -> None:
    events: list[str] = []
    executor = _RecordingToolExecutor(
        events=events,
        fail_release=fail_tool_cleanup,
    )
    budget = _TracingBudgetLedger(
        events,
        fail_release=fail_budget_cleanup,
    )
    budget.reserve("episode:coordinator", AgentId.SLEEP_CARE, 10)
    store = _ResultStore(events)
    finalizer = EpisodeResultFinalizer(
        result_store=store,
        tool_execution_coordinator=ToolExecutionCoordinator(executor),
        provider_input_budget_ledger=budget,
    )
    result = _result(terminal=True)

    returned = finalizer.finalize(result, subject_id="subject-1")

    assert returned == result
    assert store.terminal == [result]
    assert events == ["append_terminal", "tool_release", "budget_release"]


def test_confirmed_action_finalization_merges_typed_deltas_and_owns_revision(
) -> None:
    request, frozen = _confirmed_finalization_fixture()
    existing_receipt = _tool_result(request.fact_snapshot).receipt
    frozen = bind_product_episode_checkpoint(
        frozen.model_copy(
            update={
                "receipt": frozen.receipt.model_copy(
                    update={
                        "tool_receipt_ids": [
                            existing_receipt.tool_invocation_id
                        ]
                    }
                ),
                "tool_receipts": [existing_receipt],
                "committed_memory_candidate_ids": ["memory:existing"],
            }
        )
    )
    additional_receipt = existing_receipt.model_copy(
        update={
            "tool_invocation_id": "tool:state.commit_memory:confirmed",
            "tool_name": "state.commit_memory",
            "tool_version": "state.commit_memory.v1",
            "input_hash": "d" * 64,
        }
    )
    events: list[str] = []
    finalizer = EpisodeResultFinalizer(
        result_store=_ResultStore(events),
        tool_execution_coordinator=ToolExecutionCoordinator(
            _RecordingToolExecutor(events=events)
        ),
        provider_input_budget_ledger=_TracingBudgetLedger(events),
    )
    lineage = ProductContinuationLineage(
        kind="commit_frozen_confirmed_action",
        parent_checkpoint_hash="e" * 64,
        command_hash="f" * 64,
    )

    result = finalizer.finalize_confirmed_action(
        frozen_result=frozen,
        outcome=ConfirmedActionOutcome(
            additional_tool_receipts=(additional_receipt,),
            committed_memory_candidate_ids=("memory:new",),
            declined_confirmation_ids=("confirmation:declined",),
        ),
        decision=TerminalEpisodeDecision(
            status=EpisodeStatus.COMPLETE,
            goal_achieved=True,
        ),
        lineage=lineage,
        subject_id="subject-1",
        request=request,
    )

    assert result.receipt.receipt_revision == 5
    assert result.receipt.terminal is True
    assert result.receipt.status is EpisodeStatus.COMPLETE
    assert result.pending_confirmations == []
    assert result.committed_memory_candidate_ids == [
        "memory:existing",
        "memory:new",
    ]
    assert result.declined_confirmation_ids == ["confirmation:declined"]
    assert result.tool_receipts == [existing_receipt, additional_receipt]
    assert result.continuation_kind == lineage.kind
    assert result.continuation_parent_checkpoint_hash == (
        lineage.parent_checkpoint_hash
    )
    assert events == ["append_terminal", "tool_release", "budget_release"]


def test_confirmed_action_finalization_rejects_receipt_identity_collision(
) -> None:
    request, frozen = _confirmed_finalization_fixture()
    existing_receipt = _tool_result(request.fact_snapshot).receipt
    frozen = bind_product_episode_checkpoint(
        frozen.model_copy(update={"tool_receipts": [existing_receipt]})
    )
    collision = existing_receipt.model_copy(
        update={"output": {"policy_version": "different"}}
    )
    events: list[str] = []
    finalizer = EpisodeResultFinalizer(
        result_store=_ResultStore(events),
        tool_execution_coordinator=ToolExecutionCoordinator(
            _RecordingToolExecutor(events=events)
        ),
        provider_input_budget_ledger=_TracingBudgetLedger(events),
    )

    with pytest.raises(ValueError, match="receipt identity collision"):
        finalizer.finalize_confirmed_action(
            frozen_result=frozen,
            outcome=ConfirmedActionOutcome(
                additional_tool_receipts=(collision,)
            ),
            decision=TerminalEpisodeDecision(
                status=EpisodeStatus.COMPLETE,
                goal_achieved=True,
            ),
            lineage=ProductContinuationLineage(
                kind="commit_frozen_confirmed_action",
                parent_checkpoint_hash="e" * 64,
                command_hash="f" * 64,
            ),
            subject_id="subject-1",
            request=request,
        )

    assert events == []
