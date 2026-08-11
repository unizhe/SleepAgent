from __future__ import annotations

import pytest
from pydantic import ValidationError

from sleepagent.product_runtime.knowledge import (
    SeedKnowledgeAccessError,
    SeedKnowledgeChunk,
    SeedKnowledgeIndex,
    SeedKnowledgeIngestionError,
    SeedKnowledgeStore,
    default_seed_knowledge_store,
)
from sleepagent.product_runtime.schemas import ReviewStatus


def test_default_seed_covers_all_four_reviewed_knowledge_classes() -> None:
    chunks = default_seed_knowledge_store().list_reviewed()

    assert {chunk.source_type for chunk in chunks} == {
        "safety",
        "device",
        "role_template",
        "personal_summary",
    }
    assert all(chunk.review_status == ReviewStatus.REVIEWED for chunk in chunks)
    assert all(chunk.version for chunk in chunks)
    assert all(chunk.applicable_roles for chunk in chunks)
    assert all(chunk.safety_notes for chunk in chunks)
    assert all(chunk.citation_id.startswith("seed:") for chunk in chunks)
    assert len({chunk.citation_id for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize(
    ("query", "expected_type"),
    [
        ("雷达设备离线且覆盖率不足", "device"),
        ("如何写家属角色报告", "role_template"),
        ("查看30天长期趋势和个人偏好", "personal_summary"),
    ],
)
def test_keyword_router_returns_only_safety_and_matched_categories(
    query: str,
    expected_type: str,
) -> None:
    chunks = default_seed_knowledge_store().search(role="family", query=query)
    source_types = {chunk.source_type for chunk in chunks}

    assert source_types == {"safety", expected_type}


def test_unrelated_query_fails_closed_to_safety_instead_of_returning_all_chunks() -> None:
    chunks = default_seed_knowledge_store().search(
        role="family",
        query="完全无关的园艺问题",
    )

    assert {chunk.source_type for chunk in chunks} == {"safety"}


def test_role_filter_only_returns_applicable_role_template() -> None:
    store = default_seed_knowledge_store()
    family = store.search(role="family", query="三角色报告话术")
    doctor = store.search(role="doctor", query="三角色报告话术")

    family_roles = [chunk for chunk in family if chunk.source_type == "role_template"]
    doctor_roles = [chunk for chunk in doctor if chunk.source_type == "role_template"]
    assert [chunk.citation_id for chunk in family_roles] == ["seed:role:family:v1"]
    assert [chunk.citation_id for chunk in doctor_roles] == ["seed:role:doctor:v1"]


def test_unreviewed_free_web_and_whole_document_ingestion_are_blocked() -> None:
    draft = SeedKnowledgeChunk(
        chunk_id="draft",
        citation_id="draft-citation",
        title="Unreviewed draft",
        content="Not reviewed.",
        source_type="safety",
        source_origin="reviewed_guideline",
        content_scope="summary",
        version="0.1.0",
        applicable_roles=["family"],
        safety_notes=["draft"],
        review_status=ReviewStatus.DRAFT,
    )
    with pytest.raises(SeedKnowledgeIngestionError, match="Only reviewed"):
        SeedKnowledgeStore([draft])

    payload = {
        **draft.model_dump(mode="python"),
        "citation_id": "seed:unsafe:web:v1",
        "review_status": ReviewStatus.REVIEWED,
    }
    with pytest.raises(ValidationError):
        SeedKnowledgeChunk.model_validate({**payload, "source_origin": "free_web"})
    with pytest.raises(ValidationError):
        SeedKnowledgeChunk.model_validate({**payload, "content_scope": "full_document"})
    with pytest.raises(ValidationError):
        SeedKnowledgeChunk.model_validate({**payload, "content": "x" * 2000})

    with pytest.raises(SeedKnowledgeAccessError, match="cannot disable"):
        default_seed_knowledge_store().search(
            role="family", query="安全", reviewed_only=False
        )


def test_vector_search_remains_an_interface_not_the_v1_router() -> None:
    store = default_seed_knowledge_store()

    assert getattr(SeedKnowledgeIndex, "_is_protocol", False) is True
    assert not hasattr(store, "vector_index")
    assert store.router.__class__.__name__ == "SeedKnowledgeRouter"
