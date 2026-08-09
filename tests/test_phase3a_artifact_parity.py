from __future__ import annotations

from datetime import date

import pytest

from sleepagent.radar_agent.product_agent.contracts import AgentId
from sleepagent.radar_agent.product_agent.skill_methods.role_material import (
    RoleMaterialExpressionDraft,
    SleepCareRoleMaterialSkill,
)
from sleepagent.radar_agent.product_agent.tools.artifact_rendering import (
    ArtifactRenderRequest,
    ArtifactRenderingTool,
)
from sleepagent.radar_agent.schemas import (
    ContextPacket,
    EvidenceClaim,
    EvidenceLedger,
    EvidencePacket,
    QuestionnaireEntry,
    RadarDataQualityStatus,
    RadarDeviceStatus,
    RadarNightSummary,
    RagContext,
    ReviewStatus,
    RiskLevel,
    TaskContext,
)
from tests.golden_fixtures import (
    canonical_json_sha256,
    load_phase3a_capability_goldens,
)


AUDIENCES = ("elder", "family", "doctor")


@pytest.mark.parametrize("audience_role", AUDIENCES)
def test_single_audience_artifact_render_matches_no_model_report_agent(
    audience_role: str,
) -> None:
    context = _context()
    ledger = _ledger(context)
    expected = load_phase3a_capability_goldens()["artifact_rendering"][audience_role]

    rendered = ArtifactRenderingTool().render(
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=ledger,
            audience_role=audience_role,
        )
    )

    assert rendered.audience_role == audience_role
    artifact_payload = _without_volatile_timestamps(
        rendered.artifact.model_dump(mode="json")
    )
    assert canonical_json_sha256(artifact_payload) == expected["sha256"]
    assert rendered.artifact.claim_ids == expected["claim_ids"]
    assert rendered.artifact.risk_level.value == expected["risk_level"]
    assert rendered.source_refs == expected["source_refs"]
    assert rendered.committed is False
    assert rendered.exported is False


def _without_volatile_timestamps(value):
    if isinstance(value, dict):
        return {
            key: _without_volatile_timestamps(item)
            for key, item in value.items()
            if key not in {"generated_at", "collected_at"}
        }
    if isinstance(value, list):
        return [_without_volatile_timestamps(item) for item in value]
    return value


def test_artifact_render_preserves_doctor_completeness_and_safety_boundaries() -> None:
    context = _context()
    doctor = ArtifactRenderingTool().render(
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=_ledger(context),
            audience_role="doctor",
        )
    ).artifact

    assert doctor.claim_ids == ["claim-trend", "claim-risk"]
    assert doctor.evidence_refs == [
        "night-summary:phase3a-role-material",
        "source:questionnaire:phase3a",
    ]
    assert doctor.risk_level is RiskLevel.ESCALATE
    assert doctor.trend_highlights == [
        "近七晚夜间离床次数较个人基线上升。"
    ]
    assert doctor.facts[0].source_kind == "trend_tool"
    assert {
        claim.generated_by for claim in doctor.facts
    } == {AgentId.EVIDENCE_REASONING.value}
    assert "source_kind" not in doctor.facts[1].model_dump(mode="json")
    assert "数据存在缺失区间。" in doctor.caveats
    assert any("不能替代 PSG" in item for item in doctor.caveats)
    assert doctor.structured_summary["evidence_chain"] == [
        item.model_dump(mode="json") for item in doctor.facts
    ]
    assert doctor.structured_summary["data_quality"] == doctor.data_quality
    assert doctor.structured_summary["questionnaire_entries"] == [
        item.model_dump(mode="json") for item in doctor.questionnaire_entries
    ]
    assert doctor.structured_summary["source_refs"] == doctor.source_refs
    assert doctor.structured_summary["caveats"] == doctor.caveats
    assert doctor.structured_summary["safety_notices"] == doctor.safety_notices
    assert doctor.structured_summary["non_diagnostic_boundary"] is True


def test_artifact_render_requires_the_exact_context_ledger() -> None:
    context = _context()
    other = _ledger(context).model_copy(update={"ledger_id": "ledger:other"})

    with pytest.raises(ValueError, match="exact EvidenceLedger"):
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=other,
            audience_role="family",
        )


