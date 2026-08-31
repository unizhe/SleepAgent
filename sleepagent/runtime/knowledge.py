"""经审查知识、种子与 grounding。"""

from __future__ import annotations


# 合并自 knowledge/grounding.py。
from sleepagent.runtime.schemas import ContextPacket, EvidenceLedger


class GroundingCitationError(ValueError):
    pass


def grounded_citation_refs(
    context: ContextPacket,
    ledger: EvidenceLedger,
    *,
    role: str | None = None,
) -> list[str]:
    ledger_refs = [
        ref
        for ref in list(ledger.canonical_evidence_refs) + list(ledger.raw_evidence_refs)
        if not ref.startswith("seed:")
    ]
    ledger_refs.extend(ref for claim in ledger.claims for ref in claim.evidence_refs)
    seed_refs = _seed_refs_for_role(context, role)
    invalid_seed = [ref for ref in seed_refs if not ref.startswith("seed:")]
    if invalid_seed:
        raise GroundingCitationError(
            "RAG citations must use reviewed seed citation IDs: "
            + ", ".join(invalid_seed)
        )
    refs = list(dict.fromkeys([*ledger_refs, *seed_refs]))
    if not refs:
        raise GroundingCitationError(
            "Report and Chat output require Evidence Ledger or reviewed seed citations."
        )
    return refs


def _seed_refs_for_role(context: ContextPacket, role: str | None) -> list[str]:
    if role is None:
        return list(context.rag_context.citation_ids)
    allowed: list[str] = []
    for source in context.rag_context.source_metadata:
        citation_id = source.get("citation_id")
        roles = source.get("applicable_roles", [])
        if citation_id and role in roles:
            allowed.append(str(citation_id))
    return allowed


__all__ = ["GroundingCitationError", "grounded_citation_refs"]


# 合并自 knowledge/seed.py。
from typing import Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.runtime.schemas import RadarAgentSchema, ReviewStatus


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


# 评审知识仓储与查询服务与知识索引同属一个边界。

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, cast

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import StrictContract
REVIEWED_KNOWLEDGE_SERVICE_VERSION = "sleepagent-reviewed-knowledge.v1"

KnowledgeRole = Literal["elder", "family", "doctor", "system"]
KnowledgeSourceType = Literal[
    "safety",
    "device",
    "role_template",
    "personal_summary",
]
KnowledgeSourceOrigin = Literal[
    "reviewed_guideline",
    "product_manual",
    "reviewed_role_template",
    "confirmed_personal_record",
]
KnowledgeContentScope = Literal["excerpt", "summary", "template"]

_ALLOWED_SOURCE_TYPES = frozenset(
    {"safety", "device", "role_template", "personal_summary"}
)
_ALLOWED_SOURCE_ORIGINS = frozenset(
    {
        "reviewed_guideline",
        "product_manual",
        "reviewed_role_template",
        "confirmed_personal_record",
    }
)
_ALLOWED_CONTENT_SCOPES = frozenset({"excerpt", "summary", "template"})
_ALLOWED_ROLES = frozenset({"elder", "family", "doctor", "system"})
_SOURCE_PRIORITY = {
    "safety": 0,
    "device": 1,
    "role_template": 2,
    "personal_summary": 3,
}


class ReviewedKnowledgePolicyError(PermissionError):
    """Raised when repository output cannot cross the reviewed-only boundary."""


class ReviewedKnowledgeQuery(StrictContract):
    """Typed retrieval request; trust status is intentionally not caller input."""

    query: str = Field(default="", max_length=1600)
    roles: tuple[KnowledgeRole, ...] = Field(min_length=1, max_length=4)
    subject_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def require_unique_roles(self) -> "ReviewedKnowledgeQuery":
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("roles must be unique and ordered")
        return self


class RepositoryKnowledgeRecord(StrictContract):
    """Untrusted projection returned by a lower-level knowledge repository.

    The service validates all trust-bearing fields before constructing a
    reviewed output.  Deliberately broad string fields prevent an adapter from
    converting repository claims into trust merely through model parsing.
    """

    chunk_id: str = Field(..., min_length=1)
    citation_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=160)
    content: str = Field(..., min_length=1, max_length=1600)
    source_type: str = Field(..., min_length=1)
    source_origin: str = Field(..., min_length=1)
    content_scope: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    applicable_roles: tuple[str, ...] = Field(min_length=1)
    safety_notes: tuple[str, ...] = Field(min_length=1)
    keywords: tuple[str, ...] = ()
    review_status: str = Field(..., min_length=1)
    subject_id: str | None = Field(default=None, min_length=1)


