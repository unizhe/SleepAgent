from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.product_runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeType,
    FactSnapshot,
    InvocationOutcome,
    SourceScope,
    SourceScopeKind,
    ToolEffect,
    TrustLabel,
    stable_hash,
)
from sleepagent.product_runtime.governance import (
    GOVERNANCE_VERSION,
    CareActionCatalog,
    CareActionDefinition,
    CareContextState,
    CareTransitionEvent,
    InMemoryCareContextStore,
)
from sleepagent.product_runtime.policies.workflow import (
    CANONICAL_WORKFLOW_POLICY,
)
from sleepagent.product_runtime.registry import (
    EPISODE_DEFINITIONS,
    REGISTRY_VERSION,
    InvocationPolicyError,
)
from sleepagent.product_runtime.runtime_ports import (
    ProductToolExecutionContext,
)
from sleepagent.product_runtime.services.runtime_capabilities import (
    ProductRuntimeReadService,
)
from sleepagent.product_runtime.tooling import (
    CoreProductToolService,
    ProductToolError,
    ProductToolExecutor,
)
from sleepagent.product_runtime.schemas import RadarNightSummary


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)


def snapshot(
    *, active_constraint_codes: tuple[str, ...] = ()
) -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="s1",
        binding=AuthenticatedBinding(
            actor_id="a1", subject_id="u1", role="elder"
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.SEVEN_DAY,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 7, 20),
            date_end=date(2026, 7, 26),
            valid_night_count=7,
        ),
        canonical_data_version="v1",
        active_constraint_codes=active_constraint_codes,
        source_refs=(
            "range:1",
            *tuple(item.source_report_ref for item in trend_summaries()),
        ),
        created_at=NOW,
    )


def trend_summaries() -> list[RadarNightSummary]:
    return [
        RadarNightSummary(
            radar_device_id="radar-1",
            subject_id="u1",
            night_of=date(2026, 7, 24 + offset),
            timezone_name="Asia/Shanghai",
            total_sleep_minutes=value,
            data_coverage_ratio=0.95,
            explainable_metrics={"calibration_state": "known_uncalibrated"},
            source_report_ref=(
                f"night-summary:radar-1:2026-07-{24 + offset:02d}"
            ),
        )
        for offset, value in enumerate((420.0, 390.0, 360.0))
    ]


def trend_arguments() -> dict[str, object]:
    return {
        "night_summaries": [
            item.model_dump(mode="json") for item in trend_summaries()
        ]
    }


def runtime_read_executor(
    service: ProductRuntimeReadService,
) -> ProductToolExecutor:
    return ProductToolExecutor(
        handlers={
            "care.read_state": service.read_current_care_state,
            "care.read_catalog": service.read_care_catalog,
            "care.read_constraints": service.read_care_constraints,
            "policy.read": service.read_runtime_policy,
        },
        core_service=CoreProductToolService(),
    )


def runtime_read_service() -> ProductRuntimeReadService:
    store = InMemoryCareContextStore()
    store.compare_and_set(
        "u1",
        0,
        CareContextState(
            subject_id="u1",
            version=1,
            active_primary_action={"candidate_id": "care:active"},
            transition_history=[
                CareTransitionEvent(
                    strategy_id="strategy:persisted",
                    disposition="propose",
                    to_candidate_id="care:active",
                    evidence_packet_refs=["evidence:persisted"],
                    committed_at=NOW,
                )
            ],
        ),
    )
    catalog = CareActionCatalog(
        definitions=[
            CareActionDefinition(
                care_action_id="reviewed-test-action",
                version=7,
                allowed_parameters={"minutes": (5.0, 15.0)},
                contraindication_codes=["constraint:fall-risk"],
            )
        ]
    )
    return ProductRuntimeReadService(care_store=store, care_catalog=catalog)


