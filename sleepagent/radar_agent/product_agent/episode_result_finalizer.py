from __future__ import annotations

import logging

from sleepagent.radar_agent.product_agent.agent_invocation_coordinator import (
    ProviderInputBudgetLedger,
)
from sleepagent.radar_agent.product_agent.contracts import EpisodeStatus
from sleepagent.radar_agent.product_agent.longitudinal_memory import (
    canonical_result_hash,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
    PRODUCT_EPISODE_RUNNER_VERSION,
    ConfirmedActionOutcome,
    ProductContinuationLineage,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    TerminalEpisodeDecision,
    bind_product_episode_checkpoint,
    product_episode_frozen_identity_hash,
    product_episode_request_hash,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductEpisodeResultStore,
)
from sleepagent.radar_agent.product_agent.tool_execution_coordinator import (
    ToolExecutionCoordinator,
)


LOGGER = logging.getLogger(__name__)
EPISODE_RESULT_FINALIZER_VERSION = "sleepagent-episode-result-finalizer.v1"


class EpisodeResultFinalizer:
    """Persist an Episode result before releasing its transient runtime state."""

    def __init__(
        self,
        *,
        result_store: ProductEpisodeResultStore,
        tool_execution_coordinator: ToolExecutionCoordinator,
        provider_input_budget_ledger: ProviderInputBudgetLedger,
    ) -> None:
        self.result_store = result_store
        self.tool_execution_coordinator = tool_execution_coordinator
        self.provider_input_budget_ledger = provider_input_budget_ledger

    def finalize(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
        request: ProductEpisodeRunRequest | None = None,
    ) -> ProductEpisodeRunResult:
        """Persist one revision and clean transient state only after terminal durability."""

        episode_id = result.receipt.episode_id
        if request is not None:
            if (
                request.episode_id != episode_id
                or subject_id != request.fact_snapshot.binding.subject_id
                or request.fact_snapshot.fact_snapshot_id
                != result.receipt.fact_snapshot_id
                or request.fact_snapshot.fact_snapshot_hash
                != result.receipt.fact_snapshot_hash
            ):
                raise ValueError("Episode finalization request binding mismatch")
            result = result.model_copy(
                update={
                    "continuation_request_hash": product_episode_request_hash(
                        request
                    ),
                    **(
                        {
                            "continuation_kind": (
                                request.continuation_lineage.kind
                            ),
                            "continuation_parent_checkpoint_hash": (
                                request.continuation_lineage.parent_checkpoint_hash
                            ),
                            "continuation_command_hash": (
                                request.continuation_lineage.command_hash
                            ),
                        }
                        if request.continuation_lineage is not None
                        else {}
                    ),
                }
            )
        result = self._normalize_checkpoint(result)
        history = self.result_store.history(episode_id)
        if history:
            latest = history[-1]
            if (
                latest.receipt.receipt_revision
                >= result.receipt.receipt_revision
                and canonical_result_hash(latest) != canonical_result_hash(result)
            ):
                result = result.model_copy(
                    update={
                        "receipt": result.receipt.model_copy(
                            update={
                                "receipt_revision": (
                                    latest.receipt.receipt_revision + 1
                                )
                            }
                        )
                    }
                )
                result = self._normalize_checkpoint(result)
        if result.receipt.terminal:
            # The terminal Result + Manifest + induction job must become durable
            # before either transient authority is released.  An append failure
            # intentionally escapes without touching either cleanup boundary.
            self.result_store.append_terminal_bundle(
                result,
                subject_id=subject_id,
            )
            self._release_terminal_state(episode_id)
        else:
            self.result_store.append_nonterminal(
                result,
                subject_id=subject_id,
            )
        return result

    def resolve_frozen_checkpoint(
        self,
        frozen_result: ProductEpisodeRunResult,
        *,
        request: ProductEpisodeRunRequest,
        result_request: ProductEpisodeRunRequest,
        lineage: ProductContinuationLineage,
    ) -> ProductEpisodeRunResult | None:
        """Validate the latest lineage and return an already-durable descendant."""

        history = self.result_store.history(frozen_result.receipt.episode_id)
        latest = history[-1] if history else None
        if latest is None:
            raise ValueError("frozen continuation has no persisted checkpoint")

        def normalized(
            candidate: ProductEpisodeRunResult,
        ) -> ProductEpisodeRunResult:
            if candidate.continuation_request_hash is not None:
                return candidate
            if candidate.schema_version != "ProductEpisodeRunResult.v38":
                raise ValueError(
                    "persisted checkpoint lacks its exact request binding"
                )
            return candidate.model_copy(
                update={
                    "continuation_request_hash": product_episode_request_hash(
                        request
                    )
                }
            )

        expected_hash = product_episode_frozen_identity_hash(frozen_result)
        normalized_latest = normalized(latest)
        if (
            not normalized_latest.receipt.terminal
            and product_episode_frozen_identity_hash(normalized_latest)
            == expected_hash
        ):
            durable_records = tuple(
                record
                for result in history
                if not result.receipt.terminal
                for record in result.agent_invocations
            )
            self.provider_input_budget_ledger.restore_from_invocations(
                frozen_result.receipt.episode_id,
                durable_records,
            )
            return None

        if (
            latest.continuation_kind == lineage.kind
            and latest.continuation_parent_checkpoint_hash == expected_hash
            and latest.continuation_command_hash == lineage.command_hash
            and latest.continuation_request_hash
            == product_episode_request_hash(result_request)
        ):
            return latest
        raise ValueError(
            "frozen continuation does not match the latest persisted lineage"
        )

    def finalize_confirmed_action(
        self,
        *,
        frozen_result: ProductEpisodeRunResult,
        outcome: ConfirmedActionOutcome,
        decision: TerminalEpisodeDecision,
        lineage: ProductContinuationLineage,
        subject_id: str,
        request: ProductEpisodeRunRequest,
    ) -> ProductEpisodeRunResult:
        """Apply commit deltas and finalize the Runner's terminal decision."""

        tool_receipts = list(frozen_result.tool_receipts)
        receipt_by_id = {
            item.tool_invocation_id: item for item in tool_receipts
        }
        for additional_receipt in outcome.additional_tool_receipts:
            existing = receipt_by_id.get(
                additional_receipt.tool_invocation_id
            )
            if existing is not None:
                if existing != additional_receipt:
                    raise ValueError("confirmed Tool receipt identity collision")
                continue
            receipt_by_id[
                additional_receipt.tool_invocation_id
            ] = additional_receipt
            tool_receipts.append(additional_receipt)

        def merged_ids(existing: list[str], added: tuple[str, ...]) -> list[str]:
            return list(dict.fromkeys([*existing, *added]))

        terminal_receipt = frozen_result.receipt.model_copy(
            update={
                "receipt_revision": frozen_result.receipt.receipt_revision + 1,
                "terminal": True,
                "status": decision.status,
                "goal_achieved": decision.goal_achieved,
                "execution_mode": (
                    decision.execution_mode
                    if decision.execution_mode is not None
                    else frozen_result.receipt.execution_mode
                ),
                "failure_codes": list(decision.failure_codes),
                "tool_receipt_ids": [
                    item.tool_invocation_id for item in tool_receipts
                ],
            }
        )
        result = ProductEpisodeRunResult.model_validate(
            frozen_result.model_copy(
                update={
                    "continuation_checkpoint_hash": None,
                    "schema_version": PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
                    "runner_version": PRODUCT_EPISODE_RUNNER_VERSION,
                    "continuation_kind": lineage.kind,
                    "continuation_parent_checkpoint_hash": (
                        lineage.parent_checkpoint_hash
                    ),
                    "continuation_command_hash": lineage.command_hash,
                    "receipt": terminal_receipt,
                    "tool_receipts": tool_receipts,
                    "committed_memory_candidate_ids": merged_ids(
                        frozen_result.committed_memory_candidate_ids,
                        outcome.committed_memory_candidate_ids,
                    ),
                    "committed_habit_change_set_id": (
                        outcome.committed_habit_change_set_id
                        or frozen_result.committed_habit_change_set_id
                    ),
                    "declined_confirmation_ids": sorted(
                        set(frozen_result.declined_confirmation_ids)
                        | set(outcome.declined_confirmation_ids)
                    ),
                    "committed_care_candidate_id": (
                        outcome.committed_care_candidate_id
                        or frozen_result.committed_care_candidate_id
                    ),
                    "external_action_receipt_id": (
                        outcome.external_action_receipt_id
                        or frozen_result.external_action_receipt_id
                    ),
                    "external_action_delivery_status": (
                        outcome.external_action_delivery_status
                        or frozen_result.external_action_delivery_status
                    ),
                    "pending_confirmations": [],
                }
            ).model_dump(mode="python")
        )
        return self.finalize(
            result,
            subject_id=subject_id,
            request=request,
        )

    def _release_terminal_state(self, episode_id: str) -> None:
        try:
            self.tool_execution_coordinator.release_episode(episode_id)
        except Exception:
            LOGGER.exception(
                "terminal Tool cache cleanup failed for Episode %s",
                episode_id,
            )
        try:
            self.provider_input_budget_ledger.release_episode(episode_id)
        except Exception:
            LOGGER.exception(
                "terminal provider-ledger cleanup failed for Episode %s",
                episode_id,
            )

    @staticmethod
    def _normalize_checkpoint(
        result: ProductEpisodeRunResult,
    ) -> ProductEpisodeRunResult:
        if result.receipt.terminal:
            return result.model_copy(
                update={"continuation_checkpoint_hash": None}
            )
        if result.receipt.status in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            return bind_product_episode_checkpoint(result)
        return result


__all__ = ["EPISODE_RESULT_FINALIZER_VERSION", "EpisodeResultFinalizer"]
