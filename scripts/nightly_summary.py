#!/usr/bin/env python3
"""Render compact, deterministic operator output from nightly read-only JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def _display(value: Any) -> str:
    if value is None or value == "" or value == {}:
        return "unavailable"
    return str(value)


def _counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "unavailable"
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def render_summary(payload: dict[str, Any]) -> str:
    wake_date = _display(payload.get("wake_date"))
    if not payload.get("night_found"):
        return "\n".join(
            (
                f"NIGHT_NOT_FOUND={wake_date}",
                "Stored acquisition evidence may still be listed below.",
                "Governed backfill: supported separately; never started by nightly.sh.",
                _acquisition_line(payload.get("acquisition", {})),
            )
        )

    acquisition = payload.get("acquisition", {})
    episode = payload.get("night_episode") or {}
    wake = payload.get("wake_confirmation") or {}
    data = payload.get("data") or {}
    finalization = payload.get("finalization") or {}
    product = payload.get("product") or {}
    return "\n".join(
        (
            f"Night: {wake_date}",
            _acquisition_line(acquisition),
            (
                "Episode: "
                f"{_display(episode.get('state'))}; "
                f"id={_display(episode.get('episode_id'))}; "
                f"revision={_display(episode.get('current_revision'))}"
            ),
            (
                "Boundary: "
                f"collection={_display(episode.get('collection_start_at'))}; "
                f"bed={_display(episode.get('bed_at'))}; "
                f"wake={_display(episode.get('wake_at'))}; "
                f"policy={_display(episode.get('boundary_policy_version'))}"
            ),
            (
                "Wake confirmation: "
                f"candidate={_display(wake.get('candidate_wake_at'))}; "
                f"latest_bed_presence={_display(wake.get('latest_bed_presence_at'))}; "
                f"revision={_display(wake.get('confirmed_revision'))}; "
                f"cause={_display(wake.get('confirmed_revision_cause'))}"
            ),
            (
                "Data: "
                f"canonical_observations={_display(data.get('canonical_observation_count'))}; "
                f"types={_counts(data.get('observation_type_counts'))}"
            ),
            (
                "Finalization: "
                f"state={_display(finalization.get('state'))}; "
                f"coverage={_display(finalization.get('coverage_status'))}; "
                f"revision={_display(finalization.get('current_revision'))}; "
                f"cause={_display(finalization.get('revision_cause'))}"
            ),
            (
                "Product: "
                f"analysis_revisions={_display(product.get('analysis_revision_count'))}; "
                f"operations={_counts(product.get('operation_statuses'))}"
            ),
        )
    )


def _acquisition_line(acquisition: dict[str, Any]) -> str:
    push = acquisition.get("push") or {}
    history = acquisition.get("history") or {}
    report = acquisition.get("sleep_report") or {}
    return (
        "Acquisition: "
        f"Push={_display(push.get('status'))}({push.get('raw_receipt_count', 0)}); "
        f"History={_display(history.get('status'))}({history.get('raw_response_count', 0)}); "
        f"SleepReport={_display(report.get('status'))}"
        f"(raw={report.get('raw_response_count', 0)}, "
        f"versions={_display(report.get('source_version_count'))})"
    )


def render_list(payload: dict[str, Any]) -> str:
    rows = payload.get("nights") or []
    header = f"{'DATE':<10}  {'EPISODE_STATE':<22}  {'WAKE':<5}  {'FINALIZATION':<23}  REPORT"
    rendered = [header]
    for row in rows:
        rendered.append(
            f"{_display(row.get('wake_date')):<10}  "
            f"{_display(row.get('episode_state')):<22}  "
            f"{_display(row.get('wake')):<5}  "
            f"{_display(row.get('finalization_state')):<23}  "
            f"{_display(row.get('report_state'))}"
        )
    if not rows:
        rendered.append("NO_STORED_NIGHTS")
    return "\n".join(rendered)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("summary", "list"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--summary-text", type=Path)
    parser.add_argument("--acceptance-exit-code", type=int, default=0)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        payload = json.loads(arguments.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"nightly summary input failed: {exc}", file=sys.stderr)
        return 2
    if payload.get("transaction_read_only") is not True:
        print("nightly query did not attest a read-only transaction", file=sys.stderr)
        return 2
    if arguments.mode == "list":
        print(render_list(payload))
        return 0
    if arguments.summary_json is None or arguments.summary_text is None:
        print("summary output paths are required", file=sys.stderr)
        return 2
    payload["acceptance"] = {
        "status": "passed" if arguments.acceptance_exit_code == 0 else "failed",
        "exit_code": arguments.acceptance_exit_code,
    }
    text = render_summary(payload)
    _atomic_write(
        arguments.summary_json,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
    )
    _atomic_write(arguments.summary_text, text)
    print(text)
    return 0 if payload.get("night_found") else 3


if __name__ == "__main__":
    raise SystemExit(main())
