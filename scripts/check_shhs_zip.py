"""Safely inspect a local SHHS zip archive without extracting data."""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SHHS_ZIP_ENV_VAR = "SLEEPAGENT_SHHS_ZIP"
DEFAULT_SHHS_ZIP_PATH = Path("../data/raw/shhs.zip")
SUGGESTED_SAMPLE_DIR = Path("../data/raw/shhs_sample")


@dataclass(frozen=True)
class ZipInspectionResult:
    zip_path: Path
    total_entries: int
    edf_count: int
    xml_count: int
    edf_samples: list[str]
    xml_samples: list[str]


def resolve_zip_path(zip_path: str | Path | None = None) -> Path:
    """Resolve a CLI zip path, environment value, or the documented default."""
    path_value = zip_path or os.getenv(SHHS_ZIP_ENV_VAR) or DEFAULT_SHHS_ZIP_PATH
    return Path(path_value).expanduser().resolve()


def inspect_shhs_zip(
    zip_path: str | Path,
    max_entries_per_type: int = 5,
) -> ZipInspectionResult:
    """Inspect zip member names only; do not extract or read EDF/XML contents."""
    if max_entries_per_type < 1:
        raise ValueError("max_entries_per_type must be at least 1.")

    resolved_zip_path = Path(zip_path).expanduser().resolve()
    edf_samples: list[str] = []
    xml_samples: list[str] = []
    edf_count = 0
    xml_count = 0

    with zipfile.ZipFile(resolved_zip_path) as archive:
        entries = archive.infolist()
        for entry in entries:
            if entry.is_dir():
                continue
            name = entry.filename
            normalized_name = name.lower()
            if normalized_name.endswith(".edf"):
                edf_count += 1
                if len(edf_samples) < max_entries_per_type:
                    edf_samples.append(name)
            elif normalized_name.endswith(".xml"):
                xml_count += 1
                if len(xml_samples) < max_entries_per_type:
                    xml_samples.append(name)

    return ZipInspectionResult(
        zip_path=resolved_zip_path,
        total_entries=len(entries),
        edf_count=edf_count,
        xml_count=xml_count,
        edf_samples=edf_samples,
        xml_samples=xml_samples,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely inspect a local SHHS zip archive. This command lists only a "
            "small number of .edf and .xml member names and never extracts files."
        )
    )
    parser.add_argument(
        "zip_path",
        nargs="?",
        help=(
            "Path to the SHHS zip archive. If omitted, uses "
            f"{SHHS_ZIP_ENV_VAR} or {DEFAULT_SHHS_ZIP_PATH}."
        ),
    )
    parser.add_argument(
        "--max-entries",
        type=int,
        default=5,
        help="Maximum number of EDF and XML member names to print per type.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    zip_path = resolve_zip_path(args.zip_path)

    if not zip_path.exists():
        _print_missing_zip_error(zip_path)
        return 2
    if not zip_path.is_file():
        print(f"ERROR: SHHS zip path is not a file: {zip_path}", file=sys.stderr)
        return 2

    try:
        result = inspect_shhs_zip(
            zip_path,
            max_entries_per_type=args.max_entries,
        )
    except zipfile.BadZipFile:
        print(f"ERROR: File is not a readable zip archive: {zip_path}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_result(result)
    return 0


def _print_missing_zip_error(zip_path: Path) -> None:
    print(f"ERROR: SHHS zip file not found: {zip_path}", file=sys.stderr)
    print(
        f"Set {SHHS_ZIP_ENV_VAR}=../data/raw/shhs.zip or pass a zip path "
        "as the command argument.",
        file=sys.stderr,
    )
    print(
        "Suggested local layout: keep raw data outside the code repository at "
        "../data/raw/shhs.zip, with future samples under ../data/raw/shhs_sample/.",
        file=sys.stderr,
    )


def _print_result(result: ZipInspectionResult) -> None:
    print(f"SHHS zip: {result.zip_path}")
    print(f"Total zip entries: {result.total_entries}")
    print(f"EDF entries found: {result.edf_count}")
    _print_samples("Sample EDF entries", result.edf_samples)
    print(f"XML entries found: {result.xml_count}")
    _print_samples("Sample XML entries", result.xml_samples)
    print("No extraction performed. EDF signal contents were not read.")
    print(
        "Next step: confirm the zip internal paths, then extract only 1-3 XML/EDF "
        f"samples to {SUGGESTED_SAMPLE_DIR}/ for local smoke tests."
    )


def _print_samples(title: str, samples: list[str]) -> None:
    print(f"{title}:")
    if not samples:
        print("  - none found")
        return
    for sample in samples:
        print(f"  - {sample}")


if __name__ == "__main__":
    raise SystemExit(main())
