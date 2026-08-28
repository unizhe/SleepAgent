from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from uuid import uuid4

import pytest

from sleepagent.config import (
    DataMode as BackendDataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.runtime.agents import (
    EpisodePlanProposal,
    SleepCareEvaluation,
    _SleepCareContentPlan,
)
from sleepagent.runtime.contracts import (
    EpisodeType,
    ExecutionMode,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
    _parse_context_packet,
)
from sleepagent.runtime.invocation import (
    CareStrategyModelOutput,
    EvidenceReasoningModelOutput,
    SafetyReviewModelOutput,
)
from sleepagent.workers.product import (
    build_product_agent_worker_handlers,
)
from sleepagent.runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
)
from sleepagent.runtime.factory import (
    ProductRuntimeBundle,
    _build_postgres_worker_product_runtime_bundle_from_env,
)
from sleepagent.workers.runtime import (
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)
from tests.integration.test_product_postgres_integration import (
    _assert_committed_closure,
    _claim_product_work,
    _required_environment,
    _seed_product_scope,
    _worker_runtime,
)
from tests.support.openai_compatible_server import (
    LoopbackOpenAICompatibleServer,
    OpenAIRequestRecord,
    ScriptedOpenAIResponse,
)
from tests.unit.test_product_agent_runner import request as product_request


API_KEY = "sk-loopback-live-worker-secret"
MODEL_ID = "loopback-live-product-model"

_SCHEMAS = (
    EpisodePlanProposal,
    SleepCareEvaluation,
    EvidenceReasoningModelOutput,
    CareStrategyModelOutput,
    SafetyReviewModelOutput,
    _SleepCareContentPlan,
)


def _configure_live_product_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_MODEL", MODEL_ID)
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_BASE_URL", base_url)
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS", "512")


def _live_worker_settings(
    *,
    worker_dsn: str,
    worker_principal: str,
    namespace_id: str,
) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="product-live-postgres-integration",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=BackendDataMode.REPLAY,
        database_dsn=worker_dsn,
        database_identity="integration-database",
        database_role="integration-worker",
        service_principal_id=worker_principal,
        database_scope=BackendDataMode.REPLAY,
        namespace_prefixes=(namespace_id,),
        worker_queues=("product_agent",),
        model_mode=ModelMode.LIVE,
        service_credential_ref="test:worker-service",
        signing_key_ref="test:worker-signing",
        encryption_key_ref="test:worker-encryption",
        pool_min_size=1,
        pool_max_size=2,
    )


def _structured_replay_responder(
    request: OpenAIRequestRecord,
    index: int,
) -> ScriptedOpenAIResponse:
    body = request.json_body
    assert isinstance(body, Mapping)
    messages = body.get("messages")
    assert isinstance(messages, list) and len(messages) >= 2
    prompt_json = json.dumps(messages, ensure_ascii=False)
    assert all(
        scenario_name not in prompt_json
        for scenario_name in (
            "normal-one-night",
            "habit-baseline-night-exit",
            "worsening-vital-trend",
            "habit-family-report",
            "urgent-zero-model",
        )
    )
    instruction = messages[0]
    assert isinstance(instruction, Mapping)
    instruction_content = instruction.get("content")
    assert isinstance(instruction_content, str)

    schema = next(
        (
            candidate
            for candidate in _SCHEMAS
            if f"exact schema for {candidate.__name__}." in instruction_content
        ),
        None,
    )
    assert schema is not None
    prompt_marker = " Prompt version: "
    context_marker = ". Context packet: "
    prompt_tail = instruction_content.rsplit(prompt_marker, 1)[1]
    prompt_version, separator, context_tail = prompt_tail.partition(
        context_marker
    )
    assert separator and prompt_version
    context_packet_id = context_tail.removesuffix(".").strip()
    assert context_packet_id

    context_message = next(
        item
        for item in reversed(messages[1:])
        if isinstance(item, Mapping) and item.get("role") == "user"
    )
    context_content = context_message.get("content")
    assert isinstance(context_content, str)
    packet = _parse_context_packet(
        [dict(item) for item in messages[1:]],
        context_packet_id,
    )
    output = DeterministicReplayStructuredAgentModel().generate(
        messages=[dict(item) for item in messages[1:]],
        schema=schema,
        prompt_version=prompt_version,
        context_packet_id=packet.context_packet_id,
    )
    return ScriptedOpenAIResponse(
        body={
            "id": f"loopback-live-request-{index + 1}",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": output.model_dump_json(),
                    }
                }
            ],
        }
    )


def _assert_live_models(runtime_bundle: ProductRuntimeBundle) -> None:
    roster = runtime_bundle.roster
    models = [
        roster.sleepcare.model,
        roster.sleepcare.planning_model,
        roster.evidence_reasoning.model,
        roster.care_strategy.model,
        roster.safety_review.model,
    ]
    assert all(
        isinstance(model, OpenAICompatibleStructuredAgentModel)
        for model in models
    )
    assert all(model.model_id == MODEL_ID for model in models)
    assert all(
        model.config.base_url.startswith("http://127.0.0.1:")
        for model in models
    )


