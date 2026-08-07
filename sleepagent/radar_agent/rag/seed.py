from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.schemas import RadarAgentSchema, ReviewStatus


SeedSourceType = Literal["safety", "device", "role_template", "personal_summary"]
SeedRole = Literal["elder", "family", "doctor", "system"]


class SeedKnowledgeIngestionError(ValueError):
    pass


class SeedKnowledgeAccessError(PermissionError):
    pass


class SeedKnowledgeChunk(RadarAgentSchema):
    chunk_id: str = Field(..., min_length=1)
    citation_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=160)
    content: str = Field(..., min_length=1, max_length=1600)
    source_type: SeedSourceType
    source_origin: Literal[
        "reviewed_guideline",
        "product_manual",
        "reviewed_role_template",
        "confirmed_personal_record",
    ]
    content_scope: Literal["excerpt", "summary", "template"]
    version: str = Field(..., min_length=1)
    applicable_roles: list[SeedRole] = Field(min_length=1)
    safety_notes: list[str] = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.DRAFT

    @model_validator(mode="after")
    def reviewed_chunks_require_traceable_metadata(self) -> "SeedKnowledgeChunk":
        if self.review_status == ReviewStatus.REVIEWED:
            if not self.citation_id.startswith("seed:"):
                raise ValueError("reviewed seed chunks require a seed: citation ID.")
            if not self.version or not self.applicable_roles or not self.safety_notes:
                raise ValueError("reviewed seed chunks require version, roles, and safety notes.")
        return self


class SeedKnowledgeRoute(RadarAgentSchema):
    query: str = ""
    source_types: list[SeedSourceType] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)


class SeedKnowledgeRouter:
    _ROUTES: dict[SeedSourceType, tuple[str, ...]] = {
        "safety": (
            "安全", "诊断", "就医", "急救", "药物", "psg", "ahi",
            "safety", "diagnosis", "emergency", "medical",
        ),
        "device": (
            "雷达", "设备", "离线", "覆盖率", "缺失", "质量", "体动",
            "radar", "device", "offline", "coverage", "missing", "quality",
        ),
        "role_template": (
            "老人", "家属", "医生", "报告", "解释", "话术", "角色",
            "elder", "family", "doctor", "report", "explain", "role",
        ),
        "personal_summary": (
            "长期", "历史", "趋势", "偏好", "确认记录", "7天", "30天", "90天",
            "long-term", "history", "trend", "preference", "personal",
        ),
    }

    def route(self, query: str | None) -> SeedKnowledgeRoute:
        normalized = (query or "").lower()
        source_types: list[SeedSourceType] = ["safety"]
        matched: list[str] = []
        for source_type, keywords in self._ROUTES.items():
            hits = [keyword for keyword in keywords if keyword in normalized]
            if hits:
                if source_type not in source_types:
                    source_types.append(source_type)
                matched.extend(hits)
        return SeedKnowledgeRoute(
            query=query or "",
            source_types=source_types,
            matched_keywords=list(dict.fromkeys(matched)),
        )


class SeedKnowledgeIndex(Protocol):
    """Reserved vector index interface; v1 uses SeedKnowledgeRouter instead."""

    def upsert_reviewed(self, chunks: list[SeedKnowledgeChunk]) -> None: ...

    def search_citation_ids(self, *, query: str, role: str, limit: int) -> list[str]: ...


class SeedKnowledgeStore:
    def __init__(
        self,
        chunks: list[SeedKnowledgeChunk] | None = None,
        *,
        router: SeedKnowledgeRouter | None = None,
    ) -> None:
        self._chunks: list[SeedKnowledgeChunk] = []
        self.router = router or SeedKnowledgeRouter()
        for chunk in chunks or []:
            self.add(chunk)

    def add(self, chunk: SeedKnowledgeChunk) -> None:
        if chunk.review_status != ReviewStatus.REVIEWED:
            raise SeedKnowledgeIngestionError(
                "Only reviewed seed knowledge may enter the v1 RAG store."
            )
        if chunk.content_scope not in {"excerpt", "summary", "template"}:
            raise SeedKnowledgeIngestionError(
                "Whole documents and papers cannot enter the v1 seed store."
            )
        if any(existing.citation_id == chunk.citation_id for existing in self._chunks):
            raise SeedKnowledgeIngestionError(
                f"Duplicate seed citation ID: {chunk.citation_id}."
            )
        self._chunks.append(chunk)

    def search(
        self,
        *,
        role: str | None = None,
        reviewed_only: bool = True,
        query: str | None = None,
        limit: int = 8,
    ) -> list[SeedKnowledgeChunk]:
        if not reviewed_only:
            raise SeedKnowledgeAccessError(
                "The v1 RAG boundary cannot disable reviewed-only filtering."
            )
        if role is not None and role not in {"elder", "family", "doctor", "system"}:
            raise SeedKnowledgeAccessError(f"Unsupported RAG role: {role}.")
        route = self.router.route(query)
        chunks = [
            chunk
            for chunk in self._chunks
            if chunk.review_status == ReviewStatus.REVIEWED
            and chunk.source_type in route.source_types
            and (role is None or role in chunk.applicable_roles)
        ]
        normalized = (query or "").lower()
        priority = {"safety": 0, "device": 1, "role_template": 2, "personal_summary": 3}
        chunks.sort(
            key=lambda chunk: (
                priority[chunk.source_type],
                -sum(1 for keyword in chunk.keywords if keyword.lower() in normalized),
                chunk.chunk_id,
            )
        )
        return chunks[:limit]

    def list_reviewed(self) -> list[SeedKnowledgeChunk]:
        return list(self._chunks)


