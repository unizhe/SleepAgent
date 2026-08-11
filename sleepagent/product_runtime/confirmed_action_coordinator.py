from __future__ import annotations

from typing import Callable, Literal, Protocol, cast

from sleepagent.product_runtime.contracts import (
    AgentId,
    CareStrategy,
    EpisodeStatus,
    InvocationOutcome,
    ToolReceipt,
    stable_hash,
)
from sleepagent.product_runtime.governance import (
    AcceptanceError,
    DeterministicCommitController,
)
from sleepagent.product_runtime.hitl import (
    ApprovalGrant,
    HumanDecisionError,
    HumanDecisionRequest,
    HumanDecisionService,
    HumanDecisionStatus,
)
from sleepagent.product_runtime.registry import product_agent_manifest
from sleepagent.product_runtime.runtime_contracts import (
    CommitFrozenConfirmedAction,
    ConfirmedActionOutcome,
    PendingConfirmationTarget,
    ProductEpisodeRunRequest,
)
from sleepagent.product_runtime.runtime_ports import (
    ExternalActionExecutor,
)


CONFIRMED_ACTION_COORDINATOR_VERSION = (
    "sleepagent-confirmed-action-coordinator.v1"
)


class VerifiedApprovalCapabilityPort(Protocol):
    """Static view of HDS's dynamically sealed in-process capability."""

    @property
    def grant(self) -> ApprovalGrant: ...


