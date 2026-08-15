from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sleepagent.simulation.cli import (
    ActorAssertionSigner,
    DemoCliError,
    _remember_habit,
    _verify_demo_model_evidence,
    _verify_longitudinal_personalization_pins,
    render_product_demo,
    run_product_demo_story,
    show_product_demo,
    verify_abnormal_backend,
    verify_backend,
    verify_bounded_retention_backend,
    verify_effects_reconciliation_backend,
    verify_command_backend,
    verify_read_models_backend,
)
from sleepagent.simulation.seed_registry import load_replay_seed_registry


pytestmark = pytest.mark.unit


def _model_proof_attempt(*invocations: dict) -> dict:
    return {
        "role_runs": [
            {
                "agent_invocations": list(invocations),
            }
        ]
    }


def _real_invocation(request_id: str = "provider-request:one") -> dict:
    return {
        "agent_id": "evidence_reasoning",
        "provider": "openai-compatible",
        "model_id": "deepseek-live",
        "provider_request_id": request_id,
    }


def _care_catalog_preflight_invocation() -> dict:
    return {
        "agent_id": "care_strategy",
        "provider": "sleepagent-deterministic",
        "model_id": "care-catalog-preflight.v1",
        "provider_request_id": None,
    }


def test_live_model_proof_accepts_all_real_provider_invocations() -> None:
    _verify_demo_model_evidence(
        model="live",
        story_id="worsening-care",
        selected_attempts=[
            _model_proof_attempt(
                _real_invocation("provider-request:evidence"),
                _real_invocation("provider-request:care"),
            )
        ],
        technical_trace={},
    )


def test_live_model_proof_excludes_only_exact_care_catalog_preflight() -> None:
    _verify_demo_model_evidence(
        model="live",
        story_id="worsening-care",
        selected_attempts=[
            _model_proof_attempt(
                _care_catalog_preflight_invocation(),
                _real_invocation("provider-request:evidence"),
                _real_invocation("provider-request:care"),
            )
        ],
        technical_trace={},
    )


def test_live_model_proof_rejects_preflight_without_real_provider() -> None:
    with pytest.raises(DemoCliError, match="no substantive real-provider"):
        _verify_demo_model_evidence(
            model="live",
            story_id="worsening-care",
            selected_attempts=[
                _model_proof_attempt(_care_catalog_preflight_invocation())
            ],
            technical_trace={},
        )


def test_live_model_proof_rejects_unrecognized_deterministic_invocation() -> None:
    unrecognized = {
        **_care_catalog_preflight_invocation(),
        "agent_id": "sleep_care",
    }
    with pytest.raises(DemoCliError, match="real configured provider"):
        _verify_demo_model_evidence(
            model="live",
            story_id="worsening-care",
            selected_attempts=[
                _model_proof_attempt(unrecognized, _real_invocation())
            ],
            technical_trace={},
        )


def test_live_model_proof_rejects_real_provider_without_request_id() -> None:
    with pytest.raises(DemoCliError, match="real configured provider"):
        _verify_demo_model_evidence(
            model="live",
            story_id="worsening-care",
            selected_attempts=[_model_proof_attempt(_real_invocation(""))],
            technical_trace={},
        )


def test_urgent_zero_model_proof_remains_strict() -> None:
    _verify_demo_model_evidence(
        model="live",
        story_id="urgent-safety",
        selected_attempts=[],
        technical_trace={
            "product_attempt_count": 0,
            "durable_invocations": [],
            "fast_path_succeeded_count": 1,
        },
    )
    with pytest.raises(DemoCliError, match="zero-model safety path"):
        _verify_demo_model_evidence(
            model="live",
            story_id="urgent-safety",
            selected_attempts=[_model_proof_attempt(_real_invocation())],
            technical_trace={
                "product_attempt_count": 1,
                "durable_invocations": [],
                "fast_path_succeeded_count": 1,
            },
        )


def test_deterministic_model_proof_keeps_original_provider_rule() -> None:
    _verify_demo_model_evidence(
        model="deterministic",
        story_id="worsening-care",
        selected_attempts=[
            _model_proof_attempt(
                {
                    "agent_id": "evidence_reasoning",
                    "provider": "sleepagent-deterministic-replay",
                    "provider_request_id": None,
                }
            )
        ],
        technical_trace={},
    )
    with pytest.raises(DemoCliError, match="unexpected model provider"):
        _verify_demo_model_evidence(
            model="deterministic",
            story_id="worsening-care",
            selected_attempts=[
                _model_proof_attempt(_care_catalog_preflight_invocation())
            ],
            technical_trace={},
        )


