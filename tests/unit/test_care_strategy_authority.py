from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from sleepagent.runtime.agents import (
    CareStrategyAgent,
    RuntimeRoleInvocation,
    _CareCatalogAuthorityUnit,
    _CareDeliverySelection,
    _CareStrategyPlan,
    _CareStrategySelectedAction,
    _CareStrategyAuthorityModel,
    _build_care_strategy_authority_manifest,
    _materialize_nonurgent_care_delivery,
)
from sleepagent.runtime.contracts import (
    AgentId,
    CareActionCandidate,
    CareDeliveryDecision,
    CareDeliveryModality,
    CareDeliveryTiming,
    ContextPacket,
    EpisodeType,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    SourceScope,
    SourceScopeKind,
    TrustedContextItem,
    TrustLabel,
    stable_hash,
)
from sleepagent.runtime.governance import (
    AcceptanceError,
    CareActionCatalog,
    CareActionDefinition,
    CareDeliveryPolicy,
    PRODUCT_SAFETY_POLICY_VERSION,
)
from sleepagent.runtime.invocation import CareStrategyModelOutput
from sleepagent.runtime.registry import (
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
    default_agent_profiles,
    default_skill_packages,
)


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
HASH = "a" * 64
EVIDENCE_REF = "evidence-work-product:opaque:7f9c"
CLAIM_ID = "claim:opaque:8ad3"
CARE_ACTION_ID = "care-action:opaque:62be"


def _scope() -> SourceScope:
    return SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 8, 14),
        date_end=date(2026, 8, 14),
        valid_night_count=1,
    )


def _evidence() -> EvidencePacket:
    return EvidencePacket(
        packet_id="evidence:opaque",
        source_scope=_scope(),
        claims=[
            EvidenceClaim(
                claim_id=CLAIM_ID,
                semantic=EvidenceSemantic.USER_REPORTED,
                statement=(
                    "已验收的个人背景支持在近期保持稳定安排。"
                ),
                source_kind=EvidenceSourceKind.CONFIRMED_HABIT,
                evidence_refs=["habit-fact:opaque:913a"],
                confidence=1,
                date_start=date(2026, 8, 14),
                date_end=date(2026, 8, 14),
            )
        ],
    )


def _catalog_definition() -> CareActionDefinition:
    return CareActionDefinition(
        care_action_id=CARE_ACTION_ID,
        version=7,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        contraindication_codes=["constraint:opaque"],
        delivery_required=False,
    )


def _immediate_only_definition() -> CareActionDefinition:
    return CareActionDefinition(
        care_action_id="care-action:immediate-only:91ab",
        version=2,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.IMMEDIATE,),
        allowed_delivery_modalities=(CareDeliveryModality.LIGHT,),
    )


def _morning_safe_definition() -> CareActionDefinition:
    return CareActionDefinition(
        care_action_id="care-action:morning-safe:72cd",
        version=4,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.MORNING,),
        allowed_delivery_modalities=(CareDeliveryModality.LIGHT,),
    )


def _context(
    *,
    context_packet_id: str = "context:care:one",
    invocation_id: str = "care_strategy:episode:care:1",
    include_catalog: bool = False,
    include_care_state: bool = False,
    catalog_definition: CareActionDefinition | None = None,
    catalog_definitions: tuple[CareActionDefinition, ...] | None = None,
) -> ContextPacket:
    items: list[TrustedContextItem] = [
        TrustedContextItem(
            key="accepted:evidence_packet",
            trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
            value=_evidence().model_dump(mode="json"),
            source_refs=(EVIDENCE_REF,),
        )
    ]
    if include_catalog:
        definitions = catalog_definitions or (
            catalog_definition or _catalog_definition(),
        )
        catalog_refs = tuple(
            f"care-catalog:{definition.care_action_id}:v{definition.version}"
            for definition in definitions
        )
        items.append(
            TrustedContextItem(
                key="tool:care.read_catalog",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={
                    "catalog_version": "care-catalog.v-test",
                    "actions": [
                        definition.model_dump(mode="json")
                        for definition in definitions
                    ],
                    "source_refs": list(catalog_refs),
                },
                source_refs=("tool-invocation:catalog", *catalog_refs),
            )
        )
    if include_care_state:
        items.append(
            TrustedContextItem(
                key="tool:care.read_state",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={
                    "state": {
                        "subject_id": "subject:care",
                        "version": 1,
                        "status": "none",
                    },
                    "source_refs": ["care-state:subject:care:v1"],
                },
                source_refs=(
                    "tool-invocation:care-state",
                    "care-state:subject:care:v1",
                ),
            )
        )
    return ContextPacket(
        context_packet_id=context_packet_id,
        episode_id="episode:care",
        invocation_id=invocation_id,
        agent_id=AgentId.CARE_STRATEGY,
        objective="基于已验收证据形成单一照护策略",
        fact_snapshot_id="snapshot:care",
        fact_snapshot_hash=HASH,
        episode_state_revision=2,
        care_context_version=1,
        source_scope=_scope(),
        authorization_scope=("read_sleep_data",),
        items=tuple(items),
    )


