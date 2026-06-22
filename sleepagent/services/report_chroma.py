from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sleepagent.schemas import (
    ReportKnowledgeChunk,
    ReportKnowledgeReviewStatus,
    RetrievedReportKnowledgeChunk,
)
from sleepagent.services.report_knowledge import (
    DEFAULT_REPORT_KNOWLEDGE_CHUNKS,
    STAGE7_REPORT_KNOWLEDGE_SCHEMA_VERSION,
)


DEFAULT_REPORT_CHROMA_COLLECTION = "sleepagent_report_knowledge"
DEFAULT_HASH_EMBEDDING_DIMENSIONS = 32


class ChromaUnavailableError(RuntimeError):
    """Raised when the optional Chroma dependency is needed but unavailable."""


@dataclass(frozen=True)
class ReportChromaSeedResult:
    persist_directory: Path | None
    collection_name: str
    indexed_chunk_count: int
    query: str
    retrieved_chunks: list[RetrievedReportKnowledgeChunk]


class HashEmbeddingFunction:
    """Small deterministic embedding function for local Chroma smoke tests."""

    def __init__(self, dimensions: int = DEFAULT_HASH_EMBEDDING_DIMENSIONS) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than 0.")
        self.dimensions = dimensions

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return [_hash_embedding(str(text), self.dimensions) for text in input]

    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        return self(input)

    def embed_documents(self, input: Sequence[str]) -> list[list[float]]:
        return self(input)

    def name(self) -> str:
        return "sleepagent_hash_embedding_v1"


class ChromaReportKnowledgeAdapter:
    """Thin boundary around Chroma for Stage 7 report knowledge retrieval.

    The adapter accepts an injected client for tests and lazy-loads `chromadb`
    only when it has to create a real client.
    """

    def __init__(
        self,
        *,
        collection_name: str = DEFAULT_REPORT_CHROMA_COLLECTION,
        persist_directory: str | Path | None = None,
        client: Any | None = None,
        embedding_function: Any | None = None,
    ) -> None:
        if not collection_name:
            raise ValueError("collection_name must be non-empty.")
        self.collection_name = collection_name
        self.persist_directory = Path(persist_directory) if persist_directory else None
        self._client = client
        self.embedding_function = embedding_function
        self._collection: Any | None = None

    @property
    def collection(self) -> Any:
        if self._collection is None:
            self._collection = self._get_or_create_collection()
        return self._collection

    def upsert_chunks(self, chunks: Sequence[ReportKnowledgeChunk]) -> int:
        if not chunks:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        for chunk in chunks:
            ids.append(chunk.chunk_id)
            documents.append(chunk.content)
            metadatas.append(_chunk_to_chroma_metadata(chunk))

        self.collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(chunks)

    def query(
        self,
        query: str,
        *,
        top_k: int = 3,
    ) -> list[RetrievedReportKnowledgeChunk]:
        if top_k <= 0:
            raise ValueError("top_k must be greater than 0.")
        if not query.strip():
            return []

        raw = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        return _retrieved_chunks_from_chroma_query(raw)

    def _get_or_create_collection(self) -> Any:
        client = self._client if self._client is not None else self._build_chroma_client()
        kwargs: dict[str, Any] = {"name": self.collection_name}
        if self.embedding_function is not None:
            kwargs["embedding_function"] = self.embedding_function
        return client.get_or_create_collection(**kwargs)

    def _build_chroma_client(self) -> Any:
        try:
            import chromadb
        except ImportError as exc:
            raise ChromaUnavailableError(
                "Chroma is not installed. Install SleepAgent with the optional "
                "`rag` extra before creating a real Chroma client."
            ) from exc

        if self.persist_directory is not None:
            return chromadb.PersistentClient(path=str(self.persist_directory))
        return chromadb.Client()


