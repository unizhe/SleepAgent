from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.radar_agent.product_agent.habit_api import (
    PRODUCT_API_KEY_ENV,
    reset_habit_profile_api_for_tests,
)


API_KEY = "habit-profile-test-key"


@pytest.fixture(autouse=True)
def _habit_api(monkeypatch):
    monkeypatch.setenv(PRODUCT_API_KEY_ENV, API_KEY)
    reset_habit_profile_api_for_tests()


def headers(
    *,
    role: str = "elder",
    actor_id: str = "elder-actor",
    subject_id: str = "elder-subject",
    scopes: str = "",
) -> dict[str, str]:
    result = {
        "x-api-key": API_KEY,
        "x-actor-id": actor_id,
        "x-actor-role": role,
        "x-subject-id": subject_id,
    }
    if scopes:
        result["x-authorization-scopes"] = scopes
    return result


def start(
    client: TestClient,
    *,
    episode_id: str = "habit-api-episode",
    concepts: tuple[str, ...] = (
        "habit.primary_goal",
        "habit.schedule_constraint",
    ),
    request_headers: dict[str, str] | None = None,
):
    return client.post(
        "/product/habit-profile/interactions/start",
        headers=request_headers or headers(),
        json={
            "episode_id": episode_id,
            "trigger": "optional_light_intake",
            "candidate_concept_ids": list(concepts),
            "max_questions": len(concepts),
        },
    )


def submit_answer(
    client: TestClient,
    selection: dict,
    *,
    values: dict[str, object] | None = None,
    disposition: str = "answered",
    request_headers: dict[str, str] | None = None,
):
    values = values or {
        item["concept_id"]: item["options"][0]
        for item in selection["candidates"]
    }
    return client.post(
        "/product/habit-profile/interactions/answers",
        headers=request_headers or headers(),
        json={
            "selection": selection,
            "answers": [
                {
                    "concept_id": item["concept_id"],
                    "concept_version": item["concept_version"],
                    "disposition": disposition,
                    **(
                        {"value": values[item["concept_id"]]}
                        if disposition == "answered"
                        else {}
                    ),
                }
                for item in selection["candidates"]
            ],
        },
    )


def confirm(
    client: TestClient,
    pending: dict,
    *,
    confirmation_id: str,
    idempotency_key: str,
    request_headers: dict[str, str] | None = None,
):
    changes = pending["change_set"]
    return client.post(
        "/product/habit-profile/confirm",
        headers=request_headers or headers(),
        json={
            "confirmation_id": confirmation_id,
            "change_set_id": changes["change_set_id"],
            "change_set_version": changes["version"],
            "manifest_hash": changes["manifest_hash"],
            "idempotency_key": idempotency_key,
        },
    )


def test_habit_api_is_authenticated_and_default_proactive_intake_is_off() -> None:
    with TestClient(app) as client:
        availability = client.get("/product/habit-profile/availability")
        denied = client.get("/product/habit-profile/concepts")
        concepts = client.get(
            "/product/habit-profile/concepts", headers=headers()
        )

    assert availability.status_code == 200
    assert availability.json()["default_proactive_intake_enabled"] is False
    assert "3_to_5" in availability.json()["release_gate"]
    assert denied.status_code == 401
    assert concepts.status_code == 200
    assert all(
        item["domain_review_status"] == "approved" for item in concepts.json()
    )


