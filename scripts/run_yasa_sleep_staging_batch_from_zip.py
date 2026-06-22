import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from sleepagent.preprocessing import (
    SHHSZipRecord,
    build_shhs_zip_record_index,
    extract_shhs_zip_record,
    select_complete_shhs_zip_records,
)
from sleepagent.services import (
    build_yasa_runner_payload,
    build_yasa_shhs_evaluation_payload,
    evaluate_yasa_summary_against_shhs_xml,
    run_yasa_sleep_staging,
    write_yasa_shhs_evaluation_payload,
)


STAGE3_YASA_BATCH_SCHEMA_VERSION = "stage3.yasa_batch_reproduction.v1"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    zip_path = Path(args.zip).expanduser().resolve()
    sample_root = Path(args.sample_root).expanduser().resolve()
    output_dir = Path(args.out_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building SHHS zip record index: {zip_path}")
    records = build_shhs_zip_record_index(zip_path)
    selected = select_complete_shhs_zip_records(
        records,
        limit=args.limit,
        visit=args.visit,
        require_profusion=True,
    )
    print(f"Indexed records: {len(records)}")
    print(f"Selected complete records: {len(selected)}")

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, record in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] {record.record_id}")
        try:
            paths = extract_shhs_zip_record(
                zip_path=zip_path,
                record=record,
                output_root=sample_root,
                include_nsrr=True,
            )
            summary_path = output_dir / f"{record.record_id}_yasa_summary.json"
            evaluation_path = output_dir / f"{record.record_id}_yasa_vs_profusion_metrics.json"
            runner_result = run_yasa_sleep_staging(
                paths["edf"],
                eeg_name=args.eeg,
                eog_name=args.eog,
                emg_name=args.emg,
                yasa_src=args.yasa_src,
            )
            summary_payload = build_yasa_runner_payload(runner_result)
            summary_path.write_text(
                json.dumps(summary_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            evaluation_result = evaluate_yasa_summary_against_shhs_xml(
                yasa_summary_path=summary_path,
                shhs_xml_path=paths["profusion_xml"],
            )
            evaluation_payload = build_yasa_shhs_evaluation_payload(evaluation_result)
            write_yasa_shhs_evaluation_payload(evaluation_payload, evaluation_path)
            metrics = evaluation_payload["metrics"]
            successes.append(
                {
                    "record_id": record.record_id,
                    "visit": record.visit,
                    "edf_member": record.edf_member,
                    "profusion_xml_member": record.profusion_xml_member,
                    "summary_path": str(summary_path),
                    "evaluation_path": str(evaluation_path),
                    "compared_epoch_count": evaluation_payload["compared_epoch_count"],
                    "metrics": metrics,
                }
            )
            print(
                "  ok "
                f"accuracy={metrics['accuracy']:.4f} "
                f"kappa={metrics['cohen_kappa']:.4f} "
                f"macro_f1={metrics['macro_f1']:.4f}"
            )
        except Exception as exc:  # noqa: BLE001 - batch must record per-record failures.
            failures.append(
                {
                    "record_id": record.record_id,
                    "visit": record.visit,
                    "edf_member": record.edf_member,
                    "profusion_xml_member": record.profusion_xml_member,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            print(f"  failed {type(exc).__name__}: {exc}")

    batch_payload = build_batch_payload(
        zip_path=zip_path,
        sample_root=sample_root,
        output_dir=output_dir,
        selected=selected,
        successes=successes,
        failures=failures,
        eeg=args.eeg,
        eog=args.eog,
        emg=args.emg,
    )
    batch_path = output_dir / "batch_yasa_metrics_summary.json"
    batch_path.write_text(
        json.dumps(batch_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote batch summary JSON: {batch_path}")
    print(
        f"Batch complete: {len(successes)} succeeded, {len(failures)} failed, "
        f"requested={args.limit}."
    )
    return 0 if not failures else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stage 3 YASA sleep staging reproduction for a small SHHS zip batch."
    )
    parser.add_argument("--zip", default="../data/raw/shhs.zip", help="Local SHHS zip path.")
    parser.add_argument(
        "--sample-root",
        default="../data/raw/shhs_sample",
        help="Local SHHS-like extraction root for selected records.",
    )
    parser.add_argument(
        "--out-dir",
        default="../data/processed/sleepagent/stage3/batch_yasa",
        help="Directory for per-record and batch JSON outputs.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Number of complete records to run.")
    parser.add_argument("--visit", default="shhs1", help="Visit to select, e.g. shhs1 or shhs2.")
    parser.add_argument("--eeg", default="EEG", help="EEG channel name passed to YASA.")
    parser.add_argument("--eog", default="EOG(L)", help="Optional EOG channel name passed to YASA.")
    parser.add_argument("--emg", default="EMG", help="Optional EMG channel name passed to YASA.")
    parser.add_argument(
        "--yasa-src",
        default="../yasa/src",
        help="Local YASA source directory passed to the runner.",
    )
    return parser.parse_args(argv)


def build_batch_payload(
    *,
    zip_path: Path,
    sample_root: Path,
    output_dir: Path,
    selected: list[SHHSZipRecord],
    successes: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    eeg: str,
    eog: str | None,
    emg: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": STAGE3_YASA_BATCH_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "zip_path": str(zip_path),
        "sample_root": str(sample_root),
        "output_dir": str(output_dir),
        "channels": {"eeg": eeg, "eog": eog, "emg": emg},
        "selected_record_count": len(selected),
        "success_count": len(successes),
        "failure_count": len(failures),
        "selected_records": [
            {
                "record_id": record.record_id,
                "visit": record.visit,
                "edf_member": record.edf_member,
                "nsrr_xml_member": record.nsrr_xml_member,
                "profusion_xml_member": record.profusion_xml_member,
            }
            for record in selected
        ],
        "metric_means": _metric_means(successes),
        "successes": successes,
        "failures": failures,
        "notes": [
            "Stage 3 local YASA small-batch reproduction summary.",
            "Raw EDF/XML files are extracted only under the local ignored sample root.",
            "Metrics compare YASA Wake/REM/NREM predictions against Profusion XML labels.",
        ],
    }


def _metric_means(successes: list[dict[str, Any]]) -> dict[str, float | None]:
    keys = ("accuracy", "cohen_kappa", "macro_f1", "weighted_f1")
    if not successes:
        return {key: None for key in keys}
    return {
        key: mean(float(success["metrics"][key]) for success in successes)
        for key in keys
    }


if __name__ == "__main__":
    raise SystemExit(main())
