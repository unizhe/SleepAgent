from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from sleepagent.radar_agent.agents import (
    ContextPacket,
    DialogueAgent,
    EvidencePacket,
    RAGAgent,
    ReportAgent,
    TaskContext,
)
from sleepagent.radar_agent.rag import (
    GroundingCitationError,
    SeedKnowledgeAccessError,
    SeedKnowledgeChunk,
    SeedKnowledgeIndex,
    SeedKnowledgeIngestionError,
    SeedKnowledgeStore,
    default_seed_knowledge_store,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    RadarDataQualityStatus,
    RadarNightSummary,
    RagContext,
    ReviewStatus,
    RiskLevel,
)


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


def test_rag_agent_outputs_full_review_metadata_and_citation_ids() -> None:
    context = _context(
        purpose="rag",
        data_quality={"rag_query": "雷达数据质量和家属角色解释"},
    )
    result = RAGAgent().run(context)
    sources = result.output_payload["sources"]
    rag_context = result.output_payload["rag_context"]

    assert result.evidence_refs == rag_context["citation_ids"]
    assert all(ref.startswith("seed:") for ref in result.evidence_refs)
    assert all(source["review_status"] == "reviewed" for source in sources)
    assert all(source["version"] for source in sources)
    assert all(source["applicable_roles"] for source in sources)
    assert all(source["safety_notes"] for source in sources)
    assert "no_free_web_search" in result.safety_flags


def test_multi_role_report_rag_retrieves_and_filters_each_role_citation() -> None:
    rag_result = RAGAgent().run(
        _context(
            purpose="rag",
            data_quality={
                "rag_query": "三角色报告解释",
                "rag_roles": ["elder", "family", "doctor"],
            },
        )
    )
    rag_context = RagContext.model_validate(rag_result.output_payload["rag_context"])
    report_context = _context(purpose="report", rag_context=rag_context)
    reports = ReportAgent().run(report_context).output_payload["reports"]

    for report in reports:
        role_ref = f"seed:role:{report['role']}:v1"
        other_refs = {
            f"seed:role:{role}:v1"
            for role in {"elder", "family", "doctor"} - {report["role"]}
        }
        assert role_ref in report["evidence_refs"]
        assert not other_refs.intersection(report["evidence_refs"])
        assert "night-summary:radar-001:2026-07-10" in report["evidence_refs"]


def test_chat_cites_current_role_seed_or_evidence_ledger_only() -> None:
    rag_result = RAGAgent().run(
        _context(
            purpose="rag",
            data_quality={"rag_query": "家属角色解释"},
        )
    )
    context = _context(
        purpose="chat",
        rag_context=RagContext.model_validate(rag_result.output_payload["rag_context"]),
    )
    result = DialogueAgent().run(context)

    assert "seed:role:family:v1" in result.evidence_refs
    assert "seed:role:doctor:v1" not in result.evidence_refs
    assert result.output_payload["dialogue"]["evidence_refs"] == result.evidence_refs


@pytest.mark.parametrize("agent", [ReportAgent(), DialogueAgent()])
def test_report_and_chat_reject_ungrounded_output(agent) -> None:
    empty = EvidenceLedger(
        ledger_id="ledger-empty",
        task_id="task-rag",
        review_status=ReviewStatus.REVIEWED,
    )
    context = ContextPacket(
        task_context=TaskContext(
            task_id="task-rag",
            trace_id="trace-rag",
            role="family",
            purpose="report" if isinstance(agent, ReportAgent) else "chat",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=empty),
    )

    with pytest.raises(GroundingCitationError, match="require Evidence Ledger"):
        agent.run(context)


def test_vector_search_remains_an_interface_not_the_v1_router() -> None:
    store = default_seed_knowledge_store()

    assert getattr(SeedKnowledgeIndex, "_is_protocol", False) is True
    assert not hasattr(store, "vector_index")
    assert store.router.__class__.__name__ == "SeedKnowledgeRouter"


def _context(
    *,
    purpose: str,
    data_quality: dict | None = None,
    rag_context: RagContext | None = None,
) -> ContextPacket:
    ref = "night-summary:radar-001:2026-07-10"
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        data_coverage_ratio=0.95,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref=ref,
    )
    claim = EvidenceClaim(
        claim_id="claim-rag",
        task_id="task-rag",
        text="昨夜数据可用于健康观察。",
        evidence_refs=[ref],
        confidence=0.8,
        risk_level=RiskLevel.INFO,
        generated_by="radar_data",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-rag",
        task_id="task-rag",
        canonical_evidence_refs=[ref],
        derived_metrics={"risk_level": "info"},
        claims=[claim],
        confidence=0.8,
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-rag",
            trace_id="trace-rag",
            role="family",
            purpose=purpose,
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality=data_quality or {},
            evidence_ledger=ledger,
        ),
        rag_context=rag_context or RagContext(),
    )
