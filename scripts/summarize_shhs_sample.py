"""Print a tiny XML-derived preprocessing summary for a local SHHS sample."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from sleepagent.preprocessing.shhs_summary import (
    build_shhs_preprocessing_manifest,
    build_shhs_preprocessing_summary,
    validate_shhs_preprocessing_manifest_file,
    write_shhs_preprocessing_manifest,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a tiny XML-derived SHHS preprocessing summary without "
            "reading EDF signal contents."
        )
    )
    parser.add_argument(
        "--root",
        default="../data/raw/shhs_sample",
        help="Local SHHS-like root containing polysomnography/.",
    )
    parser.add_argument("--record-id", default="shhs1-200001")
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="Print the minimal Stage 2 preprocessing manifest schema.",
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Write the manifest JSON to a local data directory.",
    )
    parser.add_argument(
        "--manifest-dir",
        default="../data/manifests",
        help="Output directory used with --write-manifest.",
    )
    parser.add_argument(
        "--no-profusion",
        action="store_true",
        help="Skip Profusion XML even if it is present.",
    )
    parser.add_argument(
        "--validate-manifest",
        help="Validate an existing Stage 2 manifest JSON file and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.validate_manifest:
        result = validate_shhs_preprocessing_manifest_file(args.validate_manifest)
        if result.is_valid:
            print("manifest valid")
            return 0
        for error in result.errors:
            print(f"manifest invalid: {error}")
        return 1

    if args.manifest:
        manifest = build_shhs_preprocessing_manifest(
            root=args.root,
            record_ids=[args.record_id],
            include_profusion=not args.no_profusion,
        )
        if args.write_manifest:
            output_path = write_shhs_preprocessing_manifest(
                manifest,
                output_dir=args.manifest_dir,
            )
            print(str(output_path))
            return 0
        print(json.dumps(manifest.to_json_dict(), indent=2, sort_keys=True))
        return 0

    if args.write_manifest:
        parser.error("--write-manifest requires --manifest.")

    summary = build_shhs_preprocessing_summary(
        root=args.root,
        record_id=args.record_id,
        include_profusion=not args.no_profusion,
    )
    print(json.dumps(summary.to_json_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