def _role_invocation(context: ContextPacket) -> RuntimeRoleInvocation:
    registry = SkillRegistry(default_skill_packages())
    skill_id = "propose_single_care_action"
    bundle, skill_lock = SkillResolver(registry).resolve(
        episode_id=context.episode_id,
        episode_type=EpisodeType.MORNING_REVIEW,
        agent_id=AgentId.CARE_STRATEGY,
        mandatory_skill_ids=[skill_id],
        subject_id="subject:care",
    )
    package = bundle.packages[0]
    profile = default_agent_profiles()[AgentId.CARE_STRATEGY]
    compiled = PromptCompiler().compile(
        global_policy=("只使用已验收证据和授权工具结果。",),
        profile=profile,
        bundle=bundle,
        context=context,
    )
    return RuntimeRoleInvocation(
        context=context,
        episode_type=EpisodeType.MORNING_REVIEW,
        subject_id="subject:care",
        target_id=f"care-strategy:{context.invocation_id}",
        target_hash_material={"test": context.context_packet_id},
        skill_id=skill_id,
        skill_version=package.version,
        prompt_version=f"{skill_id}.prompt.{package.version}",
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        profile_version=profile.version,
        profile_hash=profile.profile_hash,
        skill_package_hash=package.package_hash,
        skill_lock_hash=skill_lock.lock_hash,
        prompt_bundle_hash=compiled.receipt.prompt_bundle_hash,
        compiled_messages=compiled.messages,
        accepted_evidence_ref=EVIDENCE_REF,
    )


def _selected_action(
    *,
    care_action_id: str | None = CARE_ACTION_ID,
    version: int | None = 7,
    rationale_refs: list[str] | None = None,
    activatable: bool = True,
    delivery: _CareDeliverySelection | None = None,
) -> _CareStrategySelectedAction:
    return _CareStrategySelectedAction(
        care_action_id=care_action_id,
        version=version,
        title="保持稳定的日常安排",
        rationale_evidence_refs=(
            [CLAIM_ID] if rationale_refs is None else rationale_refs
        ),
        parameters={"tolerance_minutes": 30} if activatable else {},
        delivery=delivery,
        confirmation_required=True,
        activatable=activatable,
    )


def _plan(
    *,
    action: _CareStrategySelectedAction | None = None,
    disposition: str = "propose",
    needs_care_state: bool = False,
) -> _CareStrategyPlan:
    selected_action = _selected_action() if action is None else action
    return _CareStrategyPlan.model_validate(
        {
            "disposition": disposition,
            "summary": "已形成受约束的照护策略。",
            "reason_codes": [],
            "selected_action": selected_action,
            "needs_care_state": needs_care_state,
        }
    )


def _no_action_plan(*, needs_care_state: bool = False) -> _CareStrategyPlan:
    return _CareStrategyPlan(
        disposition="no_action",
        summary="已形成受约束的照护策略。",
        selected_action=None,
        needs_care_state=needs_care_state,
    )


class RecordingModel:
    provider = "live-test-provider"
    model_id = "live-test-care-model"

    def __init__(self, outputs: list[_CareStrategyPlan]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.last_provider_request_id: str | None = None
        self.last_provider_input_tokens: int | None = None

    def generate(self, **kwargs: Any) -> _CareStrategyPlan:
        self.calls.append(kwargs)
        self.last_provider_request_id = f"request:{len(self.calls)}"
        self.last_provider_input_tokens = 100 + len(self.calls)
        return self.outputs.pop(0)


class NeverCalledModel:
    provider = "live-test-provider"
    model_id = "must-not-run"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, **kwargs: Any) -> _CareStrategyPlan:
        del kwargs
        self.calls += 1
        raise AssertionError("Live provider must not run before catalog preflight")


