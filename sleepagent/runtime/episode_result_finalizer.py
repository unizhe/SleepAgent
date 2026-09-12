from __future__ import annotations

# 结果持久化与发布终态协调共置。

import logging

from sleepagent.runtime.agent_invocation_coordinator import (
    ProviderInputBudgetLedger,
)
from sleepagent.runtime.contracts import EpisodeStatus
from sleepagent.runtime.memory import (
    canonical_result_hash,
)
from sleepagent.runtime.results import (
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    product_episode_request_hash,
)
from sleepagent.runtime.contracts import (
    ProductEpisodeResultStore,
)
from sleepagent.runtime.tool_execution_coordinator import (
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
            return result
        return result


__all__ = ["EPISODE_RESULT_FINALIZER_VERSION", "EpisodeResultFinalizer"]
