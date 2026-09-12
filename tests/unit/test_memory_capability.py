from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from sleepagent.runtime.contracts import (
    AgentId,
    MemoryChangeCandidate,
    SourceScopeKind,
    TrustLabel,
    stable_hash,
)
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
    LegacyMemoryItemV1,
    MemoryChange,
    MemoryConfirmation,
    MemoryConflictError,
    MemoryItemStatus,
    MemoryOperation,
    MemoryPurpose,
    MemoryQueryIntent,
    ProvenanceType,
    SensitivityClass,
    apply_memory_change,
    project_current_memory,
    resolve_memory_query,
    select_memory_slice,
    validate_memory_handle,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
SUBJECT = "subject-1"
ACTOR = "elder-1"


def _item(**updates: Any) -> GovernedMemoryItemV2:
    values: dict[str, Any] = {
        "memory_id": "memory:wake-time",
        "subject_id": SUBJECT,
        "memory_type": "routine",
        "concept_id": "sleep.wake_time",
        "value_schema_id": "bounded_string.v1",
        "typed_value": "07:00",
        "provenance_type": ProvenanceType.ELDER_CONFIRMED,
        "source_ref": "user-report:1",
        "source_scope_kind": SourceScopeKind.THIRTY_DAY,
        "version": 1,
        "recorded_at": NOW - timedelta(days=2),
        "valid_from": NOW - timedelta(days=2),
        "valid_until": NOW + timedelta(days=30),
        "sensitivity_class": SensitivityClass.PERSONAL,
        "allowed_roles": (AgentId.EVIDENCE_REASONING,),
        "allowed_purposes": (MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,),
        "confirmation_ref": "confirmation:1",
        "retention_policy_version": "retention.v1",
        **updates,
    }
    return GovernedMemoryItemV2(**values)


def _state(*items: Any, version: int = 0) -> GovernedMemoryState:
    return GovernedMemoryState(subject_id=SUBJECT, version=version, revisions=items)


def _query(
    *concepts: str,
    purpose: MemoryPurpose = MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
    scope: SourceScopeKind = SourceScopeKind.THIRTY_DAY,
    max_items: int = 8,
    token_budget: int = 1200,
    **updates: Any,
):
    intent = MemoryQueryIntent(
        purpose=purpose,
        concept_ids=concepts or ("sleep.wake_time",),
        source_scope_kind=scope,
        max_items=max_items,
        token_budget=token_budget,
    )
    values: dict[str, Any] = {
        "invocation_id": "invocation-1",
        "actor_id": ACTOR,
        "actor_role": "elder",
        "subject_id": SUBJECT,
        "requesting_agent": AgentId.EVIDENCE_REASONING,
        "authorization_scope": ("personal_memory:read",),
        "as_of": NOW,
        "privacy_epoch": 3,
        "authorization_epoch": 5,
        **updates,
    }
    return resolve_memory_query(intent, **values)


def _candidate(
    memory_id: str = "memory:wake-time",
    *,
    operation: str = "create",
    concept_id: str = "sleep.wake_time",
    value: Any = "07:00",
    **updates: Any,
) -> MemoryChangeCandidate:
    values: dict[str, Any] = {
        "candidate_id": memory_id,
        "operation": operation,
        "subject_id": SUBJECT,
        "memory_type": "routine",
        "concept_id": concept_id,
        "value_schema_id": "bounded_string.v1",
        "typed_value": value,
        "provenance_type": "elder_confirmed",
        "source_ref": "user-report:1",
        "sensitivity_class": "personal",
        "allowed_roles": (AgentId.EVIDENCE_REASONING,),
        "allowed_purposes": ("personal_evidence_context",),
        "explicit_user_authorization": True,
        **updates,
    }
    return MemoryChangeCandidate(**values)


