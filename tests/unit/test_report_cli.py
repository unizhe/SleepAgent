from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from sleepagent import report_cli
from sleepagent.report_cli import (
    ReportCliConfig,
    ReportCliConfigurationError,
    build_parser,
    execute_command,
)


def config() -> ReportCliConfig:
    return ReportCliConfig(
        base_url="https://sleep.example",
        service_credential="service-secret-never-print",
        actor_private_key="/private/actor.pem",
        actor_id="actor-private-1",
        subject_id="subject-private-1",
        role="elder",
        assertion_issuer="issuer",
        assertion_audience="audience",
        assertion_key_id="key-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        http_timeout_seconds=30,
        poll_interval_seconds=0.5,
        wait_timeout_seconds=180,
    )


def accepted() -> dict[str, Any]:
    return {
        "schema_version": "product_sleep_report_run_accepted.v1",
        "wake_date": "2026-08-26",
        "state": "accepted",
        "status_url": "/product/sleep/reports/2026-08-26",
    }


def ready_report(*, include_trace: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "product_sleep_report.v1",
        "wake_date": "2026-08-26",
        "state": "ready",
        "audience": "elder",
        "quality": "partial",
        "quality_caveat": "Some sensors were unavailable.",
        "projection": {
            "audience": "elder",
            "summary_text": "You slept for about seven hours.",
            "context_notice": "This report uses the available sleep signals.",
        },
        "narrative": {"state": "ready", "text": "A steady night overall."},
    }
    if include_trace:
        value["trace"] = {
            "gate": "analyzable",
            "shared_analysis": "reused",
            "elder_narrative": "created",
            "fallback_used": False,
            "provider_call_count": 1,
            "provider_input_tokens": 90,
            "provider_output_tokens": 25,
            "provider_request_ids_present": True,
            "unsafe_extra": "must-not-render",
        }
    return value


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.report = ready_report(include_trace=True)

    def run_report(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run", kwargs))
        return accepted()

    def await_report(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("await", kwargs))
        return self.report

    def get_report(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return self.report

    def list_reports(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        item = {
            key: value
            for key, value in self.report.items()
            if key
            in {
                "wake_date",
                "state",
                "audience",
                "quality",
                "quality_caveat",
            }
        }
        item["narrative_state"] = "ready"
        return {
            "schema_version": "product_sleep_report_list.v1",
            "items": [item],
            "next_cursor": "cursor-2",
            "trace": self.report["trace"],
        }


def environment() -> dict[str, str]:
    return {
        "SLEEPAGENT_REPORT_BASE_URL": "https://sleep.example",
        "SLEEPAGENT_REPORT_SERVICE_CREDENTIAL": "service-secret",
        "SLEEPAGENT_REPORT_ACTOR_PRIVATE_KEY": "/private/actor.pem",
        "SLEEPAGENT_REPORT_ACTOR_ID": "actor-1",
        "SLEEPAGENT_REPORT_SUBJECT_ID": "subject-1",
        "SLEEPAGENT_REPORT_ROLE": "family",
    }


def test_config_is_environment_only_and_uses_protocol_defaults() -> None:
    loaded = ReportCliConfig.from_environment(environment())

    assert loaded.base_url == "https://sleep.example"
    assert loaded.role == "family"
    assert loaded.assertion_issuer == "sleepagent-bff-v1"
    assert loaded.assertion_audience == "sleepagent-backend"
    assert loaded.authorization_epoch == 1
    assert loaded.poll_interval_seconds == 0.5

    missing = environment()
    missing.pop("SLEEPAGENT_REPORT_SUBJECT_ID")
    with pytest.raises(
        ReportCliConfigurationError,
        match="SLEEPAGENT_REPORT_SUBJECT_ID",
    ):
        ReportCliConfig.from_environment(missing)

    stale_epoch = environment()
    stale_epoch["SLEEPAGENT_REPORT_AUTHORIZATION_EPOCH"] = "0"
    with pytest.raises(ReportCliConfigurationError, match="at least 1"):
        ReportCliConfig.from_environment(stale_epoch)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--wake-date", "20260826"],
        ["run", "--wake-date", "2026-02-30"],
        ["run", "--wake-date", "2026-08-26", "--timeout", "0"],
        ["list", "--limit", "101"],
        ["list", "--cursor", "contains space"],
    ],
)
def test_parser_rejects_noncanonical_or_unbounded_values(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(argv)
    assert captured.value.code == 2


def test_run_waits_by_default_and_forwards_safe_trace() -> None:
    client = FakeClient()
    args = build_parser().parse_args(
        ["run", "--wake-date", "2026-08-26", "--timeout", "45", "--trace"]
    )

    outcome = execute_command(args, client, config())

    assert outcome.exit_code == 0
    assert [name for name, _ in client.calls] == ["run", "await"]
    run_call = client.calls[0][1]
    assert run_call["wake_date"] == date(2026, 8, 26)
    assert str(run_call["idempotency_key"]).startswith("sleepagent-report:")
    await_call = client.calls[1][1]
    assert await_call == {
        "wake_date": date(2026, 8, 26),
        "timeout_seconds": 45.0,
        "interval_seconds": 0.5,
        "include_trace": True,
    }
    assert outcome.payload["trace"] == {
        "gate": "analyzable",
        "shared_analysis": "reused",
        "elder_narrative": "created",
        "fallback_used": False,
        "provider_call_count": 1,
        "provider_input_tokens": 90,
        "provider_output_tokens": 25,
        "provider_request_ids_present": True,
    }


def test_run_no_wait_returns_acceptance_without_polling() -> None:
    client = FakeClient()
    args = build_parser().parse_args(
        ["run", "--wake-date", "2026-08-26", "--no-wait", "--json"]
    )

    outcome = execute_command(args, client, config())

    assert outcome == report_cli.CliOutcome(payload=accepted(), exit_code=0)
    assert [name for name, _ in client.calls] == ["run"]


def test_elder_run_continues_polling_until_narrative_is_terminal(
    monkeypatch,
) -> None:
    client = FakeClient()
    pending = ready_report(include_trace=True)
    pending["narrative"] = {"state": "pending", "text": None}
    final = ready_report(include_trace=True)
    final["narrative"] = {
        "state": "fallback",
        "text": "确定性中文回退内容。",
    }

    def await_shared(**kwargs: Any) -> dict[str, Any]:
        client.calls.append(("await", kwargs))
        return pending

    def get_terminal(**kwargs: Any) -> dict[str, Any]:
        client.calls.append(("get", kwargs))
        return final

    client.await_report = await_shared  # type: ignore[method-assign]
    client.get_report = get_terminal  # type: ignore[method-assign]
    monkeypatch.setattr(report_cli.time, "sleep", lambda _: None)
    args = build_parser().parse_args(
        ["run", "--wake-date", "2026-08-26", "--timeout", "45"]
    )

    outcome = execute_command(args, client, config())

    assert outcome.payload["narrative"] == final["narrative"]
    assert [name for name, _ in client.calls] == ["run", "await", "get"]


@pytest.mark.parametrize("role", ["family", "doctor"])
def test_non_elder_run_does_not_wait_for_narrative(role: str) -> None:
    client = FakeClient()
    client.report["audience"] = role
    projection = client.report["projection"]
    assert isinstance(projection, dict)
    projection["audience"] = role
    client.report.pop("narrative", None)
    args = build_parser().parse_args(
        ["run", "--wake-date", "2026-08-26"]
    )

    execute_command(args, client, replace(config(), role=role))

    assert [name for name, _ in client.calls] == ["run", "await"]


def test_elder_pretty_mode_is_narrative_first_without_duplicate_projection() -> None:
    report = ready_report()
    report["quality_caveat"] = "部分时段的数据不完整。"
    projection = report["projection"]
    assert isinstance(projection, dict)
    projection["summary_text"] = "不应与就绪叙述重复打印的确定性投影。"
    projection["context_notice"] = "不应默认打印的内部范围说明。"
    narrative = report["narrative"]
    assert isinstance(narrative, dict)
    narrative["text"] = (
        "设备记录到约80分钟的睡眠分期数据。\n\n"
        "本次部分时段的数据不完整，因此结果仅作为日常睡眠观察参考。\n\n"
        "本报告由 AI 辅助整理，不构成诊断或医疗建议。"
    )

    rendered = report_cli._render_text(report, include_trace=False)

    assert "不应与就绪叙述重复打印的确定性投影" not in rendered
    assert "不应默认打印的内部范围说明" not in rendered
    assert "quality_caveat:" not in rendered
    assert rendered.count("数据不完整") == 1
    assert rendered.count("不构成诊断或医疗建议") == 1
    assert "narrative_state:" not in rendered


def test_elder_pending_show_uses_projection_with_compact_generation_state() -> None:
    report = ready_report()
    report["narrative"] = {"state": "pending", "text": None}

    rendered = report_cli._render_text(report, include_trace=False)

    assert "You slept for about seven hours." in rendered
    assert rendered.count("自然语言报告生成中。") == 1


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    [
        ("ready", 0),
        ("not_run", 4),
        ("pending", 4),
        ("failed", 4),
        ("stale", 4),
        ("policy_blocked", 4),
        ("unusable_blocked", 4),
        ("urgent_handled", 4),
    ],
)
def test_show_has_stable_ready_and_nonready_exit_codes(
    state: str,
    expected_exit: int,
) -> None:
    client = FakeClient()
    client.report["state"] = state
    if state != "ready":
        client.report.pop("projection", None)
    if state == "unusable_blocked":
        client.report["quality"] = "unusable"
        client.report.pop("quality_caveat", None)
    args = build_parser().parse_args(["show", "--wake-date", "2026-08-26"])

    outcome = execute_command(args, client, config())

    assert outcome.exit_code == expected_exit
    assert "trace" not in outcome.payload


def test_list_forwards_pagination_and_renders_only_safe_trace() -> None:
    client = FakeClient()
    args = build_parser().parse_args(
        ["list", "--limit", "10", "--cursor", "cursor-1", "--trace"]
    )

    outcome = execute_command(args, client, config())

    assert outcome.exit_code == 0
    assert client.calls == [
        (
            "list",
            {"limit": 10, "cursor": "cursor-1", "include_trace": True},
        )
    ]
    assert "unsafe_extra" not in outcome.payload["trace"]


def test_privacy_guard_rejects_internal_ids_before_rendering() -> None:
    client = FakeClient()
    client.report["analysis_revision_id"] = "must-never-print"
    args = build_parser().parse_args(["show", "--wake-date", "2026-08-26"])

    with pytest.raises(report_cli.ReportCliError, match="privacy"):
        execute_command(args, client, config())


@pytest.mark.parametrize(
    ("container", "field"),
    [
        ("report", "debug"),
        ("projection", "raw_vendor_payload"),
        ("narrative", "model_response"),
    ],
)
def test_privacy_guard_rejects_every_non_public_response_field(
    container: str,
    field: str,
) -> None:
    client = FakeClient()
    if container == "report":
        client.report[field] = "subject-private raw-vendor-private"
    else:
        nested = client.report[container]
        assert isinstance(nested, dict)
        nested[field] = "subject-private raw-vendor-private"
    args = build_parser().parse_args(["show", "--wake-date", "2026-08-26"])

    with pytest.raises(report_cli.ReportCliError, match="non-public"):
        execute_command(args, client, config())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quality_caveat", {"unexpected": "object"}),
        ("quality_caveat", "   "),
    ],
)
def test_report_rejects_invalid_allowlisted_scalar_values(
    field: str,
    value: Any,
) -> None:
    client = FakeClient()
    client.report["quality"] = "good"
    client.report[field] = value
    args = build_parser().parse_args(["show", "--wake-date", "2026-08-26"])

    with pytest.raises(report_cli.ReportCliError, match="quality caveat"):
        execute_command(args, client, config())