def test_runtime_read_service_derives_catalog_constraints_state_and_policy() -> None:
    service = runtime_read_service()
    executor = runtime_read_executor(service)
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=snapshot(
            active_constraint_codes=("constraint:fall-risk",)
        ),
        episode_id="episode-care-followup",
    )

    catalog = executor.execute("care.read_catalog", {}, context=context)
    constraints = executor.execute(
        "care.read_constraints",
        {"care_action_ids": ["reviewed-test-action"]},
        context=context,
    )
    state = executor.execute("care.read_state", {}, context=context)
    policy = executor.execute("policy.read", {}, context=context)

    assert catalog.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert [item["care_action_id"] for item in catalog.receipt.output["actions"]] == [
        "reviewed-test-action"
    ]
    assert catalog.receipt.output["catalog_version"] == GOVERNANCE_VERSION
    assert constraints.receipt.output["constraints"] == [
        {
            "care_action_id": "reviewed-test-action",
            "version": 7,
            "allowed_parameters": {"minutes": (5.0, 15.0)},
            "contraindication_codes": ["constraint:fall-risk"],
            "active_contraindication_codes": ["constraint:fall-risk"],
            "delivery_required": False,
            "allowed_delivery_timings": [],
            "allowed_delivery_modalities": [],
        }
    ]
    assert constraints.receipt.output["active_constraint_codes"] == [
        "constraint:fall-risk"
    ]
    assert state.receipt.output["state"]["version"] == 1
    assert state.receipt.output["state"]["transition_history"][0]["strategy_id"] == (
        "strategy:persisted"
    )
    assert state.receipt.output["state"]["transition_history"][0][
        "evidence_packet_refs"
    ] == ["evidence:persisted"]
    assert policy.receipt.output["registry_version"] == REGISTRY_VERSION
    assert policy.receipt.output["governance_version"] == GOVERNANCE_VERSION
    assert policy.receipt.output["workflow_policy"] == {
        "version": CANONICAL_WORKFLOW_POLICY.version,
        "invariants": [
            item.value for item in CANONICAL_WORKFLOW_POLICY.invariants
        ],
    }
    assert {
        item.receipt.tool_name: item.receipt.tool_version
        for item in (catalog, constraints, state, policy)
    } == {
        "care.read_catalog": "care.read_catalog.v2",
        "care.read_constraints": "care.read_constraints.v2",
        "care.read_state": "care.read_state.v2",
        "policy.read": "policy.read.v2",
    }
    assert "care.read_state" in EPISODE_DEFINITIONS[
        EpisodeType.CARE_FOLLOWUP
    ].required_tools
    assert "care.read_feedback" not in EPISODE_DEFINITIONS[
        EpisodeType.CARE_FOLLOWUP
    ].required_tools


def test_tool_receipt_bounds_large_fact_snapshot_provenance() -> None:
    source_refs = tuple(f"canonical-observation:{index}" for index in range(75))
    fact_snapshot = FactSnapshot.create(
        fact_snapshot_id="large-snapshot",
        binding=AuthenticatedBinding(
            actor_id="a1", subject_id="u1", role="elder"
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 7, 26),
            date_end=date(2026, 7, 26),
            valid_night_count=1,
        ),
        canonical_data_version="v1",
        source_refs=source_refs,
        created_at=NOW,
    )
    executor = ProductToolExecutor(core_service=CoreProductToolService())

    result = executor.execute(
        "runtime.build_fact_snapshot",
        {},
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=fact_snapshot,
            episode_id="episode-large-snapshot",
        ),
    )

    assert result.receipt.output["source_ref_count"] == len(source_refs)
    assert result.receipt.output["source_refs"] == list(source_refs[:50])
    assert result.receipt.source_refs == list(source_refs[:50])