def _change(
    state: GovernedMemoryState,
    operation: MemoryOperation,
    *,
    prior: GovernedMemoryItemV2 | None = None,
    value: Any = "07:30",
    concept_id: str = "sleep.wake_time",
    candidate_updates: dict[str, Any] | None = None,
    **updates: Any,
) -> MemoryChange:
    carries_value = operation in {MemoryOperation.REMEMBER, MemoryOperation.CORRECT}
    candidate = (
        _candidate(
            operation="create" if operation == MemoryOperation.REMEMBER else "replace",
            concept_id=concept_id,
            value=value,
            **(candidate_updates or {}),
        )
        if carries_value
        else None
    )
    created_at = NOW + timedelta(minutes=state.version * 2)
    values: dict[str, Any] = {
        "change_id": f"memory-change:{operation.value}:{state.version}",
        "operation": operation,
        "memory_id": "memory:wake-time",
        "subject_id": SUBJECT,
        "expected_state_version": state.version,
        "proposed_value": candidate,
        "causal_ref": "user-report:1",
        "source_actor_id": ACTOR,
        "source_actor_role": "elder",
        "source_scope_kind": SourceScopeKind.THIRTY_DAY,
        "target_revision_ref": prior.revision_ref if prior else None,
        "target_revision_hash": stable_hash(prior) if prior else None,
        "confirmation_actor_id": ACTOR,
        "created_at": created_at,
        "confirmation_expires_at": created_at + timedelta(minutes=10),
        **updates,
    }
    return MemoryChange(**values)


def _confirmation(change: MemoryChange, **updates: Any) -> MemoryConfirmation:
    values: dict[str, Any] = {
        "confirmation_id": f"confirmation:{change.change_id}",
        "actor_id": ACTOR,
        "subject_id": SUBJECT,
        "target_change_id": change.change_id,
        "target_change_hash": change.change_hash,
        "approved_at": change.created_at + timedelta(minutes=1),
        "expires_at": change.confirmation_expires_at,
        **updates,
    }
    return MemoryConfirmation(**values)


def _apply(
    state: GovernedMemoryState,
    change: MemoryChange,
    **confirmation_updates: Any,
) -> GovernedMemoryState:
    confirmation = _confirmation(change, **confirmation_updates)
    return apply_memory_change(state, change, confirmation, now=confirmation.approved_at)


def _remembered(value: Any = "07:00") -> tuple[GovernedMemoryState, GovernedMemoryItemV2]:
    empty = _state()
    state = _apply(empty, _change(empty, MemoryOperation.REMEMBER, value=value))
    item = state.revisions[-1]
    assert isinstance(item, GovernedMemoryItemV2)
    return state, item


@pytest.mark.parametrize(
    ("schema", "value"),
    [("bounded_string.v1", "07:00"), ("boolean.v1", True),
     ("number.v1", 7.5), ("enum.v1", "early_bird")],
)
def test_v2_item_binds_typed_value_hash_and_governance(schema: str, value: Any) -> None:
    item = _item(value_schema_id=schema, typed_value=value)
    assert item.value_hash == stable_hash(
        {"concept_id": item.concept_id, "value_schema_id": schema,
         "value_schema_version": "1", "typed_value": value}
    )
    assert (item.provenance_type, item.sensitivity_class) == (
        ProvenanceType.ELDER_CONFIRMED, SensitivityClass.PERSONAL
    )
    assert item.model_dump(mode="json")["trust_label"] == "user_memory_untrusted_data"
    assert item.trust_label is TrustLabel.USER_MEMORY_UNTRUSTED_DATA