def test_list_rejects_cursor_with_renderable_whitespace() -> None:
    client = FakeClient()

    def unsafe_list(**_: Any) -> dict[str, Any]:
        payload = FakeClient().list_reports()
        payload["next_cursor"] = "cursor\nspoofed-state: ready"
        return payload

    client.list_reports = unsafe_list  # type: ignore[method-assign]
    args = build_parser().parse_args(["list"])

    with pytest.raises(report_cli.ReportCliError, match="invalid cursor"):
        execute_command(args, client, config())


def test_main_emits_canonical_json_without_trace_or_credentials(
    monkeypatch,
    capsys,
) -> None:
    client = FakeClient()
    for name, value in environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(report_cli, "build_client", lambda _: client)

    exit_code = report_cli.main(
        ["show", "--wake-date", "2026-08-26", "--json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    decoded = json.loads(captured.out)
    assert decoded["state"] == "ready"
    assert "trace" not in decoded
    assert "service-secret" not in captured.out + captured.err
    assert "actor-1" not in captured.out + captured.err
    assert "subject-1" not in captured.out + captured.err


def test_main_maps_timeout_and_api_details_to_safe_exit_codes(
    monkeypatch,
    capsys,
) -> None:
    client = FakeClient()
    for name, value in environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(report_cli, "build_client", lambda _: client)

    def timeout(**_: Any) -> dict[str, Any]:
        raise TimeoutError("private-operation-1 private-subject-1")

    client.await_report = timeout  # type: ignore[method-assign]
    exit_code = report_cli.main(["run", "--wake-date", "2026-08-26"])

    captured = capsys.readouterr()
    assert exit_code == 5
    assert "private-operation-1" not in captured.err
    assert "private-subject-1" not in captured.err
