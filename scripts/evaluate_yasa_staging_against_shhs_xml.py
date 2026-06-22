import argparse
import json
from typing import Any

from sleepagent.services import (
    build_yasa_shhs_evaluation_payload,
    evaluate_yasa_summary_against_shhs_xml,
    write_yasa_shhs_evaluation_payload,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = evaluate_yasa_summary_against_shhs_xml(
        yasa_summary_path=args.yasa_summary,
        shhs_xml_path=args.shhs_xml,
    )
    payload = build_yasa_shhs_evaluation_payload(result)
    print_summary(payload)
    if args.out:
        output_path = write_yasa_shhs_evaluation_payload(payload, args.out)
        print(f"Wrote Stage 3 YASA-vs-SHHS evaluation JSON: {output_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Stage 3 YASA staging output against SHHS XML sleep stages."
    )
    parser.add_argument(
        "--yasa-summary",
        required=True,
        help="Path to a JSON payload created by run_yasa_sleep_staging_sample.py.",
    )
    parser.add_argument(
        "--shhs-xml",
        required=True,
        help="Path to a local authorized SHHS XML annotation file.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional JSON output path. If omitted, only metrics are printed.",
    )
    return parser.parse_args(argv)


def print_summary(payload: dict[str, Any]) -> None:
    metrics = payload["metrics"]
    print("Stage 3 YASA-vs-SHHS evaluation")
    print(f"- shhs_stage_source: {payload['shhs_stage_source']}")
    print(f"- yasa_epoch_count: {payload['yasa_epoch_count']}")
    print(f"- shhs_epoch_count: {payload['shhs_epoch_count']}")
    print(f"- compared_epoch_count: {payload['compared_epoch_count']}")
    print(f"- dropped_yasa_epochs: {payload['dropped_yasa_epochs']}")
    print(f"- dropped_shhs_epochs: {payload['dropped_shhs_epochs']}")
    print(f"- accuracy: {metrics['accuracy']}")
    print(f"- cohen_kappa: {metrics['cohen_kappa']}")
    print(f"- macro_f1: {metrics['macro_f1']}")
    print(f"- weighted_f1: {metrics['weighted_f1']}")
    print(f"- per_class_f1: {json.dumps(metrics['per_class_f1'], ensure_ascii=False)}")


if __name__ == "__main__":
    raise SystemExit(main())
