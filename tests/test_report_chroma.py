import subprocess
import sys

import pytest

from sleepagent.services import (
    DEFAULT_REPORT_KNOWLEDGE_CHUNKS,
    ChromaReportKnowledgeAdapter,
    HashEmbeddingFunction,
    seed_default_report_chroma_knowledge,
)


class FakeChromaCollection:
    def __init__(self) -> None:
        self.upsert_payload = None

    def upsert(self, *, ids, documents, metadatas):
        self.upsert_payload = {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
        }

    def query(self, *, query_texts, n_results, include):
        assert query_texts == ["ahi"]
        assert n_results == 2
        assert include == ["documents", "metadatas", "distances"]
        payload = self.upsert_payload
        return {
            "ids": [payload["ids"][:1]],
            "documents": [payload["documents"][:1]],
            "metadatas": [payload["metadatas"][:1]],
            "distances": [[0.25]],
        }


class FakeChromaClient:
    def __init__(self) -> None:
        self.collection = FakeChromaCollection()
        self.collection_kwargs = None

    def get_or_create_collection(self, **kwargs):
        self.collection_kwargs = kwargs
        return self.collection


def test_chroma_adapter_upserts_report_knowledge_chunks() -> None:
    client = FakeChromaClient()
    adapter = ChromaReportKnowledgeAdapter(client=client)

    count = adapter.upsert_chunks(DEFAULT_REPORT_KNOWLEDGE_CHUNKS[:2])

    assert count == 2
    assert client.collection_kwargs == {"name": "sleepagent_report_knowledge"}
    payload = client.collection.upsert_payload
    assert payload["ids"] == ["sleep-efficiency-basic", "ahi-basic"]
    assert payload["documents"][1].startswith("AHI summarizes")
    assert payload["metadatas"][1]["chunk_id"] == "ahi-basic"
    assert payload["metadatas"][1]["source_type"] == "internal_seed"
    assert payload["metadatas"][1]["review_status"] == "reviewed"
    assert payload["metadatas"][1]["last_reviewed_at"]
    assert payload["metadatas"][1]["topic_tags"] == (
        '["ahi", "hypopnea", "suspected_apnea", "respiratory_events"]'
    )


def test_chroma_adapter_query_returns_retrieved_chunk_contract() -> None:
    client = FakeChromaClient()
    adapter = ChromaReportKnowledgeAdapter(client=client)
    adapter.upsert_chunks(DEFAULT_REPORT_KNOWLEDGE_CHUNKS[1:2])

    results = adapter.query("ahi", top_k=2)

    assert len(results) == 1
    assert results[0].chunk.chunk_id == "ahi-basic"
    assert results[0].chunk.topic_tags == [
        "ahi",
        "hypopnea",
        "suspected_apnea",
        "respiratory_events",
    ]
    assert results[0].chunk.source_type == "internal_seed"
    assert results[0].chunk.review_status == "reviewed"
    assert results[0].chunk.last_reviewed_at is not None
    assert results[0].score == pytest.approx(0.8)
    assert results[0].matched_terms == []


def test_chroma_adapter_query_rejects_non_positive_top_k() -> None:
    adapter = ChromaReportKnowledgeAdapter(client=FakeChromaClient())

    with pytest.raises(ValueError, match="top_k"):
        adapter.query("ahi", top_k=0)


def test_chroma_adapter_query_returns_empty_for_blank_query() -> None:
    adapter = ChromaReportKnowledgeAdapter(client=FakeChromaClient())

    assert adapter.query("  ") == []


def test_chroma_adapter_filters_dev_only_chunks_from_query_results() -> None:
    client = FakeChromaClient()
    adapter = ChromaReportKnowledgeAdapter(client=client)
    adapter.upsert_chunks(DEFAULT_REPORT_KNOWLEDGE_CHUNKS[1:2])
    payload = client.collection.upsert_payload
    payload["metadatas"][0]["review_status"] = "dev_only"

    assert adapter.query("ahi", top_k=2) == []


def test_chroma_adapter_rejects_empty_collection_name() -> None:
    with pytest.raises(ValueError, match="collection_name"):
        ChromaReportKnowledgeAdapter(collection_name="")


def test_hash_embedding_function_is_deterministic() -> None:
    embedding_function = HashEmbeddingFunction(dimensions=8)

    first = embedding_function(["AHI hypopnea"])
    second = embedding_function(["AHI hypopnea"])

    assert first == second
    assert len(first) == 1
    assert len(first[0]) == 8
    assert embedding_function.embed_query(["AHI hypopnea"]) == first
    assert embedding_function.embed_documents(["AHI hypopnea"]) == first
    assert embedding_function.name() == "sleepagent_hash_embedding_v1"


def test_seed_default_report_chroma_knowledge_uses_adapter_boundary() -> None:
    client = FakeChromaClient()

    result = seed_default_report_chroma_knowledge(
        client=client,
        query="ahi",
        top_k=2,
    )

    assert result.collection_name == "sleepagent_report_knowledge"
    assert result.indexed_chunk_count == len(DEFAULT_REPORT_KNOWLEDGE_CHUNKS)
    assert result.query == "ahi"
    assert result.retrieved_chunks[0].chunk.chunk_id == "sleep-efficiency-basic"


def test_seed_report_chroma_script_help_is_import_safe_without_chromadb() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/seed_report_chroma.py", "--help"],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Seed SleepAgent Stage 7 report knowledge" in completed.stdout