@pytest.mark.parametrize(
    ("schema", "value"),
    [("boolean.v1", 1), ("number.v1", float("inf")),
     ("enum.v1", "Not Normalized"), ("bounded_string.v1", "")],
)
def test_v2_item_rejects_invalid_schema_value(schema: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        _item(value_schema_id=schema, typed_value=value)


def test_v2_item_rejects_hash_and_serialized_value_tampering() -> None:
    item = _item()
    with pytest.raises(ValidationError, match="value_hash"):
        _item(value_hash="0" * 64)
    with pytest.raises(ValidationError, match="value_hash"):
        GovernedMemoryItemV2.model_validate(
            {**item.model_dump(mode="json"), "typed_value": "09:00"}
        )


def test_query_requires_exact_subject() -> None:
    with pytest.raises(PermissionError, match="subject"):
        select_memory_slice(_query(subject_id="subject-2"), _state(_item()), now=NOW)


@pytest.mark.parametrize(
    ("item_updates", "query_args", "reason"),
    [
        ({"allowed_roles": (AgentId.SLEEP_CARE,)}, {}, "role_excluded"),
        ({"allowed_purposes": (MemoryPurpose.EXPLICIT_MEMORY_REVIEW,)}, {}, "purpose_excluded"),
        ({"source_scope_kind": SourceScopeKind.SEVEN_DAY}, {}, "source_scope_excluded"),
        ({}, {"concepts": ("sleep.other",)}, "concept_excluded"),
        ({"status": MemoryItemStatus.EXPIRED, "typed_value": None}, {}, "inactive_or_expired_excluded"),
        ({"status": MemoryItemStatus.FORGOTTEN, "typed_value": None}, {}, "inactive_or_expired_excluded"),
        ({"valid_until": NOW}, {}, "inactive_or_expired_excluded"),
    ],
)
def test_query_filter_fails_closed_per_governance_condition(
    item_updates: dict[str, Any], query_args: dict[str, Any], reason: str
) -> None:
    concepts = query_args.pop("concepts", ("sleep.wake_time",))
    receipt = select_memory_slice(_query(*concepts, **query_args), _state(_item(**item_updates)), now=NOW)
    assert receipt.items == ()
    assert reason in receipt.filter_reason_codes


def test_legacy_v1_is_identified_but_excluded_from_normal_retrieval() -> None:
    legacy = LegacyMemoryItemV1(
        memory_id="legacy:1", subject_id=SUBJECT, value="old plaintext",
        source_ref="legacy-db:1", version=1,
    )
    receipt = select_memory_slice(_query(), _state(legacy, _item()), now=NOW)
    assert tuple(item.revision_ref for item in receipt.items) == ("memory:wake-time:v1",)
    assert "legacy_v1_excluded" in receipt.filter_reason_codes


def test_selection_order_result_and_budget_are_deterministic() -> None:
    items = (
        _item(memory_id="memory:c", concept_id="sleep.c", typed_value="c"),
        _item(memory_id="memory:a", concept_id="sleep.a", typed_value="a"),
        _item(memory_id="memory:b", concept_id="sleep.b", typed_value="b"),
    )
    query = _query("sleep.a", "sleep.b", "sleep.c", max_items=2)
    forward = select_memory_slice(query, _state(*items), now=NOW)
    reverse = select_memory_slice(query, _state(*reversed(items)), now=NOW)
    assert forward == reverse
    assert tuple(item.concept_id for item in forward.items) == ("sleep.a", "sleep.b")
    assert "deterministic_budget_exhausted" in forward.filter_reason_codes


def test_incompatible_current_values_fail_closed_as_unresolved_conflict() -> None:
    state = _state(
        _item(memory_id="memory:a", typed_value="07:00"),
        _item(memory_id="memory:b", typed_value="09:00"),
    )
    with pytest.raises(MemoryConflictError) as raised:
        select_memory_slice(_query(), state, now=NOW)
    assert raised.value.revision_refs == ("memory:a:v1", "memory:b:v1")


def test_remember_is_append_only_and_does_not_mutate_prior_state() -> None:
    empty = _state()
    state = _apply(empty, _change(empty, MemoryOperation.REMEMBER, value="07:00"))
    assert (empty.version, empty.revisions) == (0, ())
    assert (state.version, len(state.revisions)) == (1, 1)
    assert project_current_memory(state, now=NOW + timedelta(minutes=2))[0].typed_value == "07:00"


def test_correct_preserves_raw_history_and_projects_effective_superseded_per_lineage() -> None:
    state, prior = _remembered()
    other = _item(memory_id="memory:other", concept_id="sleep.other", typed_value="other")
    state = GovernedMemoryState(subject_id=SUBJECT, version=state.version, revisions=(*state.revisions, other))
    corrected = _apply(state, _change(state, MemoryOperation.CORRECT, prior=prior, value="07:30"))
    old, unrelated, current = corrected.revisions
    assert old.status == unrelated.status == current.status == MemoryItemStatus.ACTIVE
    audit = {record["revision_ref"]: record for record in corrected.audit_projection()}
    assert audit[prior.revision_ref]["effective_status"] == "superseded"
    assert audit[other.revision_ref]["effective_status"] == "active"
    assert current.supersedes_ref == prior.revision_ref
    assert project_current_memory(corrected, now=NOW + timedelta(minutes=5))[-1] == current


@pytest.mark.parametrize(
    ("operation", "status"),
    [(MemoryOperation.EXPIRE, MemoryItemStatus.EXPIRED),
     (MemoryOperation.FORGET, MemoryItemStatus.FORGOTTEN)],
)
def test_terminal_reducers_append_tombstone(
    operation: MemoryOperation, status: MemoryItemStatus
) -> None:
    state, prior = _remembered()
    terminal = _apply(state, _change(state, operation, prior=prior))
    tombstone = terminal.revisions[-1]
    assert len(terminal.revisions) == 2 and prior.typed_value == "07:00"
    assert isinstance(tombstone, GovernedMemoryItemV2)
    assert (tombstone.status, tombstone.typed_value, tombstone.supersedes_ref) == (
        status, None, prior.revision_ref
    )
    assert project_current_memory(terminal, now=NOW + timedelta(minutes=5)) == ()


@pytest.mark.parametrize("drift", ["state_version", "target_ref", "target_hash"])
def test_reducer_rejects_stale_state_and_target_drift(drift: str) -> None:
    state, prior = _remembered()
    updates = {
        "state_version": {"expected_state_version": 0},
        "target_ref": {"target_revision_ref": "memory:other:v1"},
        "target_hash": {"target_revision_hash": "0" * 64},
    }[drift]
    change = _change(state, MemoryOperation.CORRECT, prior=prior, **updates)
    with pytest.raises((ValueError, PermissionError)):
        _apply(state, change)


def test_state_rejects_broken_replacement_relation() -> None:
    prior = _item()
    replacement = _item(version=2, supersedes_ref="memory:wrong:v1", typed_value="07:30")
    with pytest.raises(ValidationError, match="replacement relation"):
        _state(prior, replacement)


@pytest.mark.parametrize("terminal_operation", [MemoryOperation.EXPIRE, MemoryOperation.FORGET])
def test_inactive_or_forgotten_target_rejects_further_change(
    terminal_operation: MemoryOperation,
) -> None:
    state, prior = _remembered()
    state = _apply(state, _change(state, terminal_operation, prior=prior))
    terminal = state.revisions[-1]
    assert isinstance(terminal, GovernedMemoryItemV2)
    with pytest.raises(ValueError, match="not current"):
        _apply(state, _change(state, MemoryOperation.CORRECT, prior=terminal))


@pytest.mark.parametrize("field", ["candidate_id", "subject_id"])
def test_change_candidate_must_bind_memory_and_subject_identity(field: str) -> None:
    empty = _state()
    updates = {field: "memory:other" if field == "candidate_id" else "subject-2"}
    with pytest.raises(ValidationError, match="does not bind"):
        _change(empty, MemoryOperation.REMEMBER, candidate_updates=updates)


def test_correct_cannot_change_concept_identity_within_lineage() -> None:
    state, prior = _remembered()
    change = _change(
        state, MemoryOperation.CORRECT, prior=prior, concept_id="sleep.bed_time"
    )
    with pytest.raises(ValueError, match="concept"):
        _apply(state, change)


def test_rebuilt_state_rejects_concept_drift_within_lineage() -> None:
    prior = _item()
    drifted = _item(
        concept_id="sleep.bed_time",
        version=2,
        supersedes_ref=prior.revision_ref,
        typed_value="22:30",
    )
    with pytest.raises(ValidationError, match="concept identity"):
        _state(prior, drifted)


@pytest.mark.parametrize(
    ("field", "value"),
    [("actor_id", "elder-2"), ("subject_id", "subject-2"),
     ("target_change_id", "memory-change:other"),
     ("target_change_hash", "0" * 64),
     ("expires_at", NOW + timedelta(hours=1))],
)
def test_confirmation_requires_exact_binding(field: str, value: Any) -> None:
    state, prior = _remembered()
    change = _change(state, MemoryOperation.CORRECT, prior=prior)
    with pytest.raises(PermissionError, match="exactly bound"):
        _apply(state, change, **{field: value})


def test_expired_confirmation_is_rejected() -> None:
    state, prior = _remembered()
    change = _change(state, MemoryOperation.CORRECT, prior=prior)
    confirmation = _confirmation(change)
    with pytest.raises(PermissionError, match="exactly bound"):
        apply_memory_change(state, change, confirmation, now=confirmation.expires_at)


def test_forget_is_logical_redaction_with_non_value_audit_proof() -> None:
    state, first = _remembered("private plaintext")
    state = _apply(state, _change(state, MemoryOperation.CORRECT, prior=first, value="new secret"))
    second = state.revisions[-1]
    assert isinstance(second, GovernedMemoryItemV2)
    forgotten = _apply(state, _change(state, MemoryOperation.FORGET, prior=second))
    assert project_current_memory(forgotten, now=NOW + timedelta(minutes=10)) == ()
    assert first.typed_value == "private plaintext"  # raw ledger is not physical erase
    required = {"revision_ref", "memory_id", "concept_id", "operation", "raw_status",
                "effective_status", "recorded_at", "supersedes_ref", "confirmation_ref"}
    for record in forgotten.audit_projection():
        assert required <= record.keys()
        assert {"typed_value", "value_hash", "revision_hash"}.isdisjoint(record)
    with pytest.raises(ValueError, match="already exists"):
        _apply(forgotten, _change(forgotten, MemoryOperation.REMEMBER, value="resurrect"))


@pytest.mark.parametrize(
    "drift", ["invocation", "query", "result", "privacy_epoch", "authorization_epoch"]
)
def test_handle_rejects_any_binding_or_epoch_drift(drift: str) -> None:
    state = _state(_item())
    query = _query()
    receipt = select_memory_slice(query, state, now=NOW)
    handle = receipt.handles[0]
    kwargs: dict[str, Any] = {
        "receipt": receipt, "query": query, "state": state,
        "privacy_epoch": 3, "authorization_epoch": 5, "now": NOW,
    }
    if drift == "invocation":
        handle = handle.model_copy(update={"invocation_id": "invocation-2"})
    elif drift == "query":
        kwargs["query"] = query.model_copy(update={"query_id": "memory-query:" + "0" * 32})
    elif drift == "result":
        kwargs["receipt"] = receipt.model_copy(update={"result_hash": "0" * 64})
    else:
        kwargs[drift] += 1
    with pytest.raises((PermissionError, ValueError)):
        validate_memory_handle(handle, **kwargs)


@pytest.mark.parametrize("operation", [MemoryOperation.CORRECT, MemoryOperation.FORGET])
def test_correct_or_forget_invalidates_previously_issued_handle(
    operation: MemoryOperation,
) -> None:
    state, prior = _remembered()
    query = _query()
    receipt = select_memory_slice(query, state, now=NOW + timedelta(minutes=1))
    changed = _apply(state, _change(state, operation, prior=prior))
    with pytest.raises(ValueError, match="no longer references"):
        validate_memory_handle(
            receipt.handles[0], receipt=receipt, query=query, state=changed,
            privacy_epoch=3, authorization_epoch=5, now=NOW + timedelta(minutes=5),
        )


@pytest.mark.parametrize("boundary", ["change_id", "causal_ref", "source_ref"])
def test_habit_identity_or_source_cannot_enter_memory_reducer(boundary: str) -> None:
    empty = _state()
    updates: dict[str, Any] = {}
    candidate_updates: dict[str, Any] = {}
    if boundary == "change_id":
        updates[boundary] = "memory-change:habit:1"
    elif boundary == "causal_ref":
        updates[boundary] = "habit-fact:1"
    else:
        candidate_updates[boundary] = "habit-answer:1"
    with pytest.raises(ValidationError, match="Habit and Memory"):
        _change(empty, MemoryOperation.REMEMBER, candidate_updates=candidate_updates, **updates)


def test_memory_slice_is_untrusted_context_not_evidence_or_clinical_fact() -> None:
    item = _item()
    receipt = select_memory_slice(_query(), _state(item), now=NOW)
    projected = receipt.items[0]
    assert projected.model_dump(mode="json")["trust_label"] == "user_memory_untrusted_data"
    assert projected.verified_evidence is projected.verified_medical_fact is False
    with pytest.raises(ValidationError):
        type(projected).model_validate({**projected.model_dump(), "verified_evidence": True})
    with pytest.raises(ValidationError, match="Extra inputs"):
        GovernedMemoryItemV2.model_validate({**item.model_dump(), "verified_clinical_fact": True})
