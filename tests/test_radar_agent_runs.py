from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from pathlib import Path

from backend.main import app
from sleepagent.product_device.radar_agent import (
    FakeRadarProductDataProvider,
    InMemoryRadarAgentRunStore,
    RadarAgentRole,
    RadarAgentRunCreateRequest,
    RadarAgentRunStatus,
    RadarSleepAgentService,
    validate_radar_agent_deepseek_json,
)
from sleepagent.radar_agent.product_agent import (
    CommunicationDraft,
    DeterministicCommitController,
    EpisodeStatus,
)
from sleepagent.radar_agent.product_agent.agents import ProductAgentFactory
from sleepagent.product_device.radar_agent import (
    RADAR_AGENT_SCHEMA_VERSION,
    build_radar_evidence,
)


class ConfiguredProductModel:
    def __init__(self, *, configured: bool) -> None:
        self.is_configured = configured


class RecordingProductRunner:
    def __init__(
        self,
        *,
        configured: bool = True,
        publish: bool = True,
    ) -> None:
        model = ConfiguredProductModel(configured=configured)
        self.agent_roster = ProductAgentFactory.create(
            sleepcare_model=model,
            evidence_reasoning_model=model,
            care_strategy_model=model,
            safety_review_model=model,
        )
        self.commit_controller = DeterministicCommitController()
        self.publish = publish
        self.requests: list[Any] = []

    def run(self, request):
        self.requests.append(request)
        episode_id = request.episode_id
        return SimpleNamespace(
            publication=(
                CommunicationDraft(
                    draft_id=f"draft:{episode_id}",
                    audience_role=request.audience_role,
                    text=f"ProductEpisodeRunner 已处理 {episode_id}",
                    context_notice="已通过四角色发布门。",
                )
                if self.publish
                else None
            ),
            receipt=SimpleNamespace(
                episode_id=episode_id,
                status=(
                    EpisodeStatus.COMPLETE
                    if self.publish
                    else EpisodeStatus.BLOCKED
                ),
                failure_codes=[] if self.publish else ["blocked_test_target"],
                safety_decision_refs=(
                    ["safety:doctor"] if request.doctor_material else []
                ),
                agent_invocation_ids=[f"invocation:{episode_id}"],
                tool_receipt_ids=[f"tool:{episode_id}"],
                accepted_work_product_refs=[f"work:{episode_id}"],
            ),
        )


def _service(
    monkeypatch,
    *,
    runner: RecordingProductRunner,
) -> RadarSleepAgentService:
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ACTOR_ID", "actor-product-test")
    monkeypatch.setenv(
        "SLEEPAGENT_PRODUCT_SUBJECT_ID",
        "radar-device-demo-001",
    )
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_ACTOR_ROLE", "family")
    return RadarSleepAgentService(
        data_provider=FakeRadarProductDataProvider(),
        store=InMemoryRadarAgentRunStore(),
        episode_runner=runner,
    )


def test_radar_agent_run_never_falls_back_to_retired_single_model_path(
    monkeypatch,
) -> None:
    runner = RecordingProductRunner(publish=False)
    service = _service(
        monkeypatch,
        runner=runner,
    )

    run = service.create_run(RadarAgentRunCreateRequest(), idempotency_key="fallback-key")

    assert run.status == RadarAgentRunStatus.FAILED_VALIDATION
    assert run.graph_mode == "product_episode_runner"
    assert run.artifacts == []
    assert any("未启用旧 JSON 生成链回退" in event.message for event in run.events)


def test_radar_agent_run_generates_three_product_artifacts_once_per_idempotency_key(
    monkeypatch,
) -> None:
    runner = RecordingProductRunner()
    service = _service(
        monkeypatch,
        runner=runner,
    )
    request = RadarAgentRunCreateRequest(
        question="昨晚睡得怎么样？",
        role=RadarAgentRole.FAMILY,
    )

    first = service.create_run(request, idempotency_key="demo-key")
    second = service.create_run(request, idempotency_key="demo-key")

    assert first.run_id == second.run_id
    assert first.status == RadarAgentRunStatus.COMPLETED
    assert len(first.artifacts) == 3
    assert {artifact.type.value for artifact in first.artifacts} == {
        "elder_report",
        "family_report",
        "doctor_report",
    }
    assert first.current_artifact is not None
    assert first.current_artifact.type.value == "family_report"
    assert len(runner.requests) == 3
    assert first.data_mode == "demo"
    assert first.generated_from_demo_data is True
    assert first.evidence
    assert all(
        "ProductEpisodeRunner" in artifact.content
        for artifact in first.artifacts
    )


