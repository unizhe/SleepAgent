import argparse
import json
from typing import Any

from sleepagent.services import (
    build_yasa_channel_comparison_payload,
    compare_yasa_channel_evaluations,
    write_yasa_channel_comparison_payload,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    candidates = dict(_parse_candidate(candidate) for candidate in args.candidate)
    result = compare_yasa_channel_evaluations(candidates)
    payload = build_yasa_channel_comparison_payload(result)
    print_summary(payload)
    if args.out:
        output_path = write_yasa_channel_comparison_payload(payload, args.out)
        print(f"Wrote Stage 3 YASA channel comparison JSON: {output_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Stage 3 YASA channel evaluation JSON payloads."
    )
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="Candidate in NAME=/path/to/evaluation.json format. Provide at least two.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional JSON output path. If omitted, only the ranking is printed.",
    )
    return parser.parse_args(argv)


def print_summary(payload: dict[str, Any]) -> None:
    print("Stage 3 YASA channel comparison")
    print(f"- recommended_channel: {payload['recommended_channel']}")
    print(f"- score_keys: {payload['score_keys']}")
    for index, candidate in enumerate(payload["candidates"], start=1):
        print(f"{index}. {candidate['name']}")
        print(f"   compared_epoch_count: {candidate['compared_epoch_count']}")
        print(f"   metrics: {json.dumps(candidate['metrics'], ensure_ascii=False)}")


def _parse_candidate(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("Candidate must use NAME=/path/to/evaluation.json format.")
    name, path = value.split("=", maxsplit=1)
    name = name.strip()
    path = path.strip()
    if not name or not path:
        raise ValueError("Candidate name and path cannot be empty.")
    return name, path


if __name__ == "__main__":
    raise SystemExit(main())
