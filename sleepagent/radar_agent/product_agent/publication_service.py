from __future__ import annotations

from typing import Literal, cast

from sleepagent.radar_agent.product_agent.cold_start import ClaimCeiling
from sleepagent.radar_agent.product_agent.contracts import (
    CommunicationDraft,
    InvocationOutcome,
    ToolReceipt,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.governance import PublicationError
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ProductEpisodeRunRequest,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    FactSnapshotRevalidator,
    MemoryPublicationGate,
    ProductEpisodeResultStore,
    PublicationJournalEntryPort,
    PublicationPublisher,
)


PUBLICATION_SERVICE_VERSION = "sleepagent-publication-service.v2"


class PublicationIndeterminateError(PublicationError):
    """A reserved publication whose delivery truth is not safely replayable."""

    def __init__(
        self,
        message: str,
        *,
        phase: Literal["reservation", "publisher", "journal"],
        intent_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.intent_id = intent_id


class PublicationService:
    """Revalidate and journal one publication attempt at the delivery boundary."""

    def __init__(
        self,
        *,
        publisher: PublicationPublisher,
        result_store: ProductEpisodeResultStore,
        longitudinal_memory: MemoryPublicationGate,
        revalidator: FactSnapshotRevalidator | None,
    ) -> None:
        self.publisher = publisher
        self.result_store = result_store
        self.longitudinal_memory = longitudinal_memory
        self.revalidator = revalidator

    def publish(
        self,
        draft: CommunicationDraft,
        *,
        episode_id: str,
        request: ProductEpisodeRunRequest,
        tool_receipts: list[ToolReceipt],
    ) -> bool:
        """Publish exactly the validated draft or return a prior final outcome."""

        if episode_id != request.episode_id:
            raise ValueError("publication Episode binding mismatch")

        source_dependent_cold_start = any(
            decision.scope_source_refs
            or decision.baseline_source_refs
            or decision.scope_valid_night_count > 0
            or decision.baseline_valid_night_count > 0
            or decision.claim_ceiling != ClaimCeiling.GENERAL_KNOWLEDGE
            for decision in request.runtime_readiness_decisions
        )
        if source_dependent_cold_start:
            if self.revalidator is None:
                raise PublicationError(
                    "cold-start publication requires current authorization "
                    "and source revalidation"
                )
            try:
                snapshot_is_current = self.revalidator(request.fact_snapshot)
            except Exception as exc:
                raise PublicationError(
                    "source revalidation failed before publication"
                ) from exc
            if not snapshot_is_current:
                raise PublicationError(
                    "authorization or canonical source changed after FactSnapshot"
                )
        try:
            self.longitudinal_memory.validate_prepublication(
                tuple(
                    receipt.output
                    for receipt in tool_receipts
                    if receipt.tool_name == "memory.read"
                    and receipt.outcome == InvocationOutcome.SUCCEEDED
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                actor_id=request.fact_snapshot.binding.actor_id,
            )
        except Exception as exc:
            raise PublicationError(
                "Memory publication gate failed before reservation"
            ) from exc
        command_hash = self._publication_command_hash(request)
        draft_hash = stable_hash(draft)
        try:
            entry, created = self.result_store.reserve_publication(
                command_hash=command_hash,
                episode_id=episode_id,
                draft_hash=draft_hash,
            )
        except Exception as exc:
            raise PublicationIndeterminateError(
                "publication reservation outcome is indeterminate",
                phase="reservation",
            ) from exc
        if type(created) is not bool:
            raise PublicationIndeterminateError(
                "publication reservation creation flag is invalid",
                phase="reservation",
            )
        state, prior_delivery = self._validate_journal_entry(
            entry,
            phase="reservation",
            expected_episode_id=(episode_id if created else None),
            command_hash=command_hash,
            draft_hash=draft_hash,
            expected_intent_id=None,
            expected_state=("reserved" if created else None),
        )
        if not created and state == "reserved":
            raise PublicationIndeterminateError(
                "indeterminate prior publication requires reconciliation",
                phase="reservation",
                intent_id=entry.intent_id,
            )
        if state != "reserved":
            assert prior_delivery is not None
            return prior_delivery
        try:
            publisher_outcome = self.publisher.publish(draft)
        except Exception as exc:
            raise PublicationIndeterminateError(
                "publisher delivery outcome is indeterminate",
                phase="publisher",
                intent_id=entry.intent_id,
            ) from exc
        if type(publisher_outcome) is not bool:
            raise PublicationIndeterminateError(
                "publisher returned an invalid delivery outcome",
                phase="publisher",
                intent_id=entry.intent_id,
            )
        delivered = publisher_outcome
        try:
            completed = self.result_store.complete_publication(
                intent_id=entry.intent_id,
                delivered=delivered,
            )
        except Exception as exc:
            raise PublicationIndeterminateError(
                "publication journal finalization is indeterminate",
                phase="journal",
                intent_id=entry.intent_id,
            ) from exc
        self._validate_journal_entry(
            completed,
            phase="journal",
            expected_episode_id=episode_id,
            command_hash=command_hash,
            draft_hash=draft_hash,
            expected_intent_id=entry.intent_id,
            expected_state=("delivered" if delivered else "failed"),
        )
        return delivered

    @staticmethod
    def _validate_journal_entry(
        entry: PublicationJournalEntryPort,
        *,
        phase: Literal["reservation", "journal"],
        expected_episode_id: str | None,
        command_hash: str,
        draft_hash: str,
        expected_intent_id: str | None,
        expected_state: Literal["reserved", "delivered", "failed"] | None,
    ) -> tuple[Literal["reserved", "delivered", "failed"], bool | None]:
        """Validate journal identity and its reserved/final state invariant."""

        intent_id: str | None = None
        try:
            raw_intent_id = entry.intent_id
            raw_episode_id = entry.episode_id
            raw_command_hash = entry.command_hash
            raw_draft_hash = entry.draft_hash
            raw_state = entry.state
            raw_delivered = entry.delivered
            if not isinstance(raw_intent_id, str) or not raw_intent_id:
                raise ValueError("publication journal intent ID is invalid")
            intent_id = raw_intent_id
            if not isinstance(raw_episode_id, str) or not raw_episode_id:
                raise ValueError("publication journal Episode ID is invalid")
            if (
                expected_episode_id is not None
                and raw_episode_id != expected_episode_id
            ):
                raise ValueError("publication journal Episode mismatch")
            if (
                raw_command_hash != command_hash
                or raw_draft_hash != draft_hash
            ):
                raise ValueError("publication journal identity mismatch")
            if expected_intent_id is not None and intent_id != expected_intent_id:
                raise ValueError("publication journal intent changed")
            if raw_state not in {"reserved", "delivered", "failed"}:
                raise ValueError("publication journal state is invalid")
            if expected_state is not None and raw_state != expected_state:
                raise ValueError("publication journal transition mismatch")
            if raw_state == "reserved":
                if raw_delivered is not None:
                    raise ValueError(
                        "reserved publication cannot record a delivery outcome"
                    )
            elif raw_state == "delivered":
                if raw_delivered is not True:
                    raise ValueError(
                        "delivered publication requires delivered=True"
                    )
            elif raw_delivered is not False:
                raise ValueError("failed publication requires delivered=False")
        except Exception as exc:
            raise PublicationIndeterminateError(
                "publication journal entry is invalid or indeterminate",
                phase=phase,
                intent_id=intent_id,
            ) from exc
        return raw_state, raw_delivered

    @staticmethod
    def _publication_command_hash(
        request: ProductEpisodeRunRequest,
    ) -> str:
        """Bind one logical externally visible response across retries.

        The idempotency key is stable even when a worker recreates an Episode.
        Episode type keeps task execution and dialogue commands distinct, while
        the authenticated binding prevents cross-subject key reuse.
        """

        binding = request.fact_snapshot.binding
        return cast(
            str,
            stable_hash(
                {
                    "idempotency_key": (
                        request.idempotency_key or request.episode_id
                    ),
                    "episode_type": request.episode_type.value,
                    "actor_id": binding.actor_id,
                    "subject_id": binding.subject_id,
                    "role": binding.role,
                    "authorization_scope": tuple(
                        sorted(binding.authorization_scope)
                    ),
                }
            ),
        )


__all__ = [
    "PUBLICATION_SERVICE_VERSION",
    "PublicationIndeterminateError",
    "PublicationService",
]
