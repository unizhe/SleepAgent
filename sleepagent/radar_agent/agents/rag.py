from __future__ import annotations

from sleepagent.radar_agent.rag import SeedKnowledgeStore, default_seed_knowledge_store
from sleepagent.radar_agent.schemas import AgentResult, ContextPacket, RadarAgentName


class RAGAgent:
    name = RadarAgentName.RAG

    def __init__(self, store: SeedKnowledgeStore | None = None) -> None:
        self.store = store or default_seed_knowledge_store()

    def run(self, context: ContextPacket) -> AgentResult:
        query = _query(context)
        chunks = []
        for role in _roles(context):
            for chunk in self.store.search(
                role=role,
                reviewed_only=True,
                query=query,
            ):
                if chunk.citation_id not in {item.citation_id for item in chunks}:
                    chunks.append(chunk)
        refs = [chunk.citation_id for chunk in chunks]
        return AgentResult(
            agent_name=self.name,
            evidence_refs=refs,
            confidence=0.85 if chunks else 0.0,
            uncertainties=[] if chunks else ["no_reviewed_seed_knowledge_match"],
            safety_flags=["reviewed_seed_only", "no_free_web_search", "no_fact_mutation"],
            output_payload={
                "rag_context": {
                    "chunk_ids": [chunk.chunk_id for chunk in chunks],
                    "citation_ids": refs,
                    "snippets": [chunk.content for chunk in chunks],
                    "caveats": [note for chunk in chunks for note in chunk.safety_notes],
                    "source_metadata": [
                        {
                            "chunk_id": chunk.chunk_id,
                            "citation_id": chunk.citation_id,
                            "source_type": chunk.source_type,
                            "review_status": chunk.review_status.value,
                            "version": chunk.version,
                            "applicable_roles": chunk.applicable_roles,
                            "safety_notes": chunk.safety_notes,
                        }
                        for chunk in chunks
                    ],
                },
                "sources": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "citation_id": chunk.citation_id,
                        "version": chunk.version,
                        "source_type": chunk.source_type,
                        "review_status": chunk.review_status.value,
                        "applicable_roles": chunk.applicable_roles,
                        "safety_notes": chunk.safety_notes,
                    }
                    for chunk in chunks
                ],
            },
        )


def _query(context: ContextPacket) -> str:
    values = context.evidence_packet.data_quality.get("rag_query", "")
    if isinstance(values, list):
        return " ".join(str(value) for value in values)
    return str(values)


def _roles(context: ContextPacket) -> list[str]:
    values = context.evidence_packet.data_quality.get("rag_roles")
    if isinstance(values, list) and values:
        return [str(value) for value in values]
    return [context.task_context.role]


__all__ = ["RAGAgent"]
