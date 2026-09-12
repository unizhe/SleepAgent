from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from sleepagent.application.care_execution import (
    CARE_READ_SCOPE,
    CareExecutionCommandResult,
    CareExecutionPrincipal,
    CarePlanApplicationService,
    CarePlanFilter,
    CarePlanView,
)
from sleepagent.care_cli import build_parser, execute_command
from sleepagent.domain.care_actions import (
    CareActionCandidateV2,
    CareActionProposal,
    CareActionType,
    CareApprovalGrant,
    CareAudience,
    CareProposalState,
    DeterministicCareActionPolicy,
)
from sleepagent.domain.care_execution import (
    CARE_EXECUTION_SCOPE,
    HUMAN_ATTESTED_AUTHORITY,
    CareExecutionError,
    CareExecutionEvent,
    CareExecutionEventType,
    CareExecutionMode,
    CareExecutionState,
    CareExecutionStatus,
    CarePlanEntry,
    care_plan_id_for,
    command_fingerprint,
    execution_policy_for,
    render_action_zh_cn,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _proposal(
    action_type: CareActionType = CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
) -> CareActionProposal:
    mapping = {
        CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME: (
            "consistent-wake-time", "routine_adjustment", CareAudience.ELDER,
            {"tolerance_minutes": 30},
        ),
        CareActionType.RECOMMEND_MORNING_LIGHT: (
            "morning-light", "environment_adjustment", CareAudience.ELDER,
            {"minutes": 20},
        ),
        CareActionType.REQUEST_MANUAL_FOLLOW_UP: (
            "nighttime-gentle-support", "manual_support", CareAudience.FAMILY,
            {},
        ),
        CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK: (
            "morning-review-feedback", "review_feedback", CareAudience.FAMILY,
            {},
        ),
    }
    catalog_id, intent, audience, parameters = mapping[action_type]
    candidate = CareActionCandidateV2.create(
        candidate_id=f"candidate-{action_type.value}",
        subject_id="subject-a",
        source_analysis_revision_id="analysis-1",
        source_shared_analysis_sha256="a" * 64,
        source_night_finalization_revision_id="finalization-1",
        source_care_strategy_invocation_id="invocation-1",
        source_care_strategy_version="care-strategy.v1",
        source_care_work_product_ref="care-work-1",
        action_type=action_type,
        catalog_action_id=catalog_id,
        catalog_action_version=1,
        intent=intent,
        rationale_evidence_refs=("claim-1",),
        urgency="normal",
        audience=audience,
        parameters=parameters,
        created_at=NOW,
        display_explanation="Display-only model explanation.",
    )
    policy = DeterministicCareActionPolicy().evaluate(
        candidate,
        source_is_current=True,
        source_is_hard_finalized=True,
        evidence_is_sufficient=True,
    )
    return CareActionProposal.create(candidate, policy).transition(
        CareProposalState.APPROVED
    )


def _plan(
    action_type: CareActionType = CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
) -> CarePlanEntry:
    proposal = _proposal(action_type)
    grant = CareApprovalGrant.issue(
        proposal,
        approver_actor_id=(
            "elder-1" if proposal.candidate.audience is CareAudience.ELDER
            else "family-1"
        ),
        approver_role=proposal.required_approver_role,
        approver_binding_id="binding-1",
        authorization_epoch=3,
        idempotency_key="approve-1",
        issued_at=NOW + timedelta(minutes=1),
    )
    return CarePlanEntry.create(
        grant,
        proposal,
        source_night_key="2026-08-31",
    )


def _principal(role: CareAudience = CareAudience.ELDER) -> CareExecutionPrincipal:
    return CareExecutionPrincipal(
        service_principal_id="api-principal",
        actor_id=f"{role.value}-1",
        actor_role=role,
        actor_binding_id="binding-1",
        subject_id="subject-a",
        effective_scopes=frozenset({CARE_READ_SCOPE, CARE_EXECUTION_SCOPE}),
        authorization_epoch=3,
        privacy_epoch=2,
        retrieval_policy_epoch=4,
        namespace_id="live:test",
        namespace_generation=1,
        data_mode="live",
        run_id=None,
        arm_id=None,
    )


class _Repository:
    def __init__(self, plan: CarePlanEntry) -> None:
        self.view = CarePlanView(
            plan=plan,
            execution=CareExecutionStatus.initial(plan),
            approval_grant_state="active",
            executable=True,
        )
        self.events: list[CareExecutionEvent] = []
        self.commands: dict[str, CareExecutionEvent] = {}

    def list_plans(self, principal, *, state, limit, now):  # type: ignore[no-untyped-def]
        del principal, now
        if state is not None and state not in {
            CarePlanFilter.ACTIVE,
            CarePlanFilter(self.view.execution.state.value),
        }:
            return ()
        return (self.view,)[:limit]

    def get_plan(self, principal, *, care_plan_id, now):  # type: ignore[no-untyped-def]
        del principal, now
        return self.view if care_plan_id == self.view.plan.care_plan_id else None

    def history(self, principal, *, care_plan_id, now):  # type: ignore[no-untyped-def]
        del principal, care_plan_id, now
        return tuple(self.events)

    def execute(  # type: ignore[no-untyped-def]
        self,
        principal,
        *,
        care_plan_id,
        event_type,
        expected_version,
        idempotency_key,
        note,
        occurred_at,
    ):
        fingerprint = command_fingerprint(
            care_plan_id=care_plan_id,
            event_type=event_type,
            actor_id=principal.actor_id,
            note=note,
        )
        existing = self.commands.get(idempotency_key)
        if existing is not None:
            outcome = (
                "idempotent"
                if existing.command_fingerprint == fingerprint
                else "conflict"
            )
            return CareExecutionCommandResult(outcome, self.view, existing)
        if expected_version != self.view.execution.version:
            return CareExecutionCommandResult("conflict", self.view, None)
        previous = self.view.execution
        resulting = previous.transition(
            event_type,
            occurred_at=occurred_at,
            direct_complete_allowed=self.view.plan.direct_complete_allowed,
        )
        event = CareExecutionEvent(
            event_id=f"event-{len(self.events) + 1}",
            care_plan_id=care_plan_id,
            event_type=event_type,
            actor_principal_id=principal.service_principal_id,
            actor_id=principal.actor_id,
            actor_role=principal.actor_role,
            actor_binding_id=principal.actor_binding_id,
            subject_id=principal.subject_id,
            occurred_at=occurred_at,
            recorded_at=occurred_at,
            idempotency_key=idempotency_key,
            command_fingerprint=fingerprint,
            note=note,
            authorization_epoch=principal.authorization_epoch,
            previous_state=previous.state,
            resulting_state=resulting.state,
            previous_version=previous.version,
            resulting_version=resulting.version,
        )
        self.events.append(event)
        self.commands[idempotency_key] = event
        self.view = replace(self.view, execution=resulting)
        return CareExecutionCommandResult("applied", self.view, event)


def test_one_grant_is_one_immutable_semantic_care_plan() -> None:
    first = _plan()
    retry = _plan()
    assert first == retry
    assert first.care_plan_id == care_plan_id_for(first.approval_grant_id)
    assert first.valid_until > first.valid_from
    assert first.version == 1


def test_revoked_grant_cannot_create_a_plan() -> None:
    proposal = _proposal()
    grant = CareApprovalGrant.issue(
        proposal,
        approver_actor_id="elder-1",
        approver_role=CareAudience.ELDER,
        approver_binding_id="binding-1",
        authorization_epoch=1,
        idempotency_key="approve",
        issued_at=NOW + timedelta(minutes=1),
    ).revoke()
    with pytest.raises(CareExecutionError, match="usable"):
        CarePlanEntry.create(grant, proposal, source_night_key="2026-08-31")


def test_execution_policy_is_closed_and_action_specific() -> None:
    bounded = execution_policy_for(
        CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME
    )
    assert bounded.execution_mode is CareExecutionMode.BOUNDED_PERIOD
    assert bounded.start_required is True
    assert bounded.direct_complete_allowed is False
    for action in (
        CareActionType.RECOMMEND_MORNING_LIGHT,
        CareActionType.REQUEST_MANUAL_FOLLOW_UP,
        CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
    ):
        assert execution_policy_for(action).direct_complete_allowed is True
    with pytest.raises(CareExecutionError, match="Unsupported"):
        execution_policy_for("send_email")


def test_state_machine_allows_only_governed_transitions() -> None:
    plan = _plan()
    initial = CareExecutionStatus.initial(plan)
    started = initial.transition(
        CareExecutionEventType.STARTED,
        occurred_at=NOW + timedelta(minutes=2),
        direct_complete_allowed=False,
    )
    completed = started.transition(
        CareExecutionEventType.COMPLETED,
        occurred_at=NOW + timedelta(minutes=3),
        direct_complete_allowed=False,
    )
    assert completed.state is CareExecutionState.COMPLETED
    for event in CareExecutionEventType:
        with pytest.raises(CareExecutionError, match="invalid"):
            completed.transition(
                event,
                occurred_at=NOW + timedelta(minutes=4),
                direct_complete_allowed=False,
            )
    assert initial.transition(
        CareExecutionEventType.CANCELLED,
        occurred_at=NOW + timedelta(minutes=2),
        direct_complete_allowed=False,
    ).state is CareExecutionState.CANCELLED
    assert started.transition(
        CareExecutionEventType.CANCELLED,
        occurred_at=NOW + timedelta(minutes=3),
        direct_complete_allowed=False,
    ).state is CareExecutionState.CANCELLED


def test_direct_completion_follows_action_policy_not_cli_order() -> None:
    bounded = CareExecutionStatus.initial(_plan())
    with pytest.raises(CareExecutionError, match="requires START"):
        bounded.transition(
            CareExecutionEventType.COMPLETED,
            occurred_at=NOW + timedelta(minutes=2),
            direct_complete_allowed=False,
        )
    one_time = _plan(CareActionType.RECOMMEND_MORNING_LIGHT)
    direct = CareExecutionStatus.initial(one_time).transition(
        CareExecutionEventType.COMPLETED,
        occurred_at=NOW + timedelta(minutes=2),
        direct_complete_allowed=one_time.direct_complete_allowed,
    )
    assert direct.state is CareExecutionState.COMPLETED


def test_human_events_are_attestation_not_outcome_or_measurement() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    result = service.start(
        _principal(),
        care_plan_id=repository.view.plan.care_plan_id,
        idempotency_key="start-1",
        note="  今天   开始执行  ",
    )
    assert result.event is not None
    assert result.event.source_authority == HUMAN_ATTESTED_AUTHORITY
    assert result.event.note == "今天 开始执行"
    encoded = result.event.model_dump_json()
    for forbidden in ("device_measured", "clinically_confirmed", "effective"):
        assert forbidden not in encoded


def test_human_note_is_optional_bounded_and_sanitized_before_persistence() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    plan_id = repository.view.plan.care_plan_id
    with pytest.raises(CareExecutionError, match="too long"):
        service.start(
            _principal(),
            care_plan_id=plan_id,
            idempotency_key="long-note",
            note="字" * 501,
        )
    with pytest.raises(CareExecutionError, match="control"):
        service.start(
            _principal(),
            care_plan_id=plan_id,
            idempotency_key="control-note",
            note="开始\x00执行",
        )
    result = service.start(
        _principal(),
        care_plan_id=plan_id,
        idempotency_key="no-note",
    )
    assert result.event is not None
    assert result.event.note is None


def test_exact_command_retry_is_idempotent_and_key_reuse_conflicts() -> None:
    repository = _Repository(_plan(CareActionType.RECOMMEND_MORNING_LIGHT))
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    principal = _principal()
    first = service.complete(
        principal,
        care_plan_id=repository.view.plan.care_plan_id,
        idempotency_key="complete-1",
    )
    retry = service.complete(
        principal,
        care_plan_id=repository.view.plan.care_plan_id,
        idempotency_key="complete-1",
    )
    assert first.outcome == "applied"
    assert retry.outcome == "idempotent"
    assert len(repository.events) == 1
    conflict = service.cancel(
        principal,
        care_plan_id=repository.view.plan.care_plan_id,
        idempotency_key="complete-1",
    )
    assert conflict.outcome == "conflict"


def test_wrong_subject_role_scope_and_stale_view_fail_closed() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    no_scope = replace(_principal(), effective_scopes=frozenset())
    with pytest.raises(CareExecutionError, match="missing scope"):
        service.start(
            no_scope,
            care_plan_id=repository.view.plan.care_plan_id,
            idempotency_key="start",
        )
    with pytest.raises(CareExecutionError, match="executor role"):
        service.show(
            _principal(CareAudience.FAMILY),
            care_plan_id=repository.view.plan.care_plan_id,
        )
    with pytest.raises(CareExecutionError, match="subject"):
        service.show(
            replace(_principal(), subject_id="subject-b"),
            care_plan_id=repository.view.plan.care_plan_id,
        )


def test_zh_cn_terminal_rendering_is_deterministic_and_safe() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    parser = build_parser()
    args = parser.parse_args(
        (
            "--actor-id", "elder-1", "--subject-id", "subject-a",
            "--role", "elder", "--authorization-epoch", "3",
            "--privacy-epoch", "2", "--retrieval-policy-epoch", "4",
            "show", repository.view.plan.care_plan_id,
        )
    )
    payload, rendered = execute_command(args, service, _principal())
    assert "照护计划" in rendered
    assert "保持较固定的起床时间" in rendered
    assert "人工确认，不代表医疗效果" in rendered
    assert "ApprovalGrant" not in rendered
    assert "semantic" not in rendered
    assert payload["completion_semantics"] == "human_attested_execution_only"
    assert render_action_zh_cn(repository.view.plan) in rendered


@pytest.mark.parametrize(
    ("action_type", "expected"),
    [
        (
            CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
            "保持较固定的起床时间（前后误差不超过 30 分钟）",
        ),
        (CareActionType.RECOMMEND_MORNING_LIGHT, "早晨接受约 20 分钟自然光照"),
        (CareActionType.REQUEST_MANUAL_FOLLOW_UP, "由家人进行一次人工照护跟进"),
        (
            CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
            "由家人在早晨记录一次睡眠回顾反馈",
        ),
    ],
)
def test_all_closed_actions_have_deterministic_zh_cn_templates(
    action_type: CareActionType,
    expected: str,
) -> None:
    assert render_action_zh_cn(_plan(action_type)) == expected


def test_cli_help_contains_required_commands() -> None:
    help_text = build_parser().format_help()
    for command in ("list", "show", "start", "complete", "cancel", "history"):
        assert command in help_text


def _cli_arguments(*command: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(
        (
            "--actor-id", "elder-1", "--subject-id", "subject-a",
            "--role", "elder", "--authorization-epoch", "3",
            "--privacy-epoch", "2", "--retrieval-policy-epoch", "4",
            *command,
        )
    )


def test_cli_list_start_show_history_complete_and_cancel_paths() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    plan_id = repository.view.plan.care_plan_id
    listed, listed_text = execute_command(
        _cli_arguments("list"), service, _principal()
    )
    assert [item["care_plan_id"] for item in listed["items"]] == [plan_id]
    assert "照护计划" in listed_text
    started, _ = execute_command(
        _cli_arguments(
            "start", plan_id, "--idempotency-key", "start-cli",
            "--note", "今天开始",
        ),
        service,
        _principal(),
    )
    assert started["care_plan"]["state"] == "in_progress"
    shown, _ = execute_command(
        _cli_arguments("show", plan_id), service, _principal()
    )
    assert shown["state"] == "in_progress"
    history, history_text = execute_command(
        _cli_arguments("history", plan_id), service, _principal()
    )
    assert [item["event_type"] for item in history["events"]] == ["started"]
    assert "人工确认" in history_text
    completed, _ = execute_command(
        _cli_arguments(
            "complete", plan_id, "--idempotency-key", "complete-cli"
        ),
        service,
        _principal(),
    )
    assert completed["care_plan"]["state"] == "completed"

    cancel_repository = _Repository(_plan())
    cancelled, _ = execute_command(
        _cli_arguments(
            "cancel",
            cancel_repository.view.plan.care_plan_id,
            "--idempotency-key",
            "cancel-cli",
        ),
        CarePlanApplicationService(
            cancel_repository, now_factory=lambda: NOW + timedelta(minutes=2)
        ),
        _principal(),
    )
    assert cancelled["care_plan"]["state"] == "cancelled"


def test_cli_nonexistent_expired_and_terminal_plans_fail_or_render_safely() -> None:
    repository = _Repository(_plan())
    service = CarePlanApplicationService(
        repository, now_factory=lambda: NOW + timedelta(minutes=2)
    )
    with pytest.raises(CareExecutionError, match="not found"):
        execute_command(
            _cli_arguments("show", "care-plan:missing"), service, _principal()
        )
    with pytest.raises(CareExecutionError, match="subject"):
        execute_command(
            _cli_arguments("show", repository.view.plan.care_plan_id),
            service,
            replace(_principal(), subject_id="unauthorized-subject"),
        )
    expired_status = repository.view.execution.model_copy(
        update={
            "state": CareExecutionState.EXPIRED,
            "invalidated_at": NOW + timedelta(days=2),
            "invalidation_reason": "execution_window_expired",
        }
    )
    repository.view = replace(
        repository.view, execution=expired_status, executable=False
    )
    payload, rendered = execute_command(
        _cli_arguments("show", repository.view.plan.care_plan_id),
        service,
        _principal(),
    )
    assert payload["state"] == "expired"
    assert "可执行操作：\n无" in rendered
    for internal in ("semantic_hash", "authorization_epoch", "prompt"):
        assert internal not in rendered


def test_migration_021_pins_database_authority_and_isolation() -> None:
    sql = Path(
        "sleepagent/persistence/migrations/021_terminal_care_execution.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "backend_care_plans_v1",
        "backend_care_execution_states_v1",
        "backend_care_execution_events_v1",
    ):
        assert f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "approval_grant_id TEXT NOT NULL UNIQUE" in sql
    assert "source_authority = 'human_attested'" in sql
    assert "UNIQUE (care_plan_id, idempotency_key)" in sql
    assert "SECURITY DEFINER" in sql
    assert "REVOKE ALL ON FUNCTION public.sleepagent_execute_care_plan_v1" in sql
    assert "INSERT INTO public.backend_delivery_intents" not in sql
    assert "INSERT INTO public.backend_delivery_journal" not in sql
    assert "INSERT INTO public.backend_governed_memory_revisions" not in sql
    assert "INSERT INTO public.backend_habit_profile_revisions" not in sql
    assert "CREATE TABLE public.backend_care_outcome" not in sql
