"""Command-line entry point for the production real-night acceptance audit."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sleepagent.integrations.perceptor.real_acceptance import (
    AuthorizedHumanAcceptanceApproval,
    RealPerceptorAcceptanceAuditor,
    RealPerceptorAcceptanceManifest,
)
from sleepagent.sleep_api.runtime import (
    SLEEP_API_MODE_ENV,
    build_sleep_api_runtime_from_env,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit one real Perceptor night from committed production "
            "projections and redacted content-addressed evidence."
        )
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Redacted real_perceptor_acceptance_manifest.v1 JSON file.",
    )
    parser.add_argument(
        "--approval",
        type=Path,
        help=(
            "Optional authorized_human_acceptance_approval.v1 JSON bound to "
            "a prior automated audit digest."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional report path. The file is created with owner-only "
            "permissions; otherwise JSON is written to stdout."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get(SLEEP_API_MODE_ENV, "").strip().lower() != "production":
        print(
            f"{SLEEP_API_MODE_ENV}=production is required for the real gate.",
            file=sys.stderr,
        )
        return 2
    runtime = None
    try:
        manifest = RealPerceptorAcceptanceManifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        approval = (
            None
            if args.approval is None
            else AuthorizedHumanAcceptanceApproval.model_validate_json(
                args.approval.read_text(encoding="utf-8")
            )
        )
        runtime = build_sleep_api_runtime_from_env()
        bundle = RealPerceptorAcceptanceAuditor(
            runtime.repository,
            event_reader=runtime.api_persistence,
        ).build_bundle(manifest, approval=approval)
        output = bundle.model_dump_json(indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(output)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                args.output,
                flags,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(output)
        statuses = {item.status.value for item in bundle.report.verdicts}
        if "failed" in statuses:
            return 2
        if "pending" in statuses:
            return 3
        return 0
    except Exception as exc:
        print(
            f"real Perceptor acceptance audit failed closed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    finally:
        if runtime is not None:
            runtime.repository.store.connection.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