def test_runtime_interaction_write_is_receipted_and_replayed_exactly_once() -> None:
    calls: list[dict[str, object]] = []

    def select_once(arguments, _context):
        calls.append(arguments)
        return {
            "selection": {"selection_id": "selection-1"},
            "source_refs": ["selection-1"],
        }

    executor = ProductToolExecutor(
        handlers={"questionnaire.select_profile": select_once},
        core_service=CoreProductToolService(),
    )
    arguments = {"request": {"request_id": "request-1"}}
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=snapshot(),
        episode_id="episode-questionnaire",
    )

    first = executor.execute(
        "questionnaire.select_profile", arguments, context=context
    )
    replay = executor.execute(
        "questionnaire.select_profile", arguments, context=context
    )

    assert calls == [arguments]
    assert replay == first
    assert first.receipt.effect is ToolEffect.STATE_WRITE
    assert first.receipt.tool_version == "questionnaire.select_profile.v2"
    assert first.receipt.idempotency_key is not None
    assert first.receipt.outcome is InvocationOutcome.SUCCEEDED

    collision = executor.execute(
        "questionnaire.select_profile",
        {"request": {"request_id": "request-1", "max_questions": 2}},
        context=context,
    )
    assert calls == [arguments]
    assert collision.receipt.outcome is InvocationOutcome.FAILED
    assert collision.receipt.error_code == "RuntimeInteractionPayloadConflict"
    assert collision.receipt.idempotency_key == first.receipt.idempotency_key

    with pytest.raises(InvocationPolicyError):
        executor.execute(
            "questionnaire.select_profile",
            arguments,
            context=context.model_copy(update={"caller": AgentId.SLEEP_CARE}),
        )


def test_cached_capture_is_revalidated_after_its_authority_window() -> None:
    calls: list[dict[str, object]] = []

    def capture_once(arguments, _context):
        calls.append(arguments)
        if len(calls) > 1:
            raise ValueError("captured Habit Safety event authority expired")
        return {
            "capture": {
                "selection_id": "selection-1",
                "answers": [
                    {
                        "episode_valid_until": (NOW + timedelta(hours=3)).isoformat()
                    }
                ],
                "safety_events": [
                    {"valid_until": (NOW + timedelta(hours=1)).isoformat()}
                ],
            }
        }

    executor = ProductToolExecutor(
        handlers={"questionnaire.capture_profile": capture_once},
        core_service=CoreProductToolService(),
    )
    base_arguments = {
        "selection": {
            "selection_id": "selection-1",
            "episode_id": "episode-questionnaire",
            "subject_id": "u1",
            "actor_id": "a1",
            "role": "elder",
        },
        "answers": [{"concept_id": "habit.observed_snoring"}],
    }
    context = ProductToolExecutionContext(
        caller="runtime",
        fact_snapshot=snapshot(),
        episode_id="episode-questionnaire",
    )

    first = executor.execute(
        "questionnaire.capture_profile",
        {**base_arguments, "_now": NOW.isoformat()},
        context=context,
    )
    expired = executor.execute(
        "questionnaire.capture_profile",
        {
            **base_arguments,
            "_now": (NOW + timedelta(hours=2)).isoformat(),
        },
        context=context,
    )

    assert first.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert expired.receipt.outcome is InvocationOutcome.FAILED
    assert expired.receipt.error_code == "ValueError"
    assert len(calls) == 2
    assert expired.receipt.idempotency_key == first.receipt.idempotency_key


@pytest.mark.parametrize(
    "tool_name",
    (
        "care.read_catalog",
        "care.read_constraints",
        "care.read_state",
        "policy.read",
    ),
)
def test_runtime_read_service_rejects_caller_supplied_data(
    tool_name: str,
) -> None:
    result = runtime_read_executor(runtime_read_service()).execute(
        tool_name,
        {"data": {"forged": True}},
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-runtime-read-authority",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.output == {}
    assert result.receipt.error_code == "ValueError"


def test_trend_is_deterministic_tool_not_agent() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )
    assert result.receipt.output["tool_version"].startswith(
        "sleepagent-trend-analysis-tool"
    )
    assert result.context_item.trust_label == TrustLabel.TOOL_OUTPUT_UNTRUSTED