class ConfirmedActionCoordinator:
    """Execute an exact HDS-authorized commit against a frozen result.

    This boundary deliberately has no Agent, model, publication, or result-store
    dependency.  Its only public input is the validated frozen continuation
    command, so callers cannot inject a raw approval capability or reconstruct a
    target from new reasoning output.
    """

    def __init__(
        self,
        *,
        commit_controller: DeterministicCommitController,
        human_decisions: HumanDecisionService,
        external_executor: ExternalActionExecutor,
    ) -> None:
        self.commit_controller = commit_controller
        self.human_decisions = human_decisions
        self.external_executor = external_executor

    def commit(
        self,
        command: CommitFrozenConfirmedAction,
    ) -> ConfirmedActionOutcome:
        """Commit the frozen targets without persisting or publishing a result."""

        request = command.request
        frozen_result = command.frozen_result
        if frozen_result.receipt.status != EpisodeStatus.WAITING_CONFIRMATION:
            raise AcceptanceError("frozen Episode is not waiting for confirmation")
        if frozen_result.receipt.episode_id != request.episode_id:
            raise AcceptanceError("frozen Episode ID mismatch")
        if (
            frozen_result.receipt.fact_snapshot_hash
            != request.fact_snapshot.fact_snapshot_hash
        ):
            raise AcceptanceError("frozen FactSnapshot mismatch")
        current_registry_hash = stable_hash(product_agent_manifest())
        if frozen_result.registry_hash != current_registry_hash:
            raise AcceptanceError("registry changed; frozen Episode requires replan")

        pending_by_id = {
            item.confirmation_id: item
            for item in frozen_result.pending_confirmations
        }
        if len(pending_by_id) != len(frozen_result.pending_confirmations):
            raise AcceptanceError("frozen confirmations contain duplicate IDs")

        decision_by_confirmation: dict[str, HumanDecisionRequest] = {}
        declined_confirmation_ids: set[str] = set()
        declined_states = {
            HumanDecisionStatus.REJECTED,
            HumanDecisionStatus.EXPIRED,
            HumanDecisionStatus.REVOKED,
            HumanDecisionStatus.SUPERSEDED,
            HumanDecisionStatus.HARD_BLOCKED,
        }
        executable_states = {
            HumanDecisionStatus.APPROVED,
            HumanDecisionStatus.EXECUTING,
            HumanDecisionStatus.COMMITTED,
            HumanDecisionStatus.EXECUTION_FAILED,
            HumanDecisionStatus.OUTCOME_UNKNOWN,
        }
        decision_ids = [target.decision_id for target in pending_by_id.values()]
        proposal_ids = [target.proposal_id for target in pending_by_id.values()]
        if any(item is None for item in (*decision_ids, *proposal_ids)):
            raise AcceptanceError(
                "frozen confirmation is missing its explicit authority binding"
            )
        if len(set(decision_ids)) != len(decision_ids) or len(
            set(proposal_ids)
        ) != len(proposal_ids):
            raise AcceptanceError("frozen confirmations reuse an authority binding")
        for confirmation_id, target in pending_by_id.items():
            assert target.decision_id is not None
            assert target.proposal_id is not None
            try:
                decision = self.human_decisions.get(target.decision_id)
            except KeyError as exc:
                raise AcceptanceError(
                    "frozen confirmation authority decision does not exist"
                ) from exc
            proposal = decision.proposal
            if (
                proposal.proposal_id != target.proposal_id
                or proposal.episode_id != request.episode_id
                or proposal.subject_id != target.subject_id
                or proposal.subject_id
                != request.fact_snapshot.binding.subject_id
                or proposal.proposer_actor_id != target.actor_id
                or proposal.target_id != target.candidate_id
                or proposal.target_hash != target.candidate_hash
                or proposal.action_scope != target.action_scope
                or proposal.fact_snapshot_id
                != request.fact_snapshot.fact_snapshot_id
                or proposal.fact_snapshot_hash
                != request.fact_snapshot.fact_snapshot_hash
                or proposal.expires_at != target.expires_at
            ):
                raise AcceptanceError(
                    "frozen confirmation authority binding does not match its target"
                )
            decision = self.human_decisions.expire(target.decision_id)
            if decision.status in declined_states:
                declined_confirmation_ids.add(confirmation_id)
            elif decision.status not in executable_states:
                raise AcceptanceError(
                    "frozen confirmations remain unresolved: "
                    f"{confirmation_id} is {decision.status.value}"
                )
            decision_by_confirmation[confirmation_id] = decision

        additional_tool_receipts: list[ToolReceipt] = []
        committed_memory_ids: list[str] = []
        committed_care_id: str | None = None
        committed_habit_id: str | None = None
        external_receipt_id: str | None = None
        external_delivery_status: Literal["pending", "delivered"] | None = None
        publication = frozen_result.publication
        memory_version = request.fact_snapshot.memory_context_version + len(
            frozen_result.committed_memory_candidate_ids
        )

        if publication is not None:
            candidates = {
                item.candidate_id: item
                for item in publication.memory_change_candidates
            }
            for confirmation_id, target in pending_by_id.items():
                if (
                    target.target_kind != "memory"
                    or confirmation_id in declined_confirmation_ids
                ):
                    continue
                candidate = candidates.get(target.candidate_id)
                if (
                    candidate is None
                    or str(candidate.candidate_hash) != target.candidate_hash
                ):
                    raise AcceptanceError("frozen Memory target drift")
                idempotency_key = (
                    f"{request.idempotency_key or request.episode_id}:"
                    f"memory:{candidate.candidate_id}:{candidate.candidate_version}"
                )
                capability = self._acquire_confirmation_capability(
                    decision_by_confirmation[confirmation_id],
                    target=target,
                    request=request,
                    idempotency_key=idempotency_key,
                )
                commit = self._execute_confirmed_commit(
                    capability,
                    lambda: self.commit_controller.commit_memory(
                        candidate=candidate,
                        expected_version=memory_version,
                        fact_snapshot=request.fact_snapshot,
                        idempotency_key=idempotency_key,
                        approval_capability=capability,
                    ),
                )
                if commit.outcome != InvocationOutcome.SUCCEEDED:
                    raise AcceptanceError("Memory commit returned unknown outcome")
                additional_tool_receipts.append(commit)
                committed_memory_ids.append(candidate.candidate_id)
                memory_version += 1

        care_products = [
            item
            for item in frozen_result.accepted_work_products
            if item.agent_id == AgentId.CARE_STRATEGY
        ]
        for confirmation_id, target in pending_by_id.items():
            if (
                target.target_kind != "care"
                or confirmation_id in declined_confirmation_ids
            ):
                continue
            if len(care_products) != 1:
                raise AcceptanceError("frozen Care strategy is missing or ambiguous")
            care_product = care_products[0]
            strategy = CareStrategy.model_validate(care_product.payload)
            if strategy.disposition == "propose" and strategy.primary_action is not None:
                action = strategy.primary_action
                if (
                    action.candidate_id != target.candidate_id
                    or action.candidate_hash != target.candidate_hash
                ):
                    raise AcceptanceError("frozen Care target drift")
                idempotency_key = (
                    f"{request.idempotency_key or request.episode_id}:"
                    f"care:{action.candidate_id}:{action.candidate_version}"
                )
                capability = self._acquire_confirmation_capability(
                    decision_by_confirmation[confirmation_id],
                    target=target,
                    request=request,
                    idempotency_key=idempotency_key,
                )
                commit = self._execute_confirmed_commit(
                    capability,
                    lambda: self.commit_controller.activate_care(
                        action=action,
                        subject_id=request.fact_snapshot.binding.subject_id,
                        expected_version=request.fact_snapshot.care_context_version,
                        fact_snapshot=request.fact_snapshot,
                        idempotency_key=idempotency_key,
                        approval_capability=capability,
                    ),
                )
                committed_care_id = action.candidate_id
            else:
                if (
                    strategy.strategy_id != target.candidate_id
                    or care_product.target_hash != target.candidate_hash
                ):
                    raise AcceptanceError("frozen Care transition drift")
                idempotency_key = (
                    f"{request.idempotency_key or request.episode_id}:"
                    f"care-transition:{strategy.strategy_id}"
                )
                capability = self._acquire_confirmation_capability(
                    decision_by_confirmation[confirmation_id],
                    target=target,
                    request=request,
                    idempotency_key=idempotency_key,
                )
                commit = self._execute_confirmed_commit(
                    capability,
                    lambda: self.commit_controller.transition_care(
                        strategy=strategy,
                        strategy_target_hash=care_product.target_hash,
                        subject_id=request.fact_snapshot.binding.subject_id,
                        expected_version=request.fact_snapshot.care_context_version,
                        fact_snapshot=request.fact_snapshot,
                        idempotency_key=idempotency_key,
                        approval_capability=capability,
                    ),
                )
                committed_care_id = (
                    strategy.primary_action.candidate_id
                    if strategy.primary_action is not None
                    else strategy.strategy_id
                )
            if commit.outcome != InvocationOutcome.SUCCEEDED:
                raise AcceptanceError("Care commit returned unknown outcome")
            additional_tool_receipts.append(commit)

        for confirmation_id, target in pending_by_id.items():
            if (
                target.target_kind != "habit_profile"
                or confirmation_id in declined_confirmation_ids
            ):
                continue
            change_set = frozen_result.habit_change_set
            if (
                change_set is None
                or change_set.change_set_id != target.candidate_id
                or change_set.manifest_hash != target.candidate_hash
            ):
                raise AcceptanceError("frozen Habit Profile target drift")
            idempotency_key = (
                f"{request.idempotency_key or request.episode_id}:"
                f"habit:{change_set.change_set_id}:{change_set.version}"
            )
            capability = self._acquire_confirmation_capability(
                decision_by_confirmation[confirmation_id],
                target=target,
                request=request,
                idempotency_key=idempotency_key,
            )
            commit = self._execute_confirmed_commit(
                capability,
                lambda: self.commit_controller.commit_habit_profile(
                    change_set=change_set,
                    approval_capability=capability,
                    fact_snapshot=request.fact_snapshot,
                    idempotency_key=idempotency_key,
                ),
            )
            if commit.outcome != InvocationOutcome.SUCCEEDED:
                raise AcceptanceError("Habit Profile commit returned unknown outcome")
            additional_tool_receipts.append(commit)
            committed_habit_id = change_set.change_set_id

        for confirmation_id, target in pending_by_id.items():
            if (
                target.target_kind != "external_action"
                or confirmation_id in declined_confirmation_ids
            ):
                continue
            external_target = frozen_result.external_action_target
            if (
                external_target is None
                or frozen_result.external_action_target_id != target.candidate_id
                or frozen_result.external_action_target_hash != target.candidate_hash
                or external_target.target_id != target.candidate_id
            ):
                raise AcceptanceError("frozen external-action target drift")
            idempotency_key = (
                request.idempotency_key
                or f"{request.episode_id}:external:{external_target.target_id}"
            )
            capability = self._acquire_confirmation_capability(
                decision_by_confirmation[confirmation_id],
                target=target,
                request=request,
                idempotency_key=idempotency_key,
            )
            commit = self._execute_confirmed_commit(
                capability,
                lambda: self.commit_controller.execute_external(
                    tool_name=external_target.tool_name,
                    target=external_target.payload,
                    snapshot=request.fact_snapshot,
                    idempotency_key=idempotency_key,
                    executor=self.external_executor,
                    approval_capability=capability,
                    actor_id=external_target.actor_id,
                    subject_id=external_target.subject_id,
                    action_scope=external_target.action_scope,
                    target_id=external_target.target_id,
                    target_version=external_target.target_version,
                    target_hash=target.candidate_hash,
                ),
            )
            if commit.outcome == InvocationOutcome.UNKNOWN:
                raise AcceptanceError("external action outcome is unknown")
            additional_tool_receipts.append(commit)
            external_receipt_id = commit.tool_invocation_id
            delivery_status = commit.output["delivery_status"]
            if delivery_status == "pending":
                external_delivery_status = "pending"
            elif delivery_status == "delivered":
                external_delivery_status = "delivered"
            else:
                raise AcceptanceError(
                    "external action returned an invalid delivery status"
                )

        return ConfirmedActionOutcome(
            additional_tool_receipts=tuple(additional_tool_receipts),
            committed_memory_candidate_ids=tuple(committed_memory_ids),
            committed_habit_change_set_id=committed_habit_id,
            declined_confirmation_ids=tuple(sorted(declined_confirmation_ids)),
            committed_care_candidate_id=committed_care_id,
            external_action_receipt_id=external_receipt_id,
            external_action_delivery_status=external_delivery_status,
        )

    def _acquire_confirmation_capability(
        self,
        decision: HumanDecisionRequest,
        *,
        target: PendingConfirmationTarget,
        request: ProductEpisodeRunRequest,
        idempotency_key: str,
    ) -> VerifiedApprovalCapabilityPort:
        try:
            return cast(
                VerifiedApprovalCapabilityPort,
                self.human_decisions.acquire_verified_capability(
                    decision.decision_id,
                    expected_proposal_id=decision.proposal.proposal_id,
                    expected_subject_id=target.subject_id,
                    expected_target_id=target.candidate_id,
                    expected_target_hash=target.candidate_hash,
                    expected_action_scope=target.action_scope,
                    expected_fact_snapshot_id=(
                        request.fact_snapshot.fact_snapshot_id
                    ),
                    expected_fact_snapshot_hash=(
                        request.fact_snapshot.fact_snapshot_hash
                    ),
                    expected_policy_version=decision.proposal.policy_version,
                    idempotency_key=idempotency_key,
                ),
            )
        except HumanDecisionError as exc:
            raise AcceptanceError(
                "authoritative approval could not be acquired"
            ) from exc

    def _execute_confirmed_commit(
        self,
        capability: VerifiedApprovalCapabilityPort,
        operation: Callable[[], ToolReceipt],
    ) -> ToolReceipt:
        try:
            receipt = operation()
        except Exception as exc:
            self.human_decisions.record_execution_result(
                capability,
                status=HumanDecisionStatus.EXECUTION_FAILED,
                failure_reason=type(exc).__name__,
            )
            raise
        status = (
            HumanDecisionStatus.COMMITTED
            if receipt.outcome == InvocationOutcome.SUCCEEDED
            else (
                HumanDecisionStatus.OUTCOME_UNKNOWN
                if receipt.outcome == InvocationOutcome.UNKNOWN
                else HumanDecisionStatus.EXECUTION_FAILED
            )
        )
        self.human_decisions.record_execution_result(
            capability,
            status=status,
            receipt_ref=receipt.tool_invocation_id,
            failure_reason=receipt.error_code,
        )
        return receipt


__all__ = [
    "CONFIRMED_ACTION_COORDINATOR_VERSION",
    "ConfirmedActionCoordinator",
]
