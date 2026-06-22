from __future__ import annotations

import argparse
import json
from pathlib import Path

from sleepagent.services import (
    DEFAULT_REPORT_CHROMA_COLLECTION,
    ChromaUnavailableError,
    seed_default_report_chroma_knowledge,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed SleepAgent Stage 7 report knowledge into local Chroma."
    )
    parser.add_argument(
        "--persist-dir",
        type=Path,
        default=Path("../data/processed/sleepagent/stage7/report_chroma"),
        help="Directory for the local persistent Chroma database.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_REPORT_CHROMA_COLLECTION,
        help="Chroma collection name.",
    )
    parser.add_argument(
        "--query",
        default="ahi hypopnea suspected apnea",
        help="Smoke-test query to run after indexing.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Number of retrieved chunks to return for the smoke query.",
    )
    args = parser.parse_args()

    try:
        result = seed_default_report_chroma_knowledge(
            persist_directory=args.persist_dir,
            collection_name=args.collection,
            query=args.query,
            top_k=args.top_k,
        )
    except ChromaUnavailableError as exc:
        print(str(exc))
        return 2

    payload = {
        "persist_directory": str(result.persist_directory)
        if result.persist_directory is not None
        else None,
        "collection_name": result.collection_name,
        "indexed_chunk_count": result.indexed_chunk_count,
        "query": result.query,
        "retrieved_chunks": [
            {
                "chunk_id": item.chunk.chunk_id,
                "title": item.chunk.title,
                "score": item.score,
                "topic_tags": item.chunk.topic_tags,
            }
            for item in result.retrieved_chunks
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