def test_artifact_render_rejects_unbound_rag_context() -> None:
    context = _context().model_copy(
        update={
            "rag_context": RagContext(
                chunk_ids=["chunk:forged"],
                citation_ids=["citation:forged"],
                snippets=["unreviewed knowledge injection"],
                source_metadata=[{"source_id": "forged"}],
            )
        }
    )

    with pytest.raises(ValueError, match="Knowledge receipt binding"):
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=_ledger(context),
            audience_role="family",
        )


def test_context_contract_rejects_retired_collaboration_payloads() -> None:
    payload = _context().model_dump(mode="python")
    payload["a2a_messages"] = []

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ContextPacket.model_validate(payload)


@pytest.mark.parametrize("generated_by", ("trend", "risk_signal", "report"))
def test_artifact_render_rejects_legacy_claim_owner(generated_by: str) -> None:
    context = _context()
    ledger = _ledger(context)
    claims = [
        ledger.claims[0].model_copy(update={"generated_by": generated_by}),
        *ledger.claims[1:],
    ]
    ledger = ledger.model_copy(update={"claims": claims})
    context = context.model_copy(
        update={
            "evidence_packet": context.evidence_packet.model_copy(
                update={"evidence_ledger": ledger}
            )
        }
    )

    with pytest.raises(ValueError, match="EvidenceReasoning owner"):
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=ledger,
            audience_role="doctor",
        )


def test_artifact_render_rejects_claim_from_another_task() -> None:
    context = _context()
    ledger = _ledger(context)
    claims = [
        ledger.claims[0].model_copy(update={"task_id": "another-task"}),
        *ledger.claims[1:],
    ]
    ledger = ledger.model_copy(update={"claims": claims})
    context = context.model_copy(
        update={
            "evidence_packet": context.evidence_packet.model_copy(
                update={"evidence_ledger": ledger}
            )
        }
    )

    with pytest.raises(ValueError, match="claim task identity"):
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=ledger,
            audience_role="family",
        )


def test_artifact_render_requires_reviewed_ledger_and_claims() -> None:
    context = _context()
    ledger = _ledger(context)

    with pytest.raises(ValueError, match="reviewed EvidenceLedger"):
        ArtifactRenderRequest(
            context=context.model_copy(
                update={
                    "evidence_packet": context.evidence_packet.model_copy(
                        update={
                            "evidence_ledger": ledger.model_copy(
                                update={"review_status": ReviewStatus.DRAFT}
                            )
                        }
                    )
                }
            ),
            evidence_ledger=ledger.model_copy(
                update={"review_status": ReviewStatus.DRAFT}
            ),
            audience_role="family",
        )

    draft_claim = ledger.claims[0].model_copy(
        update={"review_status": ReviewStatus.DRAFT}
    )
    draft_ledger = ledger.model_copy(
        update={"claims": [draft_claim, *ledger.claims[1:]]}
    )
    draft_context = context.model_copy(
        update={
            "evidence_packet": context.evidence_packet.model_copy(
                update={"evidence_ledger": draft_ledger}
            )
        }
    )
    with pytest.raises(ValueError, match="reviewed claims"):
        ArtifactRenderRequest(
            context=draft_context,
            evidence_ledger=draft_ledger,
            audience_role="family",
        )


def test_artifact_render_rejects_risk_downgrade_below_claim_floor() -> None:
    context = _context()
    ledger = _ledger(context).model_copy(
        update={"derived_metrics": {"risk_level": "info"}}
    )
    context = context.model_copy(
        update={
            "evidence_packet": context.evidence_packet.model_copy(
                update={"evidence_ledger": ledger}
            )
        }
    )

    with pytest.raises(ValueError, match="risk floor"):
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=ledger,
            audience_role="family",
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("claim_ids", ["claim:invented"]),
        ("evidence_refs", ["source:invented"]),
        ("risk_level", RiskLevel.INFO),
        ("caveats", ["caveat removed"]),
    ),
)
def test_sleepcare_role_material_expression_fails_closed_on_fact_drift(
    field: str,
    replacement: object,
) -> None:
    context = _context()
    rendered = ArtifactRenderingTool().render(
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=_ledger(context),
            audience_role="doctor",
        )
    )
    artifact = rendered.artifact
    draft = RoleMaterialExpressionDraft.from_artifact(
        artifact,
        tone="clinical",
    ).model_copy(update={field: replacement})

    skill = SleepCareRoleMaterialSkill(audience_role="doctor")
    assert skill.owner is AgentId.SLEEP_CARE
    assert skill.skill_id == "draft_doctor_material"
    with pytest.raises(ValueError, match="cannot modify"):
        skill.draft(rendered, draft)


