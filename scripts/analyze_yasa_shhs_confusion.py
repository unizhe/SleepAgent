import argparse
from typing import Any

from sleepagent.services import (
    analyze_yasa_shhs_confusion,
    build_yasa_confusion_analysis_payload,
    write_yasa_confusion_analysis_payload,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = analyze_yasa_shhs_confusion(
        yasa_summary_path=args.yasa_summary,
        shhs_xml_path=args.shhs_xml,
    )
    payload = build_yasa_confusion_analysis_payload(result)
    print_summary(payload)
    if args.out:
        output_path = write_yasa_confusion_analysis_payload(payload, args.out)
        print(f"Wrote Stage 3 YASA confusion analysis JSON: {output_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze confusion between Stage 3 YASA predictions and SHHS XML labels."
    )
    parser.add_argument("--yasa-summary", required=True, help="Stage 3 YASA summary JSON.")
    parser.add_argument("--shhs-xml", required=True, help="SHHS Profusion XML path.")
    parser.add_argument("--out", default=None, help="Optional JSON output path.")
    return parser.parse_args(argv)


def print_summary(payload: dict[str, Any]) -> None:
    print("Stage 3 YASA-vs-SHHS confusion analysis")
    print(f"- compared_epoch_count: {payload['compared_epoch_count']}")
    print(f"- total_error_count: {payload['total_error_count']}")
    print(f"- accuracy: {payload['accuracy']:.4f}")
    share = payload["rem_nrem_confusion_share_of_errors"]
    share_text = "n/a" if share is None else f"{share:.4f}"
    print(
        "- REM<->NREM confusion: "
        f"{payload['rem_nrem_confusion_count']} errors, share_of_errors={share_text}"
    )
    print("- confusion_counts rows=true columns=predicted:")
    labels = payload["labels"]
    print("  " + "\t".join(["true\\pred", *labels]))
    for true_label in labels:
        row = payload["confusion_counts"][true_label]
        print("  " + "\t".join([true_label, *(str(row[pred_label]) for pred_label in labels)]))
    print("- top_confusions:")
    if not payload["top_confusions"]:
        print("  none")
    for pair in payload["top_confusions"]:
        share_value = pair["share_of_errors"]
        pair_share = "n/a" if share_value is None else f"{share_value:.4f}"
        print(
            f"  true={pair['true_stage']} predicted={pair['predicted_stage']} "
            f"count={pair['count']} share_of_errors={pair_share}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
