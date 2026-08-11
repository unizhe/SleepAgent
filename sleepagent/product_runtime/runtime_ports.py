from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from pydantic import Field

from sleepagent.product_runtime.contracts import (
    AgentId,
    CommunicationDraft,
    FactSnapshot,
    StrictContract,
    ToolReceipt,
    TrustedContextItem,
)
from sleepagent.product_runtime.external_actions import (
    ExternalActionExecutionRequest,
    ExternalActionExecutionResult,
)

if TYPE_CHECKING:
    from sleepagent.product_runtime.runtime_contracts import (
        CommitFrozenConfirmedAction,
        ProductEpisodeRunRequest,
        ProductEpisodeRunResult,
        ReexecuteWithAddedFact,
    )


class PublicationPublisher(Protocol):
    def publish(self, draft: CommunicationDraft) -> bool: ...


class MemoryPublicationGate(Protocol):
    def validate_prepublication(
        self,
        receipt_outputs: tuple[dict[str, Any], ...],
        *,
        subject_id: str,
        actor_id: str,
    ) -> None: ...


class PublicationJournalEntryPort(Protocol):
    @property
    def intent_id(self) -> str: ...

    @property
    def episode_id(self) -> str: ...

    @property
    def command_hash(self) -> str | None: ...

    @property
    def draft_hash(self) -> str: ...

    @property
    def state(self) -> Literal["reserved", "delivered", "failed"]: ...

    @property
    def delivered(self) -> bool | None: ...


class ProductEpisodeResultStore(Protocol):
    def append_nonterminal(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
    ) -> None: ...

    def append_terminal_bundle(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> object: ...

    def latest(self, episode_id: str) -> ProductEpisodeRunResult: ...

    def history(self, episode_id: str) -> list[ProductEpisodeRunResult]: ...

    def reserve_publication(
        self,
        *,
        command_hash: str,
        episode_id: str,
        draft_hash: str,
        now: datetime | None = None,
    ) -> tuple[PublicationJournalEntryPort, bool]: ...

    def complete_publication(
        self,
        *,
        intent_id: str,
        delivered: bool,
        now: datetime | None = None,
    ) -> PublicationJournalEntryPort: ...


class ProductEpisodeRunnerPort(Protocol):
    """The narrow Product Agent facade consumed by process adapters."""

    def run(
        self,
        request: ProductEpisodeRunRequest,
    ) -> ProductEpisodeRunResult: ...

    def commit_frozen_confirmations(
        self,
        command: CommitFrozenConfirmedAction,
    ) -> ProductEpisodeRunResult: ...

    def reexecute_with_added_fact(
        self,
        command: ReexecuteWithAddedFact,
    ) -> ProductEpisodeRunResult: ...

    def process_induction_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[object]: ...


class ProductToolExecutionContext(StrictContract):
    caller: AgentId | str
    fact_snapshot: FactSnapshot
    authorization_scope: tuple[str, ...] = ()
    episode_id: str | None = None
    plan_id: str | None = None
    plan_revision: int | None = Field(default=None, ge=0)
    allowed_plan_step_ids: tuple[str, ...] = ()
    plan_step_id: str | None = None
    invocation_id: str = Field(default="runtime:unbound", min_length=1)
    user_intent_ref: str | None = None
    user_intent_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    user_intent_purpose: Literal[
        "explicit_memory_review",
        "explicit_memory_change",
        "explicit_memory_forget",
    ] | None = None
    max_memory_items: int = Field(default=8, ge=1, le=20)
    memory_token_budget: int = Field(default=1200, ge=64, le=4000)


class ProductToolResult(StrictContract):
    receipt: ToolReceipt
    context_item: TrustedContextItem | None = None


ToolHandler = Callable[
    [dict[str, Any], ProductToolExecutionContext],
    dict[str, Any],
]


class ProductToolExecutorPort(Protocol):
    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ProductToolExecutionContext,
    ) -> ProductToolResult: ...

    def release_episode(self, episode_id: str) -> None: ...


class ExternalActionExecutor(Protocol):
    def __call__(
        self,
        request: ExternalActionExecutionRequest,
    ) -> ExternalActionExecutionResult: ...


FactSnapshotRevalidator = Callable[[FactSnapshot], bool]


__all__ = [
    "ExternalActionExecutor",
    "FactSnapshotRevalidator",
    "MemoryPublicationGate",
    "ProductEpisodeResultStore",
    "ProductEpisodeRunnerPort",
    "ProductToolExecutionContext",
    "ProductToolExecutorPort",
    "ProductToolResult",
    "PublicationJournalEntryPort",
    "PublicationPublisher",
    "ToolHandler",
]