class ProductDemoHttp:
    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        replay = {"data_mode": "replay", "synthetic_non_release": True}
        if path == "/livez":
            return {"status": "alive"}
        if path == "/demo/v1/seed":
            assert kwargs["payload"] == {
                "artifact_family": "canonical-replay-fixtures",
                "scenario_id": self.scenario_id,
                "batch_size": 100,
            }
            return {**replay, "operation_id": "root-1", "generation": 1}
        if path == "/demo/v1/operations/root-1":
            if self.scenario_id == "urgent-zero-model":
                return {
                    **replay,
                    "operation_id": "root-1",
                    "state": "failed",
                    "error_code": "unexpected_urgent_route",
                }
            subject = (
                "synthetic-subject-normal-001"
                if self.scenario_id == "normal-one-night"
                else "synthetic-subject-lin-001"
            )
            return {
                **replay,
                "operation_id": "root-1",
                "state": "succeeded",
                "result": {
                    **replay,
                    "subject_ref": subject,
                    "night_episode_id": "episode-1",
                    "night_episode_revision_id": "revision-1",
                    "analysis_revision_id": "analysis-1",
                },
            }
        if path == "/demo/v1/clock":
            return {
                **replay,
                "generation": 1,
                "scenario_time": "2026-03-05T20:00:00+08:00",
            }
        if path == "/demo/v1/advance":
            return {**replay, "operation_id": "advance-1", "generation": 1}
        if path == "/demo/v1/operations/advance-1":
            return {
                **replay,
                "operation_id": "advance-1",
                "state": "succeeded",
                "result": {**replay, "released_fact_count": 1491},
            }
        if path == "/demo/v1/trace":
            return {
                **replay,
                "generation": 1,
                "entries": [
                    {
                        "sequence": 1,
                        "event_type": "journey_reserved",
                        "state": "accepted",
                        "operation_id": "root-1",
                    },
                    {
                        "sequence": 2,
                        "event_type": "journey_checkpoint",
                        "state": (
                            "failed"
                            if self.scenario_id == "urgent-zero-model"
                            else "succeeded"
                        ),
                        "operation_id": "root-1",
                        "night_episode_revision_id": "revision-1",
                        "fast_path_operation_id": "fast-1",
                        "product_operation_id": (
                            None
                            if self.scenario_id == "urgent-zero-model"
                            else "product-1"
                        ),
                        "analysis_revision_id": (
                            None
                            if self.scenario_id == "urgent-zero-model"
                            else "analysis-1"
                        ),
                    },
                ],
                "next_cursor": None,
            }
        if path == "/demo/v1/technical-trace":
            urgent = self.scenario_id == "urgent-zero-model"
            attempts = [] if urgent else [
                {
                    "analysis": {"analysis_revision_id": "analysis-1"},
                    "role_runs": [
                        {
                            "role": "elder",
                            "agent_invocations": [
                                {
                                    "agent_id": "sleep_care",
                                    "provider": "openai-compatible",
                                    "model_id": "live-model",
                                    "provider_request_id": "request-1",
                                    "skill_lock_hash": "a" * 64,
                                }
                            ],
                            "tool_receipts": [],
                            "accepted_work_products": [],
                        }
                    ],
                }
            ]
            return {
                **replay,
                "schema_version": "demo_technical_trace.v1",
                "root_operation_id": "root-1",
                "namespace_generation": 1,
                "run_id": "run-1",
                "arm_id": "arm-1",
                "subject_id": (
                    "synthetic-subject-urgent-001"
                    if urgent
                    else "synthetic-subject-normal-001"
                    if self.scenario_id == "normal-one-night"
                    else "synthetic-subject-lin-001"
                ),
                "journey_state": "failed" if urgent else "succeeded",
                "journey_error_code": (
                    "unexpected_urgent_route" if urgent else None
                ),
                "journey_result": None,
                "product_operation_count": 0 if urgent else 1,
                "product_attempt_count": 0 if urgent else 1,
                "fast_path_succeeded_count": 1,
                "product_attempts": attempts,
                "durable_invocations": [],
                "habit_revisions": [],
                "memory_revisions": [],
                "memory_read_receipts": [],
            }
        raise AssertionError((method, path, kwargs))


class ProductDemoProduct:
    def __init__(self, *, night_count: int, urgent: bool = False) -> None:
        self.night_count = night_count
        self.urgent = urgent
        self.calls: list[tuple[str, str]] = []
        self.habit_profile_version = 0

    def night_episodes(self, *, subject_id, **kwargs):
        del kwargs
        self.calls.append(("night_episodes", subject_id))
        return (
            {
                "schema_version": "night_episode_page_response.v1",
                "items": [
                    {
                        "night_episode_id": f"episode-{index}",
                        "subject_id": subject_id,
                        "local_sleep_date": f"2026-03-{index:02d}",
                        "episode_local_date": f"2026-03-{index:02d}",
                        "assignment_basis": "observed_wake",
                        "lifecycle_state": "finalized",
                        "data_sufficiency": "sufficient",
                        "quality_flags": ["synthetic_replay"],
                        "current_revision_number": 1,
                    }
                    for index in range(1, self.night_count + 1)
                ],
                "page": {"next_cursor": None},
            },
            {
                "X-SleepAgent-Data-Mode": "replay",
                "X-SleepAgent-Synthetic-Non-Release": "true",
            },
        )

    def today(self, *, subject_id, role, **kwargs):
        del kwargs
        self.calls.append(("today:" + role, subject_id))
        if self.urgent:
            return {
                "schema_version": "product_sleep_today.v1",
                "data_mode": "replay",
                "synthetic_non_release": True,
                "state": "no_data",
                "subject_ref": subject_id,
                "role": role,
                "content": None,
            }
        content = {
            "audience": role,
            "summary_text": f"{role} public summary",
            "context_notice": "Public Product projection.",
        }
        if role == "doctor":
            content["evidence_refs"] = ["evidence:public-1"]
        return {
            "schema_version": "product_sleep_today.v1",
            "data_mode": "replay",
            "synthetic_non_release": True,
            "state": "ready",
            "subject_ref": subject_id,
            "role": role,
            "episode_id": "episode-1",
            "analysis_revision_id": "analysis-1",
            "content": content,
        }

    def habit_profile(self, *, subject_id, **kwargs):
        del kwargs
        return {
            "schema_version": "habit_profile.v2",
            "profile_version": self.habit_profile_version,
            "profile_hash": None if self.habit_profile_version == 0 else "a" * 64,
            "current_facts": [],
            "stale_concept_ids": [],
            "disputed_concept_ids": [],
        }

    def habit_questions(self, *, concept_id, **kwargs):
        del kwargs
        return {
            "selection": {
                "selection_id": "selection-1",
                "selected_concepts": [concept_id],
            },
            "questions": [
                {"concept_id": concept_id, "concept_version": "1.0.0"}
            ],
            "profile_version": self.habit_profile_version,
        }

    def habit_change(self, *, payload, **kwargs):
        del kwargs
        assert payload["selection_id"] == "selection-1"
        return {
            "pending_changes": [
                {
                    "change_id": "change-1",
                    "change_hash": "b" * 64,
                    "confirmation_handle": "handle-1",
                }
            ]
        }

    def confirm_personalization(self, *, capability, payload, **kwargs):
        del kwargs
        assert capability == "habit"
        assert payload == {
            "change_id": "change-1",
            "change_hash": "b" * 64,
            "confirmation_handle": "handle-1",
        }
        self.habit_profile_version = 1
        return {
            "capability": "habit",
            "state_version": 1,
            "revision_ref": "habit:fact-1",
            "revision_hash": "c" * 64,
        }

    def read_model(self, *, kind, subject_id, role, **kwargs):
        del kwargs
        self.calls.append((kind, subject_id))
        if kind == "trends":
            items = [
                {
                    "episode_local_date": f"2026-03-{index:02d}",
                    "projection_state": "ready",
                    "sleep_window_minutes": 480,
                }
                for index in range(1, self.night_count + 1)
            ]
        else:
            items = []
        return {
            "schema_version": {
                "trends": "product_sleep_trends.v1",
                "care": "product_sleep_care.v1",
            }[kind],
            "data_mode": "replay",
            "synthetic_non_release": True,
            "subject_ref": subject_id,
            "role": role,
            "items": items,
            "next_cursor": None,
        }

    def current_risk(self, *, subject_id, **kwargs):
        del kwargs
        self.calls.append(("risk", subject_id))
        risk_state = (
            "reviewed_urgent_signal" if self.urgent else "no_reviewed_signal"
        )
        return (
            {
                "schema_version": "current_risk_response.v1",
                "subject_id": subject_id,
                "risk_state": risk_state,
                "data_sufficiency": "sufficient",
                "reason_codes": ["reviewed_vendor_alert"] if self.urgent else [],
                "health_escalation_allowed": self.urgent,
            },
            {
                "X-SleepAgent-Data-Mode": "replay",
                "X-SleepAgent-Synthetic-Non-Release": "true",
            },
            200,
        )