def test_elder_optional_intake_confirm_read_and_forget_flow() -> None:
    with TestClient(app) as client:
        offered = start(client)
        assert offered.status_code == 200
        offered_payload = offered.json()
        assert "都可以跳过" in offered_payload["purpose_notice"]
        assert len(offered_payload["selection"]["candidates"]) == 2

        answered = submit_answer(
            client, offered_payload["selection"]
        )
        assert answered.status_code == 200
        answer_payload = answered.json()
        assert answer_payload["next_status"] == "waiting_elder_confirmation"
        pending = answer_payload["pending_change_set"]
        assert len(pending["change_set"]["candidates"]) == 2

        before = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )
        assert before.status_code == 200
        assert before.json()["facts"] == []

        committed = confirm(
            client,
            pending,
            confirmation_id="habit-api-confirm",
            idempotency_key="habit-api-commit",
        )
        replay = confirm(
            client,
            pending,
            confirmation_id="habit-api-confirm",
            idempotency_key="habit-api-commit",
        )
        assert committed.status_code == 200
        assert committed.json()["tool_receipt"]["outcome"] == "succeeded"
        assert replay.json() == committed.json()

        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )
        facts = profile.json()["facts"]
        assert len(facts) == 2
        assert all(item["trust_label"] == "user_data" for item in facts)

        forget = client.post(
            "/product/habit-profile/forget",
            headers=headers(),
            json={
                "episode_id": "habit-forget-episode",
                "fact_id": facts[0]["fact_ref"],
            },
        )
        assert forget.status_code == 200
        forgotten = confirm(
            client,
            forget.json(),
            confirmation_id="habit-api-forget-confirm",
            idempotency_key="habit-api-forget",
        )
        assert forgotten.status_code == 200
        after = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )
        assert len(after.json()["facts"]) == 1
    assert "excluded from personalization" in after.json()["retention_notice"]


def test_pending_habit_change_set_survives_application_restart() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    reset_habit_profile_api_for_tests(connection)
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-pending-restart",
            concepts=("habit.primary_goal",),
        )
        answered = submit_answer(client, offered.json()["selection"])
        pending = answered.json()["pending_change_set"]

        reset_habit_profile_api_for_tests(connection)
        committed = confirm(
            client,
            pending,
            confirmation_id="habit-pending-restart-confirm",
            idempotency_key="habit-pending-restart-commit",
        )

    assert committed.status_code == 200
    assert committed.json()["tool_receipt"]["outcome"] == "succeeded"


def test_confirmed_profile_survives_api_runtime_restart(tmp_path) -> None:
    database = tmp_path / "habit-api.sqlite3"
    first_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(first_connection)
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-persistent-api",
            concepts=("habit.primary_goal",),
        ).json()
        pending = submit_answer(client, offered["selection"]).json()[
            "pending_change_set"
        ]
        committed = confirm(
            client,
            pending,
            confirmation_id="habit-persistent-confirm",
            idempotency_key="habit-persistent-commit",
        )
    assert committed.status_code == 200
    first_connection.close()

    second_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(second_connection)
    with TestClient(app) as client:
        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )

    assert profile.status_code == 200
    assert profile.json()["memory_version"] == 1
    assert len(profile.json()["facts"]) == 1
    assert profile.json()["facts"][0]["concept_id"] == "habit.primary_goal"


def test_confirmed_never_ask_survives_api_runtime_restart(tmp_path) -> None:
    database = tmp_path / "habit-suppression-api.sqlite3"
    first_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(first_connection)
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-suppression-first",
            concepts=("habit.nap_pattern",),
        ).json()
        suppressed = client.post(
            "/product/habit-profile/interactions/answers",
            headers=headers(),
            json={
                "selection": offered["selection"],
                "answers": [
                    {
                        "concept_id": "habit.nap_pattern",
                        "concept_version": "1.0.0",
                        "disposition": "never_ask",
                    }
                ],
                "suppression_confirmation_ref": "confirmation:never-ask-nap",
            },
        )
    assert suppressed.status_code == 200
    assert len(suppressed.json()["capture"]["suppressions"]) == 1
    first_connection.close()

    second_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(second_connection)
    with TestClient(app) as client:
        later = start(
            client,
            episode_id="habit-suppression-after-restart",
            concepts=("habit.nap_pattern",),
        )

    assert later.status_code == 200
    assert later.json()["selection"]["candidates"] == []


def test_episode_budget_and_cross_episode_cooldown_survive_restart(
    tmp_path,
) -> None:
    database = tmp_path / "habit-question-state.sqlite3"
    first_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(first_connection)
    with TestClient(app) as client:
        first = start(
            client,
            episode_id="durable-budget",
            concepts=(
                "habit.primary_goal",
                "habit.schedule_constraint",
            ),
        )
        cooldown_seed = start(
            client,
            episode_id="durable-cooldown-seed",
            concepts=("habit.nap_pattern",),
        )
    assert len(first.json()["selection"]["candidates"]) == 2
    assert len(cooldown_seed.json()["selection"]["candidates"]) == 1
    first_connection.close()

    second_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(second_connection)
    with TestClient(app) as client:
        remaining = start(
            client,
            episode_id="durable-budget",
            concepts=(
                "habit.environment_preference",
                "habit.stimulant_timing",
            ),
        )
        cooled_down = start(
            client,
            episode_id="durable-cooldown-after-restart",
            concepts=("habit.nap_pattern",),
        )
    assert remaining.status_code == 200
    assert len(remaining.json()["selection"]["candidates"]) == 1
    assert cooled_down.status_code == 200
    assert cooled_down.json()["selection"]["candidates"] == []
    second_connection.close()

    third_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(third_connection)
    with TestClient(app) as client:
        exhausted = start(
            client,
            episode_id="durable-budget",
            concepts=("habit.pre_sleep_behavior",),
        )
    assert exhausted.status_code == 200
    assert exhausted.json()["selection"]["candidates"] == []


