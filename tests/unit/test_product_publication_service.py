from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from sleepagent.runtime.cold_start import ClaimCeiling
from sleepagent.runtime.contracts import (
    CommunicationDraft,
    EpisodeStatus,
    EpisodeType,
    InvocationOutcome,
    stable_hash,
)
from sleepagent.runtime.governance import PublicationError
from sleepagent.runtime.publication_service import (
    PublicationIndeterminateError,
    PublicationService,
)


@dataclass
class _JournalEntry:
    intent_id: str = "intent-1"
    episode_id: str = "episode-1"
    command_hash: str = ""
    draft_hash: str = ""
    state: str = "reserved"
    delivered: bool | None = None


class _MemoryGate:
    def __init__(
        self,
        events: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def validate_prepublication(
        self,
        receipt_outputs: tuple[dict[str, Any], ...],
        *,
        subject_id: str,
        actor_id: str,
    ) -> None:
        self.events.append("memory_gate")
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "receipt_outputs": receipt_outputs,
                "subject_id": subject_id,
                "actor_id": actor_id,
            }
        )


class _Revalidator:
    def __init__(
        self,
        events: list[str],
        *,
        current: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.current = current
        self.error = error
        self.snapshots: list[object] = []

    def __call__(self, snapshot: object) -> bool:
        self.events.append("revalidate")
        self.snapshots.append(snapshot)
        if self.error is not None:
            raise self.error
        return self.current


class _Publisher:
    def __init__(
        self,
        events: list[str],
        *,
        delivered: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.delivered = delivered
        self.error = error
        self.drafts: list[CommunicationDraft] = []

    def publish(self, draft: CommunicationDraft) -> bool:
        self.events.append("publisher")
        self.drafts.append(draft)
        if self.error is not None:
            raise self.error
        return self.delivered


class _ResultStore:
    def __init__(
        self,
        events: list[str],
        *,
        entry: _JournalEntry | None = None,
        created: bool = True,
        reserve_error: Exception | None = None,
        complete_error: Exception | None = None,
        completed_entry: _JournalEntry | None = None,
    ) -> None:
        self.events = events
        self.entry = entry or _JournalEntry()
        self.created = created
        self.reserve_error = reserve_error
        self.complete_error = complete_error
        self.completed_entry = completed_entry
        self.reserve_calls: list[dict[str, object]] = []
        self.complete_calls: list[dict[str, object]] = []

    def reserve_publication(
        self,
        *,
        command_hash: str,
        episode_id: str,
        draft_hash: str,
    ) -> tuple[_JournalEntry, bool]:
        self.events.append("reserve")
        self.reserve_calls.append(
            {
                "command_hash": command_hash,
                "episode_id": episode_id,
                "draft_hash": draft_hash,
            }
        )
        if self.reserve_error is not None:
            raise self.reserve_error
        if not self.entry.draft_hash:
            self.entry.draft_hash = draft_hash
        if not self.entry.command_hash:
            self.entry.command_hash = command_hash
        return self.entry, self.created

    def complete_publication(
        self,
        *,
        intent_id: str,
        delivered: bool,
    ) -> _JournalEntry:
        self.events.append("complete")
        self.complete_calls.append(
            {"intent_id": intent_id, "delivered": delivered}
        )
        if self.complete_error is not None:
            raise self.complete_error
        if self.completed_entry is not None:
            return self.completed_entry
        self.entry.state = "delivered" if delivered else "failed"
        self.entry.delivered = delivered
        return self.entry


def _draft() -> CommunicationDraft:
    return CommunicationDraft(
        draft_id="draft-1",
        audience_role="elder",
        text="这是经过验证的发布内容。",
        context_notice="测试上下文。",
    )


def _source_dependent_request() -> SimpleNamespace:
    binding = SimpleNamespace(
        actor_id="actor-1",
        subject_id="subject-1",
        role="elder",
        authorization_scope=("read_sleep_data",),
    )
    snapshot = SimpleNamespace(binding=binding)
    readiness = SimpleNamespace(
        scope_source_refs=("night:1",),
        baseline_source_refs=(),
        scope_valid_night_count=1,
        baseline_valid_night_count=0,
        claim_ceiling=ClaimCeiling.SINGLE_NIGHT_DESCRIPTION,
    )
    return SimpleNamespace(
        episode_id="episode-1",
        episode_type=EpisodeType.MORNING_REVIEW,
        idempotency_key="publication-command-1",
        fact_snapshot=snapshot,
        runtime_readiness_decisions=(readiness,),
    )


def _tool_receipts() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            tool_name="memory.read",
            outcome=InvocationOutcome.SUCCEEDED,
            output={"items": [{"retrieval_handle": "memory-handle-1"}]},
        ),
        SimpleNamespace(
            tool_name="memory.read",
            outcome=InvocationOutcome.FAILED,
            output={"items": [{"retrieval_handle": "ignored-failed-read"}]},
        ),
        SimpleNamespace(
            tool_name="radar.get_night_evidence",
            outcome=InvocationOutcome.SUCCEEDED,
            output={"items": [{"retrieval_handle": "ignored-other-tool"}]},
        ),
    ]