@pytest.mark.postgres
def test_live_product_worker_invokes_loopback_http_and_commits_postgres_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
    )
    provider, factory, store = _worker_runtime(
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        namespace_id=seed.namespace_id,
    )
    try:
        with LoopbackOpenAICompatibleServer(
            _structured_replay_responder
        ) as server:
            _configure_live_product_environment(
                monkeypatch,
                base_url=server.base_url,
            )
            handlers = build_product_agent_worker_handlers(
                _live_worker_settings(
                    worker_dsn=worker_dsn,
                    worker_principal=worker_principal,
                    namespace_id=seed.namespace_id,
                )
            )
            handler = handlers["product_agent"]
            processor_factory = handler._processor_factory
            assert processor_factory is not None
            _assert_live_models(processor_factory(factory).runtime_bundle)

            claim = _claim_product_work(
                store,
                worker_instance=f"product-live-worker-{uuid4().hex}",
            )
            result = handler(WorkContext(claim, store, threading.Event()))

            assert server.errors == ()
            assert result.disposition is WorkDisposition.SUCCEEDED
            assert result.finalization_mode is WorkFinalizationMode.HANDLER_OWNED
            assert result.result["analysis_status"] == "ready"
            assert len(result.result["role_view_ids"]) == 3
            assert server.request_count > 0
            assert all(
                item.path == "/v1/chat/completions"
                for item in server.requests
            )
            assert all(
                item.headers["Authorization"] == f"Bearer {API_KEY}"
                for item in server.requests
            )
            assert all(
                API_KEY not in json.dumps(item.json_body, sort_keys=True)
                for item in server.requests
            )
    finally:
        provider.close()

    _assert_committed_closure(
        psycopg,
        admin_dsn=admin_dsn,
        seed=seed,
        analysis_revision_id=result.result["analysis_revision_id"],
        product_attempt_id=result.result["product_attempt_id"],
        analysis_status=result.result["analysis_status"],
    )

    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT invocation.invocation_key, invocation.current_state,
                       invocation.provider_request_id,
                       array_agg(journal.to_state ORDER BY journal.sequence),
                       jsonb_agg(journal.event_json ORDER BY journal.sequence)
                FROM public.backend_invocations AS invocation
                JOIN public.backend_invocation_journal AS journal
                  ON journal.invocation_id = invocation.invocation_id
                WHERE invocation.operation_id = %s
                GROUP BY invocation.invocation_key,
                         invocation.current_state,
                         invocation.provider_request_id
                """,
                (seed.operation_id,),
            )
            invocation = cursor.fetchone()
            assert invocation is not None
            assert invocation[:3] == (
                (
                    f"product-agent:{seed.operation_id}:"
                    f"{seed.night_episode_revision_id}:live.v1"
                ),
                "response_received",
                None,
            )
            assert invocation[3] == [
                "reserved",
                "send_started",
                "response_received",
            ]
            assert "loopback-live-request-" in json.dumps(
                invocation[4], sort_keys=True
            )

            cursor.execute(
                """
                SELECT operation.operation_json::text,
                       attempt.attempt_json::text,
                       analysis.analysis_json::text,
                       role_view.view_json::text,
                       role_view.public_today_json::text,
                       outbox.event_json::text
                FROM public.sleep_domain_operations AS operation
                JOIN public.backend_product_attempts AS attempt
                  ON attempt.operation_id = operation.operation_id
                JOIN public.sleep_domain_analysis_revisions AS analysis
                  ON analysis.analysis_revision_id = %s
                JOIN public.sleep_domain_analysis_role_views AS role_view
                  ON role_view.analysis_revision_id = analysis.analysis_revision_id
                JOIN public.sleep_domain_domain_outbox AS outbox
                  ON outbox.operation_id = operation.operation_id
                 AND outbox.aggregate_type = 'AnalysisRevision'
                WHERE operation.operation_id = %s
                """,
                (result.result["analysis_revision_id"], seed.operation_id),
            )
            visible_rows = cursor.fetchall()
            assert len(visible_rows) == 3

    database_visible_payload = json.dumps(
        {
            "invocation_journal": invocation[4],
            "product_payloads": visible_rows,
        },
        sort_keys=True,
    )
    assert API_KEY not in database_visible_payload
    assert "loopback-live-request-" in database_visible_payload
    assert MODEL_ID in database_visible_payload


def test_live_product_runtime_urgent_preflight_makes_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with LoopbackOpenAICompatibleServer(
        ScriptedOpenAIResponse(
            status_code=500,
            body={"error": {"message": "must not be called"}},
        )
    ) as server:
        _configure_live_product_environment(
            monkeypatch,
            base_url=server.base_url,
        )
        bundle = _build_postgres_worker_product_runtime_bundle_from_env()
        _assert_live_models(bundle)

        result = bundle.runner.run(
            product_request(
                EpisodeType.MORNING_REVIEW,
                user_text="我现在胸痛并且呼吸困难",
            )
        )

        assert result.receipt.execution_mode is ExecutionMode.DETERMINISTIC_ONLY
        assert result.receipt.episode_type is EpisodeType.URGENT_BOUNDARY
        assert not result.agent_invocations
        assert server.request_count == 0
        assert server.errors == ()