def test_selection_receipt_consumption_survives_restart(tmp_path) -> None:
    database = tmp_path / "habit-selection-state.sqlite3"
    first_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(first_connection)
    with TestClient(app) as client:
        selection = start(
            client,
            episode_id="durable-selection",
            concepts=("habit.primary_goal",),
        ).json()["selection"]
    first_connection.close()

    second_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(second_connection)
    with TestClient(app) as client:
        captured = submit_answer(client, selection)
    assert captured.status_code == 200
    assert captured.json()["next_status"] == "waiting_elder_confirmation"
    second_connection.close()

    third_connection = sqlite3.connect(database, check_same_thread=False)
    reset_habit_profile_api_for_tests(third_connection)
    with TestClient(app) as client:
        replay = submit_answer(client, selection)
    assert replay.status_code == 409
    assert "already consumed" in replay.json()["detail"]


def test_skip_does_not_create_change_set_or_reduce_service() -> None:
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-skip",
            concepts=("habit.primary_goal",),
        ).json()
        skipped = submit_answer(
            client,
            offered["selection"],
            disposition="skipped",
        )
        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )

    assert skipped.status_code == 200
    assert skipped.json()["next_status"] == "continue"
    assert skipped.json()["pending_change_set"] is None
    assert profile.json()["facts"] == []


def test_manifest_tampering_and_family_confirmation_are_rejected() -> None:
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-tamper",
            concepts=("habit.primary_goal",),
        ).json()
        pending = submit_answer(client, offered["selection"]).json()[
            "pending_change_set"
        ]
        changes = pending["change_set"]
        tampered = client.post(
            "/product/habit-profile/confirm",
            headers=headers(),
            json={
                "confirmation_id": "tampered",
                "change_set_id": changes["change_set_id"],
                "change_set_version": changes["version"],
                "manifest_hash": "f" * 64,
                "idempotency_key": "tampered",
            },
        )
        family = confirm(
            client,
            pending,
            confirmation_id="family-cannot-confirm",
            idempotency_key="family-cannot-confirm",
            request_headers=headers(
                role="family",
                actor_id="family-actor",
                scopes="read_habit_profile_family",
            ),
        )

    assert tampered.status_code == 409
    assert family.status_code == 403


def test_elder_can_remove_one_candidate_and_old_manifest_is_revoked() -> None:
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-prune",
            concepts=("habit.primary_goal", "habit.schedule_constraint"),
        ).json()
        pending = submit_answer(client, offered["selection"]).json()[
            "pending_change_set"
        ]
        remove_id = pending["change_set"]["candidates"][1]["candidate_id"]
        revised = client.post(
            "/product/habit-profile/change-sets/prune",
            headers=headers(),
            json={
                "change_set_id": pending["change_set"]["change_set_id"],
                "candidate_ids_to_remove": [remove_id],
            },
        )
        old_confirmation = confirm(
            client,
            pending,
            confirmation_id="old-pruned-confirmation",
            idempotency_key="old-pruned-confirmation",
        )
        new_confirmation = confirm(
            client,
            revised.json(),
            confirmation_id="new-pruned-confirmation",
            idempotency_key="new-pruned-confirmation",
        )
        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )

    assert revised.status_code == 200
    assert len(revised.json()["change_set"]["candidates"]) == 1
    assert old_confirmation.status_code == 409
    assert new_confirmation.status_code == 200
    assert len(profile.json()["facts"]) == 1