def _invoke_agent(
    model: Any,
    context: ContextPacket,
):
    agent = CareStrategyAgent(
        model,
        skill_registry=SkillRegistry(default_skill_packages()),
    )
    return agent.invoke(agent.bind(_role_invocation(context)))


def _manifest_from_call(call: dict[str, Any]) -> dict[str, Any]:
    message = next(
        item
        for item in call["messages"]
        if "CareStrategy per-invocation authority manifest" in item["content"]
    )
    return json.loads(message["content"].split("Authority manifest JSON: ", 1)[1])


def test_missing_catalog_uses_deterministic_preflight_without_live_call() -> None:
    model = NeverCalledModel()

    result = _invoke_agent(model, _context())

    assert model.calls == 0
    assert result.record.provider == "sleepagent-deterministic"
    assert result.record.model_id == "care-catalog-preflight.v1"
    assert result.record.provider_request_id is None
    assert result.payload.disposition == "no_action"
    assert result.payload.primary_action is None
    assert result.payload.evidence_packet_refs == [EVIDENCE_REF]
    assert len(result.envelope.tool_requests) == 1
    request = result.envelope.tool_requests[0]
    assert request.tool_name == "care.read_catalog"
    assert request.arguments == {}
    assert request.request_id.startswith("care-catalog-preflight:")


def test_catalog_feedback_calls_live_model_with_exact_authority_manifest() -> None:
    model = RecordingModel([_plan()])

    result = _invoke_agent(model, _context(include_catalog=True))
    manifest = _manifest_from_call(model.calls[0])

    assert len(model.calls) == 1
    assert model.calls[0]["schema"] is _CareStrategyPlan
    assert manifest["accepted_evidence"]["work_product_ref"] == EVIDENCE_REF
    assert manifest["accepted_evidence"]["claims"] == [
        {
            "claim_id": CLAIM_ID,
            "statement": "已验收的个人背景支持在近期保持稳定安排。",
            "source_kind": "confirmed_habit",
            "evidence_refs": ["habit-fact:opaque:913a"],
        }
    ]
    catalog_action = manifest["care_catalog"]["actions"][0]
    assert (catalog_action["care_action_id"], catalog_action["version"]) == (
        CARE_ACTION_ID,
        7,
    )
    assert catalog_action["allowed_parameters"] == {
        "tolerance_minutes": [0.0, 45.0]
    }
    assert result.payload.evidence_packet_refs == [EVIDENCE_REF]
    assert result.payload.primary_action is not None
    assert result.payload.primary_action.rationale_evidence_refs == [CLAIM_ID]
    assert result.record.schema_version == "CareStrategyModelOutput.v1"
    assert result.record.provider_request_id == "request:1"


def test_plan_assembly_derives_evidence_refs_ids_and_candidate_hash() -> None:
    result = _invoke_agent(
        RecordingModel([_plan()]),
        _context(include_catalog=True),
    )

    action = result.payload.primary_action
    assert result.payload.evidence_packet_refs == [EVIDENCE_REF]
    assert result.payload.strategy_id.startswith("care-strategy:")
    assert action is not None
    assert action.candidate_id.startswith("care-candidate:")
    assert action.candidate_hash == stable_hash(
        action.model_dump(mode="python", exclude={"candidate_hash"})
    )


def test_private_plan_rejects_deterministic_output_identity_fields() -> None:
    with pytest.raises(ValidationError, match="strategy_id"):
        _CareStrategyPlan.model_validate(
            {
                **_no_action_plan().model_dump(mode="python"),
                "strategy_id": "model-must-not-generate-this",
            }
        )


def test_nonurgent_voice_intent_cannot_recreate_excess_volume_failure() -> None:
    definition = CareActionDefinition(
        care_action_id=CARE_ACTION_ID,
        version=7,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.MORNING,),
        allowed_delivery_modalities=(
            CareDeliveryModality.VOICE,
            CareDeliveryModality.SILENT,
        ),
    )
    policy = CareDeliveryPolicy()
    old_untrusted_delivery = CareDeliveryDecision(
        timing="morning",
        modality="voice",
        interruption_burden="low",
        voice_volume_percent=80,
        voice_tone="neutral",
        device_policy_ref=policy.device_policy_ref,
    )
    old_action = CareActionCandidate.create(
        candidate_id="old-untrusted-delivery",
        candidate_version=1,
        care_action_id=CARE_ACTION_ID,
        care_action_version=7,
        title="保持稳定的日常安排",
        rationale_evidence_refs=[CLAIM_ID],
        parameters={"tolerance_minutes": 30},
        delivery=old_untrusted_delivery,
        activatable=True,
    )
    catalog = CareActionCatalog([definition], delivery_policy=policy)
    with pytest.raises(
        AcceptanceError,
        match="non-urgent voice volume exceeds delivery policy",
    ):
        catalog.validate(old_action)

    result = _invoke_agent(
        RecordingModel(
            [
                _plan(
                    action=_selected_action(
                        delivery=_CareDeliverySelection(
                            preferred_timing="morning",
                            preferred_modality="voice",
                        )
                    )
                )
            ]
        ),
        _context(include_catalog=True, catalog_definition=definition),
    )

    assembled = result.payload.primary_action
    assert assembled is not None
    assert assembled.delivery == policy.conservative_default()
    assert assembled.delivery.voice_volume_percent is None
    catalog.validate(assembled)