def _service(
    *,
    events: list[str],
    entry: _JournalEntry | None = None,
    created: bool = True,
    publisher_delivered: bool = True,
    reserve_error: Exception | None = None,
    publisher_error: Exception | None = None,
    complete_error: Exception | None = None,
    completed_entry: _JournalEntry | None = None,
    revalidator_current: bool = True,
    revalidator_error: Exception | None = None,
    memory_gate_error: Exception | None = None,
) -> tuple[PublicationService, _MemoryGate, _Revalidator, _Publisher, _ResultStore]:
    memory_gate = _MemoryGate(events, error=memory_gate_error)
    revalidator = _Revalidator(
        events,
        current=revalidator_current,
        error=revalidator_error,
    )
    publisher = _Publisher(
        events,
        delivered=publisher_delivered,
        error=publisher_error,
    )
    store = _ResultStore(
        events,
        entry=entry,
        created=created,
        reserve_error=reserve_error,
        complete_error=complete_error,
        completed_entry=completed_entry,
    )
    service = PublicationService(
        publisher=publisher,
        result_store=store,
        longitudinal_memory=memory_gate,
        revalidator=revalidator,
    )
    return service, memory_gate, revalidator, publisher, store


def _publish(service: PublicationService) -> bool:
    return service.publish(
        _draft(),
        episode_id="episode-1",
        request=_source_dependent_request(),
        tool_receipts=_tool_receipts(),
    )


def test_publication_command_hash_canonicalizes_authorization_scope_order(
) -> None:
    first = _source_dependent_request()
    second = _source_dependent_request()
    first.fact_snapshot.binding.authorization_scope = (
        "read_sleep_data",
        "publish_summary",
    )
    second.fact_snapshot.binding.authorization_scope = (
        "publish_summary",
        "read_sleep_data",
    )

    assert PublicationService._publication_command_hash(
        first
    ) == PublicationService._publication_command_hash(second)


def test_publish_orders_revalidation_memory_reservation_delivery_and_completion(
) -> None:
    events: list[str] = []
    service, memory_gate, revalidator, publisher, store = _service(
        events=events
    )

    delivered = _publish(service)

    assert delivered is True
    assert events == [
        "revalidate",
        "memory_gate",
        "reserve",
        "publisher",
        "complete",
    ]
    assert revalidator.snapshots == [_source_dependent_request().fact_snapshot]
    assert memory_gate.calls == [
        {
            "receipt_outputs": (
                {"items": [{"retrieval_handle": "memory-handle-1"}]},
            ),
            "subject_id": "subject-1",
            "actor_id": "actor-1",
        }
    ]
    assert publisher.drafts == [_draft()]
    assert store.reserve_calls == [
        {
            "command_hash": PublicationService._publication_command_hash(
                _source_dependent_request()
            ),
            "episode_id": "episode-1",
            "draft_hash": stable_hash(_draft()),
        }
    ]
    assert store.complete_calls == [
        {"intent_id": "intent-1", "delivered": True}
    ]


def test_episode_binding_mismatch_fails_before_all_publication_gates() -> None:
    events: list[str] = []
    service, memory_gate, revalidator, publisher, store = _service(
        events=events
    )
    request = _source_dependent_request()
    request.episode_id = "episode-other"

    with pytest.raises(ValueError, match="Episode binding mismatch"):
        service.publish(
            _draft(),
            episode_id="episode-1",
            request=request,
            tool_receipts=_tool_receipts(),
        )

    assert events == []
    assert revalidator.snapshots == []
    assert memory_gate.calls == []
    assert publisher.drafts == []
    assert store.reserve_calls == []