def test_sleepcare_role_material_expression_changes_only_role_presentation() -> None:
    context = _context()
    rendered = ArtifactRenderingTool().render(
        ArtifactRenderRequest(
            context=context,
            evidence_ledger=_ledger(context),
            audience_role="family",
        )
    )
    artifact = rendered.artifact
    skill = SleepCareRoleMaterialSkill(audience_role="family")

    expressed = skill.draft(
        rendered,
        RoleMaterialExpressionDraft.from_artifact(artifact, tone="warm"),
    )

    assert skill.owner is AgentId.SLEEP_CARE
    assert skill.skill_id == "draft_user_material"
    assert expressed.title != artifact.title
    assert expressed.content != artifact.content
    for field in (
        "source_ledger_id",
        "risk_level",
        "claim_ids",
        "facts",
        "evidence_refs",
        "source_refs",
        "trend_highlights",
        "anomaly_highlights",
        "confirmation_actions",
        "data_quality",
        "questionnaire_entries",
        "structured_summary",
        "caveats",
        "safety_notices",
    ):
        assert getattr(expressed, field) == getattr(artifact, field)
    assert all(claim.text in expressed.content for claim in artifact.facts)
    assert all(item in expressed.content for item in artifact.safety_notices)


def _ledger(context: ContextPacket) -> EvidenceLedger:
    ledger = context.evidence_packet.evidence_ledger
    assert ledger is not None
    return ledger


def _context() -> ContextPacket:
    task_id = "task-phase3a-role-material"
    summary = RadarNightSummary(
        radar_device_id="radar-phase3a",
        subject_id="elder-phase3a",
        night_of=date(2026, 7, 11),
        device_status=RadarDeviceStatus.ONLINE,
        total_sleep_minutes=315,
        out_of_bed_count=6,
        movement_count=35,
        data_coverage_ratio=0.78,
        data_quality_status=RadarDataQualityStatus.PARTIAL,
        confidence_label="low_confidence",
        invalid_reading_count=4,
        missing_intervals=["02:10-02:35"],
        quality_reasons=["夜间存在短时缺失。"],
        caveats=["数据存在缺失区间。"],
        source_report_ref="night-summary:phase3a-role-material",
    )
    questionnaire = QuestionnaireEntry(
        entry_id="questionnaire:phase3a:001",
        subject_id="elder-phase3a",
        role="family",
        question_id="q-daytime-sleepiness",
        answer="明显",
        prompt_text="近期白天是否明显困倦？",
        evidence_ref="source:questionnaire:phase3a",
    )
    claims = [
        EvidenceClaim(
            claim_id="claim-trend",
            task_id=task_id,
            text="近七晚夜间离床次数较个人基线上升。",
            source_kind="trend_tool",
            evidence_refs=["night-summary:phase3a-role-material"],
            confidence=0.76,
            risk_level=RiskLevel.WATCH,
            caveats=["趋势受部分缺失数据影响。"],
            generated_by=AgentId.EVIDENCE_REASONING.value,
            review_status=ReviewStatus.REVIEWED,
        ),
        EvidenceClaim(
            claim_id="claim-risk",
            task_id=task_id,
            text=(
                "多项连续观察线索叠加，"
                "建议整理材料进一步评估。"
            ),
            evidence_refs=[
                "night-summary:phase3a-role-material",
                "source:questionnaire:phase3a",
            ],
            confidence=0.71,
            risk_level=RiskLevel.ESCALATE,
            caveats=["该线索不构成临床诊断。"],
            generated_by=AgentId.EVIDENCE_REASONING.value,
            review_status=ReviewStatus.REVIEWED,
        ),
    ]
    ledger = EvidenceLedger(
        ledger_id="ledger-phase3a-role-material",
        task_id=task_id,
        canonical_evidence_refs=[
            "night-summary:phase3a-role-material",
            "source:questionnaire:phase3a",
        ],
        derived_metrics={
            "risk_level": "escalate",
            "data_quality_status": "partial",
            "data_coverage_ratio": 0.78,
        },
        questionnaire_entries=[questionnaire],
        claims=claims,
        confidence=0.71,
        uncertainty="部分缺失区间降低趋势置信度。",
        caveats=["毫米波雷达仅用于健康观察。"],
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        context_packet_id="context:phase3a:role-material",
        task_context=TaskContext(
            task_id=task_id,
            trace_id="trace-phase3a-role-material",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            questionnaire_entries=[questionnaire],
            evidence_ledger=ledger,
        ),
    )
