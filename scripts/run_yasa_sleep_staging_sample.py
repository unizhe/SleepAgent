import argparse
import json
from pathlib import Path
from typing import Any

from sleepagent.services import (
    build_yasa_runner_payload,
    inspect_edf_signal,
    run_yasa_sleep_staging,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    metadata = _metadata_from_args(args)
    yasa_src = args.yasa_src
    if yasa_src is None:
        default_yasa_src = Path(__file__).resolve().parents[2] / "yasa" / "src"
        yasa_src = str(default_yasa_src) if default_yasa_src.exists() else None

    edf_info = inspect_edf_signal(args.edf)
    print_edf_info(edf_info)

    result = run_yasa_sleep_staging(
        args.edf,
        eeg_name=args.eeg,
        eog_name=args.eog,
        emg_name=args.emg,
        metadata=metadata,
        preload=not args.no_preload,
        yasa_src=yasa_src,
    )
    payload = build_yasa_runner_payload(result)
    print_summary(payload)

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote Stage 3 YASA summary JSON: {out_path}")

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run YASA SleepStaging on one authorized local EDF sample."
    )
    parser.add_argument("--edf", required=True, help="Path to an authorized local EDF file.")
    parser.add_argument("--eeg", required=True, help="EEG channel name passed to YASA.")
    parser.add_argument("--eog", default=None, help="Optional EOG channel name passed to YASA.")
    parser.add_argument("--emg", default=None, help="Optional EMG channel name passed to YASA.")
    parser.add_argument(
        "--out",
        default=None,
        help="Optional JSON output path. If omitted, only EDF info and summary are printed.",
    )
    parser.add_argument(
        "--yasa-src",
        default=None,
        help="Optional local YASA source directory, usually /path/to/yasa/src.",
    )
    parser.add_argument(
        "--age",
        type=int,
        default=None,
        help="Optional participant age metadata passed to YASA.",
    )
    parser.add_argument(
        "--male",
        type=int,
        choices=[0, 1],
        default=None,
        help="Optional participant sex metadata passed to YASA: 1=male, 0=female.",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="Read EDF without preload before YASA. Preload is the default for smoke runs.",
    )
    return parser.parse_args(argv)


def print_edf_info(edf_info: Any) -> None:
    print("EDF info")
    print(f"- path: {edf_info.path}")
    print(f"- sampling_rate_hz: {edf_info.sampling_rate_hz:.3f}")
    print(f"- duration_seconds: {edf_info.duration_seconds:.3f}")
    print(f"- n_samples: {edf_info.n_samples}")
    print("- channels:")
    for channel_name in edf_info.channel_names:
        print(f"  - {channel_name}")


def print_summary(payload: dict[str, Any]) -> None:
    summary = payload["sleep_summary"]
    print("SleepAgent YASA staging summary")
    print(f"- epoch_count: {payload['epoch_count']}")
    print(f"- total_recording_minutes: {summary['total_recording_minutes']}")
    print(f"- total_sleep_time_minutes: {summary['total_sleep_time_minutes']}")
    print(f"- wake_minutes: {summary['wake_minutes']}")
    print(f"- rem_minutes: {summary['rem_minutes']}")
    print(f"- nrem_minutes: {summary['nrem_minutes']}")
    print(f"- sleep_efficiency_percent: {summary['sleep_efficiency_percent']}")


def _metadata_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    if args.age is not None:
        metadata["age"] = args.age
    if args.male is not None:
        metadata["male"] = args.male
    return metadata or None


if __name__ == "__main__":
    raise SystemExit(main())