def test_missing_revalidator_fails_closed_before_memory_or_reservation() -> None:
    events: list[str] = []
    service, memory_gate, _, publisher, store = _service(events=events)
    service.revalidator = None

    with pytest.raises(PublicationError, match="requires current"):
        _publish(service)

    assert events == []
    assert memory_gate.calls == []
    assert publisher.drafts == []
    assert store.reserve_calls == []


def test_stale_revalidation_fails_closed_before_memory_or_reservation() -> None:
    events: list[str] = []
    service, memory_gate, revalidator, publisher, store = _service(
        events=events,
        revalidator_current=False,
    )

    with pytest.raises(PublicationError, match="source changed"):
        _publish(service)

    assert events == ["revalidate"]
    assert len(revalidator.snapshots) == 1
    assert memory_gate.calls == []
    assert publisher.drafts == []
    assert store.reserve_calls == []


def test_revalidator_exception_fails_closed_before_memory_or_reservation(
) -> None:
    events: list[str] = []
    service, memory_gate, _, publisher, store = _service(
        events=events,
        revalidator_error=TimeoutError("authorization lookup failed"),
    )

    with pytest.raises(PublicationError, match="revalidation failed") as caught:
        _publish(service)

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert events == ["revalidate"]
    assert memory_gate.calls == []
    assert publisher.drafts == []
    assert store.reserve_calls == []


def test_memory_gate_exception_fails_closed_before_reservation() -> None:
    events: list[str] = []
    service, memory_gate, _, publisher, store = _service(
        events=events,
        memory_gate_error=RuntimeError("memory governance unavailable"),
    )

    with pytest.raises(PublicationError, match="Memory publication gate") as caught:
        _publish(service)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert events == ["revalidate", "memory_gate"]
    assert memory_gate.calls == []
    assert publisher.drafts == []
    assert store.reserve_calls == []


@pytest.mark.parametrize(
    ("state", "delivered"),
    [("delivered", True), ("failed", False)],
)
def test_completed_replay_returns_journal_outcome_without_calling_publisher(
    state: str,
    delivered: bool,
) -> None:
    events: list[str] = []
    entry = _JournalEntry(state=state, delivered=delivered)
    service, _, _, publisher, store = _service(
        events=events,
        entry=entry,
        created=False,
    )

    assert _publish(service) is delivered
    assert events == ["revalidate", "memory_gate", "reserve"]
    assert publisher.drafts == []
    assert store.complete_calls == []


