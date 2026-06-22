import pytest
from pydantic import ValidationError

from sleepagent.schemas import (
    ReportKnowledgeChunk,
    ReportKnowledgeReviewStatus,
    ReportKnowledgeSourceType,
    RetrievedReportKnowledgeChunk,
)
from sleepagent.services import (
    DEFAULT_REPORT_KNOWLEDGE_CHUNKS,
    STAGE7_REPORT_KNOWLEDGE_SCHEMA_VERSION,
    retrieve_report_knowledge,
)


def test_report_knowledge_chunk_contract_is_strict() -> None:
    chunk = ReportKnowledgeChunk(
        chunk_id="test-ahi",
        title="AHI",
        content="AHI should be treated as a risk signal.",
        source="unit test",
        topic_tags=["ahi"],
        audience_tags=["professional"],
        safety_notes=["Do not diagnose from this field alone."],
    )

    payload = chunk.model_dump(mode="json")

    assert payload["schema_version"] == STAGE7_REPORT_KNOWLEDGE_SCHEMA_VERSION
    assert payload["source_type"] == ReportKnowledgeSourceType.INTERNAL_SEED.value
    assert payload["review_status"] == ReportKnowledgeReviewStatus.REVIEWED.value
    assert payload["last_reviewed_at"] is not None
    assert payload["topic_tags"] == ["ahi"]

    with pytest.raises(ValidationError):
        ReportKnowledgeChunk(
            chunk_id="extra-field",
            title="Extra",
            content="Extra fields should be rejected.",
            source="unit test",
            unsupported=True,
        )


def test_retrieved_report_knowledge_chunk_contract_requires_score_bounds() -> None:
    chunk = DEFAULT_REPORT_KNOWLEDGE_CHUNKS[0]

    result = RetrievedReportKnowledgeChunk(
        chunk=chunk,
        score=1.0,
        matched_terms=["sleep_efficiency"],
    )

    assert result.chunk.chunk_id == chunk.chunk_id

    with pytest.raises(ValidationError):
        RetrievedReportKnowledgeChunk(
            chunk=chunk,
            score=-0.1,
            matched_terms=["sleep_efficiency"],
        )


def test_retrieve_report_knowledge_prefers_ahi_chunk() -> None:
    results = retrieve_report_knowledge(
        ["ahi", "hypopnea", "suspected_apnea"],
        top_k=2,
    )

    assert results
    assert results[0].chunk.chunk_id == "ahi-basic"
    assert results[0].matched_terms == ["ahi", "hypopnea", "suspected_apnea"]
    assert results[0].score == 3.0


def test_retrieve_report_knowledge_returns_empty_for_empty_query() -> None:
    assert retrieve_report_knowledge("   ") == []


def test_retrieve_report_knowledge_rejects_non_positive_top_k() -> None:
    with pytest.raises(ValueError, match="top_k"):
        retrieve_report_knowledge("ahi", top_k=0)


def test_retrieve_report_knowledge_filters_dev_only_chunks_by_default() -> None:
    dev_only = ReportKnowledgeChunk(
        chunk_id="dev-only-guidance",
        title="Dev only",
        content="AHI internal draft text.",
        source="unit test",
        source_type=ReportKnowledgeSourceType.INTERNAL_SEED,
        review_status=ReportKnowledgeReviewStatus.DEV_ONLY,
        topic_tags=["ahi"],
    )

    assert retrieve_report_knowledge("ahi", chunks=[dev_only]) == []
    assert retrieve_report_knowledge(
        "ahi",
        chunks=[dev_only],
        include_dev_only=True,
    )[0].chunk.chunk_id == "dev-only-guidance"