def test_agent_cannot_self_attest_scalar_trend_values() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "trend.calculate_metrics",
        {"values": [7.0, 6.5, 6.0], "source_refs": ["range:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_agent_selector_reuses_only_runtime_bound_tool_output() -> None:
    executor = ProductToolExecutor(core_service=CoreProductToolService())
    runtime_result = executor.execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    selected = executor.execute(
        "trend.calculate_metrics",
        {
            "bound_tool_invocation_id": (
                runtime_result.receipt.tool_invocation_id
            )
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert selected.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert selected.receipt.output == runtime_result.receipt.output


def test_runtime_bound_tool_output_is_episode_scoped_and_releasable() -> None:
    executor = ProductToolExecutor(core_service=CoreProductToolService())
    bound = executor.execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-a",
        ),
    )
    selector = {
        "bound_tool_invocation_id": bound.receipt.tool_invocation_id
    }

    cross_episode = executor.execute(
        "trend.calculate_metrics",
        selector,
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-b",
        ),
    )
    assert cross_episode.receipt.outcome is InvocationOutcome.FAILED
    assert executor.runtime_binding_count("episode-a") == 1

    executor.release_episode("episode-a")
    released = executor.execute(
        "trend.calculate_metrics",
        selector,
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-a",
        ),
    )
    assert released.receipt.outcome is InvocationOutcome.FAILED
    assert executor.runtime_binding_count("episode-a") == 0


def test_agent_selector_fails_when_runtime_has_not_bound_tool_output() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "trend.calculate_metrics",
        {"bound_tool_invocation_id": "tool:trend.calculate_metrics:missing"},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "ProductToolError"


def test_invalid_runtime_receipt_is_never_available_to_agent_selector() -> None:
    arguments = trend_arguments()

    def invalid_output(values, context):
        return {
            "source_refs": [f"source:{index}" for index in range(51)]
        }

    executor = ProductToolExecutor(
        {"trend.calculate_metrics": invalid_output},
        core_service=CoreProductToolService(),
    )
    with pytest.raises(ValueError, match="at most 50"):
        executor.execute(
            "trend.calculate_metrics",
            arguments,
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=snapshot(),
                episode_id="episode-1",
            ),
        )

    expected_ref = (
        "tool:trend.calculate_metrics:" + stable_hash(arguments)[:16]
    )
    selected = executor.execute(
        "trend.calculate_metrics",
        {"bound_tool_invocation_id": expected_ref},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert selected.receipt.outcome is InvocationOutcome.FAILED
    assert selected.receipt.error_code == "ProductToolError"


def test_reviewed_knowledge_fails_closed_for_unreviewed_content() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "knowledge.retrieve_reviewed",
        {"reviewed": False, "passages": ["unsafe"]},
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=snapshot(),
        ),
    )
    assert result.receipt.outcome == InvocationOutcome.FAILED
    assert result.context_item is None


def test_artifact_render_rejects_untyped_content_compatibility_input() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "artifact.render",
        {"content": "draft", "source_refs": ["claim:1"]},
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )
    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "ValueError"


def test_sleepcare_cannot_self_attest_unbound_artifact_content() -> None:
    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
        "artifact.render",
        {"content": "draft", "source_refs": ["claim:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_model_agent_cannot_execute_state_change() -> None:
    with pytest.raises(Exception, match="state.commit_care"):
        ProductToolExecutor(
            core_service=CoreProductToolService()
        ).execute(
            "state.commit_care",
            {},
            context=ProductToolExecutionContext(
                caller=AgentId.CARE_STRATEGY,
                fact_snapshot=snapshot(),
            ),
        )


def test_missing_handler_fails_explicitly() -> None:
    executor = ProductToolExecutor(core_service=CoreProductToolService())
    executor.handlers.pop("trend.calculate_metrics")
    with pytest.raises(ProductToolError, match="no handler"):
        executor.execute(
            "trend.calculate_metrics",
            {},
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=snapshot(),
            ),
        )
