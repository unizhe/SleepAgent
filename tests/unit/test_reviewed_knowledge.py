from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest
from pydantic import ValidationError

from sleepagent.product_runtime.services.reviewed_knowledge import (
    RepositoryKnowledgeRecord,
    ReviewedKnowledgePolicyError,
    ReviewedKnowledgeQuery,
    ReviewedKnowledgeRepository,
    ReviewedKnowledgeService,
)
from sleepagent.product_runtime.tools.knowledge_retrieval import (
    KnowledgeRetrievalTool,
)
from tests.support.golden_fixtures import load_product_capability_goldens


@pytest.mark.parametrize(
    ("query", "roles"),
    [
        ("雷达设备离线且覆盖率不足", ("family",)),
        ("如何写家属角色报告", ("family",)),
        ("查看30天长期趋势和个人偏好", ("family",)),
        ("完全无关的园艺问题", ("family",)),
        ("三角色报告解释", ("elder", "family", "doctor")),
    ],
)
def test_reviewed_knowledge_tool_preserves_legacy_routing_and_metadata(
    query: str,
    roles: tuple[str, ...],
) -> None:
    expected = load_product_capability_goldens()["reviewed_knowledge"][
        f"{query}|{','.join(roles)}"
    ]
    result = KnowledgeRetrievalTool().retrieve(
        ReviewedKnowledgeQuery(query=query, roles=roles, limit=8)
    )

    assert result.chunk_ids == tuple(expected["chunk_ids"])
    assert result.citation_ids == tuple(expected["citation_ids"])
    assert result.source_refs == result.citation_ids
    assert result.caveats == tuple(expected["caveats"])
    assert [source.source_type for source in result.sources] == expected[
        "source_types"
    ]
    assert len(result.snippets) == len(result.sources)
    assert all(source.review_status == "reviewed" for source in result.sources)
    assert all(source.version for source in result.sources)
    assert all(source.applicable_roles for source in result.sources)
    assert all(source.safety_notes for source in result.sources)
    assert all(
        source.source_origin
        in {
            "reviewed_guideline",
            "product_manual",
            "reviewed_role_template",
            "confirmed_personal_record",
        }
        for source in result.sources
    )
    assert result.safety_flags == (
        "reviewed_repository_only",
        "no_free_web_search",
        "no_fact_mutation",
    )
    personal_boundary = next(
        (
            source
            for source in result.sources
            if source.citation_id == "seed:personal:confirmed-summary:v1"
        ),
        None,
    )
    if personal_boundary is not None:
        assert personal_boundary.source_type == "role_template"
        assert personal_boundary.source_origin == "reviewed_role_template"
        assert personal_boundary.subject_id is None


def test_role_filtering_and_cross_role_citation_dedupe_are_stable() -> None:
    tool = KnowledgeRetrievalTool()

    result = tool.retrieve(
        ReviewedKnowledgeQuery(
            query="三角色报告话术",
            roles=("elder", "family", "doctor"),
            limit=8,
        )
    )

    assert result.citation_ids == (
        "seed:safety:nondiagnostic:v1",
        "seed:role:elder:v1",
        "seed:role:family:v1",
        "seed:role:doctor:v1",
    )
    assert len(result.citation_ids) == len(set(result.citation_ids))
    role_sources = [
        source
        for source in result.sources
        if source.source_type == "role_template"
    ]
    assert [source.applicable_roles for source in role_sources] == [
        ("elder",),
        ("family",),
        ("doctor",),
    ]


def test_limit_is_applied_per_role_without_breaking_safety_first_routing() -> None:
    result = KnowledgeRetrievalTool().retrieve(
        ReviewedKnowledgeQuery(
            query="三角色报告话术",
            roles=("elder", "family", "doctor"),
            limit=1,
        )
    )

    assert result.citation_ids == ("seed:safety:nondiagnostic:v1",)
    assert result.sources[0].source_type == "safety"


def test_caller_cannot_self_attest_review_status() -> None:
    with pytest.raises(ValidationError, match="reviewed"):
        ReviewedKnowledgeQuery.model_validate(
            {
                "query": "安全",
                "roles": ["family"],
                "limit": 8,
                "reviewed": True,
            }
        )


