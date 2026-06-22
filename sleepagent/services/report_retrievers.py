from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from sleepagent.schemas import RetrievedReportKnowledgeChunk
from sleepagent.services.report_chroma import (
    DEFAULT_REPORT_CHROMA_COLLECTION,
    ChromaReportKnowledgeAdapter,
    HashEmbeddingFunction,
)
from sleepagent.services.report_knowledge import retrieve_report_knowledge


REPORT_RETRIEVER_ENV = "SLEEPAGENT_REPORT_RETRIEVER"
REPORT_CHROMA_DIR_ENV = "SLEEPAGENT_REPORT_CHROMA_DIR"
REPORT_CHROMA_COLLECTION_ENV = "SLEEPAGENT_REPORT_CHROMA_COLLECTION"


class ReportRetrieverMode(str, Enum):
    IN_MEMORY = "in_memory"
    CHROMA = "chroma"


class ReportKnowledgeRetriever(Protocol):
    def retrieve(
        self,
        query: str | Iterable[str],
        *,
        top_k: int = 3,
    ) -> list[RetrievedReportKnowledgeChunk]:
        ...


@dataclass(frozen=True)
class ReportRetrieverConfig:
    mode: ReportRetrieverMode = ReportRetrieverMode.IN_MEMORY
    chroma_persist_directory: Path | None = None
    chroma_collection_name: str = DEFAULT_REPORT_CHROMA_COLLECTION


class InMemoryReportKnowledgeRetriever:
    def retrieve(
        self,
        query: str | Iterable[str],
        *,
        top_k: int = 3,
    ) -> list[RetrievedReportKnowledgeChunk]:
        return retrieve_report_knowledge(query, top_k=top_k)


class ChromaReportKnowledgeRetriever:
    def __init__(
        self,
        *,
        persist_directory: str | Path | None,
        collection_name: str = DEFAULT_REPORT_CHROMA_COLLECTION,
        adapter: ChromaReportKnowledgeAdapter | None = None,
    ) -> None:
        self.adapter = adapter or ChromaReportKnowledgeAdapter(
            collection_name=collection_name,
            persist_directory=persist_directory,
            embedding_function=HashEmbeddingFunction(),
        )

    def retrieve(
        self,
        query: str | Iterable[str],
        *,
        top_k: int = 3,
    ) -> list[RetrievedReportKnowledgeChunk]:
        if isinstance(query, str):
            query_text = query
        else:
            query_text = " ".join(str(term) for term in query)
        return self.adapter.query(query_text, top_k=top_k)


def load_report_retriever_config_from_env(
    environ: dict[str, str] | None = None,
) -> ReportRetrieverConfig:
    values = os.environ if environ is None else environ
    mode = _parse_report_retriever_mode(values.get(REPORT_RETRIEVER_ENV, "in_memory"))
    persist_dir = values.get(REPORT_CHROMA_DIR_ENV)
    collection_name = values.get(
        REPORT_CHROMA_COLLECTION_ENV,
        DEFAULT_REPORT_CHROMA_COLLECTION,
    )
    return ReportRetrieverConfig(
        mode=mode,
        chroma_persist_directory=Path(persist_dir) if persist_dir else None,
        chroma_collection_name=collection_name,
    )


def build_report_knowledge_retriever(
    config: ReportRetrieverConfig | None = None,
) -> ReportKnowledgeRetriever:
    resolved = config or load_report_retriever_config_from_env()
    if resolved.mode == ReportRetrieverMode.IN_MEMORY:
        return InMemoryReportKnowledgeRetriever()
    if resolved.mode == ReportRetrieverMode.CHROMA:
        return ChromaReportKnowledgeRetriever(
            persist_directory=resolved.chroma_persist_directory,
            collection_name=resolved.chroma_collection_name,
        )
    raise ValueError(f"Unsupported report retriever mode: {resolved.mode}.")


def retrieve_report_context(
    query: str | Iterable[str],
    *,
    top_k: int = 3,
    retriever: ReportKnowledgeRetriever | None = None,
    config: ReportRetrieverConfig | None = None,
) -> list[RetrievedReportKnowledgeChunk]:
    selected_retriever = retriever or build_report_knowledge_retriever(config)
    return selected_retriever.retrieve(query, top_k=top_k)


def _parse_report_retriever_mode(raw_mode: str) -> ReportRetrieverMode:
    normalized = raw_mode.strip().lower().replace("-", "_")
    aliases = {
        "memory": ReportRetrieverMode.IN_MEMORY,
        "inmemory": ReportRetrieverMode.IN_MEMORY,
        "in_memory": ReportRetrieverMode.IN_MEMORY,
        "chroma": ReportRetrieverMode.CHROMA,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        allowed = ", ".join(mode.value for mode in ReportRetrieverMode)
        raise ValueError(
            f"Unsupported {REPORT_RETRIEVER_ENV} value {raw_mode!r}; "
            f"expected one of: {allowed}."
        ) from exc
