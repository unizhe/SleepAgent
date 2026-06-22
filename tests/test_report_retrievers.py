import pytest

from sleepagent.schemas import ReportKnowledgeChunk, RetrievedReportKnowledgeChunk
from sleepagent.services import (
    ChromaReportKnowledgeRetriever,
    InMemoryReportKnowledgeRetriever,
    ReportRetrieverMode,
    build_report_knowledge_retriever,
    load_report_retriever_config_from_env,
    retrieve_report_context,
)


class FakeChromaAdapter:
    def __init__(self) -> None:
        self.query_calls = []

    def query(self, query: str, *, top_k: int = 3):
        self.query_calls.append((query, top_k))
        chunk = ReportKnowledgeChunk(
            chunk_id="fake-chroma",
            title="Fake Chroma",
            content="Chroma retrieval result.",
            source="unit test",
            topic_tags=["ahi"],
        )
        return [
            RetrievedReportKnowledgeChunk(
                chunk=chunk,
                score=0.9,
                matched_terms=[],
            )
        ]


def test_load_report_retriever_config_defaults_to_in_memory() -> None:
    config = load_report_retriever_config_from_env({})

    assert config.mode == ReportRetrieverMode.IN_MEMORY
    assert config.chroma_persist_directory is None


def test_load_report_retriever_config_accepts_chroma_env() -> None:
    config = load_report_retriever_config_from_env(
        {
            "SLEEPAGENT_REPORT_RETRIEVER": "chroma",
            "SLEEPAGENT_REPORT_CHROMA_DIR": "/tmp/report-chroma",
            "SLEEPAGENT_REPORT_CHROMA_COLLECTION": "custom_collection",
        }
    )

    assert config.mode == ReportRetrieverMode.CHROMA
    assert str(config.chroma_persist_directory) == "/tmp/report-chroma"
    assert config.chroma_collection_name == "custom_collection"


def test_load_report_retriever_config_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="SLEEPAGENT_REPORT_RETRIEVER"):
        load_report_retriever_config_from_env({"SLEEPAGENT_REPORT_RETRIEVER": "llm"})


def test_build_report_knowledge_retriever_defaults_to_in_memory() -> None:
    retriever = build_report_knowledge_retriever(
        load_report_retriever_config_from_env({})
    )

    assert isinstance(retriever, InMemoryReportKnowledgeRetriever)


def test_chroma_report_retriever_joins_iterable_query_terms() -> None:
    adapter = FakeChromaAdapter()
    retriever = ChromaReportKnowledgeRetriever(
        persist_directory=None,
        adapter=adapter,
    )

    results = retriever.retrieve(["ahi", "hypopnea"], top_k=2)

    assert adapter.query_calls == [("ahi hypopnea", 2)]
    assert results[0].chunk.chunk_id == "fake-chroma"


def test_retrieve_report_context_uses_injected_retriever() -> None:
    adapter = FakeChromaAdapter()
    retriever = ChromaReportKnowledgeRetriever(
        persist_directory=None,
        adapter=adapter,
    )

    results = retrieve_report_context(["ahi"], top_k=1, retriever=retriever)

    assert adapter.query_calls == [("ahi", 1)]
    assert results[0].score == 0.9
