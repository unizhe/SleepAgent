from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from sleepagent.schemas import (
    ReportKnowledgeChunk,
    ReportKnowledgeReviewStatus,
    ReportKnowledgeSourceType,
    RetrievedReportKnowledgeChunk,
)


STAGE7_REPORT_KNOWLEDGE_SCHEMA_VERSION = "stage7.report_knowledge_chunk.v1"
INTERNAL_SEED_SOURCE = (
    "SleepAgent internal Stage 7 seed knowledge; replace with reviewed medical "
    "sources before clinical-facing use."
)

DEFAULT_REPORT_KNOWLEDGE_CHUNKS: tuple[ReportKnowledgeChunk, ...] = (
    ReportKnowledgeChunk(
        chunk_id="sleep-efficiency-basic",
        title="Sleep efficiency",
        content=(
            "Sleep efficiency describes the share of recording time spent asleep. "
            "A low value can reflect wakefulness during the night, fragmented sleep, "
            "or recording conditions, and should be interpreted with symptoms and "
            "the full PSG context."
        ),
        source=INTERNAL_SEED_SOURCE,
        source_type=ReportKnowledgeSourceType.INTERNAL_SEED,
        review_status=ReportKnowledgeReviewStatus.REVIEWED,
        topic_tags=["sleep_efficiency", "sleep_summary", "fragmented_sleep"],
        audience_tags=["elder", "professional"],
    ),
    ReportKnowledgeChunk(
        chunk_id="ahi-basic",
        title="AHI and breathing events",
        content=(
            "AHI summarizes suspected apnea and hypopnea events per hour of sleep. "
            "SleepAgent reports should describe AHI as a risk signal, not a standalone "
            "diagnosis, and should recommend professional review when risk is elevated."
        ),
        source=INTERNAL_SEED_SOURCE,
        source_type=ReportKnowledgeSourceType.INTERNAL_SEED,
        review_status=ReportKnowledgeReviewStatus.REVIEWED,
        topic_tags=["ahi", "hypopnea", "suspected_apnea", "respiratory_events"],
        audience_tags=["elder", "professional"],
        safety_notes=["Avoid diagnosing obstructive sleep apnea from the MVP output alone."],
    ),
    ReportKnowledgeChunk(
        chunk_id="urgent-symptoms-safety",
        title="Urgent symptom boundary",
        content=(
            "If chest pain, severe breathing difficulty, abnormal consciousness, or other "
            "acute high-risk symptoms appear, the report should advise timely medical care "
            "or emergency evaluation instead of lifestyle-only suggestions."
        ),
        source=INTERNAL_SEED_SOURCE,
        source_type=ReportKnowledgeSourceType.INTERNAL_SEED,
        review_status=ReportKnowledgeReviewStatus.REVIEWED,
        topic_tags=["medical_safety", "urgent_care", "high_risk"],
        audience_tags=["elder", "professional"],
        safety_notes=["Keep emergency advice clear for acute symptoms."],
    ),
    ReportKnowledgeChunk(
        chunk_id="stage6-demo-caveat",
        title="Stage 6 respiratory demo limitation",
        content=(
            "The current 20-record respiratory demo checkpoint is pipeline evidence only. "
            "Because it predicted normal breathing for every test window, downstream reports "
            "must not use it as evidence that respiratory abnormality is absent."
        ),
        source=INTERNAL_SEED_SOURCE,
        source_type=ReportKnowledgeSourceType.INTERNAL_SEED,
        review_status=ReportKnowledgeReviewStatus.REVIEWED,
        topic_tags=["stage6_demo", "model_limitation", "respiratory_model"],
        audience_tags=["professional"],
        safety_notes=["State model limitations when using Stage 6 demo artifacts."],
    ),
)


def retrieve_report_knowledge(
    query: str | Iterable[str],
    chunks: Sequence[ReportKnowledgeChunk] = DEFAULT_REPORT_KNOWLEDGE_CHUNKS,
    *,
    top_k: int = 3,
    include_dev_only: bool = False,
) -> list[RetrievedReportKnowledgeChunk]:
    """Return deterministic lexical matches for the Stage 7 RAG contract."""

    if top_k <= 0:
        raise ValueError("top_k must be greater than 0.")

    query_terms = _normalize_query_terms(query)
    if not query_terms:
        return []

    ranked: list[RetrievedReportKnowledgeChunk] = []
    for chunk in chunks:
        if (
            not include_dev_only
            and chunk.review_status != ReportKnowledgeReviewStatus.REVIEWED
        ):
            continue
        matched_terms = _matched_terms(query_terms, chunk)
        if not matched_terms:
            continue
        ranked.append(
            RetrievedReportKnowledgeChunk(
                chunk=chunk,
                score=float(len(matched_terms)),
                matched_terms=matched_terms,
            )
        )

    ranked.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
    return ranked[:top_k]


def _normalize_query_terms(query: str | Iterable[str]) -> list[str]:
    if isinstance(query, str):
        raw_terms = re.split(r"[^0-9A-Za-z_\u4e00-\u9fff]+", query)
    else:
        raw_terms = [str(term) for term in query]

    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        term = raw_term.strip().lower()
        if not term or term in seen:
            continue
        terms.append(term)
        seen.add(term)
    return terms


def _matched_terms(query_terms: Sequence[str], chunk: ReportKnowledgeChunk) -> list[str]:
    searchable_text = " ".join(
        [
            chunk.chunk_id,
            chunk.title,
            chunk.content,
            " ".join(chunk.topic_tags),
            " ".join(chunk.audience_tags),
            " ".join(chunk.safety_notes),
        ]
    ).lower()
    return [term for term in query_terms if term in searchable_text]
