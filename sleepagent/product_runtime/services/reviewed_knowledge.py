from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, cast

from pydantic import Field, model_validator

from sleepagent.product_runtime.contracts import StrictContract
from sleepagent.product_runtime.knowledge.seed import (
    SeedKnowledgeStore,
    default_seed_knowledge_store,
)


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
