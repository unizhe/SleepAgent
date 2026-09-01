"""Terminal-first zh-CN interface for authorized human care execution."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sleepagent.api.postgres import PostgresAuthorityStore
from sleepagent.api.product_contracts import ProductRole
from sleepagent.application.care_execution import (
    CareExecutionPrincipal,
    CarePlanApplicationService,
    CarePlanFilter,
    CarePlanView,
)
from sleepagent.application.care_outcomes import (
    CareOutcomeReadService,
    CareOutcomeView,
)
from sleepagent.config import ProcessRole, SleepBackendSettings
from sleepagent.domain.care_actions import CareAudience
from sleepagent.domain.care_execution import (
    CareExecutionError,
    CareExecutionEvent,
    CareExecutionState,
    execution_state_zh_cn,
    render_action_zh_cn,
)
from sleepagent.infrastructure.postgres_care_execution import (
    PostgresCarePlanRepository,
)
from sleepagent.infrastructure.postgres_care_outcomes import (
    PostgresCareOutcomeReadRepository,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sleepagent-care",
        description="查看照护计划并记录经授权人工执行情况。",
    )
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--role", choices=("elder", "family"), required=True)
    parser.add_argument("--authorization-epoch", type=int, required=True)
    parser.add_argument("--privacy-epoch", type=int, required=True)
    parser.add_argument("--retrieval-policy-epoch", type=int, required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="列出当前主体的照护计划")
    listing.add_argument(
        "--state",
        choices=tuple(item.value for item in CarePlanFilter),
        default=None,
    )
    listing.add_argument("--limit", type=_limit, default=50)
    _output_arguments(listing)

    show = commands.add_parser("show", help="显示一个照护计划")
    show.add_argument("care_plan_id")
    _output_arguments(show)

    history = commands.add_parser("history", help="显示不可变执行历史")
    history.add_argument("care_plan_id")
    _output_arguments(history)

    outcome = commands.add_parser("outcome", help="显示一个照护计划的观察结果")
    outcome.add_argument("care_plan_id")
    _output_arguments(outcome)

    outcomes = commands.add_parser("outcomes", help="列出照护计划观察结果")
    outcomes.add_argument("--limit", type=_limit, default=50)
    _output_arguments(outcomes)

    for command_name in ("start", "complete", "cancel"):
        command = commands.add_parser(command_name)
        command.add_argument("care_plan_id")
        command.add_argument("--idempotency-key", required=True)
        command.add_argument("--note")
        command.add_argument("--occurred-at", type=_instant)
        _output_arguments(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    pool: PsycopgPoolProvider[Any] | None = None
    try:
        settings = SleepBackendSettings.from_environment()
        if settings.process_role is not ProcessRole.API:
            raise CareExecutionError("Care CLI requires an API capability profile")
        pool = PsycopgPoolProvider.from_dsn(
            settings.database_dsn.get_secret_value(),
            configuration=PoolConfiguration(
                min_size=settings.pool_min_size,
                max_size=settings.pool_max_size,
                open_timeout_seconds=settings.pool_timeout_seconds,
            ),
            application_name="sleepagent-terminal-care-cli",
        )
        factory = UnitOfWorkFactory(
            pool,
            lock_timeout_ms=settings.lock_timeout_ms,
            statement_timeout_ms=settings.statement_timeout_ms,
            idle_in_transaction_timeout_ms=settings.idle_transaction_timeout_ms,
        )
        pool.open()
        resolved = PostgresAuthorityStore(settings, factory).resolve(
            actor_id=arguments.actor_id,
            subject_id=arguments.subject_id,
            role=ProductRole(arguments.role),
            purpose="sleep_care",
        )
        asserted_epochs = (
            arguments.authorization_epoch,
            arguments.privacy_epoch,
            arguments.retrieval_policy_epoch,
        )
        current_epochs = (
            resolved.authorization_epoch,
            resolved.privacy_epoch,
            resolved.retrieval_policy_epoch,
        )
        if asserted_epochs != current_epochs:
            raise CareExecutionError("Care CLI authority epochs are stale")
        principal = CareExecutionPrincipal(
            service_principal_id=settings.service_principal_id,
            actor_id=arguments.actor_id,
            actor_role=CareAudience(resolved.role.value),
            actor_binding_id=resolved.binding_id,
            subject_id=arguments.subject_id,
            effective_scopes=resolved.effective_scopes,
            authorization_epoch=resolved.authorization_epoch,
            privacy_epoch=resolved.privacy_epoch,
            retrieval_policy_epoch=resolved.retrieval_policy_epoch,
            namespace_id=resolved.namespace_id,
            namespace_generation=resolved.namespace_generation,
            data_mode=resolved.data_mode,
            run_id=resolved.run_id,
            arm_id=resolved.arm_id,
        )
        service = CarePlanApplicationService(PostgresCarePlanRepository(factory))
        outcome_service = CareOutcomeReadService(
            PostgresCareOutcomeReadRepository(factory)
        )
        payload, text = execute_command(
            arguments, service, principal, outcome_service=outcome_service
        )
        print(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if arguments.json
            else text
        )
        return 0
    except CareExecutionError as exc:
        print(f"sleepagent-care: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"sleepagent-care: configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("sleepagent-care: interrupted", file=sys.stderr)
        return 130
    finally:
        if pool is not None:
            pool.close()


def execute_command(
    arguments: argparse.Namespace,
    service: CarePlanApplicationService,
    principal: CareExecutionPrincipal,
    *,
    outcome_service: CareOutcomeReadService | None = None,
) -> tuple[dict[str, Any], str]:
    if arguments.command in {"outcome", "outcomes"}:
        if outcome_service is None:
            raise CareExecutionError("Care outcome read service is unavailable")
        if arguments.command == "outcome":
            view = outcome_service.outcome(
                principal, care_plan_id=arguments.care_plan_id
            )
            return (
                _outcome_payload(view, trace=arguments.trace),
                _render_outcome(view),
            )
        views = outcome_service.outcomes(principal, limit=arguments.limit)
        return (
            {
                "schema_version": "terminal_care_outcome_list.v1",
                "items": [
                    _outcome_payload(item, trace=arguments.trace) for item in views
                ],
            },
            _render_outcome_list(views),
        )
    if arguments.command == "list":
        views = service.list(
            principal,
            state=(
                None
                if arguments.state is None
                else CarePlanFilter(arguments.state)
            ),
            limit=arguments.limit,
        )
        return (
            {
                "schema_version": "terminal_care_plan_list.v1",
                "items": [_view_payload(item, trace=arguments.trace) for item in views],
            },
            _render_list(views),
        )
    if arguments.command == "show":
        view = service.show(principal, care_plan_id=arguments.care_plan_id)
        return _view_payload(view, trace=arguments.trace), _render_plan(view)
    if arguments.command == "history":
        view = service.show(principal, care_plan_id=arguments.care_plan_id)
        events = service.history(principal, care_plan_id=arguments.care_plan_id)
        return (
            {
                "schema_version": "terminal_care_history.v1",
                "care_plan": _view_payload(view, trace=arguments.trace),
                "events": [_event_payload(item, trace=arguments.trace) for item in events],
            },
            _render_history(view, events),
        )
    method = {
        "start": service.start,
        "complete": service.complete,
        "cancel": service.cancel,
    }.get(arguments.command)
    if method is None:
        raise CareExecutionError("Unsupported care command")
    result = method(
        principal,
        care_plan_id=arguments.care_plan_id,
        idempotency_key=arguments.idempotency_key,
        note=arguments.note,
        occurred_at=arguments.occurred_at,
    )
    payload = {
        "schema_version": "terminal_care_command_result.v1",
        "outcome": result.outcome,
        "care_plan": _view_payload(result.view, trace=arguments.trace),
        "event": (
            None
            if result.event is None
            else _event_payload(result.event, trace=arguments.trace)
        ),
    }
    text = (
        f"操作结果：{_outcome_zh_cn(result.outcome)}\n\n"
        + _render_plan(result.view)
    )
    return payload, text


def _render_list(views: tuple[CarePlanView, ...]) -> str:
    if not views:
        return "照护计划\n────────────────────────────\n当前没有符合条件的计划。"
    blocks = ["照护计划", "────────────────────────────"]
    for index, view in enumerate(views, start=1):
        blocks.extend(
            (
                f"{index}. {render_action_zh_cn(view.plan)}",
                f"   计划编号：{view.plan.care_plan_id}",
                f"   状态：{execution_state_zh_cn(view.execution.state)}",
                "   执行窗口："
                f"{_date(view.plan.valid_from)} ～ {_date(view.plan.valid_until)}",
            )
        )
    return "\n".join(blocks)


def _render_plan(view: CarePlanView) -> str:
    plan = view.plan
    actions = _allowed_actions(view)
    authority = "有效" if view.executable else "当前不可执行"
    return "\n".join(
        (
            "照护计划",
            "────────────────────────────",
            "",
            "计划：",
            render_action_zh_cn(plan),
            "",
            "原因：",
            "来自已确认的睡眠分析与照护建议。",
            "",
            "状态：",
            execution_state_zh_cn(view.execution.state),
            "",
            "执行授权：",
            authority,
            "",
            "执行窗口：",
            f"{_date(plan.valid_from)} ～ {_date(plan.valid_until)}",
            "",
            "来源：",
            f"{plan.source_night_key} 睡眠分析",
            "",
            "计划编号：",
            plan.care_plan_id,
            "",
            "可执行操作：",
            actions,
            "",
            "说明：完成表示授权人员的人工确认，不代表医疗效果。",
        )
    )


def _render_history(
    view: CarePlanView, events: tuple[CareExecutionEvent, ...]
) -> str:
    lines = [
        "照护执行记录",
        "────────────────────────────",
        f"计划：{render_action_zh_cn(view.plan)}",
        f"当前状态：{execution_state_zh_cn(view.execution.state)}",
        "",
    ]
    if not events:
        lines.append("尚无人工执行记录。")
    for event in events:
        label = {
            "started": "开始",
            "completed": "完成",
            "cancelled": "取消",
        }[event.event_type.value]
        lines.append(
            f"{_instant_text(event.occurred_at)}　{label}（人工确认）"
        )
        if event.note:
            lines.append(f"备注：{event.note}")
    lines.extend(("", "这些记录是人工陈述，不是设备测量或医疗效果证明。"))
    return "\n".join(lines)


def _render_outcome_list(views: tuple[CareOutcomeView, ...]) -> str:
    if not views:
        return "照护观察结果\n────────────────────────────\n当前没有已完成的照护计划。"
    lines = ["照护观察结果", "────────────────────────────"]
    for index, view in enumerate(views, start=1):
        lines.extend(
            (
                f"{index}. {render_action_zh_cn(view.plan)}",
                f"   计划编号：{view.plan.care_plan_id}",
                f"   状态：{_outcome_state_zh_cn(view)}",
            )
        )
    return "\n".join(lines)


def _render_outcome(view: CareOutcomeView) -> str:
    lines = [
        "照护观察结果",
        "────────────────────────────",
        "",
        "照护计划：",
        render_action_zh_cn(view.plan),
        "",
        "执行状态：",
        execution_state_zh_cn(CareExecutionState(view.execution_state)),
        "",
        "执行时间：",
        "未记录" if view.completed_at is None else _instant_text(view.completed_at),
        "",
        "观察窗口：",
        _outcome_window(view),
        "",
        "状态：",
        _outcome_state_zh_cn(view),
    ]
    outcome = view.outcome
    if outcome is None:
        lines.extend(("", "说明：", _bounded_reason_zh_cn(view.reason_code)))
        return "\n".join(lines)
    comparison = outcome.comparison_facts[0] if outcome.comparison_facts else None
    lines.extend(
        (
            "",
            "执行前：",
            "无可比较数值" if comparison is None else _comparison_value(comparison.baseline_value, comparison.unit),
            "",
            "执行后：",
            "无可比较数值" if comparison is None else _comparison_value(comparison.followup_value, comparison.unit),
            "",
            "观察结果：",
            _category_zh_cn(outcome.outcome_category.value, comparison),
            "",
            "证据质量：",
            _quality_zh_cn(outcome.evidence_quality.value),
            "",
            "说明：",
            "；".join(outcome.caveats),
        )
    )
    receipt = view.personalization_receipt
    if receipt is not None:
        lines.extend(
            (
                "",
                "个性化候选：",
                _receipt_state_zh_cn(receipt.state.value),
            )
        )
    return "\n".join(lines)


def _outcome_payload(view: CareOutcomeView, *, trace: bool) -> dict[str, Any]:
    outcome = view.outcome
    receipt = view.personalization_receipt
    payload: dict[str, Any] = {
        "schema_version": "terminal_care_outcome_view.v1",
        "care_plan_id": view.plan.care_plan_id,
        "action": render_action_zh_cn(view.plan),
        "execution_state": view.execution_state,
        "completed_at": None if view.completed_at is None else view.completed_at.isoformat(),
        "observation_window_start": None if view.observation_window_start is None else view.observation_window_start.isoformat(),
        "observation_window_end": None if view.observation_window_end is None else view.observation_window_end.isoformat(),
        "lifecycle_state": view.lifecycle_state.value,
        "reason_code": view.reason_code,
        "outcome_category": None if outcome is None else outcome.outcome_category.value,
        "evidence_quality": None if outcome is None else outcome.evidence_quality.value,
        "causal_claim": False,
        "personalization_state": None if receipt is None else receipt.state.value,
    }
    if trace and outcome is not None:
        payload["trace"] = {
            "baseline_revision_ids": list(outcome.baseline_revision_ids),
            "baseline_episode_revision_ids": list(
                outcome.baseline_episode_revision_ids
            ),
            "followup_revision_ids": list(outcome.followup_revision_ids),
            "followup_episode_revision_ids": list(
                outcome.followup_episode_revision_ids
            ),
            "evaluation_policy_version": outcome.policy_version,
            "evaluation_policy_hash": outcome.policy_hash,
            "outcome_hash": outcome.semantic_hash,
            "personalization_receipt_id": (
                None if receipt is None else receipt.receipt_id
            ),
        }
    return payload


def _outcome_state_zh_cn(view: CareOutcomeView) -> str:
    return {
        "waiting_for_followup": "等待后续睡眠数据",
        "ready_for_evaluation": "后续数据已就绪，等待评估",
        "evaluated": "已完成执行前后观察",
        "insufficient_data": "数据不足，暂无法评估",
        "not_comparable": "指标语义不可比较，暂无法评估",
        "superseded": "该观察结果已被新版本取代",
    }[view.lifecycle_state.value]


def _outcome_window(view: CareOutcomeView) -> str:
    if view.observation_window_start is None or view.observation_window_end is None:
        return "尚未登记"
    return f"{_date(view.observation_window_start)} ～ {_date(view.observation_window_end)}"


def _bounded_reason_zh_cn(reason: str | None) -> str:
    return {
        None: "尚未形成观察结果。",
        "eligible_hard_finalized_followup_not_available": "后续夜晚尚未 HARD_FINALIZED。",
        "execution_only_ready": "人工执行记录正在形成完成性观察。",
        "execution_not_eligible": "该执行记录不符合评估条件。",
    }.get(reason, "当前证据尚不足以形成观察结果。")


def _category_zh_cn(category: str, comparison: Any) -> str:
    if comparison is not None:
        return str(comparison.interpretation)
    return {
        "execution_only": "已记录人工确认的任务完成。",
        "insufficient_data": "数据不足，暂无法评估。",
        "not_comparable": "当前指标语义不可比较。",
    }.get(category, "已形成非因果的执行前后观察。")


def _quality_zh_cn(value: str) -> str:
    return {"high": "高", "moderate": "中等", "limited": "有限", "insufficient": "不足"}[value]


def _receipt_state_zh_cn(value: str) -> str:
    return {
        "no_personalization_change": "未提出个性化变更",
        "candidate_proposed": "已提出候选，等待现有治理流程确认",
        "candidate_accepted": "候选已由现有治理流程接受",
        "candidate_rejected": "候选已由现有治理流程拒绝",
        "superseded": "候选已被新证据取代",
    }[value]


def _comparison_value(value: float, unit: str) -> str:
    return f"{value:g} {unit}"


def _view_payload(view: CarePlanView, *, trace: bool) -> dict[str, Any]:
    plan = view.plan
    payload: dict[str, Any] = {
        "schema_version": "terminal_care_plan_view.v1",
        "care_plan_id": plan.care_plan_id,
        "action": render_action_zh_cn(plan),
        "action_type": plan.action_type.value,
        "state": view.execution.state.value,
        "state_display": execution_state_zh_cn(view.execution.state),
        "execution_version": view.execution.version,
        "executable": view.executable,
        "valid_from": plan.valid_from.isoformat(),
        "valid_until": plan.valid_until.isoformat(),
        "source_night": plan.source_night_key,
        "source_authority": "approved_care_plan",
        "completion_semantics": "human_attested_execution_only",
    }
    if trace:
        payload["trace"] = {
            "approval_grant_id": plan.approval_grant_id,
            "proposal_id": plan.proposal_id,
            "source_analysis_revision_id": plan.source_analysis_revision_id,
            "care_plan_semantic_hash": plan.care_plan_semantic_hash,
            "execution_policy_version": plan.execution_policy_version,
            "rendering_version": plan.rendering_version,
        }
    return payload


def _event_payload(event: CareExecutionEvent, *, trace: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": event.event_type.value,
        "source_authority": event.source_authority,
        "occurred_at": event.occurred_at.isoformat(),
        "recorded_at": event.recorded_at.isoformat(),
        "previous_state": event.previous_state.value,
        "resulting_state": event.resulting_state.value,
        "note": event.note,
    }
    if trace:
        payload["trace"] = {
            "event_id": event.event_id,
            "actor_binding_id": event.actor_binding_id,
            "authorization_epoch": event.authorization_epoch,
            "command_fingerprint": event.command_fingerprint,
        }
    return payload


def _allowed_actions(view: CarePlanView) -> str:
    if not view.executable:
        return "无"
    if view.execution.state is CareExecutionState.NOT_STARTED:
        actions = ["start", "cancel"]
        if view.plan.direct_complete_allowed:
            actions.insert(1, "complete")
        return " / ".join(actions)
    if view.execution.state is CareExecutionState.IN_PROGRESS:
        return "complete / cancel"
    return "无"


def _outcome_zh_cn(outcome: str) -> str:
    return {
        "applied": "已记录",
        "idempotent": "已记录（重复请求未新增记录）",
        "conflict": "状态冲突，未更改",
        "expired": "计划已过期，未更改",
        "invalidated": "执行授权已失效，未更改",
        "superseded": "计划已被新分析取代，未更改",
    }.get(outcome, "未完成")


def _output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trace", action="store_true")


def _instant(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("时间必须为 ISO-8601") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError("时间必须包含时区偏移")
    return value


def _limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit 必须是整数") from exc
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("limit 必须在 1 到 100 之间")
    return value


def _date(value: datetime) -> str:
    return value.date().isoformat()


def _instant_text(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "execute_command", "main"]
