import argparse

from sleepagent.services import (
    analyze_yasa_batch_summary,
    build_yasa_batch_analysis_payload,
    write_yasa_batch_analysis_payload,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = analyze_yasa_batch_summary(
        args.batch_summary,
        thresholds={
            "accuracy": args.min_accuracy,
            "cohen_kappa": args.min_kappa,
            "macro_f1": args.min_macro_f1,
            "weighted_f1": args.min_weighted_f1,
        },
        lowest_limit=args.lowest_limit,
    )
    payload = build_yasa_batch_analysis_payload(result)
    print_human_summary(payload)
    if args.out:
        output_path = write_yasa_batch_analysis_payload(payload, args.out)
        print(f"Wrote batch analysis JSON: {output_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze Stage 3 YASA small-batch metric distributions."
    )
    parser.add_argument(
        "batch_summary",
        help="Path to batch_yasa_metrics_summary.json.",
    )
    parser.add_argument(
        "--out",
        help="Optional output path for the analysis JSON.",
    )
    parser.add_argument("--lowest-limit", type=int, default=5, help="Number of low records to list.")
    parser.add_argument("--min-accuracy", type=float, default=0.90)
    parser.add_argument("--min-kappa", type=float, default=0.80)
    parser.add_argument("--min-macro-f1", type=float, default=0.85)
    parser.add_argument("--min-weighted-f1", type=float, default=0.90)
    return parser.parse_args(argv)


def print_human_summary(payload: dict) -> None:
    print(f"Batch summary: {payload['batch_summary_path']}")
    print(f"Successes: {payload['success_count']}  Failures: {payload['failure_count']}")
    print("Metric distributions:")
    for metric, values in payload["metric_summary"].items():
        print(
            f"  {metric}: mean={_fmt(values['mean'])} "
            f"median={_fmt(values['median'])} min={_fmt(values['min'])} "
            f"max={_fmt(values['max'])}"
        )
    print("Per-class F1 distributions:")
    for label, values in payload["per_class_f1_summary"].items():
        print(
            f"  {label}: mean={_fmt(values['mean'])} "
            f"median={_fmt(values['median'])} min={_fmt(values['min'])} "
            f"max={_fmt(values['max'])}"
        )
    print("Lowest records:")
    for record in payload["lowest_records"]:
        metrics = record["metrics"]
        print(
            f"  {record['record_id']}: accuracy={_fmt(metrics.get('accuracy'))} "
            f"kappa={_fmt(metrics.get('cohen_kappa'))} "
            f"macro_f1={_fmt(metrics.get('macro_f1'))}"
        )
    print("Records below thresholds:")
    if not payload["records_below_thresholds"]:
        print("  none")
    for record in payload["records_below_thresholds"]:
        flags = ", ".join(
            f"{metric}={_fmt(flag['value'])}<{_fmt(flag['threshold'])}"
            for metric, flag in record["flags"].items()
        )
        print(f"  {record['record_id']}: {flags}")
    if payload["failures"]:
        print("Failures:")
        for failure in payload["failures"]:
            print(f"  {failure.get('record_id')}: {failure.get('error_type')} {failure.get('error')}")


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
