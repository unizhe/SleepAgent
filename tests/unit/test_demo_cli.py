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

from sleepagent.demo_cli import (
    ActorAssertionSigner,
    DemoCliError,
    verify_abnormal_backend,
    verify_backend,
    verify_bounded_retention_backend,
    verify_effects_reconciliation_backend,
    verify_stage2_backend,
    verify_stage3_backend,
)


pytestmark = pytest.mark.unit


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
        [sys.executable, "-m", "sleepagent.demo_cli", "--help"],
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


def test_stage2_verifier_drives_real_public_command_contracts() -> None:
    class Stage2Client:
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
    result = verify_stage2_backend(
        Stage2Client(),
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


def test_stage4_verifier_observes_public_delivery_effect() -> None:
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
        stage2_result={
            "care_action_id": "care-confirm",
            "delivery_intent_id": "delivery-confirm",
            "confirm_interaction_id": "confirm-interaction",
            "decline_interaction_id": "decline-interaction",
        },
        wait_seconds=0.1,
    )

    assert product.read_count == 2
    assert result["public_care_state"] == "active"


def test_stage3_verifier_proves_advance_dedup_and_dedicated_cursors() -> None:
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

    result = verify_stage3_backend(
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
        "sleepagent.demo_cli.verify_backend",
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
        "schema_version": "backend_stage5_verification.v1",
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