def test_delivered_command_rejects_a_different_retry_draft_before_publisher(
) -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(events=events)
    assert _publish(service) is True
    store.created = False
    changed = _draft().model_copy(
        update={"text": "同一逻辑命令生成了不同的重试草稿。"}
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        service.publish(
            changed,
            episode_id="episode-1",
            request=_source_dependent_request(),
            tool_receipts=_tool_receipts(),
        )

    assert caught.value.phase == "reservation"
    assert publisher.drafts == [_draft()]
    assert len(store.complete_calls) == 1


def test_delivered_logical_command_survives_worker_episode_recreation() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(events=events)
    assert _publish(service) is True
    store.created = False
    retry = _source_dependent_request()
    retry.episode_id = "episode-recreated-by-worker"

    delivered = service.publish(
        _draft(),
        episode_id=retry.episode_id,
        request=retry,
        tool_receipts=_tool_receipts(),
    )

    assert delivered is True
    assert publisher.drafts == [_draft()]
    assert len(store.complete_calls) == 1


@pytest.mark.parametrize(
    ("entry", "created"),
    [
        (_JournalEntry(episode_id="episode-other"), True),
        (_JournalEntry(command_hash="f" * 64), True),
        (_JournalEntry(draft_hash="f" * 64), True),
        (_JournalEntry(intent_id=""), True),
        (_JournalEntry(state="unknown"), False),
        (_JournalEntry(state="reserved", delivered=True), False),
        (_JournalEntry(state="delivered", delivered=False), False),
        (_JournalEntry(state="failed", delivered=True), False),
        (_JournalEntry(state="delivered", delivered=True), True),
    ],
    ids=[
        "wrong-episode",
        "wrong-command-hash",
        "wrong-draft-hash",
        "blank-intent",
        "unknown-state",
        "reserved-with-outcome",
        "delivered-with-false",
        "failed-with-true",
        "new-reservation-already-final",
    ],
)
def test_corrupt_reservation_or_replay_entry_is_indeterminate_before_publisher(
    entry: _JournalEntry,
    created: bool,
) -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        entry=entry,
        created=created,
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "reservation"
    assert isinstance(caught.value.__cause__, ValueError)
    assert events == ["revalidate", "memory_gate", "reserve"]
    assert publisher.drafts == []
    assert store.complete_calls == []


def test_non_boolean_reservation_creation_flag_is_indeterminate() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(events=events)
    store.created = "yes"  # type: ignore[assignment]

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "reservation"
    assert events == ["revalidate", "memory_gate", "reserve"]
    assert publisher.drafts == []
    assert store.complete_calls == []


@pytest.mark.parametrize(
    "completed_entry",
    [
        _JournalEntry(
            intent_id="intent-other",
            draft_hash=stable_hash(_draft()),
            state="delivered",
            delivered=True,
        ),
        _JournalEntry(
            episode_id="episode-other",
            draft_hash=stable_hash(_draft()),
            state="delivered",
            delivered=True,
        ),
        _JournalEntry(
            draft_hash="f" * 64,
            state="delivered",
            delivered=True,
        ),
        _JournalEntry(
            draft_hash=stable_hash(_draft()),
            state="reserved",
            delivered=None,
        ),
        _JournalEntry(
            draft_hash=stable_hash(_draft()),
            state="delivered",
            delivered=False,
        ),
    ],
    ids=[
        "changed-intent",
        "changed-episode",
        "changed-draft-hash",
        "not-finalized",
        "invalid-final-outcome",
    ],
)
def test_corrupt_completion_entry_is_journal_indeterminate(
    completed_entry: _JournalEntry,
) -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        completed_entry=completed_entry,
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "journal"
    assert isinstance(caught.value.__cause__, ValueError)
    assert events == [
        "revalidate",
        "memory_gate",
        "reserve",
        "publisher",
        "complete",
    ]
    assert len(publisher.drafts) == 1
    assert store.complete_calls == [
        {"intent_id": "intent-1", "delivered": True}
    ]


def test_existing_reserved_intent_is_indeterminate_before_publisher() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        entry=_JournalEntry(intent_id="intent-existing"),
        created=False,
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "reservation"
    assert caught.value.intent_id == "intent-existing"
    assert events == ["revalidate", "memory_gate", "reserve"]
    assert publisher.drafts == []
    assert store.complete_calls == []


def test_reservation_exception_is_reservation_indeterminate() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        reserve_error=RuntimeError("reservation connection lost"),
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "reservation"
    assert caught.value.intent_id is None
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert events == ["revalidate", "memory_gate", "reserve"]
    assert publisher.drafts == []
    assert store.complete_calls == []


def test_publisher_exception_is_publisher_indeterminate() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        publisher_error=TimeoutError("delivery acknowledgement lost"),
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "publisher"
    assert caught.value.intent_id == "intent-1"
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert events == ["revalidate", "memory_gate", "reserve", "publisher"]
    assert len(publisher.drafts) == 1
    assert store.complete_calls == []


def test_non_boolean_publisher_outcome_does_not_finalize_as_failed() -> None:
    events: list[str] = []
    entry = _JournalEntry()
    service, _, _, publisher, store = _service(
        events=events,
        entry=entry,
    )
    publisher.delivered = 0  # type: ignore[assignment]

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "publisher"
    assert caught.value.intent_id == "intent-1"
    assert events == ["revalidate", "memory_gate", "reserve", "publisher"]
    assert len(publisher.drafts) == 1
    assert store.complete_calls == []
    assert entry.state == "reserved"
    assert entry.delivered is None


def test_completion_exception_is_journal_indeterminate() -> None:
    events: list[str] = []
    service, _, _, publisher, store = _service(
        events=events,
        complete_error=OSError("journal acknowledgement lost"),
    )

    with pytest.raises(PublicationIndeterminateError) as caught:
        _publish(service)

    assert caught.value.phase == "journal"
    assert caught.value.intent_id == "intent-1"
    assert isinstance(caught.value.__cause__, OSError)
    assert events == [
        "revalidate",
        "memory_gate",
        "reserve",
        "publisher",
        "complete",
    ]
    assert len(publisher.drafts) == 1
    assert store.complete_calls == [
        {"intent_id": "intent-1", "delivered": True}
    ]


def test_publisher_false_is_a_known_failed_outcome() -> None:
    events: list[str] = []
    entry = _JournalEntry()
    service, _, _, publisher, store = _service(
        events=events,
        entry=entry,
        publisher_delivered=False,
    )

    assert _publish(service) is False
    assert events == [
        "revalidate",
        "memory_gate",
        "reserve",
        "publisher",
        "complete",
    ]
    assert len(publisher.drafts) == 1
    assert store.complete_calls == [
        {"intent_id": "intent-1", "delivered": False}
    ]
    assert entry.state == "failed"
    assert entry.delivered is False


def test_runner_memory_gate_exception_cannot_escape_via_degraded_publication(
) -> None:
    from tests.unit.test_product_agent_runner import request, runner

    class _RaisingMemoryGate:
        def __init__(self) -> None:
            self.calls = 0

        def validate_prepublication(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            raise RuntimeError("memory governance unavailable")

    class _NeverPublisher:
        def __init__(self) -> None:
            self.draft_ids: list[str] = []

        def publish(self, draft: CommunicationDraft) -> bool:
            self.draft_ids.append(draft.draft_id)
            return True

    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    gate = _RaisingMemoryGate()
    publisher = _NeverPublisher()
    store = instance.publication_service.result_store
    original_reserve = store.reserve_publication
    reserve_calls: list[dict[str, object]] = []

    def tracked_reserve(
        *,
        episode_id: str,
        draft_hash: str,
        now: object | None = None,
    ) -> object:
        reserve_calls.append(
            {"episode_id": episode_id, "draft_hash": draft_hash}
        )
        return original_reserve(
            episode_id=episode_id,
            draft_hash=draft_hash,
            now=now,
        )

    store.reserve_publication = tracked_reserve  # type: ignore[method-assign]
    instance.publication_service.longitudinal_memory = gate
    instance.publication_service.publisher = publisher

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status is EpisodeStatus.BLOCKED
    assert "agent_path_failed:PublicationError" in result.receipt.failure_codes
    assert gate.calls >= 1
    assert reserve_calls == []
    assert publisher.draft_ids == []
    assert result.publication is None
    assert result.publication_delivered is False


def test_runner_does_not_publish_degraded_fallback_after_indeterminate_delivery(
) -> None:
    # Reuse the established Runner fixture so this assertion covers the real
    # main-path exception boundary rather than another component-level facade.
    from tests.unit.test_product_agent_runner import request, runner

    class _RaisingPublisher:
        def __init__(self) -> None:
            self.draft_ids: list[str] = []

        def publish(self, draft: CommunicationDraft) -> bool:
            self.draft_ids.append(draft.draft_id)
            raise TimeoutError("delivery acknowledgement lost")

    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    publisher = _RaisingPublisher()
    instance.publication_service.publisher = publisher

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status is EpisodeStatus.BLOCKED
    assert "publication_indeterminate:publisher" in result.receipt.failure_codes
    assert publisher.draft_ids and len(publisher.draft_ids) == 1
    assert not any(
        draft_id.startswith("degraded:") for draft_id in publisher.draft_ids
    )
    assert result.publication is None
    assert result.publication_delivered is False


def test_runner_known_delivery_failure_is_partial_without_second_publication(
) -> None:
    from tests.unit.test_product_agent_runner import request, runner

    class _FailedPublisher:
        def __init__(self) -> None:
            self.draft_ids: list[str] = []

        def publish(self, draft: CommunicationDraft) -> bool:
            self.draft_ids.append(draft.draft_id)
            return False

    instance, _ = runner(EpisodeType.CARE_PLAN)
    publisher = _FailedPublisher()
    instance.publication_service.publisher = publisher

    result = instance.run(request(EpisodeType.CARE_PLAN))

    assert result.receipt.status is EpisodeStatus.PARTIAL
    assert "publication_delivery_failed" in result.receipt.failure_codes
    assert publisher.draft_ids and len(publisher.draft_ids) == 1
    assert not publisher.draft_ids[0].startswith("degraded:")
    assert result.publication is not None
    assert result.publication.draft_id == publisher.draft_ids[0]
    assert result.publication_delivered is False
    assert result.pending_confirmations == []