def default_seed_knowledge_store() -> SeedKnowledgeStore:
    return SeedKnowledgeStore(
        [
            SeedKnowledgeChunk(
                chunk_id="safety-nondiagnostic-v1",
                citation_id="seed:safety:nondiagnostic:v1",
                title="睡眠观察的非诊断边界",
                content="毫米波雷达结果用于长期健康观察，不能替代临床诊断或 PSG；急症主诉应转线下医疗或急救评估。",
                source_type="safety",
                source_origin="reviewed_guideline",
                content_scope="summary",
                version="1.0.0",
                applicable_roles=["elder", "family", "doctor", "system"],
                safety_notes=["不得输出确诊、精确 AHI、处方或药物建议。"],
                keywords=["安全", "诊断", "急症", "PSG", "AHI"],
                review_status=ReviewStatus.REVIEWED,
            ),
            SeedKnowledgeChunk(
                chunk_id="device-quality-v1",
                citation_id="seed:device:data-quality:v1",
                title="雷达数据质量说明",
                content="设备离线、离床和缺失片段会降低可解释性，不能被解释为睡眠良好或健康异常。",
                source_type="device",
                source_origin="product_manual",
                content_scope="summary",
                version="1.0.0",
                applicable_roles=["elder", "family", "doctor", "system"],
                safety_notes=["数据不足时应少说并提示设备或采集检查。"],
                keywords=["雷达", "设备", "离线", "覆盖率", "缺失", "质量"],
                review_status=ReviewStatus.REVIEWED,
            ),
            _role_chunk(
                role="elder",
                title="老人版表达模板",
                content="使用短句、温和解释和一个主要建议，避免堆叠专业术语。",
            ),
            _role_chunk(
                role="family",
                title="家属版表达模板",
                content="说明趋势、数据质量、需要观察的原因和可确认的照护动作。",
            ),
            _role_chunk(
                role="doctor",
                title="医生版表达模板",
                content="保留结构化证据、趋势窗口、数据质量、限制和非诊断边界。",
            ),
            SeedKnowledgeChunk(
                chunk_id="personal-confirmed-summary-v1",
                citation_id="seed:personal:confirmed-summary:v1",
                title="个人长期记录使用边界",
                content="只检索已确认的 7/30/90 天趋势摘要、偏好和照护事件；没有确认记录时不得推测个人历史。",
                source_type="personal_summary",
                source_origin="confirmed_personal_record",
                content_scope="summary",
                version="1.0.0",
                applicable_roles=["elder", "family", "doctor", "system"],
                safety_notes=["不得存放原始雷达流、完整敏感对话或未确认个人信息。"],
                keywords=["长期", "历史", "趋势", "偏好", "7天", "30天", "90天"],
                review_status=ReviewStatus.REVIEWED,
            ),
        ]
    )


def _role_chunk(*, role: Literal["elder", "family", "doctor"], title: str, content: str) -> SeedKnowledgeChunk:
    return SeedKnowledgeChunk(
        chunk_id=f"role-{role}-v1",
        citation_id=f"seed:role:{role}:v1",
        title=title,
        content=content,
        source_type="role_template",
        source_origin="reviewed_role_template",
        content_scope="template",
        version="1.0.0",
        applicable_roles=[role],
        safety_notes=["只改变表达，不改变 Evidence Ledger 的事实或风险等级。"],
        keywords=[role, {"elder": "老人", "family": "家属", "doctor": "医生"}[role]],
        review_status=ReviewStatus.REVIEWED,
    )


__all__ = [
    "SeedKnowledgeAccessError",
    "SeedKnowledgeChunk",
    "SeedKnowledgeIndex",
    "SeedKnowledgeIngestionError",
    "SeedKnowledgeRoute",
    "SeedKnowledgeRouter",
    "SeedKnowledgeStore",
    "default_seed_knowledge_store",
]
