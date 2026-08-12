from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import pytest
from starlette.requests import Request

from sleepagent.product_api.contracts import (
    FamilyTodayContent,
    FeedbackRequest,
    InteractionStatusResponse,
    ProductCareResponse,
    ProductRecordsResponse,
    ProductRole,
    ProductSleepTodayProjection,
    ProductTrendsResponse,
    ProductTodayState,
    PublicOperationState,
)
from sleepagent.sleep_domain.episode_v2 import EpisodeAssignmentBasis
from sleepagent.product_api.service import (
    ProductApiError,
    ProductApiService,
    ProductRequestContext,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


def test_feedback_request_accepts_an_aware_wire_datetime() -> None:
    request = FeedbackRequest.model_validate(
        {
            "episode_revision_id": "episode-revision-1",
            "feedback": "The summary matched how I felt.",
            "event_at": "2026-08-11T20:10:00Z",
        }
    )

    assert request.event_at == datetime(2026, 8, 11, 20, 10, tzinfo=UTC)


def test_feedback_request_rejects_a_naive_wire_datetime() -> None:
    with pytest.raises(ValueError, match="timezone offset"):
        FeedbackRequest.model_validate(
            {
                "episode_revision_id": "episode-revision-1",
                "feedback": "The summary matched how I felt.",
                "event_at": "2026-08-11T20:10:00",
            }
        )


class Identity:
    def __init__(self, context: ProductRequestContext) -> None:
        self.context = context
        self.calls: list[tuple[bytes, str]] = []

    def resolve(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext:
        del request
        self.calls.append((body, purpose))
        return self.context


class Backend:
    def __init__(self) -> None:
        self.reservations: list[dict[str, Any]] = []

    def get_today_projection(
        self,
        context: ProductRequestContext,
    ) -> ProductSleepTodayProjection | None:
        return ProductSleepTodayProjection(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            state=ProductTodayState.READY,
            subject_ref=context.subject_id,
            role=context.role,
            episode_id="night-1",
            episode_revision_id="night-revision-1",
            episode_local_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
            assignment_basis=EpisodeAssignmentBasis.OBSERVED_WAKE,
            analysis_revision_id="analysis-1",
            projection_id="view-today",
            projection_version=1,
            committed_at=datetime(2026, 8, 7, tzinfo=UTC),
            content=FamilyTodayContent(
                summary_text="role-minimized",
                context_notice="family context",
            ) if context.role == ProductRole.FAMILY else {
                "audience": context.role.value,
                "summary_text": "role-minimized",
                "context_notice": "role context",
                **(
                    {"evidence_refs": ()}
                    if context.role == ProductRole.DOCTOR
                    else {}
                ),
            },
        )

    def reserve_command(
        self,
        context: ProductRequestContext,
        **values: Any,
    ) -> str:
        self.reservations.append({"context": context, **values})
        return "01987654-3210-7abc-8def-0123456789ab"

    def get_trends(self, context, *, limit, cursor):
        del limit, cursor
        return ProductTrendsResponse(
            data_mode=context.data_mode,
            synthetic_non_release=True,
            subject_ref=context.subject_id,
            role=context.role,
            items=(),
        )

    def get_records(self, context, *, limit, cursor):
        del limit, cursor
        return ProductRecordsResponse(
            data_mode=context.data_mode,
            synthetic_non_release=True,
            subject_ref=context.subject_id,
            role=context.role,
            items=(),
        )

    def get_care(self, context, *, limit, cursor):
        del limit, cursor
        return ProductCareResponse(
            data_mode=context.data_mode,
            synthetic_non_release=True,
            subject_ref=context.subject_id,
            role=context.role,
            items=(),
        )

    def get_operation(
        self,
        context: ProductRequestContext,
        *,
        operation_id: str,
    ) -> InteractionStatusResponse | None:
        return InteractionStatusResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            operation_id=operation_id,
            state=PublicOperationState.SUCCEEDED,
            updated_at=datetime(2026, 8, 7, tzinfo=UTC),
        )


def _context(role: ProductRole = ProductRole.ELDER) -> ProductRequestContext:
    return ProductRequestContext(
        service_principal_id="trusted-bff",
        actor_id="opaque-actor",
        binding_id="binding-1",
        subject_id="opaque-subject",
        role=role,
        effective_scopes=frozenset(
            {
                "product:sleep:today:read",
                "product:sleep:trends:read",
                "product:sleep:care:read",
                "product:sleep:records:read",
                "product:sleep:interaction:write",
                "product:sleep:interaction:answer",
                "product:sleep:care:confirm",
                "product:sleep:feedback:write",
                "product:sleep:operation:read",
            }
        ),
        namespace_id="replay:test",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-test",
        arm_id="arm-test",
        purpose="sleep_care",
        authorization_epoch=1,
        privacy_epoch=2,
        retrieval_epoch=3,
        policy_sha256="a" * 64,
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/product/sleep/today",
            "raw_path": b"/product/sleep/today",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 443),
        }
    )