@pytest.mark.parametrize(
    ("review_status", "source_origin", "expected"),
    [
        ("draft", "reviewed_guideline", "reviewed-only"),
        ("reviewed", "free_web", "source origin"),
    ],
)
def test_service_rejects_untrusted_repository_records(
    review_status: str,
    source_origin: str,
    expected: str,
) -> None:
    repository = _StaticRepository(
        [
            RepositoryKnowledgeRecord(
                chunk_id="unsafe",
                citation_id="unsafe:source",
                title="Unsafe source",
                content="Must not cross the reviewed knowledge boundary.",
                source_type="safety",
                source_origin=source_origin,
                content_scope="summary",
                version="1.0.0",
                applicable_roles=("family",),
                safety_notes=("fail closed",),
                keywords=(),
                review_status=review_status,
            )
        ]
    )

    with pytest.raises(ReviewedKnowledgePolicyError, match=expected):
        ReviewedKnowledgeService(repository).retrieve(
            ReviewedKnowledgeQuery(query="安全", roles=("family",), limit=8)
        )


def test_tool_output_has_no_agent_identity_or_free_web_escape_hatch() -> None:
    result = KnowledgeRetrievalTool().retrieve(
        ReviewedKnowledgeQuery(query="安全", roles=("family",), limit=8)
    )

    assert not hasattr(result, "agent_name")
    assert not hasattr(result, "reviewed")
    assert result.tool_version == "sleepagent-knowledge-retrieval-tool.v1"
    assert "no_free_web_search" in result.safety_flags


def test_personal_knowledge_record_is_subject_bound() -> None:
    repository = _StaticRepository(
        [
            RepositoryKnowledgeRecord(
                chunk_id="personal-other",
                citation_id="personal:other:v1",
                title="Confirmed personal summary",
                content="A subject-bound confirmed summary.",
                source_type="personal_summary",
                source_origin="confirmed_personal_record",
                content_scope="summary",
                version="1.0.0",
                applicable_roles=("family",),
                safety_notes=("subject-bound",),
                review_status="reviewed",
                subject_id="other-subject",
            )
        ]
    )

    result = ReviewedKnowledgeService(repository).retrieve(
        ReviewedKnowledgeQuery(
            query="history",
            roles=("family",),
            subject_id="current-subject",
        )
    )

    assert result.sources == ()


def test_personal_knowledge_record_requires_subject_identity() -> None:
    repository = _StaticRepository(
        [
            RepositoryKnowledgeRecord(
                chunk_id="personal-unbound",
                citation_id="personal:unbound:v1",
                title="Unbound personal summary",
                content="Must fail closed.",
                source_type="personal_summary",
                source_origin="confirmed_personal_record",
                content_scope="summary",
                version="1.0.0",
                applicable_roles=("family",),
                safety_notes=("subject-bound",),
                review_status="reviewed",
            )
        ]
    )

    with pytest.raises(ReviewedKnowledgePolicyError, match="subject"):
        ReviewedKnowledgeService(repository).retrieve(
            ReviewedKnowledgeQuery(
                query="history",
                roles=("family",),
                subject_id="current-subject",
            )
        )


def test_new_production_modules_do_not_import_legacy_agent_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    source_files = (
        root
        / "sleepagent/product_runtime/services/reviewed_knowledge.py",
        root
        / "sleepagent/product_runtime/tools/knowledge_retrieval.py",
    )
    forbidden = (
        "radar_agent.agents",
        "radar_agent.orchestrator",
        "radar_agent.dynamic",
        "A2A",
        "RadarAgentName",
        "AgentResult",
    )

    for source_file in source_files:
        source = source_file.read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden)


class _StaticRepository(ReviewedKnowledgeRepository):
    def __init__(self, records: Sequence[RepositoryKnowledgeRecord]) -> None:
        self._records = tuple(records)

    def search_reviewed(
        self,
        *,
        query: str,
        role: str,
        limit: int,
    ) -> Sequence[RepositoryKnowledgeRecord]:
        del query, role
        return self._records[:limit]
