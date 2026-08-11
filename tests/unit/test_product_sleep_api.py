from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import pytest
from starlette.requests import Request

from sleepagent.product_api.contracts import (
    InteractionStatusResponse,
    ProductRole,
    ProjectionRecord,
    PublicOperationState,
)
from sleepagent.product_api.service import (
    ProductApiError,
    ProductApiService,
    ProductRequestContext,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


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

    def list_role_projections(
        self,
        context: ProductRequestContext,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
    ):
        del limit, cursor
        return (
            (
                ProjectionRecord(
                    projection_id=f"view-{kind}",
                    projection_version=1,
                    subject_ref=context.subject_id,
                    role=context.role,
                    status="ready",
                    content={"summary": "role-minimized"},
                    committed_at=datetime(2026, 8, 7, tzinfo=UTC),
                ),
            ),
            None,
        )

    def reserve_command(
        self,
        context: ProductRequestContext,
        **values: Any,
    ) -> str:
        self.reservations.append({"context": context, **values})
        return "01987654-3210-7abc-8def-0123456789ab"

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

    result = service.query(
        _request(),
        kind="today",
        limit=1,
        cursor=None,
    )

    assert result.data_mode == "replay"
    assert result.synthetic_non_release is True
    assert result.items[0].role == ProductRole.FAMILY
    assert result.items[0].subject_ref == "opaque-subject"


def test_command_reservation_captures_body_hash_and_authority_snapshot() -> None:
    identity = Identity(_context())
    backend = Backend()
    service = ProductApiService(identity_resolver=identity, backend=backend)

    accepted = service.submit(
        _request(),
        route_template="/product/sleep/interactions/start",
        command_type="interaction.start",
        idempotency_key="caller-key-1",
        payload={"intent": "morning_review", "episode_revision_id": "rev-1"},
        target_id="rev-1",
    )

    assert accepted.state == PublicOperationState.ACCEPTED
    reservation = backend.reservations[0]
    assert reservation["idempotency_key"] == "caller-key-1"
    assert len(reservation["body_sha256"]) == 64
    assert reservation["context"].authorization_epoch == 1
    assert b"opaque-actor" not in identity.calls[0][0]


def test_missing_idempotency_key_fails_before_any_write() -> None:
    backend = Backend()
    service = ProductApiService(
        identity_resolver=Identity(_context()),
        backend=backend,
    )

    with pytest.raises(ProductApiError, match="Idempotency-Key") as captured:
        service.submit(
            _request(),
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=None,
            payload={"intent": "morning_review"},
        )
    assert captured.value.status_code == 400
    assert backend.reservations == []


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
        )
    assert captured.value.status_code == 403


def test_backend_cross_role_projection_is_treated_as_integrity_failure() -> None:
    class CrossRoleBackend(Backend):
        def list_role_projections(
            self,
            context: ProductRequestContext,
            **_: Any,
        ):
            return (
                (
                    ProjectionRecord(
                        projection_id="bad-view",
                        projection_version=1,
                        subject_ref=context.subject_id,
                        role=ProductRole.DOCTOR,
                        status="ready",
                        content={},
                        committed_at=datetime(2026, 8, 7, tzinfo=UTC),
                    ),
                ),
                None,
            )

    service = ProductApiService(
        identity_resolver=Identity(_context(ProductRole.ELDER)),
        backend=CrossRoleBackend(),
    )

    with pytest.raises(RuntimeError, match="outside the authorized role"):
        service.query(_request(), kind="today", limit=1, cursor=None)
