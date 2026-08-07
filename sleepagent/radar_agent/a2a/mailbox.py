from __future__ import annotations

from typing import Literal

from sleepagent.radar_agent.persistence.store import RadarPersistenceStore
from sleepagent.radar_agent.schemas import A2AMessage


class A2APolicyError(ValueError):
    pass


class A2AApprovalRequired(A2APolicyError):
    pass


class A2ARoundLimitExceeded(A2APolicyError):
    pass


class InMemoryA2AMailbox:
    """Process-local A2A mailbox with optional A2AMessageLog persistence."""

    def __init__(self, *, store: RadarPersistenceStore | None = None) -> None:
        self._messages: list[A2AMessage] = []
        self._store = store

    def publish(self, message: A2AMessage) -> A2AMessage:
        self._upsert(message)
        return message

    def list_messages(self, *, task_id: str | None = None) -> list[A2AMessage]:
        if task_id is None:
            return list(self._messages)
        return [message for message in self._messages if message.task_id == task_id]

    def get_message(self, message_id: str) -> A2AMessage:
        for message in self._messages:
            if message.message_id == message_id:
                return message
        raise KeyError(message_id)

    def set_status(
        self,
        message: A2AMessage,
        status: Literal["queued", "delivered", "handled", "rejected"],
    ) -> A2AMessage:
        updated = message.model_copy(update={"message_status": status})
        self._upsert(updated)
        return updated

    def queued_for(
        self,
        *,
        task_id: str,
        receiver: str,
        collaboration_round: int | None = None,
    ) -> list[A2AMessage]:
        return [
            message
            for message in self._messages
            if message.task_id == task_id
            and message.receiver == receiver
            and message.message_status == "queued"
            and (
                collaboration_round is None
                or message.collaboration_round == collaboration_round
            )
        ]

    def drain_for(self, *, task_id: str, receiver: str) -> list[A2AMessage]:
        selected = [
            message
            for message in self._messages
            if message.task_id == task_id and message.receiver == receiver
        ]
        selected_ids = {message.message_id for message in selected}
        self._messages = [
            message for message in self._messages if message.message_id not in selected_ids
        ]
        return selected

    def _upsert(self, message: A2AMessage) -> None:
        for index, existing in enumerate(self._messages):
            if existing.message_id == message.message_id:
                self._messages[index] = message
                break
        else:
            self._messages.append(message)
        if self._store is not None:
            self._store.save_a2a_message(message)


class A2AEventBus:
    """Orchestrator-managed mailbox/Event Bus for sub-agent collaboration."""

    def __init__(
        self,
        mailbox: InMemoryA2AMailbox | None = None,
        *,
        max_collaboration_rounds: int = 2,
    ) -> None:
        self.mailbox = mailbox or InMemoryA2AMailbox()
        self.max_collaboration_rounds = max_collaboration_rounds

    def publish_from_agent(self, message: A2AMessage) -> A2AMessage:
        raise A2APolicyError(
            "Sub-agents cannot publish A2A messages directly; route through Orchestrator."
        )

    def forward_from_orchestrator(
        self,
        message: A2AMessage,
        *,
        approved_by_orchestrator: bool = True,
        approved_by_workflow_runtime: bool = False,
    ) -> A2AMessage:
        self._validate_forwarding(
            message,
            approved_by_orchestrator=approved_by_orchestrator,
            approved_by_workflow_runtime=approved_by_workflow_runtime,
        )
        queued = message.model_copy(update={"message_status": "queued"})
        return self.mailbox.publish(queued)

    def deliver_to(
        self,
        *,
        task_id: str,
        receiver: str,
        collaboration_round: int | None = None,
    ) -> list[A2AMessage]:
        delivered: list[A2AMessage] = []
        for message in self.mailbox.queued_for(
            task_id=task_id,
            receiver=receiver,
            collaboration_round=collaboration_round,
        ):
            delivered.append(self.mailbox.set_status(message, "delivered"))
        return delivered

    def mark_handled(self, message: A2AMessage) -> A2AMessage:
        return self.mailbox.set_status(message, "handled")

    def reject(self, message: A2AMessage) -> A2AMessage:
        return self.mailbox.set_status(message, "rejected")

    def list_messages(self, *, task_id: str | None = None) -> list[A2AMessage]:
        return self.mailbox.list_messages(task_id=task_id)

    def _validate_forwarding(
        self,
        message: A2AMessage,
        *,
        approved_by_orchestrator: bool,
        approved_by_workflow_runtime: bool,
    ) -> None:
        if not approved_by_orchestrator:
            raise A2AApprovalRequired("A2A forwarding requires Orchestrator approval.")
        if message.collaboration_round > self.max_collaboration_rounds:
            raise A2ARoundLimitExceeded("A2A collaboration is limited to 1-2 rounds.")
        if message.target_task_id and message.target_task_id != message.task_id:
            if not approved_by_workflow_runtime:
                raise A2AApprovalRequired(
                    "Cross-task A2A sharing requires WorkflowRuntime approval."
                )
            if message.shared_artifact_type not in {
                "trend_summary",
                "pattern",
                "suggestion",
            }:
                raise A2APolicyError(
                    "Cross-task A2A sharing is limited to summaries, patterns, and suggestions."
                )


__all__ = [
    "A2AApprovalRequired",
    "A2AEventBus",
    "A2APolicyError",
    "A2ARoundLimitExceeded",
    "InMemoryA2AMailbox",
]
