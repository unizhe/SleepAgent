from __future__ import annotations

from typing import Protocol

from sleepagent.persistence.models import (
    ObjectBlobReference,
    VectorDocument,
)


class RawRadarVectorWriteError(ValueError):
    """Raised when raw radar stream content is offered to vector storage."""


class VectorStore(Protocol):
    def upsert(self, documents: list[VectorDocument]) -> None:
        ...


class ObjectStore(Protocol):
    def put(self, reference: ObjectBlobReference, data: bytes) -> ObjectBlobReference:
        ...

    def get(self, reference: ObjectBlobReference) -> bytes:
        ...


def validate_vector_document(document: VectorDocument) -> VectorDocument:
    raw_markers = {
        "RadarRawEvent",
        "RawVendorEvent",
        "raw_payload",
        "data_payload",
        "raw_radar_stream",
    }
    metadata_keys = set(document.metadata)
    if metadata_keys & raw_markers:
        raise RawRadarVectorWriteError(
            "Raw radar streams and vendor payloads must not be written to vector DB."
        )
    text = document.text.lower()
    if "raw_payload" in text or "raw radar stream" in text:
        raise RawRadarVectorWriteError(
            "Raw radar streams and vendor payloads must not be written to vector DB."
        )
    return document


__all__ = [
    "ObjectStore",
    "RawRadarVectorWriteError",
    "VectorStore",
    "validate_vector_document",
]