class ReviewedKnowledgeSource(StrictContract):
    """Policy-validated source metadata exposed by retrieval."""

    chunk_id: str = Field(..., min_length=1)
    citation_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=160)
    source_type: KnowledgeSourceType
    source_origin: KnowledgeSourceOrigin
    content_scope: KnowledgeContentScope
    review_status: Literal["reviewed"] = "reviewed"
    version: str = Field(..., min_length=1)
    applicable_roles: tuple[KnowledgeRole, ...] = Field(min_length=1)
    safety_notes: tuple[str, ...] = Field(min_length=1)
    subject_id: str | None = Field(default=None, min_length=1)


class ReviewedKnowledgeResult(StrictContract):
    """Deterministic, citation-preserving reviewed knowledge projection."""

    service_version: str = REVIEWED_KNOWLEDGE_SERVICE_VERSION
    query: str
    roles: tuple[KnowledgeRole, ...]
    limit: int = Field(ge=1, le=20)
    subject_id: str | None = Field(default=None, min_length=1)
    sources: tuple[ReviewedKnowledgeSource, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    snippets: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    safety_flags: tuple[str, ...] = (
        "reviewed_repository_only",
        "no_free_web_search",
        "no_fact_mutation",
    )

    @model_validator(mode="after")
    def require_aligned_citation_projection(self) -> "ReviewedKnowledgeResult":
        item_count = len(self.sources)
        if not (
            item_count
            == len(self.chunk_ids)
            == len(self.citation_ids)
            == len(self.snippets)
        ):
            raise ValueError("knowledge result projections must remain aligned")
        if len(self.citation_ids) != len(set(self.citation_ids)):
            raise ValueError("knowledge result citations must be unique")
        if self.source_refs != self.citation_ids:
            raise ValueError("source_refs must match reviewed citation_ids")
        if tuple(source.chunk_id for source in self.sources) != self.chunk_ids:
            raise ValueError("chunk_ids must match source order")
        if tuple(source.citation_id for source in self.sources) != self.citation_ids:
            raise ValueError("citation_ids must match source order")
        return self


class ReviewedKnowledgeRepository(Protocol):
    """Read-only repository port. Implementations own storage and indexing."""

    def search_reviewed(
        self,
        *,
        query: str,
        role: KnowledgeRole,
        limit: int,
    ) -> Sequence[RepositoryKnowledgeRecord | Mapping[str, Any]]: ...


class SeedKnowledgeRepository:
    """Adapter that keeps the existing reviewed seed store as infrastructure."""

    def __init__(self, store: SeedKnowledgeStore | None = None) -> None:
        self._store = store or default_seed_knowledge_store()

    def search_reviewed(
        self,
        *,
        query: str,
        role: KnowledgeRole,
        limit: int,
    ) -> tuple[RepositoryKnowledgeRecord, ...]:
        chunks = self._store.search(
            role=role,
            reviewed_only=True,
            query=query,
            limit=limit,
        )
        return tuple(
            RepositoryKnowledgeRecord(
                chunk_id=chunk.chunk_id,
                citation_id=chunk.citation_id,
                title=chunk.title,
                content=chunk.content,
                source_type=(
                    "role_template"
                    if chunk.source_type == "personal_summary"
                    else chunk.source_type
                ),
                source_origin=(
                    "reviewed_role_template"
                    if chunk.source_origin == "confirmed_personal_record"
                    else chunk.source_origin
                ),
                content_scope=chunk.content_scope,
                version=chunk.version,
                applicable_roles=tuple(chunk.applicable_roles),
                safety_notes=tuple(chunk.safety_notes),
                keywords=tuple(chunk.keywords),
                review_status=chunk.review_status.value,
            )
            for chunk in chunks
        )


class ReviewedKnowledgeService:
    """Enforce trust policy and produce stable, role-filtered citations."""

    def __init__(self, repository: ReviewedKnowledgeRepository) -> None:
        self._repository = repository

    def retrieve(self, request: ReviewedKnowledgeQuery) -> ReviewedKnowledgeResult:
        selected: list[RepositoryKnowledgeRecord] = []
        seen_citations: set[str] = set()

        for role in request.roles:
            repository_records = self._repository.search_reviewed(
                query=request.query,
                role=role,
                limit=request.limit,
            )
            validated = [
                _validate_repository_record(record) for record in repository_records
            ]
            applicable = [
                record
                for record in validated
                if role in record.applicable_roles
                and (
                    record.source_type != "personal_summary"
                    or record.subject_id == request.subject_id
                )
            ]
            ordered = _safety_first(applicable)[: request.limit]
            for record in ordered:
                if record.citation_id in seen_citations:
                    continue
                seen_citations.add(record.citation_id)
                selected.append(record)

        sources = tuple(_reviewed_source(record) for record in selected)
        return ReviewedKnowledgeResult(
            query=request.query,
            roles=request.roles,
            limit=request.limit,
            subject_id=request.subject_id,
            sources=sources,
            chunk_ids=tuple(record.chunk_id for record in selected),
            citation_ids=tuple(record.citation_id for record in selected),
            source_refs=tuple(record.citation_id for record in selected),
            snippets=tuple(record.content for record in selected),
            caveats=tuple(
                note for record in selected for note in record.safety_notes
            ),
            uncertainties=() if selected else ("no_reviewed_knowledge_match",),
        )


def default_reviewed_knowledge_service() -> ReviewedKnowledgeService:
    return ReviewedKnowledgeService(SeedKnowledgeRepository())


def _validate_repository_record(
    value: RepositoryKnowledgeRecord | Mapping[str, Any],
) -> RepositoryKnowledgeRecord:
    record = (
        value
        if isinstance(value, RepositoryKnowledgeRecord)
        else RepositoryKnowledgeRecord.model_validate(value)
    )
    if record.review_status != "reviewed":
        raise ReviewedKnowledgePolicyError(
            "Repository record failed the reviewed-only trust policy."
        )
    if record.source_type not in _ALLOWED_SOURCE_TYPES:
        raise ReviewedKnowledgePolicyError(
            f"Unsupported reviewed knowledge source type: {record.source_type}."
        )
    if record.source_origin not in _ALLOWED_SOURCE_ORIGINS:
        raise ReviewedKnowledgePolicyError(
            f"Unsupported reviewed knowledge source origin: {record.source_origin}."
        )
    if record.content_scope not in _ALLOWED_CONTENT_SCOPES:
        raise ReviewedKnowledgePolicyError(
            f"Unsupported reviewed knowledge content scope: {record.content_scope}."
        )
    personal = (
        record.source_type == "personal_summary"
        or record.source_origin == "confirmed_personal_record"
    )
    if personal and not (
        record.source_type == "personal_summary"
        and record.source_origin == "confirmed_personal_record"
        and record.subject_id
    ):
        raise ReviewedKnowledgePolicyError(
            "Confirmed personal knowledge requires an exact subject identity."
        )
    if not personal and record.subject_id is not None:
        raise ReviewedKnowledgePolicyError(
            "General reviewed knowledge cannot claim a subject identity."
        )
    if any(role not in _ALLOWED_ROLES for role in record.applicable_roles):
        raise ReviewedKnowledgePolicyError(
            "Repository record contains an unsupported applicable role."
        )
    if not record.version.strip() or not all(
        note.strip() for note in record.safety_notes
    ):
        raise ReviewedKnowledgePolicyError(
            "Reviewed knowledge requires version and safety metadata."
        )
    return record


def _safety_first(
    records: Sequence[RepositoryKnowledgeRecord],
) -> list[RepositoryKnowledgeRecord]:
    indexed = enumerate(records)
    return [
        record
        for _, record in sorted(
            indexed,
            key=lambda item: (_SOURCE_PRIORITY[item[1].source_type], item[0]),
        )
    ]


def _reviewed_source(record: RepositoryKnowledgeRecord) -> ReviewedKnowledgeSource:
    return ReviewedKnowledgeSource(
        chunk_id=record.chunk_id,
        citation_id=record.citation_id,
        title=record.title,
        source_type=cast(KnowledgeSourceType, record.source_type),
        source_origin=cast(KnowledgeSourceOrigin, record.source_origin),
        content_scope=cast(KnowledgeContentScope, record.content_scope),
        version=record.version,
        applicable_roles=cast(tuple[KnowledgeRole, ...], record.applicable_roles),
        safety_notes=record.safety_notes,
        subject_id=record.subject_id,
    )


__all__ = [
    "KnowledgeContentScope",
    "KnowledgeRole",
    "KnowledgeSourceOrigin",
    "KnowledgeSourceType",
    "REVIEWED_KNOWLEDGE_SERVICE_VERSION",
    "RepositoryKnowledgeRecord",
    "ReviewedKnowledgePolicyError",
    "ReviewedKnowledgeQuery",
    "ReviewedKnowledgeRepository",
    "ReviewedKnowledgeResult",
    "ReviewedKnowledgeService",
    "ReviewedKnowledgeSource",
    "SeedKnowledgeRepository",
    "default_reviewed_knowledge_service",
]
