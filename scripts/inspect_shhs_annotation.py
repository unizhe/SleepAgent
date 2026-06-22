"""Print a lightweight summary for a local SHHS XML annotation file."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from sleepagent.preprocessing.shhs_annotations import inspect_shhs_annotation_xml


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a local SHHS XML annotation without reading EDF data."
    )
    parser.add_argument("xml_path", help="Path to an authorized local SHHS XML file.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    inspection = inspect_shhs_annotation_xml(args.xml_path)
    payload = asdict(inspection)
    payload["path"] = str(Path(payload["path"]))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