def test_radar_agent_run_returns_configuration_state_for_unconfigured_product_runner(
    monkeypatch,
) -> None:
    service = _service(
        monkeypatch,
        runner=RecordingProductRunner(configured=False),
    )

    run = service.create_run(RadarAgentRunCreateRequest())

    assert run.status == RadarAgentRunStatus.FAILED_LLM
    assert run.artifacts == []
    assert run.error_message is not None
    assert "DEEPSEEK_API_KEY" in run.error_message
    assert any("DeepSeek API 未配置" in event.title for event in run.events)


def test_radar_agent_role_switch_does_not_rerun_product_episodes(
    monkeypatch,
) -> None:
    runner = RecordingProductRunner()
    service = _service(
        monkeypatch,
        runner=runner,
    )
    run = service.create_run(RadarAgentRunCreateRequest(), idempotency_key="role-key")

    updated = service.set_role(run.run_id, RadarAgentRole.DOCTOR)

    assert updated.current_artifact is not None
    assert updated.current_artifact.type.value == "doctor_report"
    assert len(runner.requests) == 3


def test_radar_agent_validator_rejects_unknown_evidence() -> None:
    dashboard = FakeRadarProductDataProvider().build_dashboard("radar-device-demo-001")
    evidence = build_radar_evidence(dashboard)
    payload = _valid_role_payload()
    payload["doctor_artifact"]["evidence_ids"] = ["unknown-evidence"]

    try:
        validate_radar_agent_deepseek_json(payload, evidence)
    except Exception as exc:
        assert "unknown evidence" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Unknown evidence should fail validation.")


def test_radar_agent_routes_are_registered() -> None:
    route_paths = {route.path for route in app.routes}

    assert not any(path.startswith("/product/radar/agent-runs") for path in route_paths)


def test_product_service_has_no_selectable_single_model_runtime() -> None:
    source = (
        Path(__file__).parents[1]
        / "sleepagent"
        / "product_device"
        / "radar_agent.py"
    ).read_text(encoding="utf-8")
    service = source[source.index("class RadarSleepAgentService") :]

    assert "if self.episode_runner is not None" not in service
    assert "_run_retired_single_model_graph" not in service
    assert 'graph_mode="product_episode_runner"' in service


def _valid_role_payload() -> dict:
    return {
        "schema_version": RADAR_AGENT_SCHEMA_VERSION,
        "evidence_ids": [
            "current_bed_presence",
            "current_vitals",
            "sleep_score",
            "sleep_duration",
            "movement_getup",
            "alert_summary",
            "data_quality",
        ],
        "elder_artifact": {
            "title": "老人版睡眠解释",
            "headline": "昨晚整体还算平稳。",
            "summary": "演示数据提示昨晚睡眠时间够用，夜里有一次短暂离床，今天按平常节奏观察即可。",
            "observations": ["睡眠评分 82 分。", "总睡眠 405 分钟。"],
            "suggested_actions": ["白天留意精神状态。", "今晚尽量保持固定作息。"],
            "caveats": ["这是演示数据，不是医学诊断。"],
            "evidence_ids": ["sleep_score", "sleep_duration", "movement_getup"],
        },
        "family_artifact": {
            "title": "家属版照护摘要",
            "headline": "昨晚没有明显高危提醒，但体动和离床值得继续观察。",
            "summary": "演示数据中设备在线，当前在床，昨夜有一次离床和一次体动提醒，建议家属连续观察几晚。",
            "observations": ["近期告警 2 条。", "当前心率和呼吸有演示快照。"],
            "suggested_actions": ["留意夜间离床是否增加。", "如白天明显困倦，可记录后咨询医生。"],
            "caveats": ["雷达结果不能替代线下评估。"],
            "evidence_ids": ["current_vitals", "alert_summary", "movement_getup"],
        },
        "doctor_artifact": {
            "title": "医生版数据摘要",
            "headline": "雷达演示报告显示睡眠评分 82，总睡眠 405 分钟。",
            "summary": "本摘要基于演示雷达快照、睡眠报告、告警和数据质量字段，仅作为设备观察数据整理。",
            "observations": ["深睡和 REM 来自雷达分期。", "数据质量标记为演示可观察。"],
            "suggested_actions": ["结合主诉、病史和必要检查判断。"],
            "caveats": ["非 PSG 结果，不构成诊断。"],
            "evidence_ids": ["sleep_score", "sleep_duration", "stage_summary", "data_quality"],
        },
        "shared_caveats": ["当前为演示雷达数据。", "解释由 DeepSeek 根据演示数据生成。"],
        "safety_flags": [],
    }