class Client:
    def __init__(self, *, watermarked: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.watermarked = watermarked

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        watermark = {
            "data_mode": "replay" if self.watermarked else "live",
            "synthetic_non_release": self.watermarked,
        }
        if path == "/livez":
            return {"status": "alive"}
        if path == "/demo/v1/clock":
            return {**watermark, "generation": 1}
        if path == "/demo/v1/seed":
            return {**watermark, "operation_id": "operation-1", "generation": 1}
        if path == "/demo/v1/trace":
            return {
                **watermark,
                "generation": 1,
                "entries": [
                    {
                        "operation_id": "operation-1",
                        "state": "succeeded",
                    }
                ],
            }
        if path == "/demo/v1/operations/operation-1":
            return {
                **watermark,
                "operation_id": "operation-1",
                "state": "succeeded",
                "result": {
                    "subject_ref": "synthetic-subject-normal-001",
                    "night_episode_id": "episode-1",
                    "night_episode_revision_id": "episode-revision-1",
                    "analysis_revision_id": "analysis-revision-1",
                },
                "updated_at": "2026-01-02T08:00:00+00:00",
            }
        raise AssertionError(path)


class ProductClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def today(self, *, actor_id: str, subject_id: str, role: str) -> dict:
        self.calls.append((actor_id, subject_id, role))
        content: dict[str, object] = {
            "audience": role,
            "summary_text": f"{role} summary",
            "context_notice": "latest completed night",
        }
        if role == "doctor":
            content["evidence_refs"] = []
        return {
            "schema_version": "product_sleep_today.v1",
            "data_mode": "replay",
            "synthetic_non_release": True,
            "state": "ready",
            "subject_ref": subject_id,
            "role": role,
            "episode_id": "episode-1",
            "episode_revision_id": "episode-revision-1",
            "episode_local_date": "2026-01-01",
            "assignment_basis": "start_local_date",
            "analysis_revision_id": "analysis-revision-1",
            "projection_id": f"projection-{role}",
            "projection_version": 1,
            "committed_at": "2026-01-02T08:00:00+00:00",
            "content": content,
        }

    def night_episodes(
        self, *, actor_id: str, subject_id: str, role: str
    ) -> tuple[dict, dict[str, str]]:
        self.calls.append((actor_id, subject_id, "sleep_episode:" + role))
        return (
            {
                "schema_version": "night_episode_page_response.v1",
                "items": [
                    {
                        "night_episode_id": "episode-1",
                        "current_revision_id": "episode-revision-1",
                        "subject_id": subject_id,
                        "assignment_basis": "observed_wake",
                        "lifecycle_state": "awaiting_report",
                    }
                ],
                "page": {"next_cursor": None},
            },
            {
                "X-SleepAgent-Data-Mode": "replay",
                "X-SleepAgent-Synthetic-Non-Release": "true",
            },
        )


@pytest.mark.parametrize(
    ("scenario_id", "night_count"),
    [
        ("normal-one-night", 1),
        ("worsening-vital-trend", 4),
    ],
)
def test_product_show_uses_only_public_http_and_registry_summary(
    scenario_id: str,
    night_count: int,
) -> None:
    demo = ProductDemoHttp(scenario_id)
    product = ProductDemoProduct(night_count=night_count)

    result = show_product_demo(
        demo,
        product,
        scenario_id=scenario_id,
        wait_seconds=0.1,
        include_trace=True,
    )
    rendered = render_product_demo(result)

    assert result["scenario"]["night_count"] == night_count
    assert result["scenario"]["observation_count"] == (
        497 if night_count == 1 else 1988
    )
    assert "expected" not in json.dumps(result)
    assert "SleepAgent Terminal Product Demo" in rendered
    assert "Role Product outputs" in rendered
    assert "replay seed -> normalization -> NightEpisode" in rendered
    if night_count > 1:
        assert any(path == "/demo/v1/advance" for _, path, _ in demo.calls)
        assert "Trends" in rendered
    else:
        assert all(path != "/demo/v1/advance" for _, path, _ in demo.calls)
    assert {call[0] for call in product.calls} >= {
        "night_episodes",
        "today:elder",
        "today:family",
        "today:doctor",
        "care",
        "risk",
    }


def test_product_show_resumes_after_completed_advance_without_advancing_twice() -> None:
    class CompletedAdvanceDemo(ProductDemoHttp):
        def request(self, method: str, path: str, **kwargs):
            if path == "/demo/v1/clock":
                self.calls.append((method, path, kwargs))
                return {
                    "data_mode": "replay",
                    "synthetic_non_release": True,
                    "generation": 1,
                    "scenario_time": "2026-03-09T06:35:00+08:00",
                }
            return super().request(method, path, **kwargs)

    demo = CompletedAdvanceDemo("worsening-vital-trend")
    result = show_product_demo(
        demo,
        ProductDemoProduct(night_count=4),
        scenario_id="worsening-vital-trend",
        wait_seconds=0.1,
        include_trace=False,
    )

    assert result["advance_operation"]["resumed"] is True
    assert all(path != "/demo/v1/advance" for _, path, _ in demo.calls)


def test_product_show_presents_urgent_zero_model_without_fake_projections() -> None:
    demo = ProductDemoHttp("urgent-zero-model")
    product = ProductDemoProduct(night_count=1, urgent=True)

    result = show_product_demo(
        demo,
        product,
        scenario_id="urgent-zero-model",
        wait_seconds=0.1,
        include_trace=True,
    )
    rendered = render_product_demo(result)

    assert result["root_operation"]["error_code"] == "unexpected_urgent_route"
    assert "deterministic urgent boundary; zero-model Product path" in rendered
    assert rendered.count("Current public contract provides no Product output.") == 3
    assert "product=product-1" not in rendered
    assert "Product Runtime and role projections were not invoked" in rendered
    assert "Product Runtime -> role projections" not in rendered


def test_urgent_product_story_requires_zero_model_durable_evidence() -> None:
    demo = ProductDemoHttp("urgent-zero-model")

    result = run_product_demo_story(
        demo,
        ProductDemoProduct(night_count=1, urgent=True),
        story_id="urgent-safety",
        model="live",
        wait_seconds=0.1,
        include_trace=True,
    )

    assert result["story"]["selected_analysis_ids"] == []
    assert result["technical_trace"]["product_attempt_count"] == 0
    assert result["technical_trace"]["durable_invocations"] == []
    assert "Model: ZERO LLM" in render_product_demo(result)


def test_urgent_product_story_rejects_any_durable_provider_invocation() -> None:
    class LeakyUrgentDemo(ProductDemoHttp):
        def request(self, method, path, **kwargs):
            value = super().request(method, path, **kwargs)
            if path == "/demo/v1/technical-trace":
                value["durable_invocations"] = [
                    {
                        "agent_id": "evidence_reasoning",
                        "provider": "openai-compatible",
                        "provider_request_id": "provider-request:forbidden",
                    }
                ]
            return value

    with pytest.raises(DemoCliError, match="zero-model safety path"):
        run_product_demo_story(
            LeakyUrgentDemo("urgent-zero-model"),
            ProductDemoProduct(night_count=1, urgent=True),
            story_id="urgent-safety",
            model="live",
            wait_seconds=0.1,
            include_trace=True,
        )


def test_cold_start_story_confirms_exact_habit_and_requires_live_evidence() -> None:
    product = ProductDemoProduct(night_count=1)

    result = run_product_demo_story(
        ProductDemoHttp("normal-one-night"),
        product,
        story_id="cold-start",
        model="live",
        wait_seconds=0.1,
        include_trace=False,
    )

    assert result["actions"]["cold_start_profile"]["profile_version"] == 0
    assert result["actions"]["confirmed_profile"]["profile_version"] == 1
    assert result["technical_trace"]["product_attempt_count"] == 1
    assert "provider=openai-compatible" in render_product_demo(result)


def test_longitudinal_verifier_requires_material_habit_memory_evidence_and_receipts(
) -> None:
    actions = {
        "episode_a_analysis_id": "analysis-a",
        "episode_b_analysis_id": "analysis-b",
        "habit_profile_after_family_report": {
            "disputed_concept_ids": ["habit.nap_pattern"]
        },
    }

    def attempt(analysis_id: str, version: int, memory_text: str) -> dict:
        return {
            "analysis": {"analysis_revision_id": analysis_id},
            "role_runs": [
                {
                    "role": "elder",
                    "product_episode_id": f"episode-{version}",
                    "personalization": {
                        "habit_profile_version": version,
                        "memory_state_version": version,
                    },
                    "accepted_work_products": [
                        {
                            "agent_id": "evidence_reasoning",
                            "payload": {
                                "claims": [
                                    {
                                        "source_kind": "confirmed_habit",
                                        "statement": f"habit-v{version}",
                                    },
                                    {
                                        "source_kind": "confirmed_memory",
                                        "statement": memory_text,
                                    },
                                ]
                            },
                        }
                    ],
                }
            ],
        }

    attempts = [
        attempt("analysis-a", 1, "quiet_room"),
        attempt("analysis-b", 2, "soft_night_light"),
    ]
    trace = {
        "habit_revisions": [
            {
                "profile_version": 1,
                "source_actor_role": "elder",
                "confirmation_ref": "confirm-habit-1",
            },
            {
                "profile_version": 2,
                "source_actor_role": "family",
                "confirmation_ref": "confirm-habit-2",
            },
        ],
        "memory_revisions": [
            {"state_version": 1, "confirmation_ref": "confirm-memory-1"},
            {"state_version": 2, "confirmation_ref": "confirm-memory-2"},
        ],
        "memory_read_receipts": [
            {"product_episode_id": "episode-1"},
            {"product_episode_id": "episode-2"},
        ],
    }

    _verify_longitudinal_personalization_pins(actions, attempts, trace)

    claims = attempts[1]["role_runs"][0]["accepted_work_products"][0][
        "payload"
    ]["claims"]
    claims.pop()
    with pytest.raises(DemoCliError, match="both confirmed Habit and Memory"):
        _verify_longitudinal_personalization_pins(actions, attempts, trace)


def test_habit_demo_can_consume_an_actor_bound_prepared_question_receipt() -> None:
    class PreparedSelectionProduct(ProductDemoProduct):
        def habit_questions(self, **kwargs):
            del kwargs
            raise AssertionError("prepared selection must not request a new question")

    product = PreparedSelectionProduct(night_count=2)
    seed = load_replay_seed_registry().lookup(
        "canonical-replay-fixtures",
        "habit-family-report",
    )
    prepared = {
        "selection": {
            "selection_id": "selection-1",
            "selected_concepts": [["habit.nap_pattern", "1.0.0"]],
        },
        "questions": [
            {"concept_id": "habit.nap_pattern", "concept_version": "1.0.0"}
        ],
        "profile_version": 0,
    }

    result = _remember_habit(
        product,
        seed=seed,
        source_role="family",
        episode_id="episode-a",
        concept_id="habit.nap_pattern",
        value="多数天午睡",
        direct_observation=True,
        prepared_selection=prepared,
    )

    assert result["question"]["concept_id"] == "habit.nap_pattern"
    assert result["confirmation"]["state_version"] == 1


def test_product_show_default_omits_trace_http_and_section() -> None:
    demo = ProductDemoHttp("normal-one-night")

    result = show_product_demo(
        demo,
        ProductDemoProduct(night_count=1),
        scenario_id="normal-one-night",
        wait_seconds=0.1,
        include_trace=False,
    )

    assert result["trace"] is None
    assert all(path != "/demo/v1/trace" for _, path, _ in demo.calls)
    assert "Public execution trace" not in render_product_demo(result)


def test_product_show_marks_public_risk_scope_limitation() -> None:
    class RiskDeniedProduct(ProductDemoProduct):
        def current_risk(self, *, subject_id, **kwargs):
            del kwargs
            self.calls.append(("risk", subject_id))
            return ({"code": "authorization_denied"}, {}, 403)

    result = show_product_demo(
        ProductDemoHttp("normal-one-night"),
        RiskDeniedProduct(night_count=1),
        scenario_id="normal-one-night",
        wait_seconds=0.1,
        include_trace=False,
    )

    assert (
        "current public authorization/contract did not provide it (HTTP 403)"
        in render_product_demo(result)
    )


def test_product_show_rejects_scenarios_outside_phase_one() -> None:
    with pytest.raises(DemoCliError, match="show supports only"):
        show_product_demo(
            ProductDemoHttp("device-abnormal"),
            ProductDemoProduct(night_count=1),
            scenario_id="device-abnormal",
            wait_seconds=0.1,
            include_trace=False,
        )


def test_verifier_uses_only_http_surface_and_replay_watermarks() -> None:
    client = Client()

    result = verify_backend(
        client,  # type: ignore[arg-type]
        scenario_id="golden-15-night",
        model="deterministic",
        wait_seconds=0.1,
    )

    assert result["verified"] is True
    assert result["synthetic_non_release"] is True
    assert [path for _, path, _ in client.calls] == [
        "/livez",
        "/demo/v1/seed",
        "/demo/v1/clock",
        "/demo/v1/trace",
        "/demo/v1/operations/operation-1",
    ]
    seed_payload = client.calls[1][2]["payload"]
    assert set(seed_payload) == {"artifact_family", "scenario_id", "batch_size"}
    assert "expected" not in seed_payload
    assert "actions" not in seed_payload
    assert result["analysis_revision_id"] == "analysis-revision-1"


def test_verifier_proves_exact_three_role_today_projections() -> None:
    product = ProductClient()

    result = verify_backend(
        Client(),  # type: ignore[arg-type]
        scenario_id="normal-one-night",
        model="deterministic",
        wait_seconds=0.1,
        product_client=product,
    )

    assert [role for _, _, role in product.calls] == [
        "sleep_episode:elder",
        "elder",
        "family",
        "doctor",
    ]
    assert result["role_projection_ids"] == {
        "elder": "projection-elder",
        "family": "projection-family",
        "doctor": "projection-doctor",
    }


def test_restart_verifier_kills_only_after_public_waiting_product_trace() -> None:
    class RestartClient(Client):
        def request(self, method: str, path: str, **kwargs):
            value = super().request(method, path, **kwargs)
            if path == "/demo/v1/trace":
                value["entries"] = [
                    {
                        "operation_id": "operation-1",
                        "state": "waiting_product",
                    },
                    {
                        "operation_id": "operation-1",
                        "state": "succeeded",
                    },
                ]
            return value

    restarts: list[str] = []
    result = verify_backend(
        RestartClient(),  # type: ignore[arg-type]
        scenario_id="normal-one-night",
        model="deterministic",
        wait_seconds=0.1,
        mode="restart-worker-before-product-commit",
        restart_worker=lambda: restarts.append("worker"),
    )

    assert restarts == ["worker"]
    assert result["mode"] == "restart-worker-before-product-commit"


def test_verifier_rejects_internal_product_field_leakage() -> None:
    class LeakingProductClient(ProductClient):
        def today(self, *, actor_id: str, subject_id: str, role: str) -> dict:
            result = super().today(
                actor_id=actor_id,
                subject_id=subject_id,
                role=role,
            )
            result["content"]["source_refs"] = ["private-row-1"]
            return result

    with pytest.raises(DemoCliError, match="leaked internal fields"):
        verify_backend(
            Client(),  # type: ignore[arg-type]
            scenario_id="normal-one-night",
            model="deterministic",
            wait_seconds=0.1,
            product_client=LeakingProductClient(),
        )


def test_verifier_rejects_response_without_non_release_watermark() -> None:
    with pytest.raises(DemoCliError, match="watermark"):
        verify_backend(
            Client(watermarked=False),  # type: ignore[arg-type]
            scenario_id="golden-15-night",
            model="deterministic",
            wait_seconds=0.1,
        )


def test_demo_cli_help_is_available_in_clean_module_execution() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "sleepagent.simulation.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "sleepagent-demo" in completed.stdout


def test_actor_assertion_signer_loads_only_0600_ed25519_file(tmp_path) -> None:
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "actor-private.pem"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    signer = ActorAssertionSigner.from_private_file(
        str(key_path.resolve()),
        issuer="issuer",
        audience="audience",
        key_id="key-1",
    )

    compact = signer.sign(
        actor_id="elder-1",
        subject_id="subject-1",
        role="elder",
        scope=("product:sleep:today:read",),
        method="GET",
        path="/product/sleep/today",
    )
    header_segment, claims_segment, signature_segment = compact.split(".")
    claims = json.loads(_decode_segment(claims_segment))
    key.public_key().verify(
        _decode_segment(signature_segment),
        f"{header_segment}.{claims_segment}".encode("ascii"),
    )
    assert claims["actor_id"] == "elder-1"
    assert claims["body_sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    assert datetime.fromtimestamp(claims["exp"], tz=timezone.utc) > datetime.now(
        tz=timezone.utc
    )

    os.chmod(key_path, 0o644)
    with pytest.raises(DemoCliError, match="0600"):
        ActorAssertionSigner.from_private_file(
            str(key_path.resolve()),
            issuer="issuer",
            audience="audience",
            key_id="key-1",
        )


def test_command_verifier_drives_real_public_command_contracts() -> None:
    class CommandClient:
        def __init__(self) -> None:
            self.counter = 0
            self.operations: dict[str, dict] = {}
            self.start_operation: str | None = None
            self.start_key: str | None = None
            self.care_confirmed = False

        def read_model(
            self,
            *,
            kind,
            actor_id,
            subject_id,
            role,
            limit=20,
            cursor=None,
        ):
            del actor_id, limit, cursor
            assert kind == "care"
            items = []
            if self.care_confirmed:
                items = [
                    {
                        "record_type": "care_action",
                        "care_action_id": "care-confirm",
                        "interaction_id": "confirm-interaction",
                        "state": "confirmed_pending_delivery",
                        "action_kind": "sleep_hygiene_followup",
                        "source_analysis_revision_id": None,
                        "confirmed_at": "2026-08-11T00:00:00+00:00",
                        "updated_at": "2026-08-11T00:00:00+00:00",
                    },
                    {
                        "record_type": "care_followup",
                        "night_episode_id": "episode-1",
                        "state": "pending_feedback",
                        "updated_at": "2026-08-11T00:00:00+00:00",
                    },
                ]
            return {
                "schema_version": "product_sleep_care.v1",
                "data_mode": "replay",
                "synthetic_non_release": True,
                "subject_ref": subject_id,
                "role": role,
                "items": items,
                "next_cursor": None,
            }

        def signed_json(self, *, method, path, payload=None, **kwargs):
            replay = {"data_mode": "replay", "synthetic_non_release": True}
            headers = {
                "X-SleepAgent-Data-Mode": "replay",
                "X-SleepAgent-Synthetic-Non-Release": "true",
            }
            accepted_statuses = kwargs["accepted_statuses"]
            key = kwargs.get("idempotency_key")
            if method == "POST" and accepted_statuses == (409,):
                return {**replay, "code": "idempotency_conflict"}, headers, 409
            if method == "POST" and path.startswith("/product/"):
                if path.endswith("/interactions/start") and key == self.start_key:
                    assert self.start_operation is not None
                    return {
                        **replay,
                        "operation_id": self.start_operation,
                        "state": "accepted",
                    }, headers, 202
                self.counter += 1
                operation_id = f"product-operation-{self.counter}"
                result = {**replay, "operation_id": operation_id}
                if path.endswith("/interactions/start"):
                    interaction = (
                        "confirm-interaction"
                        if self.start_operation is None
                        else "decline-interaction"
                    )
                    status = {
                        **result,
                        "state": "waiting_for_input",
                        "interaction_id": interaction,
                        "answer_handle": f"answer-{interaction}",
                    }
                    if self.start_operation is None:
                        self.start_operation = operation_id
                        self.start_key = key
                elif path.endswith("/ask"):
                    status = {
                        **result,
                        "state": "waiting_for_input",
                        "interaction_id": "confirm-interaction",
                        "answer_handle": "answer-after-ask",
                    }
                elif path.endswith("/answer"):
                    interaction = path.split("/")[-2]
                    status = {
                        **result,
                        "state": "waiting_for_input",
                        "interaction_id": interaction,
                        "confirmation_handle": f"confirm-{interaction}",
                    }
                elif path.endswith("/confirm"):
                    self.care_confirmed = True
                    status = {
                        **result,
                        "state": "succeeded",
                        "interaction_id": "confirm-interaction",
                        "human_decision_id": "decision-confirm",
                        "care_action_id": "care-confirm",
                        "delivery_intent_id": "delivery-confirm",
                    }
                elif path.endswith("/decline"):
                    status = {
                        **result,
                        "state": "succeeded",
                        "interaction_id": "decline-interaction",
                        "human_decision_id": "decision-decline",
                        "care_action_id": None,
                        "delivery_intent_id": None,
                    }
                else:
                    status = {
                        **result,
                        "state": "succeeded",
                        "product_operation_id": "feedback-child",
                    }
                self.operations[operation_id] = status
                return {**result, "state": "accepted"}, headers, 202
            if method == "GET" and path.startswith(
                "/product/sleep/interactions/status/"
            ):
                return self.operations[path.rsplit("/", 1)[1]], headers, 200
            if method == "POST" and path.startswith("/api/v1/"):
                self.counter += 1
                operation_id = f"sleep-operation-{self.counter}"
                self.operations[operation_id] = {
                    "operation_id": operation_id,
                    "status": "succeeded",
                }
                return {
                    "operation_id": operation_id,
                    "status": "pending",
                }, headers, 202
            if method == "GET" and path.startswith("/api/v1/operations/"):
                return self.operations[path.rsplit("/", 1)[1]], headers, 200
            raise AssertionError((method, path, payload, kwargs))

    restarts: list[str] = []
    result = verify_command_backend(
        CommandClient(),
        subject_id="subject-1",
        actor_ids={
            "elder": "elder-1",
            "family": "family-1",
            "doctor": "doctor-1",
        },
        night_episode_id="episode-1",
        night_episode_revision_id="episode-revision-1",
        wait_seconds=0.1,
        restart_worker=lambda: restarts.append("worker"),
    )

    assert result["verified"] is True
    assert set(result["operation_ids"]) == {
        "confirm_start",
        "confirm_ask",
        "confirm_answer",
        "confirm",
        "decline_start",
        "decline_answer",
        "decline",
        "product_feedback",
        "monitoring_activate",
        "monitoring_deactivate",
        "elder_feedback",
        "family_feedback",
        "explicit_reanalysis",
    }
    assert restarts == ["worker", "worker"]


def test_effect_verifier_observes_public_delivery_effect() -> None:
    class Product:
        def __init__(self) -> None:
            self.read_count = 0

        def read_model(self, **kwargs):
            self.read_count += 1
            state = "confirmed_pending_delivery" if self.read_count == 1 else "active"
            return {
                "schema_version": "product_sleep_care.v1",
                "data_mode": "replay",
                "synthetic_non_release": True,
                "subject_ref": kwargs["subject_id"],
                "role": kwargs["role"],
                "items": [
                    {
                        "record_type": "care_action",
                        "care_action_id": "care-confirm",
                        "interaction_id": "confirm-interaction",
                        "state": state,
                    }
                ],
                "next_cursor": None,
            }

    product = Product()
    result = verify_effects_reconciliation_backend(
        product,
        subject_id="subject-1",
        actor_ids={
            "elder": "elder-1",
            "family": "family-1",
            "doctor": "doctor-1",
        },
        command_result={
            "care_action_id": "care-confirm",
            "delivery_intent_id": "delivery-confirm",
            "confirm_interaction_id": "confirm-interaction",
            "decline_interaction_id": "decline-interaction",
        },
        wait_seconds=0.1,
    )

    assert product.read_count == 2
    assert result["public_care_state"] == "active"


def test_read_model_verifier_proves_advance_dedup_and_dedicated_cursors() -> None:
    class Demo:
        def request(self, method: str, path: str, **kwargs):
            watermark = {"data_mode": "replay", "synthetic_non_release": True}
            if path == "/demo/v1/clock":
                return {
                    **watermark,
                    "scenario_time": "2026-03-12T20:00:00+08:00",
                    "generation": 1,
                }
            if path == "/demo/v1/advance":
                assert method == "POST"
                return {
                    **watermark,
                    "operation_id": "advance-1",
                    "generation": 1,
                }
            if path == "/demo/v1/operations/advance-1":
                return {
                    **watermark,
                    "operation_id": "advance-1",
                    "generation": 1,
                    "state": "succeeded",
                    "result": {
                        **watermark,
                        "generation": 1,
                        "scenario_time": "2026-03-12T20:00:00+08:00",
                        "released_fact_count": 497,
                    },
                }
            raise AssertionError((method, path, kwargs))

    class Product:
        def read_model(
            self,
            *,
            kind,
            actor_id,
            subject_id,
            role,
            limit=20,
            cursor=None,
        ):
            del actor_id
            schemas = {
                "trends": "product_sleep_trends.v1",
                "records": "product_sleep_records.v1",
                "care": "product_sleep_care.v1",
            }
            all_items = [] if kind == "care" else [
                {"item": f"{kind}-2"},
                {"item": f"{kind}-1"},
            ]
            start = 1 if cursor else 0
            items = all_items[start : start + limit]
            next_cursor = (
                f"opaque-{kind}"
                if start == 0 and start + limit < len(all_items)
                else None
            )
            return {
                "schema_version": schemas[kind],
                "data_mode": "replay",
                "synthetic_non_release": True,
                "subject_ref": subject_id,
                "role": role,
                "items": items,
                "next_cursor": next_cursor,
            }

    result = verify_read_models_backend(
        Demo(),
        Product(),
        subject_id="subject-1",
        actor_ids={
            "elder": "elder-1",
            "family": "family-1",
            "doctor": "doctor-1",
        },
        expected_night_count=2,
        advance_seconds=172800,
        wait_seconds=0.1,
    )

    assert result["verified"] is True
    assert result["advance_operation_id"] == "advance-1"
    assert result["cursor_kinds"] == ["records", "trends"]


def test_abnormal_verifier_proves_urgent_zero_product_route() -> None:
    class Demo:
        def request(self, method: str, path: str, **kwargs):
            del method, kwargs
            watermark = {"data_mode": "replay", "synthetic_non_release": True}
            if path == "/demo/v1/seed":
                return {**watermark, "operation_id": "root-urgent"}
            if path == "/demo/v1/operations/root-urgent":
                return {
                    **watermark,
                    "state": "failed",
                    "error_code": "unexpected_urgent_route",
                }
            if path == "/demo/v1/trace":
                return {
                    **watermark,
                    "entries": [
                        {
                            "fast_path_operation_id": "fast-urgent",
                            "product_operation_id": None,
                            "analysis_revision_id": None,
                        }
                    ],
                }
            raise AssertionError(path)

    class Product:
        def today(self, **kwargs):
            return {
                "schema_version": "product_sleep_today.v1",
                "data_mode": "replay",
                "synthetic_non_release": True,
                "state": "no_data",
                "subject_ref": kwargs["subject_id"],
                "role": kwargs["role"],
            }

    result = verify_abnormal_backend(
        Demo(),
        scenario_id="urgent-zero-model",
        expected_error_code="unexpected_urgent_route",
        wait_seconds=0.1,
        product_client=Product(),
        subject_id="subject-urgent",
        actor_id="elder-urgent",
    )

    assert result["verified"] is True
    assert result["error_code"] == "unexpected_urgent_route"


def test_bounded_retention_verifier_fences_old_authority_and_projection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "sleepagent.simulation.cli.verify_backend",
        lambda *args, **kwargs: {"operation_id": "baseline-operation"},
    )

    class Signer:
        def sign(self, **kwargs):
            assert kwargs["authorization_epoch"] == 1
            assert kwargs["privacy_epoch"] == 1
            assert kwargs["retrieval_policy_epoch"] == 1
            return "old-epoch-assertion"

    class Product:
        signer = Signer()
        authorization_epoch = 1
        privacy_epoch = 1
        retrieval_policy_epoch = 1

        def read_model(self, **kwargs):
            del kwargs
            return {
                "data_mode": "replay",
                "synthetic_non_release": True,
                "items": [{"record_type": "night_episode"}],
                "next_cursor": "old-generation-cursor",
            }

        def signed_json(self, **kwargs):
            if kwargs.get("assertion_override") == "old-epoch-assertion":
                return {"code": "stale_actor_assertion"}, {}, 403
            assert kwargs["query"]["cursor"] == "old-generation-cursor"
            assert 409 in kwargs["accepted_statuses"]
            assert self.authorization_epoch == 2
            assert self.privacy_epoch == 2
            assert self.retrieval_policy_epoch == 2
            return {"code": "cursor_resync_required"}, {}, 409

        def today(self, **kwargs):
            del kwargs
            return {
                "schema_version": "product_sleep_today.v1",
                "data_mode": "replay",
                "synthetic_non_release": True,
                "state": "no_data",
            }

    class Demo:
        def request(self, method, path, **kwargs):
            assert path in {
                "/demo/v1/reset",
                "/demo/v1/operations/reset-operation",
            }
            if path == "/demo/v1/reset":
                assert method == "POST"
                assert kwargs["payload"] == {
                    "confirmation": "reset-replay-generation"
                }
                return {
                    "data_mode": "replay",
                    "synthetic_non_release": True,
                    "operation_id": "reset-operation",
                }
            return {
                "data_mode": "replay",
                "synthetic_non_release": True,
                "operation_id": "reset-operation",
                "state": "succeeded",
                "result": {
                    "receipt": {
                        "schema_version": "demo_reset_receipt.v1",
                        "source_generation": 1,
                        "target_generation": 2,
                        "authority_epochs": {
                            "authorization_epoch": 2,
                            "privacy_epoch": 2,
                            "retrieval_policy_epoch": 2,
                        },
                        "domain_outcomes": [
                            {
                                "outcome": "destroyed",
                                "retention_domain": "raw",
                                "dek_generation": 1,
                                "object_count": 1,
                                "reason_code": "replay_subject_forget",
                            }
                        ],
                    }
                },
            }

    result = verify_bounded_retention_backend(
        Demo(),
        Product(),  # type: ignore[arg-type]
        scenario_id="worsening-vital-trend",
        subject_id="subject-1",
        actor_ids={
            "elder": "elder-1",
            "family": "family-1",
            "doctor": "doctor-1",
        },
        wait_seconds=0.1,
    )

    assert result == {
        "schema_version": "backend_retention_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "baseline_operation_id": "baseline-operation",
        "reset_operation_id": "reset-operation",
        "source_generation": 1,
        "target_generation": 2,
        "domain_outcome_count": 1,
        "old_assertion_fenced": True,
        "old_cursor_fenced": True,
        "new_generation_today_state": "no_data",
    }


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