def test_delivery_plan_schema_has_no_operational_volume_authority() -> None:
    assert set(_CareDeliverySelection.model_fields) == {
        "preferred_timing",
        "preferred_modality",
    }
    with pytest.raises(ValidationError, match="voice_volume_percent"):
        _CareDeliverySelection.model_validate(
            {
                "preferred_timing": "morning",
                "preferred_modality": "voice",
                "voice_volume_percent": 80,
            }
        )


def test_required_delivery_uses_policy_conservative_default_when_allowed() -> None:
    definition = CareActionDefinition(
        care_action_id=CARE_ACTION_ID,
        version=7,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.MORNING,),
        allowed_delivery_modalities=(CareDeliveryModality.SILENT,),
    )

    result = _invoke_agent(
        RecordingModel([_plan()]),
        _context(include_catalog=True, catalog_definition=definition),
    )

    action = result.payload.primary_action
    assert action is not None
    assert action.delivery == CareDeliveryPolicy().conservative_default()
    CareActionCatalog([definition]).validate(action)


def test_catalog_policy_intersection_materializes_deterministic_light() -> None:
    definition = CareActionDefinition(
        care_action_id=CARE_ACTION_ID,
        version=7,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.MORNING,),
        allowed_delivery_modalities=(CareDeliveryModality.LIGHT,),
    )

    result = _invoke_agent(
        RecordingModel(
            [
                _plan(
                    action=_selected_action(
                        delivery=_CareDeliverySelection(
                            preferred_timing="immediate",
                            preferred_modality="voice",
                        )
                    )
                )
            ]
        ),
        _context(include_catalog=True, catalog_definition=definition),
    )

    action = result.payload.primary_action
    assert action is not None and action.delivery is not None
    assert action.delivery.timing is CareDeliveryTiming.MORNING
    assert action.delivery.modality is CareDeliveryModality.LIGHT
    assert action.delivery.voice_volume_percent is None
    assert not action.delivery.conservative_default_applied
    CareActionCatalog([definition]).validate(action)


def test_catalog_without_safe_nonurgent_policy_intersection_fails_closed() -> None:
    entry = _CareCatalogAuthorityUnit(
        care_action_id=CARE_ACTION_ID,
        version=7,
        catalog_source_ref=f"care-catalog:{CARE_ACTION_ID}:v7",
        delivery_required=True,
        allowed_delivery_timings=(CareDeliveryTiming.IMMEDIATE.value,),
        allowed_delivery_modalities=(CareDeliveryModality.VOICE.value,),
    )

    with pytest.raises(ValueError, match="no safe timing"):
        _materialize_nonurgent_care_delivery(
            entry=entry,
            intent=_CareDeliverySelection(
                preferred_timing="immediate",
                preferred_modality="voice",
            ),
        )


def test_manifest_exposes_only_nonurgent_policy_eligible_catalog_actions() -> None:
    immediate = _immediate_only_definition()
    morning = _morning_safe_definition()
    context = _context(
        include_catalog=True,
        catalog_definitions=(immediate, morning),
    )
    model = RecordingModel(
        [
            _plan(
                action=_selected_action(
                    care_action_id=morning.care_action_id,
                    version=morning.version,
                )
            )
        ]
    )

    result = _invoke_agent(model, context)
    manifest = _manifest_from_call(model.calls[0])
    raw_catalog = next(
        item.value
        for item in context.items
        if item.key == "tool:care.read_catalog"
    )

    assert {
        (item["care_action_id"], item["version"])
        for item in raw_catalog["actions"]
    } == {
        (immediate.care_action_id, immediate.version),
        (morning.care_action_id, morning.version),
    }
    assert [
        (item["care_action_id"], item["version"])
        for item in manifest["care_catalog"]["actions"]
    ] == [(morning.care_action_id, morning.version)]
    action = result.payload.primary_action
    assert action is not None and action.delivery is not None
    assert action.delivery.timing is CareDeliveryTiming.MORNING
    assert action.delivery.modality is CareDeliveryModality.LIGHT
    CareActionCatalog([immediate, morning]).validate(action)


def test_filtered_immediate_only_catalog_selection_fails_closed() -> None:
    immediate = _immediate_only_definition()
    morning = _morning_safe_definition()

    with pytest.raises(ValueError, match="not in invocation catalog"):
        _invoke_agent(
            RecordingModel(
                [
                    _plan(
                        action=_selected_action(
                            care_action_id=immediate.care_action_id,
                            version=immediate.version,
                        )
                    )
                ]
            ),
            _context(
                include_catalog=True,
                catalog_definitions=(immediate, morning),
            ),
        )


def test_delivery_optional_action_remains_manifest_eligible() -> None:
    optional = CareActionDefinition(
        care_action_id="care-action:optional:83ef",
        version=5,
        allowed_parameters={"tolerance_minutes": (0, 45)},
        delivery_required=False,
        allowed_delivery_timings=(CareDeliveryTiming.IMMEDIATE,),
        allowed_delivery_modalities=(CareDeliveryModality.VOICE,),
    )
    context = _context(
        include_catalog=True,
        catalog_definitions=(optional,),
    )
    model = RecordingModel(
        [
            _plan(
                action=_selected_action(
                    care_action_id=optional.care_action_id,
                    version=optional.version,
                )
            )
        ]
    )

    result = _invoke_agent(model, context)
    manifest = _manifest_from_call(model.calls[0])

    assert [
        (item["care_action_id"], item["version"])
        for item in manifest["care_catalog"]["actions"]
    ] == [(optional.care_action_id, optional.version)]
    action = result.payload.primary_action
    assert action is not None
    assert action.delivery is None
    CareActionCatalog([optional]).validate(action)


def test_empty_eligible_catalog_allows_only_no_action_without_fallback() -> None:
    immediate = _immediate_only_definition()
    model = RecordingModel([_no_action_plan()])

    result = _invoke_agent(
        model,
        _context(
            include_catalog=True,
            catalog_definitions=(immediate,),
        ),
    )
    manifest = _manifest_from_call(model.calls[0])

    assert manifest["care_catalog"]["actions"] == []
    assert result.payload.disposition == "no_action"
    assert result.payload.primary_action is None


@pytest.mark.parametrize(
    ("care_action_id", "version"),
    [
        ("care-action:forged", 7),
        (CARE_ACTION_ID, 8),
    ],
)
def test_activatable_unknown_or_wrong_version_catalog_pair_fails_closed(
    care_action_id: str,
    version: int,
) -> None:
    with pytest.raises(ValueError, match="not in invocation catalog"):
        _invoke_agent(
            RecordingModel(
                [
                    _plan(
                        action=_selected_action(
                            care_action_id=care_action_id,
                            version=version,
                        )
                    )
                ]
            ),
            _context(include_catalog=True),
        )


def test_nonactivatable_action_without_catalog_identity_is_allowed() -> None:
    output = _plan(
        action=_selected_action(
            care_action_id=None,
            version=None,
            activatable=False,
        )
    )

    result = _invoke_agent(
        RecordingModel([output]),
        _context(include_catalog=True),
    )

    assert result.payload.primary_action is not None
    assert not result.payload.primary_action.activatable
    assert result.payload.primary_action.care_action_id is None


def test_nonactivatable_forged_catalog_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown catalog identity"):
        _invoke_agent(
            RecordingModel(
                [
                    _plan(
                        action=_selected_action(
                            care_action_id="care-action:forged",
                            version=7,
                            activatable=False,
                        )
                    )
                ]
            ),
            _context(include_catalog=True),
        )


def test_care_read_state_request_remains_on_existing_tool_loop_path() -> None:
    output = _no_action_plan(needs_care_state=True)

    result = _invoke_agent(
        RecordingModel([output]),
        _context(include_catalog=True),
    )

    assert [item.tool_name for item in result.envelope.tool_requests] == [
        "care.read_state"
    ]
    assert result.envelope.tool_requests[0].arguments == {}
    assert result.envelope.tool_requests[0].request_id.startswith("care-state:")


def test_existing_care_state_receipt_prevents_duplicate_request() -> None:
    result = _invoke_agent(
        RecordingModel([_no_action_plan(needs_care_state=True)]),
        _context(include_catalog=True, include_care_state=True),
    )

    assert result.envelope.tool_requests == []
    assert result.payload.disposition == "no_action"


def test_care_state_availability_is_isolated_per_invocation() -> None:
    model = RecordingModel(
        [
            _no_action_plan(needs_care_state=True),
            _no_action_plan(needs_care_state=True),
        ]
    )

    first = _invoke_agent(
        model,
        _context(include_catalog=True, include_care_state=True),
    )
    second = _invoke_agent(
        model,
        _context(
            include_catalog=True,
            context_packet_id="context:care:no-state",
            invocation_id="care_strategy:episode:care:2",
        ),
    )

    assert first.envelope.tool_requests == []
    assert [item.tool_name for item in second.envelope.tool_requests] == [
        "care.read_state"
    ]


def test_no_action_plan_never_creates_primary_action() -> None:
    result = _invoke_agent(
        RecordingModel([_no_action_plan()]),
        _context(include_catalog=True),
    )

    assert result.payload.primary_action is None
    assert result.payload.evidence_packet_refs == [EVIDENCE_REF]


def test_catalog_manifest_is_per_invocation_and_never_crosses_contexts() -> None:
    second_definition = CareActionDefinition(
        care_action_id="care-action:second:71de",
        version=3,
    )
    model = RecordingModel(
        [
            _plan(),
            _plan(
                action=_selected_action(
                    care_action_id=second_definition.care_action_id,
                    version=second_definition.version,
                )
            ),
        ]
    )

    _invoke_agent(model, _context(include_catalog=True))
    _invoke_agent(
        model,
        _context(
            include_catalog=True,
            context_packet_id="context:care:second",
            invocation_id="care_strategy:episode:care:2",
            catalog_definition=second_definition,
        ),
    )
    first = _manifest_from_call(model.calls[0])
    second = _manifest_from_call(model.calls[1])

    assert first["context_packet_id"] == "context:care:one"
    assert second["context_packet_id"] == "context:care:second"
    assert first["care_catalog"]["actions"][0]["care_action_id"] == CARE_ACTION_ID
    assert second["care_catalog"]["actions"][0]["care_action_id"] == (
        second_definition.care_action_id
    )
    assert second_definition.care_action_id not in json.dumps(first)
    assert CARE_ACTION_ID not in json.dumps(second)


def test_forged_rationale_claim_ref_fails_closed_without_repair() -> None:
    with pytest.raises(ValueError, match="rationale cites unaccepted Evidence"):
        _invoke_agent(
            RecordingModel(
                [
                    _plan(
                        action=_selected_action(
                            rationale_refs=["claim:forged"]
                        )
                    )
                ]
            ),
            _context(include_catalog=True),
        )


def test_manifest_builder_detaches_typed_context_values() -> None:
    context = _context(include_catalog=True)
    command = CareStrategyAgent(
        NeverCalledModel(),
        skill_registry=SkillRegistry(default_skill_packages()),
    ).bind(_role_invocation(context))
    manifest = _build_care_strategy_authority_manifest(command)
    evidence_item = context.items[0]
    catalog_item = context.items[1]

    evidence_item.value["claims"][0]["statement"] = "mutated"
    catalog_item.value["actions"][0]["care_action_id"] = "mutated"

    assert manifest.evidence_claims[0].statement != "mutated"
    assert manifest.catalog_entries[0].care_action_id == CARE_ACTION_ID


def test_authority_adapter_rejects_context_mismatch_before_live_call() -> None:
    context = _context(include_catalog=True)
    agent = CareStrategyAgent(
        NeverCalledModel(),
        skill_registry=SkillRegistry(default_skill_packages()),
    )
    manifest = _build_care_strategy_authority_manifest(
        agent.bind(_role_invocation(context))
    )
    base = RecordingModel([_plan()])
    adapter = _CareStrategyAuthorityModel(
        base_model=base,
        manifest=manifest,
        care_state_available=False,
    )

    with pytest.raises(ValueError, match="ContextPacket identity mismatch"):
        adapter.generate(
            messages=[],
            schema=CareStrategyModelOutput,
            prompt_version="propose_single_care_action.prompt.test",
            context_packet_id="context:care:wrong",
        )

    assert not base.calls