def seed_default_report_chroma_knowledge(
    *,
    persist_directory: str | Path | None = None,
    collection_name: str = DEFAULT_REPORT_CHROMA_COLLECTION,
    query: str = "ahi hypopnea suspected apnea",
    top_k: int = 3,
    chunks: Sequence[ReportKnowledgeChunk] = DEFAULT_REPORT_KNOWLEDGE_CHUNKS,
    client: Any | None = None,
    embedding_function: Any | None = None,
) -> ReportChromaSeedResult:
    adapter = ChromaReportKnowledgeAdapter(
        collection_name=collection_name,
        persist_directory=persist_directory,
        client=client,
        embedding_function=embedding_function or HashEmbeddingFunction(),
    )
    indexed_chunk_count = adapter.upsert_chunks(chunks)
    retrieved_chunks = adapter.query(query, top_k=top_k)
    return ReportChromaSeedResult(
        persist_directory=Path(persist_directory) if persist_directory else None,
        collection_name=collection_name,
        indexed_chunk_count=indexed_chunk_count,
        query=query,
        retrieved_chunks=retrieved_chunks,
    )


def _chunk_to_chroma_metadata(chunk: ReportKnowledgeChunk) -> dict[str, str]:
    return {
        "schema_version": chunk.schema_version,
        "chunk_id": chunk.chunk_id,
        "title": chunk.title,
        "source": chunk.source,
        "source_type": chunk.source_type.value,
        "review_status": chunk.review_status.value,
        "last_reviewed_at": (
            chunk.last_reviewed_at.isoformat() if chunk.last_reviewed_at else ""
        ),
        "topic_tags": json.dumps(chunk.topic_tags),
        "audience_tags": json.dumps(chunk.audience_tags),
        "safety_notes": json.dumps(chunk.safety_notes),
    }


def _retrieved_chunks_from_chroma_query(
    raw: dict[str, Any],
) -> list[RetrievedReportKnowledgeChunk]:
    ids = _first_result_row(raw.get("ids"))
    documents = _first_result_row(raw.get("documents"))
    metadatas = _first_result_row(raw.get("metadatas"))
    distances = _first_result_row(raw.get("distances"))

    results: list[RetrievedReportKnowledgeChunk] = []
    for index, metadata in enumerate(metadatas):
        document = documents[index] if index < len(documents) else ""
        distance = distances[index] if index < len(distances) else None
        fallback_id = ids[index] if index < len(ids) else ""
        chunk = _chunk_from_chroma_result(metadata, document, fallback_id)
        if chunk.review_status != ReportKnowledgeReviewStatus.REVIEWED:
            continue
        results.append(
            RetrievedReportKnowledgeChunk(
                chunk=chunk,
                score=_score_from_chroma_distance(distance),
                matched_terms=[],
            )
        )
    return results


def _chunk_from_chroma_result(
    metadata: dict[str, Any],
    document: str,
    fallback_id: str,
) -> ReportKnowledgeChunk:
    return ReportKnowledgeChunk(
        schema_version=str(
            metadata.get("schema_version") or STAGE7_REPORT_KNOWLEDGE_SCHEMA_VERSION
        ),
        chunk_id=str(metadata.get("chunk_id") or fallback_id),
        title=str(metadata.get("title") or metadata.get("chunk_id") or fallback_id),
        content=document,
        source=str(metadata.get("source") or "chroma"),
        source_type=str(metadata.get("source_type") or "internal_seed"),
        review_status=str(metadata.get("review_status") or "reviewed"),
        last_reviewed_at=(
            str(metadata.get("last_reviewed_at"))
            if metadata.get("last_reviewed_at")
            else None
        ),
        topic_tags=_json_list(metadata.get("topic_tags")),
        audience_tags=_json_list(metadata.get("audience_tags")),
        safety_notes=_json_list(metadata.get("safety_notes")),
    )


def _first_result_row(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list) and value and isinstance(value[0], list):
        return value[0]
    if isinstance(value, list):
        return value
    return []


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str):
        return [str(value)]
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value] if value else []
    if not isinstance(parsed, list):
        return [str(parsed)]
    return [str(item) for item in parsed]


def _score_from_chroma_distance(distance: Any) -> float:
    if distance is None:
        return 0.0
    try:
        numeric_distance = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if numeric_distance < 0:
        return 0.0
    return 1.0 / (1.0 + numeric_distance)


def _hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in text.lower().split():
        digest = sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], byteorder="big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        return vector
    return [value / norm for value in vector]
