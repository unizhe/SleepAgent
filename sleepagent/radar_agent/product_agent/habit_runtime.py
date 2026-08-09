from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitProfileCandidateBuilder,
    HabitProfileChangeSet,
    HabitProfileStore,
    ObjectiveBaselineStore,
)
from sleepagent.radar_agent.product_agent.contracts import OnlineReasoningEvent
from sleepagent.radar_agent.product_agent.online_reasoning import (
    resolve_event_context,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductToolExecutionContext,
    ToolHandler,
)
from sleepagent.radar_agent.questionnaire import (
    DEFAULT_HABIT_CONCEPTS,
    HabitQuestionAnswer,
    HabitQuestionSelectionReceipt,
    HabitQuestionSelectionRequest,
    HabitQuestionnaireService,
)


HABIT_RUNTIME_VERSION = "sleepagent-habit-runtime.v3"


class HabitProfileRuntimeService:
    """Deterministic adapters that embed Habit capabilities in 1+2+1."""

    def __init__(
        self,
        *,
        questionnaire: HabitQuestionnaireService,
        store: HabitProfileStore,
        baseline_store: ObjectiveBaselineStore,
    ) -> None:
        self.questionnaire = questionnaire
        self.store = store
        self.baseline_store = baseline_store
        self.candidate_builder = HabitProfileCandidateBuilder(
            DEFAULT_HABIT_CONCEPTS
        )

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            "questionnaire.select_profile": self.select,
            "questionnaire.capture_profile": self.capture,
            "profile.read": self.read,
            "baseline.read": self.read_baseline,
            "reasoning.resolve_event_context": self.resolve_event,
            "profile.build_change_set": self.build_change_set,
        }

    def select(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        request = HabitQuestionSelectionRequest.model_validate(arguments["request"])
        self._require_binding(
            context,
            subject_id=request.subject_id,
            actor_id=request.actor_id,
            role=request.role,
        )
        expected_plan = (
            context.episode_id,
            context.plan_id,
            context.plan_revision,
        )
        actual_plan = (
            request.episode_id,
            request.plan_id,
            request.plan_revision,
        )
        if expected_plan != actual_plan:
            raise ValueError("Habit selection is not bound to the active plan")
        if request.plan_step_id not in context.allowed_plan_step_ids:
            raise ValueError("Habit selection plan step is not active")
        receipt = self.questionnaire.select(
            request,
            now=_optional_now(arguments),
        )
        return {
            "selection": receipt.model_dump(mode="json"),
            "source_refs": [receipt.selection_id],
        }

    def capture(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        receipt = HabitQuestionSelectionReceipt.model_validate(
            arguments["selection"]
        )
        self._require_binding(
            context,
            subject_id=receipt.subject_id,
            actor_id=receipt.actor_id,
            role=receipt.role,
        )
        if context.episode_id != receipt.episode_id:
            raise ValueError("Habit selection cannot be replayed across Episodes")
        capture = self.questionnaire.capture(
            receipt,
            (
                HabitQuestionAnswer.model_validate(item)
                for item in arguments.get("answers", ())
            ),
            episode_id=receipt.episode_id,
            subject_id=receipt.subject_id,
            actor_id=receipt.actor_id,
            role=receipt.role,
            now=_optional_now(arguments),
        )
        refs = [item.answer_ref for item in capture.answers]
        refs.extend(item.event_id for item in capture.safety_events)
        return {
            "capture": capture.model_dump(mode="json"),
            "source_refs": refs,
        }

    def read(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        binding = context.fact_snapshot.binding
        result = self.store.read(
            subject_id=binding.subject_id,
            actor_id=binding.actor_id,
            role=_human_role(binding.role),
            authorization_scope=context.authorization_scope,
            purpose=arguments["purpose"],
            requested_concept_ids=arguments.get("requested_concept_ids", ()),
            include_stale_for_review=bool(
                arguments.get("include_stale_for_review", False)
            ),
            now=_optional_now(arguments),
        )
        if result.memory_version != context.fact_snapshot.memory_context_version:
            raise ValueError("Habit Profile read is stale against FactSnapshot")
        return {
            "profile": result.model_dump(mode="json"),
            "requested_concept_ids": list(
                dict.fromkeys(arguments.get("requested_concept_ids", ()))
            ),
            "source_refs": list(result.source_refs),
        }

    def read_baseline(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        artifacts = self.baseline_store.read(
            subject_id=context.fact_snapshot.binding.subject_id,
            metric_ids=arguments.get("metric_ids", ()),
        )
        return {
            "baselines": [
                artifact.model_dump(mode="json") for artifact in artifacts
            ],
            "missing_metric_ids": [
                metric_id
                for metric_id in arguments.get("metric_ids", ())
                if metric_id
                not in {artifact.metric_id for artifact in artifacts}
            ],
            "source_refs": [
                ref
                for artifact in artifacts
                for ref in (artifact.artifact_id, *artifact.source_refs)
            ],
        }

    def resolve_event(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        event = OnlineReasoningEvent.model_validate(arguments["event"])
        resolution = resolve_event_context(event)
        return {
            "resolution": resolution.model_dump(mode="json"),
            "source_refs": list(resolution.source_refs),
        }

    def build_change_set(
        self,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
    ) -> dict[str, Any]:
        from sleepagent.radar_agent.questionnaire import CapturedHabitAnswer

        binding = context.fact_snapshot.binding
        answers = tuple(
            CapturedHabitAnswer.model_validate(item)
            for item in arguments.get("captured_answers", ())
        )
        if not 1 <= len(answers) <= 3:
            raise ValueError("Habit Profile change set requires 1-3 answers")
        for answer in answers:
            if answer.subject_id != binding.subject_id:
                raise PermissionError("Habit candidate crosses subject")
            if binding.role != "elder":
                raise PermissionError(
                    "only the elder review Episode may build a Profile change set"
                )
            if (
                answer.role == "elder"
                and answer.actor_id != binding.actor_id
            ):
                raise PermissionError("elder Habit answer actor mismatch")
        created_at = _optional_now(arguments) or datetime.now(timezone.utc)
        for answer in answers:
            self.questionnaire.verify_captured_answer(answer, now=created_at)
        candidates = tuple(
            self.candidate_builder.from_captured(answer) for answer in answers
        )
        change_set = HabitProfileChangeSet.create(
            change_set_id=arguments["change_set_id"],
            version=int(arguments.get("version", 1)),
            subject_id=binding.subject_id,
            candidates=candidates,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            expected_memory_version=context.fact_snapshot.memory_context_version,
            confirmation_expires_at=created_at + timedelta(minutes=30),
            created_at=created_at,
        )
        return {
            "change_set": change_set.model_dump(mode="json"),
            "confirmation_summary": [
                {
                    "candidate_id": item.candidate_id,
                    "concept_id": item.concept_id,
                    "operation": item.operation.value,
                    "value": item.value,
                    "origin_semantic": item.origin_semantic,
                }
                for item in candidates
            ],
            "source_refs": [
                item.source_answer_ref
                for item in candidates
                if item.source_answer_ref
            ],
        }

    @staticmethod
    def _require_binding(
        context: ProductToolExecutionContext,
        *,
        subject_id: str,
        actor_id: str,
        role: str | None = None,
        require_actor: bool = True,
    ) -> None:
        binding = context.fact_snapshot.binding
        if subject_id != binding.subject_id:
            raise PermissionError("Habit capability crosses authenticated subject")
        if require_actor and actor_id != binding.actor_id:
            raise PermissionError("Habit capability crosses authenticated actor")
        if role is not None and role != binding.role:
            raise PermissionError("Habit capability crosses authenticated role")


def _optional_now(arguments: dict[str, Any]) -> datetime | None:
    value = arguments.get("_now")
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _human_role(role: str) -> str:
    if role not in {"elder", "family", "doctor", "system"}:
        raise PermissionError("unsupported Habit Profile reader role")
    return role


__all__ = ["HABIT_RUNTIME_VERSION", "HabitProfileRuntimeService"]
