from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas import (
    ArtifactType,
    ReportKnowledgeReviewStatus,
    ReportKnowledgeSourceType,
    RetrievedReportKnowledgeChunk,
    SafetyReviewStatus,
)


SAFETY_REVIEWER_ID = "sleepagent-safety-boundary-v1"

DIAGNOSTIC_ASSERTION_TERMS = (
    "确诊",
    "已经患有",
    "无需就医",
    "不用就医",
    "不用看医生",
    "没有呼吸异常",
    "不存在呼吸异常",
    "没有睡眠呼吸暂停",
    "diagnosed with",
    "confirmed diagnosis",
    "no need to see a doctor",
    "does not need medical review",
    "does not have sleep apnea",
)
URGENT_SYMPTOM_TERMS = (
    "胸痛",
    "严重呼吸困难",
    "意识异常",
    "晕厥",
    "chest pain",
    "severe breathing difficulty",
    "loss of consciousness",
    "syncope",
)
URGENT_BOUNDARY_TERMS = (
    "急诊",
    "及时就医",
    "医生",
    "医疗评估",
    "emergency",
    "urgent medical",
    "doctor",
    "medical care",
)
MODEL_BOUNDARY_TERMS = (
    "未验证",
    "不能作为阴性证据",
    "不作为阴性证据",
    "不能用于排除",
    "not validated",
    "not used as negative evidence",
    "must not use it as evidence",
)
DISCLAIMER_TERMS = (
    "不替代医生",
    "不替代诊断",
    "仅用于",
    "辅助分析",
    "not replace",
    "not a diagnosis",
)


class SafetyCheckScope(str, Enum):
    REPORT_ARTIFACT = "report_artifact"
    DIALOGUE_ANSWER = "dialogue_answer"
    RAG_CONTEXT = "rag_context"


class SafetyCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: SafetyCheckScope
    safety_review_status: SafetyReviewStatus
    safety_flags: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_by: str = SAFETY_REVIEWER_ID

    @property
    def passed(self) -> bool:
        return self.safety_review_status == SafetyReviewStatus.PASSED


def check_artifact_safety(
    content: str,
    *,
    artifact_type: ArtifactType,
    retrieved_chunks: list[RetrievedReportKnowledgeChunk] | None = None,
) -> SafetyCheckResult:
    result = _check_text_safety(
        content,
        scope=SafetyCheckScope.REPORT_ARTIFACT,
        require_disclaimer=_requires_report_disclaimer(artifact_type),
        check_model_boundary=True,
    )
    rag_result = check_rag_source_safety(retrieved_chunks or [])
    return _merge_results(result, rag_result)


def check_dialogue_safety(answer: str) -> SafetyCheckResult:
    return _check_text_safety(
        answer,
        scope=SafetyCheckScope.DIALOGUE_ANSWER,
        require_disclaimer=False,
        check_model_boundary=True,
    )


def check_rag_source_safety(
    retrieved_chunks: list[RetrievedReportKnowledgeChunk],
) -> SafetyCheckResult:
    flags: list[str] = []
    blocked_reasons: list[str] = []
    for item in retrieved_chunks:
        chunk = item.chunk
        if chunk.review_status != ReportKnowledgeReviewStatus.REVIEWED:
            blocked_reasons.append(
                f"rag_chunk_not_reviewed:{chunk.chunk_id}:{chunk.review_status.value}"
            )
        if chunk.source_type == ReportKnowledgeSourceType.INTERNAL_SEED:
            flags.append(f"rag_source_internal_seed:{chunk.chunk_id}")
    return _result(
        SafetyCheckScope.RAG_CONTEXT,
        flags=flags,
        blocked_reasons=blocked_reasons,
    )


def _check_text_safety(
    content: str,
    *,
    scope: SafetyCheckScope,
    require_disclaimer: bool,
    check_model_boundary: bool,
) -> SafetyCheckResult:
    normalized = content.lower()
    flags: list[str] = []
    blocked_reasons: list[str] = []

    matched_diagnostic_terms = [
        term for term in DIAGNOSTIC_ASSERTION_TERMS if term.lower() in normalized
    ]
    if matched_diagnostic_terms:
        blocked_reasons.append(
            "diagnostic_assertion:" + ",".join(matched_diagnostic_terms)
        )

    if _has_any(normalized, URGENT_SYMPTOM_TERMS):
        flags.append("urgent_symptom_safety_boundary")
        if not _has_any(normalized, URGENT_BOUNDARY_TERMS):
            blocked_reasons.append("urgent_symptom_without_medical_boundary")

    if require_disclaimer and not _has_any(normalized, DISCLAIMER_TERMS):
        blocked_reasons.append("missing_medical_disclaimer")

    if check_model_boundary and _mentions_respiratory_model_or_negative(normalized):
        if not _has_any(normalized, MODEL_BOUNDARY_TERMS):
            blocked_reasons.append("missing_unvalidated_model_caveat")

    return _result(scope, flags=flags, blocked_reasons=blocked_reasons)


def _requires_report_disclaimer(artifact_type: ArtifactType) -> bool:
    return artifact_type in {
        ArtifactType.ELDER_REPORT,
        ArtifactType.FAMILY_REPORT,
        ArtifactType.DOCTOR_REPORT,
        ArtifactType.TECHNICAL_REPORT,
        ArtifactType.CARE_PLAN,
        ArtifactType.RISK_SUMMARY,
        ArtifactType.EVIDENCE_CHAIN,
    }


def _mentions_respiratory_model_or_negative(normalized: str) -> bool:
    return (
        "respiratory model" in normalized
        or "呼吸模型" in normalized
        or "normal_breathing" in normalized
        or "没有呼吸" in normalized
        or "不存在呼吸" in normalized
        or "absent" in normalized and "respiratory" in normalized
    )


def _merge_results(
    primary: SafetyCheckResult,
    secondary: SafetyCheckResult,
) -> SafetyCheckResult:
    return _result(
        primary.scope,
        flags=[*primary.safety_flags, *secondary.safety_flags],
        blocked_reasons=[*primary.blocked_reasons, *secondary.blocked_reasons],
        reviewed_at=max(primary.reviewed_at, secondary.reviewed_at),
    )


def _result(
    scope: SafetyCheckScope,
    *,
    flags: list[str],
    blocked_reasons: list[str],
    reviewed_at: datetime | None = None,
) -> SafetyCheckResult:
    deduped_flags = _dedupe(flags)
    deduped_reasons = _dedupe(blocked_reasons)
    return SafetyCheckResult(
        scope=scope,
        safety_review_status=(
            SafetyReviewStatus.BLOCKED
            if deduped_reasons
            else SafetyReviewStatus.PASSED
        ),
        safety_flags=deduped_flags,
        blocked_reasons=deduped_reasons,
        reviewed_at=reviewed_at or datetime.now(timezone.utc),
    )


def _has_any(normalized: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in normalized for term in terms)


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped
