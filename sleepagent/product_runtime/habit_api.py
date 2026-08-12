from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from sleepagent.persistence import (
    RadarPersistenceStore,
)
from sleepagent.product_runtime.contracts import (
    AuthenticatedBinding,
)
from sleepagent.product_runtime.habit_application import (
    HABIT_APPLICATION_VERSION,
    HabitAnswerSubmitRequest,
    HabitAnswerSubmitResponse,
    HabitChangeSetConfirmRequest,
    HabitChangeSetPruneRequest,
    HabitCommitResponse,
    HabitForgetRequest,
    HabitInteractionStartRequest,
    HabitInteractionStartResponse,
    HabitObserverProposalRequest,
    HabitPendingChangeSet,
    HabitProfileApplicationService,
)
from sleepagent.product_runtime.habit_profile import (
    HabitProfileReadResult,
)
from sleepagent.product_runtime.runtime_factory import (
    ProductRuntimeBundle,
    build_product_runtime_bundle_from_env,
)
from sleepagent.product_runtime.questionnaire import (
    DEFAULT_HABIT_CONCEPTS,
    HabitConceptDefinition,
)


HABIT_PROFILE_API_PREFIX = "/product/habit-profile"
PRODUCT_API_KEY_ENV = "SLEEPAGENT_PRODUCT_RADAR_API_KEY"

router = APIRouter(prefix=HABIT_PROFILE_API_PREFIX, tags=["habit-profile"])


def build_persistent_habit_profile_application(
    connection: sqlite3.Connection | None = None,
    *,
    human_decisions: Any | None = None,
) -> HabitProfileApplicationService:
    """Compatibility facade over the canonical Product runtime factory."""

    if connection is None:
        raise RuntimeError(
            "retired Habit Profile compatibility requires explicit test storage"
        )
    persistence = RadarPersistenceStore.connect_sqlite(connection)
    return build_product_runtime_bundle_from_env(
        persistence_store=persistence,
        human_decisions=human_decisions,
    ).habit_application


_RUNTIME_BUNDLE: ProductRuntimeBundle | None = None
_APPLICATION: HabitProfileApplicationService | None = None
_API_KEY: str | None = None


def configure_habit_profile_runtime(
    bundle: ProductRuntimeBundle,
    *,
    api_key: str | None = None,
) -> None:
    """Bind this transport to an already composed Product runtime bundle."""

    global _API_KEY, _APPLICATION, _RUNTIME_BUNDLE
    _RUNTIME_BUNDLE = bundle
    _APPLICATION = bundle.habit_application
    if api_key is not None:
        _API_KEY = api_key


def _runtime_bundle() -> ProductRuntimeBundle:
    if _RUNTIME_BUNDLE is None:
        raise RuntimeError("retired Habit Profile runtime is not configured")
    assert _RUNTIME_BUNDLE is not None
    return _RUNTIME_BUNDLE


def _application() -> HabitProfileApplicationService:
    application = _runtime_bundle().habit_application
    if _APPLICATION is not application:
        raise RuntimeError("Habit Profile application/runtime binding drift")
    return application


async def _authenticated_binding(
    request: Request,
    x_actor_id: Annotated[str | None, Header()] = None,
    x_actor_role: Annotated[str | None, Header()] = None,
    x_subject_id: Annotated[str | None, Header()] = None,
    x_authorization_scopes: Annotated[str | None, Header()] = None,
) -> AuthenticatedBinding:
    expected = _API_KEY
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Product API authentication is not configured.",
        )
    supplied = request.headers.get("x-api-key")
    if not supplied:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        supplied = token if scheme.lower() == "bearer" else None
    if supplied != expected:
        raise HTTPException(
            status_code=401,
            detail="Product API authentication required.",
        )
    if not x_actor_id or not x_actor_role or not x_subject_id:
        raise HTTPException(
            status_code=403,
            detail="Authenticated actor, role and subject headers are required.",
        )
    if x_actor_role not in {"elder", "family", "doctor"}:
        raise HTTPException(status_code=403, detail="Unsupported Habit Profile role.")
    scopes = tuple(
        sorted(
            {
                item.strip()
                for item in (x_authorization_scopes or "").split(",")
                if item.strip()
            }
        )
    )
    return AuthenticatedBinding(
        actor_id=x_actor_id,
        subject_id=x_subject_id,
        role=x_actor_role,
        authorization_scope=scopes,
    )