def test_queries_only_return_authoritative_role_projection() -> None:
    identity = Identity(_context(ProductRole.FAMILY))
    service = ProductApiService(identity_resolver=identity, backend=Backend())

    result = service.today(_request())

    assert result.data_mode == "replay"
    assert result.synthetic_non_release is True
    assert result.role == ProductRole.FAMILY
    assert result.subject_ref == "opaque-subject"
    assert result.content.audience == "family"


def test_stage_two_command_authenticates_then_reserves_durable_operation() -> None:
    identity = Identity(_context())
    backend = Backend()
    service = ProductApiService(identity_resolver=identity, backend=backend)

    authenticated_body = (
        b'{"episode_revision_id":"rev-1","intent":"morning_review"}'
    )
    result = service.submit(
        _request(),
        route_template="/product/sleep/interactions/start",
        command_type="interaction.start",
        idempotency_key="caller-key-1",
        payload={"intent": "morning_review", "episode_revision_id": "rev-1"},
        target_id="rev-1",
        request_body=authenticated_body,
    )

    assert result.operation_id == "01987654-3210-7abc-8def-0123456789ab"
    assert result.status_url.endswith(result.operation_id)
    assert len(backend.reservations) == 1
    assert backend.reservations[0]["command_type"] == "interaction.start"
    assert len(backend.reservations[0]["body_sha256"]) == 64
    assert identity.calls[0][1] == "sleep_care"
    assert identity.calls[0][0] == authenticated_body


def test_missing_idempotency_key_fails_after_authentication_before_any_write() -> None:
    backend = Backend()
    identity = Identity(_context())
    service = ProductApiService(
        identity_resolver=identity,
        backend=backend,
    )

    with pytest.raises(ProductApiError, match="Idempotency-Key") as captured:
        service.submit(
            _request(),
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=None,
            payload={"intent": "morning_review"},
            request_body=b'{"intent":"morning_review"}',
        )
    assert captured.value.status_code == 400
    assert backend.reservations == []
    assert len(identity.calls) == 1


def test_doctor_cannot_confirm_personal_care_action() -> None:
    service = ProductApiService(
        identity_resolver=Identity(_context(ProductRole.DOCTOR)),
        backend=Backend(),
    )

    with pytest.raises(ProductApiError, match="Doctor role") as captured:
        service.submit(
            _request(),
            route_template=(
                "/product/sleep/interactions/{interaction_id}/confirm"
            ),
            command_type="interaction.confirm",
            idempotency_key="confirm-1",
            payload={"confirmation_handle": "opaque-random-handle"},
            target_id="interaction-1",
            request_body=b'{"confirmation_handle":"opaque-random-handle"}',
        )
    assert captured.value.status_code == 403


def test_command_without_authenticated_body_fails_closed() -> None:
    backend = Backend()
    identity = Identity(_context())
    service = ProductApiService(identity_resolver=identity, backend=backend)

    with pytest.raises(ProductApiError, match="authenticated request body") as captured:
        service.submit(
            _request(),
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key="caller-key-1",
            payload={"intent": "morning_review"},
        )

    assert captured.value.code == "authenticated_body_missing"
    assert identity.calls == []
    assert backend.reservations == []


def test_backend_cross_role_projection_is_treated_as_integrity_failure() -> None:
    class CrossRoleBackend(Backend):
        def get_today_projection(
            self,
            context: ProductRequestContext,
        ) -> ProductSleepTodayProjection:
            return ProductSleepTodayProjection(
                data_mode=context.data_mode,
                synthetic_non_release=True,
                state=ProductTodayState.READY,
                subject_ref=context.subject_id,
                role=ProductRole.DOCTOR,
                episode_id="night-1",
                episode_revision_id="night-revision-1",
                episode_local_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
                assignment_basis=EpisodeAssignmentBasis.OBSERVED_WAKE,
                analysis_revision_id="analysis-1",
                projection_id="view-1",
                projection_version=1,
                committed_at=datetime(2026, 8, 7, tzinfo=UTC),
                content={
                    "audience": "doctor",
                    "summary_text": "doctor-only",
                    "context_notice": "doctor context",
                    "evidence_refs": (),
                },
            )

    service = ProductApiService(
        identity_resolver=Identity(_context(ProductRole.ELDER)),
        backend=CrossRoleBackend(),
    )

    with pytest.raises(RuntimeError, match="outside the authorized role"):
        service.today(_request())


def test_stage_three_read_models_have_distinct_typed_contracts() -> None:
    identity = Identity(_context())
    service = ProductApiService(
        identity_resolver=identity,
        backend=Backend(),
    )

    trends = service.query(_request(), kind="trends", limit=30, cursor=None)
    care = service.query(_request(), kind="care", limit=20, cursor=None)
    records = service.query(_request(), kind="records", limit=20, cursor=None)

    assert trends.schema_version == "product_sleep_trends.v1"
    assert care.schema_version == "product_sleep_care.v1"
    assert records.schema_version == "product_sleep_records.v1"
    assert identity.calls == [(b"", "sleep_care")] * 3
