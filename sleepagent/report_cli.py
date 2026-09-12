"""Authenticated HTTP-only CLI for exact-date Product sleep reports."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from reference_client.sleep_api_v1_client import (
    ApiError,
    Ed25519ActorSigner,
    HttpsJsonTransport,
    SleepApiV1Client,
)


REPORT_READ_SCOPE = "product:sleep:today:read"
REPORT_RUN_SCOPE = "sleep:reanalysis:write"
REPORT_STATES = frozenset(
    {
        "not_run",
        "pending",
        "ready",
        "failed",
        "stale",
        "policy_blocked",
        "unusable_blocked",
        "urgent_handled",
    }
)
REPORT_QUALITY_STATES = frozenset({"good", "partial", "unusable"})
REPORT_AUDIENCES = frozenset({"elder", "family", "doctor"})
NARRATIVE_STATES = frozenset({"pending", "ready", "fallback", "failed", "stale"})
TRACE_STRING_FIELDS = frozenset({"gate", "shared_analysis", "elder_narrative"})
TRACE_COUNT_FIELDS = frozenset(
    {"provider_call_count", "provider_input_tokens", "provider_output_tokens"}
)
TRACE_BOOLEAN_FIELDS = frozenset(
    {"fallback_used", "provider_request_ids_present"}
)
TRACE_FIELDS = TRACE_STRING_FIELDS | TRACE_COUNT_FIELDS | TRACE_BOOLEAN_FIELDS
TRACE_STRING_VALUES = {
    "gate": frozenset({"analyzable", "urgent", "unusable", "not_evaluated"}),
    "shared_analysis": frozenset(
        {"created", "reused", "pending", "not_applicable"}
    ),
    "elder_narrative": frozenset(
        {"created", "reused", "pending", "fallback", "not_applicable"}
    ),
}
REPORT_FAILURE_CODES = frozenset(
    {
        "analysis_failed",
        "doctor_safety_unavailable",
        "policy_blocked",
        "data_unusable",
        "source_stale",
    }
)
SCHEMA_VERSIONS = frozenset(
    {
        "product_sleep_report_run_accepted.v1",
        "product_sleep_report.v1",
        "product_sleep_report_list.v1",
    }
)
RUN_ACCEPTED_FIELDS = frozenset(
    {"schema_version", "wake_date", "state", "status_url"}
)
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "wake_date",
        "state",
        "audience",
        "quality",
        "quality_caveat",
        "projection",
        "narrative",
        "failure_code",
        "trace",
    }
)
REPORT_LIST_FIELDS = frozenset(
    {"schema_version", "items", "next_cursor", "trace"}
)
REPORT_LIST_ITEM_FIELDS = frozenset(
    {
        "wake_date",
        "state",
        "audience",
        "quality",
        "quality_caveat",
        "narrative_state",
        "failure_code",
    }
)
PROJECTION_FIELDS = frozenset(
    {"audience", "summary_text", "context_notice"}
)
NARRATIVE_FIELDS = frozenset({"state", "text"})
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ReportCliError(RuntimeError):
    """A safe, user-facing CLI failure without response or credential details."""


class ReportCliConfigurationError(ReportCliError):
    pass


class ReportClient(Protocol):
    def run_report(
        self,
        *,
        wake_date: date | str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def await_report(
        self,
        *,
        wake_date: date | str,
        timeout_seconds: float,
        interval_seconds: float,
        include_trace: bool,
    ) -> dict[str, Any]: ...

    def get_report(
        self,
        *,
        wake_date: date | str,
        include_trace: bool,
    ) -> dict[str, Any]: ...

    def list_reports(
        self,
        *,
        limit: int,
        cursor: str | None,
        include_trace: bool,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReportCliConfig:
    base_url: str
    service_credential: str
    actor_private_key: str
    actor_id: str
    subject_id: str
    role: str
    assertion_issuer: str
    assertion_audience: str
    assertion_key_id: str
    authorization_epoch: int
    privacy_epoch: int
    retrieval_policy_epoch: int
    http_timeout_seconds: float
    poll_interval_seconds: float
    wait_timeout_seconds: float

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ReportCliConfig":
        env = os.environ if environment is None else environment

        def required(name: str, *, fallback: str | None = None) -> str:
            value = env.get(name, "").strip()
            if not value and fallback is not None:
                value = env.get(fallback, "").strip()
            if not value:
                raise ReportCliConfigurationError(f"{name} is required")
            return value

        def setting(name: str, default: str, *, fallback: str | None = None) -> str:
            value = env.get(name, "").strip()
            if not value and fallback is not None:
                value = env.get(fallback, "").strip()
            return value or default

        role = required("SLEEPAGENT_REPORT_ROLE")
        if role not in REPORT_AUDIENCES:
            raise ReportCliConfigurationError(
                "SLEEPAGENT_REPORT_ROLE must be elder, family, or doctor"
            )
        return cls(
            base_url=required(
                "SLEEPAGENT_REPORT_BASE_URL",
                fallback="SLEEPAGENT_PRODUCT_BASE_URL",
            ),
            service_credential=required("SLEEPAGENT_REPORT_SERVICE_CREDENTIAL"),
            actor_private_key=required("SLEEPAGENT_REPORT_ACTOR_PRIVATE_KEY"),
            actor_id=required("SLEEPAGENT_REPORT_ACTOR_ID"),
            subject_id=required("SLEEPAGENT_REPORT_SUBJECT_ID"),
            role=role,
            assertion_issuer=setting(
                "SLEEPAGENT_REPORT_ASSERTION_ISSUER",
                "sleepagent-bff-v1",
                fallback="SLEEPAGENT_BACKEND_ACTOR_ASSERTION_ISSUER",
            ),
            assertion_audience=setting(
                "SLEEPAGENT_REPORT_ASSERTION_AUDIENCE",
                "sleepagent-backend",
                fallback="SLEEPAGENT_BACKEND_ACTOR_ASSERTION_AUDIENCE",
            ),
            assertion_key_id=setting(
                "SLEEPAGENT_REPORT_ASSERTION_KEY_ID",
                "primary",
                fallback="SLEEPAGENT_BACKEND_ACTOR_ASSERTION_KEY_ID",
            ),
            authorization_epoch=_environment_integer(
                env,
                "SLEEPAGENT_REPORT_AUTHORIZATION_EPOCH",
                default=1,
            ),
            privacy_epoch=_environment_integer(
                env,
                "SLEEPAGENT_REPORT_PRIVACY_EPOCH",
                default=1,
            ),
            retrieval_policy_epoch=_environment_integer(
                env,
                "SLEEPAGENT_REPORT_RETRIEVAL_POLICY_EPOCH",
                default=1,
            ),
            http_timeout_seconds=_environment_float(
                env,
                "SLEEPAGENT_REPORT_HTTP_TIMEOUT_SECONDS",
                default=30.0,
                maximum=120.0,
            ),
            poll_interval_seconds=_environment_float(
                env,
                "SLEEPAGENT_REPORT_POLL_INTERVAL_SECONDS",
                default=0.5,
                maximum=60.0,
            ),
            wait_timeout_seconds=_environment_float(
                env,
                "SLEEPAGENT_REPORT_WAIT_TIMEOUT_SECONDS",
                default=180.0,
                maximum=3_600.0,
            ),
        )


@dataclass(frozen=True, slots=True)
class CliOutcome:
    payload: dict[str, Any]
    exit_code: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sleepagent-report",
        description="Run and read one authenticated subject's Product sleep reports.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="request an exact-date report")
    run.add_argument("--wake-date", required=True, type=_wake_date_argument)
    run.add_argument(
        "--no-wait",
        action="store_true",
        help="return after the durable report request is accepted",
    )
    run.add_argument(
        "--timeout",
        type=_timeout_argument,
        default=None,
        help="maximum seconds to wait (default: profile setting or 180)",
    )
    _add_output_arguments(run)

    show = subcommands.add_parser("show", help="show one exact-date report")
    show.add_argument("--wake-date", required=True, type=_wake_date_argument)
    _add_output_arguments(show)

    list_command = subcommands.add_parser("list", help="list finalized report dates")
    list_command.add_argument("--limit", type=_limit_argument, default=20)
    list_command.add_argument("--cursor", type=_cursor_argument, default=None)
    _add_output_arguments(list_command)
    return parser


def build_client(config: ReportCliConfig) -> SleepApiV1Client:
    signer = Ed25519ActorSigner.from_private_key_file(
        private_key_path=config.actor_private_key,
        issuer=config.assertion_issuer,
        audience=config.assertion_audience,
        key_id=config.assertion_key_id,
        actor_id=config.actor_id,
        subject_id=config.subject_id,
        role=config.role,
        scopes=(REPORT_READ_SCOPE, REPORT_RUN_SCOPE),
        authorization_epoch=config.authorization_epoch,
        privacy_epoch=config.privacy_epoch,
        retrieval_policy_epoch=config.retrieval_policy_epoch,
    )
    return SleepApiV1Client(
        base_url=config.base_url,
        service_credential=config.service_credential,
        signer=signer,
        event_state_store=None,
        transport=HttpsJsonTransport(timeout_seconds=config.http_timeout_seconds),
    )


def execute_command(
    args: argparse.Namespace,
    client: ReportClient,
    config: ReportCliConfig,
) -> CliOutcome:
    include_trace = bool(args.trace)
    if args.command == "run":
        wake_date = _require_date(args.wake_date)
        accepted = _sanitize_payload(
            client.run_report(
                wake_date=wake_date,
                idempotency_key=f"sleepagent-report:{uuid4().hex}",
            ),
            include_trace=include_trace,
        )
        _validate_payload(accepted)
        if args.no_wait:
            return CliOutcome(payload=accepted, exit_code=0)
        timeout = (
            config.wait_timeout_seconds
            if args.timeout is None
            else float(args.timeout)
        )
        deadline = time.monotonic() + timeout
        report = _sanitize_payload(
            client.await_report(
                wake_date=wake_date,
                timeout_seconds=timeout,
                interval_seconds=config.poll_interval_seconds,
                include_trace=include_trace,
            ),
            include_trace=include_trace,
        )
        _validate_payload(report)
        if config.role == "elder":
            report = _await_elder_narrative_terminal(
                client=client,
                report=report,
                wake_date=wake_date,
                deadline=deadline,
                interval_seconds=config.poll_interval_seconds,
                include_trace=include_trace,
            )
        return CliOutcome(
            payload=report,
            exit_code=_report_exit_code(report),
        )
    if args.command == "show":
        report = _sanitize_payload(
            client.get_report(
                wake_date=_require_date(args.wake_date),
                include_trace=include_trace,
            ),
            include_trace=include_trace,
        )
        _validate_payload(report)
        return CliOutcome(
            payload=report,
            exit_code=_report_exit_code(report),
        )
    if args.command == "list":
        reports = _sanitize_payload(
            client.list_reports(
                limit=int(args.limit),
                cursor=args.cursor,
                include_trace=include_trace,
            ),
            include_trace=include_trace,
        )
        _validate_payload(reports)
        return CliOutcome(payload=reports, exit_code=0)
    raise ReportCliError("unsupported report command")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = ReportCliConfig.from_environment()
        client = build_client(config)
    except (ReportCliConfigurationError, ValueError) as exc:
        print(f"sleepagent-report: configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = execute_command(args, client, config)
        rendered = (
            _render_json(outcome.payload)
            if bool(args.json)
            else _render_text(outcome.payload, include_trace=bool(args.trace))
        )
        print(rendered)
        return outcome.exit_code
    except ApiError as exc:
        print(
            "sleepagent-report: API request failed "
            f"(HTTP {exc.status_code}, {_safe_error_code(exc.code)})",
            file=sys.stderr,
        )
        return 4 if exc.status_code == 404 else 3
    except TimeoutError:
        print("sleepagent-report: report wait timed out", file=sys.stderr)
        return 5
    except (urllib.error.URLError, OSError) as exc:
        print(
            "sleepagent-report: network request failed "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )
        return 3
    except (ReportCliError, ValueError, TypeError, json.JSONDecodeError) as exc:
        del exc
        print("sleepagent-report: server returned an invalid report", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("sleepagent-report: interrupted", file=sys.stderr)
        return 130


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one stable JSON object",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="include the allowlisted report execution summary",
    )


def _wake_date_argument(raw: str) -> date:
    if len(raw) != 10 or raw[4:5] != "-" or raw[7:8] != "-":
        raise argparse.ArgumentTypeError("wake date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("wake date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise argparse.ArgumentTypeError("wake date must use YYYY-MM-DD")
    return parsed


def _timeout_argument(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(value) or value <= 0 or value > 3_600:
        raise argparse.ArgumentTypeError("timeout must be between 0 and 3600 seconds")
    return value


def _limit_argument(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if value < 1 or value > 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return value


def _cursor_argument(raw: str) -> str:
    if not raw or len(raw) > 2_000 or any(character.isspace() for character in raw):
        raise argparse.ArgumentTypeError(
            "cursor must be a nonempty opaque value of at most 2000 characters"
        )
    return raw


def _environment_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReportCliConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise ReportCliConfigurationError(f"{name} must be at least 1")
    return value


def _environment_float(
    environment: Mapping[str, str],
    name: str,
    *,
    default: float,
    maximum: float,
) -> float:
    raw = environment.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ReportCliConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise ReportCliConfigurationError(
            f"{name} must be between 0 and {maximum:g} seconds"
        )
    return value


def _require_date(value: Any) -> date:
    if not isinstance(value, date):
        raise ReportCliError("wake date was not validated")
    return value


def _sanitize_payload(
    value: Mapping[str, Any],
    *,
    include_trace: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportCliError("report response must be an object")
    _enforce_public_shape(value)
    safe = _sanitize_json_value(value, include_trace=include_trace)
    if not isinstance(safe, dict):
        raise ReportCliError("report response must be an object")
    return safe


def _enforce_public_shape(value: Mapping[str, Any]) -> None:
    """Reject every server field outside the versioned public report DTOs."""

    schema_version = value.get("schema_version")
    if schema_version == "product_sleep_report_run_accepted.v1":
        _reject_extra_fields(value, RUN_ACCEPTED_FIELDS, "report acceptance")
        return
    if schema_version == "product_sleep_report.v1":
        _reject_extra_fields(value, REPORT_FIELDS, "report")
        projection = value.get("projection")
        if isinstance(projection, Mapping):
            _reject_extra_fields(
                projection,
                PROJECTION_FIELDS,
                "report projection",
            )
        narrative = value.get("narrative")
        if isinstance(narrative, Mapping):
            _reject_extra_fields(
                narrative,
                NARRATIVE_FIELDS,
                "report narrative",
            )
        return
    if schema_version == "product_sleep_report_list.v1":
        _reject_extra_fields(value, REPORT_LIST_FIELDS, "report list")
        items = value.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    _reject_extra_fields(
                        item,
                        REPORT_LIST_ITEM_FIELDS,
                        "report list item",
                    )
        return
    raise ReportCliError("unsupported report schema")


def _reject_extra_fields(
    value: Mapping[Any, Any],
    allowed: frozenset[str],
    label: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise ReportCliError(
            f"{label} violated the public privacy contract with a non-string field"
        )
    extras = set(value) - allowed
    if extras:
        raise ReportCliError(
            f"{label} violated the public privacy contract with a non-public field"
        )


def _sanitize_json_value(value: Any, *, include_trace: bool) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ReportCliError("report response contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) or isinstance(value, tuple):
        return [
            _sanitize_json_value(item, include_trace=include_trace) for item in value
        ]
    if not isinstance(value, Mapping):
        raise ReportCliError("report response contains a non-JSON value")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ReportCliError("report response contains a non-string key")
        if key == "trace":
            if include_trace:
                safe[key] = _sanitize_trace(item)
            continue
        if _is_private_key(key):
            raise ReportCliError("report response violated the public privacy contract")
        safe[key] = _sanitize_json_value(item, include_trace=include_trace)
    return safe


def _sanitize_trace(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportCliError("report trace must be an object")
    trace: dict[str, Any] = {}
    for key in TRACE_FIELDS:
        if key not in value:
            raise ReportCliError("report trace omitted a required summary field")
        item = value[key]
        if key in TRACE_STRING_FIELDS:
            if not isinstance(item, str) or item not in TRACE_STRING_VALUES[key]:
                raise ReportCliError("report trace contains an unsafe state")
        elif key in TRACE_COUNT_FIELDS:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ReportCliError("report trace contains an invalid count")
        elif not isinstance(item, bool):
            raise ReportCliError("report trace contains an invalid flag")
        trace[key] = item
    return trace


def _is_private_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered.endswith("_id")
        or lowered.endswith("_ids")
        or lowered
        in {
            "subject_ref",
            "provider_request_id",
            "provider_request_ids",
            "actor",
            "membership",
            "raw_payload",
            "prompt",
            "response",
        }
    )


def _validate_payload(payload: Mapping[str, Any]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version not in SCHEMA_VERSIONS:
        raise ReportCliError("unsupported report schema")
    if schema_version == "product_sleep_report_run_accepted.v1":
        _validate_wake_date(payload.get("wake_date"))
        if payload.get("state") != "accepted":
            raise ReportCliError("report request was not accepted")
        status_url = payload.get("status_url")
        expected_status_url = f"/product/sleep/reports/{payload['wake_date']}"
        if status_url != expected_status_url:
            raise ReportCliError("report request omitted its safe status URL")
        return
    if schema_version == "product_sleep_report_list.v1":
        items = payload.get("items")
        if not isinstance(items, list):
            raise ReportCliError("report list omitted its items")
        for item in items:
            if not isinstance(item, Mapping):
                raise ReportCliError("report list contains an invalid item")
            _validate_report(item, require_projection=False)
        cursor = payload.get("next_cursor")
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 2_000
            or any(character.isspace() for character in cursor)
        ):
            raise ReportCliError("report list contains an invalid cursor")
        return
    _validate_report(payload)


def _validate_report(
    report: Mapping[str, Any],
    *,
    require_projection: bool = True,
) -> None:
    _validate_wake_date(report.get("wake_date"))
    state = report.get("state")
    if state not in REPORT_STATES:
        raise ReportCliError("report contains an unsupported state")
    audience = report.get("audience")
    if audience not in REPORT_AUDIENCES:
        raise ReportCliError("report contains an unsupported audience")
    quality = report.get("quality")
    if quality is not None and quality not in REPORT_QUALITY_STATES:
        raise ReportCliError("report contains an unsupported quality state")
    caveat = report.get("quality_caveat")
    if caveat is not None and (
        not isinstance(caveat, str) or not caveat.strip()
    ):
        raise ReportCliError("report contains an invalid quality caveat")
    if quality == "partial" and (not isinstance(caveat, str) or not caveat.strip()):
        raise ReportCliError("partial report omitted its quality caveat")
    projection = report.get("projection")
    if require_projection and state == "ready" and not isinstance(projection, Mapping):
        raise ReportCliError("ready report omitted its projection")
    if state != "ready" and projection is not None:
        raise ReportCliError("non-ready report carried a projection")
    if isinstance(projection, Mapping):
        if projection.get("audience") != audience:
            raise ReportCliError("report projection audience drifted")
        for field in ("summary_text", "context_notice"):
            item = projection.get(field)
            if not isinstance(item, str) or not item.strip():
                raise ReportCliError("report projection omitted required content")
    narrative = report.get("narrative")
    if narrative is not None:
        if audience != "elder" or not isinstance(narrative, Mapping):
            raise ReportCliError("report narrative is outside the elder boundary")
        narrative_state = narrative.get("state")
        if narrative_state not in NARRATIVE_STATES:
            raise ReportCliError("report narrative contains an unsupported state")
        narrative_text = narrative.get("text")
        publishes_text = narrative_state in {"ready", "fallback"}
        if publishes_text and (
            not isinstance(narrative_text, str) or not narrative_text.strip()
        ):
            raise ReportCliError("publishable narrative omitted its text")
        if not publishes_text and narrative_text is not None:
            raise ReportCliError("non-publishable narrative carried text")
    narrative_state = report.get("narrative_state")
    if narrative_state is not None and narrative_state not in NARRATIVE_STATES:
        raise ReportCliError("report summary contains an unsupported narrative state")
    if narrative_state is not None and audience != "elder":
        raise ReportCliError("report summary narrative crossed an audience boundary")
    if state == "unusable_blocked" and quality != "unusable":
        raise ReportCliError("unusable report state omitted unusable quality")
    failure_code = report.get("failure_code")
    if failure_code is not None and failure_code not in REPORT_FAILURE_CODES:
        raise ReportCliError("report contains an unsafe failure code")


def _validate_wake_date(value: Any) -> None:
    if not isinstance(value, str):
        raise ReportCliError("report omitted its wake date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ReportCliError("report contains an invalid wake date") from exc
    if len(value) != 10 or parsed.isoformat() != value:
        raise ReportCliError("report contains an invalid wake date")


def _report_exit_code(report: Mapping[str, Any]) -> int:
    return 0 if report.get("state") == "ready" else 4


def _safe_error_code(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        return "unknown_error"
    return value


def _render_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _render_text(payload: Mapping[str, Any], *, include_trace: bool) -> str:
    schema_version = payload.get("schema_version")
    if schema_version == "product_sleep_report_list.v1":
        return _render_report_list(payload, include_trace=include_trace)
    if payload.get("audience") == "elder" and payload.get("state") == "ready":
        return _render_elder_report(payload, include_trace=include_trace)
    lines = [
        f"wake_date: {payload.get('wake_date')}",
        f"state: {payload.get('state')}",
    ]
    for field in ("audience", "quality", "quality_caveat", "failure_code"):
        value = payload.get(field)
        if value is not None:
            lines.append(f"{field}: {value}")
    status_url = payload.get("status_url")
    if status_url is not None:
        lines.append(f"status_url: {status_url}")
    projection = payload.get("projection")
    if isinstance(projection, Mapping):
        lines.append("projection:")
        for field in ("summary_text", "context_notice"):
            value = projection.get(field)
            if value is not None:
                lines.append(f"  {field}: {value}")
    narrative = payload.get("narrative")
    if isinstance(narrative, Mapping):
        lines.append(f"narrative_state: {narrative.get('state')}")
        if narrative.get("text") is not None:
            lines.append(f"narrative: {narrative.get('text')}")
    if include_trace and isinstance(payload.get("trace"), Mapping):
        lines.append("trace:")
        trace = payload["trace"]
        assert isinstance(trace, Mapping)
        for field in sorted(TRACE_FIELDS):
            if field in trace:
                lines.append(f"  {field}: {trace[field]}")
    return "\n".join(lines)


def _render_elder_report(
    payload: Mapping[str, Any],
    *,
    include_trace: bool,
) -> str:
    """Render one narrative-first Elder view without duplicating caveats."""

    lines = [
        f"wake_date: {payload.get('wake_date')}",
        f"state: {payload.get('state')}",
    ]
    if payload.get("quality") is not None:
        lines.append(f"quality: {payload.get('quality')}")
    narrative = payload.get("narrative")
    projection = payload.get("projection")
    narrative_state = (
        narrative.get("state") if isinstance(narrative, Mapping) else None
    )
    narrative_text = (
        narrative.get("text") if isinstance(narrative, Mapping) else None
    )
    fallback_text = (
        projection.get("summary_text")
        if isinstance(projection, Mapping)
        else None
    )
    if narrative_state in {"ready", "fallback"} and isinstance(
        narrative_text, str
    ):
        lines.extend(("", narrative_text))
    elif isinstance(fallback_text, str):
        lines.extend(("", fallback_text))
        if narrative_state == "pending":
            lines.extend(("", "自然语言报告生成中。"))
    if include_trace and isinstance(payload.get("trace"), Mapping):
        lines.append("trace:")
        trace = payload["trace"]
        assert isinstance(trace, Mapping)
        for field in sorted(TRACE_FIELDS):
            if field in trace:
                lines.append(f"  {field}: {trace[field]}")
    return "\n".join(lines)


def _await_elder_narrative_terminal(
    *,
    client: ReportClient,
    report: dict[str, Any],
    wake_date: date,
    deadline: float,
    interval_seconds: float,
    include_trace: bool,
) -> dict[str, Any]:
    """Continue GET polling after shared readiness until Elder prose settles."""

    current = report
    while current.get("state") == "ready":
        narrative = current.get("narrative")
        narrative_state = (
            narrative.get("state")
            if isinstance(narrative, Mapping)
            else None
        )
        if narrative_state in {"ready", "fallback", "failed"}:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Elder narrative did not reach a terminal state")
        time.sleep(min(interval_seconds, remaining))
        current = _sanitize_payload(
            client.get_report(
                wake_date=wake_date,
                include_trace=include_trace,
            ),
            include_trace=include_trace,
        )
        _validate_payload(current)
    return current


def _render_report_list(
    payload: Mapping[str, Any],
    *,
    include_trace: bool,
) -> str:
    lines: list[str] = []
    items = payload.get("items", [])
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            parts = [str(item.get("wake_date")), str(item.get("state"))]
            for field in ("audience", "quality", "narrative_state", "failure_code"):
                value = item.get(field)
                if value is not None:
                    parts.append(f"{field}={value}")
            lines.append(" ".join(parts))
            if include_trace and isinstance(item.get("trace"), Mapping):
                trace = item["trace"]
                assert isinstance(trace, Mapping)
                summary = " ".join(
                    f"{field}={trace[field]}"
                    for field in sorted(TRACE_FIELDS)
                    if field in trace
                )
                if summary:
                    lines.append(f"  trace {summary}")
    if not lines:
        lines.append("no reports")
    if payload.get("next_cursor") is not None:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    if include_trace and isinstance(payload.get("trace"), Mapping):
        trace = payload["trace"]
        assert isinstance(trace, Mapping)
        lines.append("trace:")
        for field in sorted(TRACE_FIELDS):
            if field in trace:
                lines.append(f"  {field}: {trace[field]}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CliOutcome",
    "ReportCliConfig",
    "ReportCliConfigurationError",
    "ReportCliError",
    "build_client",
    "build_parser",
    "execute_command",
    "main",
]
