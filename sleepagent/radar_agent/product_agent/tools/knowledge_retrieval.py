from __future__ import annotations

from sleepagent.radar_agent.product_agent.services.reviewed_knowledge import (
    ReviewedKnowledgeQuery,
    ReviewedKnowledgeResult,
    ReviewedKnowledgeService,
    default_reviewed_knowledge_service,
)


KNOWLEDGE_RETRIEVAL_TOOL_VERSION = "sleepagent-knowledge-retrieval-tool.v1"


class KnowledgeRetrievalResult(ReviewedKnowledgeResult):
    tool_version: str = KNOWLEDGE_RETRIEVAL_TOOL_VERSION


class KnowledgeRetrievalTool:
    """Read reviewed knowledge through a typed, provider-neutral boundary."""

    def __init__(self, service: ReviewedKnowledgeService | None = None) -> None:
        self._service = service or default_reviewed_knowledge_service()

    def retrieve(self, request: ReviewedKnowledgeQuery) -> KnowledgeRetrievalResult:
        result = self._service.retrieve(request)
        return KnowledgeRetrievalResult.model_validate(result.model_dump(mode="python"))


__all__ = [
    "KNOWLEDGE_RETRIEVAL_TOOL_VERSION",
    "KnowledgeRetrievalResult",
    "KnowledgeRetrievalTool",
    "ReviewedKnowledgeQuery",
    "ReviewedKnowledgeResult",
]