def test_family_observation_stays_current_until_elder_proposes_and_confirms() -> None:
    family_headers = headers(
        role="family",
        actor_id="family-actor",
        scopes="read_habit_profile_family",
    )
    with TestClient(app) as client:
        offered = client.post(
            "/product/habit-profile/interactions/start",
            headers=family_headers,
            json={
                "episode_id": "family-habit-observation",
                "trigger": "explicit_habit_question",
                "candidate_concept_ids": ["habit.nap_pattern"],
                "max_questions": 1,
            },
        )
        assert offered.status_code == 200
        submitted = submit_answer(
            client,
            offered.json()["selection"],
            values={"habit.nap_pattern": "多数天午睡"},
            request_headers=family_headers,
        )
        payload = submitted.json()
        assert payload["pending_change_set"] is None
        assert (
            payload["evidence_candidates"][0]["semantic"]
            == "observer_reported"
        )
        answer_ref = payload["capture"]["answers"][0]["answer_ref"]

        proposed = client.post(
            "/product/habit-profile/observer-proposals",
            headers=headers(),
            json={
                "episode_id": "elder-reviews-observer",
                "answer_refs": [answer_ref],
            },
        )
        assert proposed.status_code == 200
        assert (
            proposed.json()["change_set"]["candidates"][0]["origin_semantic"]
            == "family_observation"
        )
        committed = confirm(
            client,
            proposed.json(),
            confirmation_id="elder-observer-confirm",
            idempotency_key="elder-observer-commit",
        )
        assert committed.status_code == 200
        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        )

    assert profile.json()["facts"][0]["origin_semantic"] == "family_observation"


def test_response_level_safety_preempts_profile_change_set() -> None:
    with TestClient(app) as client:
        offered = client.post(
            "/product/habit-profile/interactions/start",
            headers=headers(),
            json={
                "episode_id": "habit-api-safety",
                "trigger": "explicit_habit_question",
                "candidate_concept_ids": ["habit.observed_snoring"],
                "max_questions": 1,
            },
        ).json()
        submitted = submit_answer(
            client,
            offered["selection"],
            values={"habit.observed_snoring": "观察到"},
        )

    assert submitted.status_code == 200
    assert submitted.json()["next_status"] == "safety_preempted"
    assert submitted.json()["pending_change_set"] is None
    assert submitted.json()["capture"]["stop_remaining_questions"] is True


def test_profile_review_can_replace_one_exact_fact_after_new_confirmation() -> None:
    with TestClient(app) as client:
        offered = start(
            client,
            episode_id="habit-create-before-correction",
            concepts=("habit.primary_goal",),
        ).json()
        pending = submit_answer(client, offered["selection"]).json()[
            "pending_change_set"
        ]
        assert confirm(
            client,
            pending,
            confirmation_id="create-before-correction",
            idempotency_key="create-before-correction",
        ).status_code == 200
        original = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        ).json()["facts"][0]

        review = client.post(
            "/product/habit-profile/interactions/start",
            headers=headers(),
            json={
                "episode_id": "habit-correction",
                "trigger": "explicit_profile_review",
                "candidate_concept_ids": ["habit.primary_goal"],
                "max_questions": 1,
                "profile_update_requested": True,
            },
        )
        assert review.status_code == 200
        assert review.json()["existing_profile"]["facts"][0]["fact_ref"] == original[
            "fact_ref"
        ]
        corrected = client.post(
            "/product/habit-profile/interactions/answers",
            headers=headers(),
            json={
                "selection": review.json()["selection"],
                "answers": [
                    {
                        "concept_id": "habit.primary_goal",
                        "concept_version": "1.0.0",
                        "disposition": "answered",
                        "value": "白天更有精神",
                    }
                ],
                "replace_fact_id_by_concept": {
                    "habit.primary_goal": original["fact_ref"]
                },
            },
        )
        assert corrected.status_code == 200
        replacement = corrected.json()["pending_change_set"]
        assert replacement["change_set"]["candidates"][0]["operation"] == "replace"
        assert confirm(
            client,
            replacement,
            confirmation_id="confirm-correction",
            idempotency_key="confirm-correction",
        ).status_code == 200
        profile = client.get(
            "/product/habit-profile?purpose=profile_review",
            headers=headers(),
        ).json()

    assert len(profile["facts"]) == 1
    assert profile["facts"][0]["value"] == "白天更有精神"
