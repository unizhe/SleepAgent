from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.product_runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.product_runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.product_runtime.runtime_contracts import (
    ProductEpisodeRunRequest,
)
from sleepagent.product_runtime.runtime_factory import (
    build_deterministic_product_runtime_bundle,
)


NOW = datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc)


def _request(
    episode_type: EpisodeType,
    *,
    episode_id: str,
    audience_role: str | None = None,
    doctor_material: bool = False,
) -> ProductEpisodeRunRequest:
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 8, 7),
        date_end=date(2026, 8, 7),
        valid_night_count=1,
    )
    snapshot = FactSnapshot.create(
        fact_snapshot_id=f"snapshot:{episode_id}",
        binding=AuthenticatedBinding(
            actor_id="synthetic-elder",
            subject_id="synthetic-subject",
            role="elder",
            authorization_scope=("read_sleep_data", "draft_material"),
        ),
        source_scope=scope,
        canonical_data_version="canonical-replay-v1",
        source_refs=("night:synthetic:2026-08-07",),
        created_at=NOW,
    )
    return ProductEpisodeRunRequest(
        episode_id=episode_id,
        episode_type=episode_type,
        objective="回顾模拟睡眠记录并生成安全、可追溯的说明",
        fact_snapshot=snapshot,
        user_text="请回顾昨晚的模拟睡眠记录。",
        audience_role=audience_role,
        doctor_material=doctor_material,
        tool_inputs={
            "radar.get_night_evidence": {
                "source_refs": ["night:synthetic:2026-08-07"],
            },
            "radar.assess_data_quality": {
                "coverage_ratio": 0.94,
                "source_refs": ["night:synthetic:2026-08-07"],
            },
            "care.read_catalog": {},
            "care.read_state": {},
            "artifact.render": {"content": "synthetic replay material"},
        },
        personalized=True,
    )


@pytest.mark.parametrize(
    ("deployment_mode", "data_mode"),
    (("production", "replay"), ("test", "live")),
)
def test_deterministic_model_is_replay_nonproduction_only(
    deployment_mode: str,
    data_mode: str,
) -> None:
    with pytest.raises(ValueError, match="replay.*non-production"):
        DeterministicReplayStructuredAgentModel(
            deployment_mode=deployment_mode,
            data_mode=data_mode,
        )


def test_deterministic_runner_completes_real_morning_review() -> None:
    model = DeterministicReplayStructuredAgentModel(deployment_mode="test")
    runtime_bundle = build_deterministic_product_runtime_bundle(
        model=model,
    )
    runner = runtime_bundle.runner

    result = runner.run(
        _request(
            EpisodeType.MORNING_REVIEW,
            episode_id="episode:deterministic:morning",
        )
    )

    assert result.receipt.status == EpisodeStatus.COMPLETE, (
        result.receipt.failure_codes
    )
    assert result.publication_delivered
    assert result.publication is not None
    assert {item.agent_id for item in result.accepted_work_products} == {
        AgentId.EVIDENCE_REASONING,
        AgentId.SLEEP_CARE,
    }
    assert all(
        item.provider == "sleepagent-deterministic-replay"
        and item.model_id == "deterministic-replay-structured-agent-v1"
        for item in result.agent_invocations
    )


def test_deterministic_runner_exercises_care_and_safety_roles() -> None:
    model = DeterministicReplayStructuredAgentModel(
        deployment_mode="development"
    )
    runtime_bundle = build_deterministic_product_runtime_bundle(
        model=model,
    )
    runner = runtime_bundle.runner

    care = runner.run(
        _request(
            EpisodeType.CARE_PLAN,
            episode_id="episode:deterministic:care",
        )
    )
    material = runner.run(
        _request(
            EpisodeType.ROLE_MATERIAL,
            episode_id="episode:deterministic:material",
            audience_role="doctor",
            doctor_material=True,
        )
    )

    assert care.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert AgentId.CARE_STRATEGY in {
        item.agent_id for item in care.accepted_work_products
    }
    assert material.receipt.status == EpisodeStatus.COMPLETE, (
        material.receipt.failure_codes
    )
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in material.accepted_work_products
    }
    assert material.publication is not None
    assert material.publication.artifact_kind == "doctor_material"