Binding = Annotated[AuthenticatedBinding, Depends(_authenticated_binding)]


@router.get("/availability")
async def habit_profile_availability() -> dict[str, object]:
    persistent = _runtime_bundle().persistence_store is not None
    return {
        "application_version": HABIT_APPLICATION_VERSION,
        "explicit_user_flows_enabled": True,
        "default_proactive_intake_enabled": False,
        "confirmed_profile_storage": "database" if persistent else "in_memory_test",
        "confirmed_question_suppression_storage": (
            "database" if persistent else "in_memory_test"
        ),
        "release_gate": "requires_real_3_to_5_participant_usability_report",
    }


@router.get("/concepts", response_model=list[HabitConceptDefinition])
async def list_habit_concepts(_: Binding) -> list[HabitConceptDefinition]:
    return [
        item
        for item in DEFAULT_HABIT_CONCEPTS
        if item.domain_review_status == "approved"
    ]


@router.post(
    "/interactions/start",
    response_model=HabitInteractionStartResponse,
)
async def start_habit_interaction(
    payload: HabitInteractionStartRequest,
    binding: Binding,
) -> HabitInteractionStartResponse:
    return _call(_application().start, payload, binding=binding)


@router.post(
    "/interactions/answers",
    response_model=HabitAnswerSubmitResponse,
)
async def submit_habit_answers(
    payload: HabitAnswerSubmitRequest,
    binding: Binding,
) -> HabitAnswerSubmitResponse:
    return _call(_application().submit, payload, binding=binding)


@router.post(
    "/observer-proposals",
    response_model=HabitPendingChangeSet,
)
async def build_observer_proposal(
    payload: HabitObserverProposalRequest,
    binding: Binding,
) -> HabitPendingChangeSet:
    return _call(
        _application().build_observer_proposal,
        payload,
        binding=binding,
    )


@router.get("", response_model=HabitProfileReadResult)
async def read_habit_profile(
    binding: Binding,
    purpose: Literal[
        "evidence",
        "care",
        "profile_review",
        "doctor_material",
        "family_coordination",
    ] = "profile_review",
    concept_id: Annotated[list[str] | None, Query()] = None,
    include_stale_for_review: bool = False,
) -> HabitProfileReadResult:
    if binding.role != "elder" and not concept_id:
        raise HTTPException(
            status_code=422,
            detail="Collaborative Profile reads require explicit concept_id.",
        )
    return _call(
        _application().read_profile,
        binding=binding,
        purpose=purpose,
        requested_concept_ids=tuple(concept_id or ()),
        include_stale_for_review=include_stale_for_review,
    )


@router.post("/forget", response_model=HabitPendingChangeSet)
async def request_habit_forget(
    payload: HabitForgetRequest,
    binding: Binding,
) -> HabitPendingChangeSet:
    return _call(_application().request_forget, payload, binding=binding)


@router.post("/confirm", response_model=HabitCommitResponse)
async def confirm_habit_change_set(
    payload: HabitChangeSetConfirmRequest,
    binding: Binding,
) -> HabitCommitResponse:
    return _call(_application().confirm, payload, binding=binding)


@router.post("/change-sets/prune", response_model=HabitPendingChangeSet)
async def prune_habit_change_set(
    payload: HabitChangeSetPruneRequest,
    binding: Binding,
) -> HabitPendingChangeSet:
    return _call(_application().prune_change_set, payload, binding=binding)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = [
    "HABIT_PROFILE_API_PREFIX",
    "PRODUCT_API_KEY_ENV",
    "build_persistent_habit_profile_application",
    "configure_habit_profile_runtime",
    "router",
]
