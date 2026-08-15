from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

import sleepagent.runtime.factory as runtime_factory
import sleepagent.runtime.agents as runtime_agents
import sleepagent.runtime.deterministic_model as deterministic_model
from sleepagent.runtime.agents import (
    RuntimeRoleInvocation,
    SleepCareAgent,
    _SleepCareAssemblyModel,
    _SleepCareContentPlan,
    _assemble_sleepcare_output,
    _build_sleepcare_source_catalog,
)
from sleepagent.runtime.contracts import (
    AgentId,
    CareActionCandidate,
    CareStrategy,
    CommunicationDraft,
    ContextPacket,
    EpisodeType,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    SafetyDecision,
    SafetyVerdict,
    SourceScope,
    SourceScopeKind,
    TrustedContextItem,
    TrustLabel,
    WorkProductStatus,
    agent_target_hash,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.governance import PRODUCT_SAFETY_POLICY_VERSION
from sleepagent.runtime.invocation import (
    CareStrategyModelOutput,
    EvidenceReasoningModelOutput,
    ProductAgentInvoker,
    SafetyReviewModelOutput,
    SleepCareModelOutput,
)
from sleepagent.runtime.registry import (
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
    default_agent_profiles,
    default_skill_packages,
)


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def _scope() -> SourceScope:
    return SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 8, 14),
        date_end=date(2026, 8, 14),
        valid_night_count=1,
    )


def _evidence_packet(
    *,
    statement: str = (
        "已确认的个人背景是通常在夜间醒来 1 次；"
        "这是非临床事实。"
    ),
    claim_id: str = "claim:habit-baseline",
) -> EvidencePacket:
    return EvidencePacket(
        packet_id="evidence:accepted",
        source_scope=_scope(),
        claims=[
            EvidenceClaim(
                claim_id=claim_id,
                semantic=EvidenceSemantic.USER_REPORTED,
                statement=statement,
                source_kind=EvidenceSourceKind.CONFIRMED_HABIT,
                evidence_refs=["habit-fact:night-awakening"],
                confidence=1,
                date_start=date(2026, 8, 14),
                date_end=date(2026, 8, 14),
            )
        ],
    )


def _care_strategy() -> CareStrategy:
    action = CareActionCandidate.create(
        candidate_id="care:consistent-wake",
        candidate_version=1,
        care_action_id="consistent-wake-time",
        care_action_version=1,
        title="连续 5 天保持较稳定的起床安排",
        rationale_evidence_refs=["claim:habit-baseline"],
        duration_days=5,
        confirmation_required=True,
        activatable=True,
    )
    return CareStrategy(
        strategy_id="strategy:accepted",
        disposition="propose",
        evidence_packet_refs=["evidence-work-product:accepted"],
        primary_action=action,
    )


def _context(
    *,
    audience_role: str = "elder",
    context_packet_id: str = "context:sleepcare:one",
    statement: str = (
        "已确认的个人背景是通常在夜间醒来 1 次；"
        "这是非临床事实。"
    ),
    claim_id: str = "claim:habit-baseline",
    include_care: bool = True,
    include_reviewed_knowledge: bool = False,
    agent_id: AgentId = AgentId.SLEEP_CARE,
) -> ContextPacket:
    evidence = _evidence_packet(statement=statement, claim_id=claim_id)
    items: list[TrustedContextItem] = [
        TrustedContextItem(
            key="accepted:evidence_packet",
            trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
            value=evidence.model_dump(mode="json"),
            source_refs=("evidence-work-product:accepted",),
        )
    ]
    if include_care:
        items.append(
            TrustedContextItem(
                key="accepted:care_strategy",
                trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
                value=_care_strategy().model_dump(mode="json"),
                source_refs=("care-work-product:accepted",),
            )
        )
    if include_reviewed_knowledge:
        items.append(
            TrustedContextItem(
                key="tool:knowledge.retrieve_reviewed",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={
                    "snippets": ["规律作息有助于形成稳定的睡眠安排。"],
                    "citation_ids": ["knowledge:citation:stable-routine"],
                    "source_refs": ["knowledge:citation:stable-routine"],
                },
                source_refs=(
                    "tool:knowledge:receipt",
                    "knowledge:citation:stable-routine",
                ),
            )
        )
    items.append(
        TrustedContextItem(
            key="requested_audience_role",
            trust_label=TrustLabel.SYSTEM_POLICY,
            value=audience_role,
        )
    )
    return ContextPacket(
        context_packet_id=context_packet_id,
        episode_id="episode:sleepcare",
        invocation_id="sleep_care:episode:sleepcare:1",
        agent_id=agent_id,
        objective="形成受约束的睡眠沟通内容",
        fact_snapshot_id="snapshot:sleepcare",
        fact_snapshot_hash=HASH,
        episode_state_revision=3,
        care_context_version=1,
        source_scope=_scope(),
        authorization_scope=("read_sleep_data",),
        items=tuple(items),
    )


def _plan(*segments: tuple[str, str]) -> _SleepCareContentPlan:
    return _SleepCareContentPlan.model_validate(
        {
            "status": "completed",
            "selected_segments": [
                {"source_type": source_type, "source_ref": source_ref}
                for source_type, source_ref in segments
            ],
            "tool_requests": [],
            "collaboration_requests": [],
            "reason_codes": [],
            "memory_change_candidates": [],
        }
    )


class StaticModel:
    def __init__(
        self,
        output: Any,
        *,
        provider: str,
        model_id: str,
        request_id: str,
        input_tokens: int,
    ) -> None:
        self.output = output
        self.provider = provider
        self.model_id = model_id
        self.last_provider_request_id = request_id
        self.last_provider_input_tokens = input_tokens
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.output


class SequencedPlanModel:
    provider = "live-test-provider"
    model_id = "live-test-model"

    def __init__(self, plans: list[_SleepCareContentPlan]) -> None:
        self._plans = list(plans)
        self.calls: list[dict[str, Any]] = []
        self.last_provider_request_id: str | None = None
        self.last_provider_input_tokens: int | None = None

    def generate(self, **kwargs: Any) -> _SleepCareContentPlan:
        self.calls.append(kwargs)
        self.last_provider_request_id = f"provider-request:{len(self.calls)}"
        self.last_provider_input_tokens = 40 + len(self.calls)
        return self._plans.pop(0)


def _plain_sleepcare_output(text: str) -> SleepCareModelOutput:
    return SleepCareModelOutput(
        status=WorkProductStatus.COMPLETED,
        summary="已形成沟通内容。",
        output_payload=CommunicationDraft(
            draft_id=f"draft:{text}",
            audience_role="elder",
            text=text,
            context_notice="内容仅覆盖当前授权范围。",
        ),
    )


def _invoke(
    invoker: ProductAgentInvoker,
    context: ContextPacket,
    *,
    caller: AgentId | str,
    model_override: Any = None,
):
    return invoker.invoke(
        caller=caller,
        context=context,
        parent_invocation_id=None,
        target_type="communication",
        target_id="communication:episode:sleepcare:3",
        target_hash_material={"test": "model-override"},
        skill_id="explain_for_elder",
        skill_version="1.0.0",
        prompt_version="explain_for_elder.prompt.1.0.0",
        agent_version="sleep_care.v1",
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        compiled_messages=[{"role": "user", "content": "opaque prompt"}],
        model_override=model_override,
    )


def test_product_invoker_uses_per_invocation_model_override_metadata() -> None:
    base = StaticModel(
        _plain_sleepcare_output("基础模型正文。"),
        provider="base-provider",
        model_id="base-model",
        request_id="base-request",
        input_tokens=11,
    )
    override = StaticModel(
        _plain_sleepcare_output("本次覆盖模型正文。"),
        provider="override-provider",
        model_id="override-model",
        request_id="override-request",
        input_tokens=22,
    )
    envelope, record = _invoke(
        ProductAgentInvoker(agent_id=AgentId.SLEEP_CARE, model=base),
        _context(include_care=False),
        caller="runtime",
        model_override=override,
    )

    assert envelope.output_payload.text == "本次覆盖模型正文。"
    assert not base.calls
    assert len(override.calls) == 1
    assert record.provider == "override-provider"
    assert record.model_id == "override-model"
    assert record.provider_request_id == "override-request"
    assert record.provider_input_tokens == 22
    assert record.schema_version == "SleepCareModelOutput.v1"


def test_product_invoker_without_override_preserves_original_model_path() -> None:
    base = StaticModel(
        _plain_sleepcare_output("原模型正文。"),
        provider="base-provider",
        model_id="base-model",
        request_id="base-request",
        input_tokens=13,
    )
    envelope, record = _invoke(
        ProductAgentInvoker(agent_id=AgentId.SLEEP_CARE, model=base),
        _context(include_care=False),
        caller="runtime",
    )

    assert envelope.output_payload.text == "原模型正文。"
    assert len(base.calls) == 1
    assert record.provider == "base-provider"
    assert record.provider_request_id == "base-request"


def test_typed_context_builds_detached_immutable_habit_source_catalog() -> None:
    context = _context(include_care=False)
    catalog = _build_sleepcare_source_catalog(context)
    unit = catalog.units[0]

    assert unit.source_type == "evidence_claim"
    assert unit.source_ref == "claim:habit-baseline"
    assert unit.authority_source_kind == "confirmed_habit"
    assert unit.authority_refs == ("habit-fact:night-awakening",)
    assert "非临床事实" in unit.exact_text

    evidence_item = next(
        item for item in context.items if item.key == "accepted:evidence_packet"
    )
    evidence_item.value["claims"][0]["statement"] = "被外部修改的正文"
    assert "非临床事实" in catalog.units[0].exact_text
    with pytest.raises(ValidationError):
        catalog.audience_role = "family"  # type: ignore[misc]


def test_content_plan_schema_has_only_selection_and_request_fields() -> None:
    assert set(_SleepCareContentPlan.model_fields) == {
        "status",
        "selected_segments",
        "tool_requests",
        "collaboration_requests",
        "reason_codes",
        "memory_change_candidates",
    }


def test_deterministic_model_builds_plan_from_shared_catalog_and_assembler() -> None:
    context = _context(audience_role="family")
    catalog = _build_sleepcare_source_catalog(context)
    model = DeterministicReplayStructuredAgentModel()
    messages = [{"role": "user", "content": context.model_dump_json()}]

    plan = model.generate(
        messages=messages,
        schema=_SleepCareContentPlan,
        prompt_version="explain_for_elder.prompt.3.0.0",
        context_packet_id=context.context_packet_id,
    )
    direct = model.generate(
        messages=messages,
        schema=SleepCareModelOutput,
        prompt_version="explain_for_elder.prompt.3.0.0",
        context_packet_id=context.context_packet_id,
    )

    assert deterministic_model._assemble_sleepcare_output is (
        runtime_agents._assemble_sleepcare_output
    )
    assert [
        (item.source_type, item.source_ref) for item in plan.selected_segments
    ] == [(item.source_type, item.source_ref) for item in catalog.units]
    assert direct == _assemble_sleepcare_output(
        catalog=catalog,
        plan=plan,
        doctor_material=False,
    )
    assert direct.output_payload.audience_role == "family"
    assert all(
        binding.rendered_text in direct.output_payload.text
        for binding in direct.output_payload.semantic_bindings
    )


def test_assembler_uses_same_exact_evidence_text_and_numeric_binding() -> None:
    catalog = _build_sleepcare_source_catalog(_context(include_care=False))
    result = _assemble_sleepcare_output(
        catalog=catalog,
        plan=_plan(("evidence_claim", "claim:habit-baseline")),
        doctor_material=False,
    )
    binding = result.output_payload.semantic_bindings[0]

    assert binding.rendered_text == catalog.units[0].exact_text
    assert binding.rendered_text in result.output_payload.text
    assert binding.source_kind == "evidence_claim"
    assert binding.source_ref == "claim:habit-baseline"
    assert binding.preserved_numbers == ["1"]
    assert result.output_payload.claim_refs == ["claim:habit-baseline"]
    assert SleepCareModelOutput.model_validate(result.model_dump()) == result


def test_assembler_uses_same_exact_care_text_and_derived_ref() -> None:
    catalog = _build_sleepcare_source_catalog(_context())
    care_unit = next(
        item for item in catalog.units if item.source_type == "care_candidate"
    )
    result = _assemble_sleepcare_output(
        catalog=catalog,
        plan=_plan(("care_candidate", "care:consistent-wake")),
        doctor_material=False,
    )
    binding = result.output_payload.semantic_bindings[0]

    assert binding.rendered_text == care_unit.exact_text
    assert binding.rendered_text in result.output_payload.text
    assert binding.source_kind == "care_candidate"
    assert binding.source_ref == "care:consistent-wake"
    assert binding.preserved_numbers == ["5"]
    assert result.output_payload.care_candidate_refs == ["care:consistent-wake"]


@pytest.mark.parametrize(
    "segments",
    [
        (("evidence_claim", "claim:forged"),),
        (
            ("evidence_claim", "claim:habit-baseline"),
            ("evidence_claim", "claim:habit-baseline"),
        ),
    ],
)
def test_assembler_fails_closed_for_unknown_or_duplicate_selection(
    segments: tuple[tuple[str, str], ...],
) -> None:
    catalog = _build_sleepcare_source_catalog(_context(include_care=False))

    with pytest.raises(ValueError, match="unknown source|duplicate source"):
        _assemble_sleepcare_output(
            catalog=catalog,
            plan=_plan(*segments),
            doctor_material=False,
        )


@pytest.mark.parametrize(
    ("audience_role", "prefix", "artifact_kind"),
    [
        ("elder", "本次已验收信息：", None),
        ("family", "供家属了解的已验收信息：", None),
        ("doctor", "已验收证据：", "doctor_material"),
    ],
)
def test_audience_and_templates_come_only_from_typed_context(
    audience_role: str,
    prefix: str,
    artifact_kind: str | None,
) -> None:
    catalog = _build_sleepcare_source_catalog(
        _context(audience_role=audience_role, include_care=False)
    )
    result = _assemble_sleepcare_output(
        catalog=catalog,
        plan=_plan(("evidence_claim", "claim:habit-baseline")),
        doctor_material=audience_role == "doctor",
    )

    assert result.output_payload.audience_role == audience_role
    assert result.output_payload.text.startswith(prefix)
    assert result.output_payload.artifact_kind == artifact_kind
    assert not any(character.isdigit() for character in prefix)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _SleepCareContentPlan.model_validate(
            {
                **_plan(
                    ("evidence_claim", "claim:habit-baseline")
                ).model_dump(mode="json"),
                "audience_role": "family",
                "rendered_text": "试图由模型改写正文",
            }
        )


def test_reviewed_knowledge_requires_deterministic_citation_pairing() -> None:
    catalog = _build_sleepcare_source_catalog(
        _context(include_care=False, include_reviewed_knowledge=True)
    )
    knowledge = next(
        item for item in catalog.units if item.source_type == "reviewed_knowledge"
    )

    assert knowledge.source_ref == "knowledge:citation:stable-routine"
    assert knowledge.binding_source_kind == "general_knowledge"
    assert knowledge.authority_refs == ("knowledge:citation:stable-routine",)

    invalid_context = _context(
        include_care=False,
        include_reviewed_knowledge=True,
        context_packet_id="context:sleepcare:invalid-knowledge",
    )
    invalid_item = next(
        item
        for item in invalid_context.items
        if item.key == "tool:knowledge.retrieve_reviewed"
    )
    invalid_item.value["citation_ids"] = ["knowledge:citation:forged"]
    invalid_catalog = _build_sleepcare_source_catalog(invalid_context)

    assert not any(
        item.source_type == "reviewed_knowledge"
        for item in invalid_catalog.units
    )


def test_safety_decision_is_a_gate_not_communication_source_authority() -> None:
    context = _context(include_care=False)
    safety = SafetyDecision(
        verdict=SafetyVerdict.APPROVE,
        review_target_type="work_product",
        review_target_id="target:communication",
        review_target_hash="d" * 64,
        fact_snapshot_hash=HASH,
        reviewed_episode_state_revision=3,
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        expires_at=NOW + timedelta(days=1),
        reason_codes=["reviewed_risk_code_must_not_become_prose"],
    )
    safety_item = TrustedContextItem(
        key="accepted:safety_decision",
        trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
        value=safety.model_dump(mode="json"),
        source_refs=("safety-work-product:accepted",),
    )
    audience = context.items[-1]
    with_safety = context.model_copy(
        update={"items": (*context.items[:-1], safety_item, audience)}
    )

    catalog = _build_sleepcare_source_catalog(with_safety)
    output = _assemble_sleepcare_output(
        catalog=catalog,
        plan=_plan(("evidence_claim", "claim:habit-baseline")),
        doctor_material=False,
    )

    assert all("safety" not in item.source_type for item in catalog.units)
    assert "reviewed_risk_code_must_not_become_prose" not in output.output_payload.text


def test_assembly_model_context_mismatch_fails_before_provider_call() -> None:
    base = SequencedPlanModel(
        [_plan(("evidence_claim", "claim:habit-baseline"))]
    )
    adapter = _SleepCareAssemblyModel(
        base_model=base,
        catalog=_build_sleepcare_source_catalog(_context(include_care=False)),
        doctor_material=False,
    )

    with pytest.raises(ValueError, match="ContextPacket identity mismatch"):
        adapter.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="explain_for_elder.prompt.1.0.0",
            context_packet_id="context:wrong",
        )

    assert not base.calls


def test_per_invocation_adapters_do_not_share_catalog_or_metadata() -> None:
    plan = _plan(("evidence_claim", "claim:shared"))
    base = SequencedPlanModel([plan, plan])
    first = _SleepCareAssemblyModel(
        base_model=base,
        catalog=_build_sleepcare_source_catalog(
            _context(
                context_packet_id="context:first",
                statement="第一份已验收来源正文。",
                claim_id="claim:shared",
                include_care=False,
            )
        ),
        doctor_material=False,
    )
    second = _SleepCareAssemblyModel(
        base_model=base,
        catalog=_build_sleepcare_source_catalog(
            _context(
                context_packet_id="context:second",
                statement="第二份已验收来源正文。",
                claim_id="claim:shared",
                include_care=False,
            )
        ),
        doctor_material=False,
    )

    first_output = first.generate(
        messages=[],
        schema=SleepCareModelOutput,
        prompt_version="explain_for_elder.prompt.1.0.0",
        context_packet_id="context:first",
    )
    second_output = second.generate(
        messages=[],
        schema=SleepCareModelOutput,
        prompt_version="explain_for_elder.prompt.1.0.0",
        context_packet_id="context:second",
    )

    assert "第一份已验收来源正文。" in first_output.output_payload.text
    assert "第二份已验收来源正文。" not in first_output.output_payload.text
    assert "第二份已验收来源正文。" in second_output.output_payload.text
    assert first.last_provider_request_id == "provider-request:1"
    assert first.last_provider_input_tokens == 41
    assert second.last_provider_request_id == "provider-request:2"
    assert second.last_provider_input_tokens == 42


def test_sleepcare_agent_returns_final_schema_record_from_content_plan() -> None:
    base = SequencedPlanModel(
        [_plan(("evidence_claim", "claim:habit-baseline"))]
    )
    registry = SkillRegistry(default_skill_packages())
    agent = SleepCareAgent(
        base,
        skill_registry=registry,
        content_plan_assembly=True,
    )
    context = _context(include_care=False)
    bundle, skill_lock = SkillResolver(registry).resolve(
        episode_id=context.episode_id,
        episode_type=EpisodeType.MORNING_REVIEW,
        agent_id=AgentId.SLEEP_CARE,
        mandatory_skill_ids=["explain_for_elder"],
        subject_id="subject:one",
    )
    package = bundle.packages[0]
    profile = default_agent_profiles()[AgentId.SLEEP_CARE]
    compiled = PromptCompiler().compile(
        global_policy=("只使用已验收来源。",),
        profile=profile,
        bundle=bundle,
        context=context,
    )
    target_id = "communication:episode:sleepcare:3"
    invocation = RuntimeRoleInvocation(
        context=context,
        episode_type=EpisodeType.MORNING_REVIEW,
        subject_id="subject:one",
        doctor_material=False,
        target_id=target_id,
        target_hash_material={"test": "sleepcare-assembly"},
        skill_id="explain_for_elder",
        skill_version=package.version,
        prompt_version=f"explain_for_elder.prompt.{package.version}",
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        profile_version=profile.version,
        profile_hash=profile.profile_hash,
        skill_package_hash=package.package_hash,
        skill_lock_hash=skill_lock.lock_hash,
        prompt_bundle_hash=compiled.receipt.prompt_bundle_hash,
        compiled_messages=compiled.messages,
        audience_role="elder",
    )

    output = agent.invoke(agent.bind(invocation))

    assert output.record.schema_version == "SleepCareModelOutput.v1"
    assert output.record.provider == "live-test-provider"
    assert output.record.provider_request_id == "provider-request:1"
    assert output.payload == output.envelope.output_payload
    assert output.record.invocation_id == context.invocation_id
    assert output.record.parent_invocation_id is None
    assert output.record.skill_version == "3.0.0"
    assert output.record.skill_package_hash == package.package_hash
    assert output.record.skill_lock_hash == skill_lock.lock_hash
    assert output.record.prompt_bundle_hash == compiled.receipt.prompt_bundle_hash
    assert output.record.target_hash == agent_target_hash(
        episode_id=context.episode_id,
        target_type="communication",
        target_id=target_id,
        fact_snapshot_id=context.fact_snapshot_id,
        fact_snapshot_hash=context.fact_snapshot_hash,
        episode_state_revision=context.episode_state_revision,
        source_scope=context.source_scope,
        input_work_product_refs=["evidence-work-product:accepted"],
        agent_id=AgentId.SLEEP_CARE,
        agent_version="sleep_care.v1",
        profile_hash=profile.profile_hash,
        skill_id=package.skill_id,
        skill_version=package.version,
        skill_package_hash=package.package_hash,
        schema_version="SleepCareModelOutput.v1",
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        output_payload=output.payload,
    )
    assert len(base.calls) == 1
    assert base.calls[0]["schema"] is _SleepCareContentPlan


def test_canonical_live_factory_enables_content_plan_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    monkeypatch.setattr(
        runtime_factory,
        "openai_compatible_provider_config_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        runtime_factory,
        "OpenAICompatibleStructuredAgentModel",
        lambda **_kwargs: object(),
    )

    def capture_bundle(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runtime_factory, "build_product_runtime_bundle", capture_bundle)

    assert runtime_factory._build_openai_compatible_product_runtime_bundle() is sentinel
    assert captured["sleepcare_content_plan_assembly"] is True


def test_deterministic_factory_enables_the_same_content_plan_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()
    model = DeterministicReplayStructuredAgentModel()

    def capture_bundle(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(runtime_factory, "build_product_runtime_bundle", capture_bundle)

    assert (
        runtime_factory.build_deterministic_product_runtime_bundle(model=model)
        is sentinel
    )
    assert captured["sleepcare_model"] is model
    assert captured["sleepcare_content_plan_assembly"] is True


@pytest.mark.parametrize(
    "agent_id",
    [
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SAFETY_REVIEW,
    ],
)
def test_other_agents_continue_using_bound_model_without_override(
    agent_id: AgentId,
) -> None:
    context = _context(include_care=False, agent_id=agent_id)
    if agent_id is AgentId.EVIDENCE_REASONING:
        output = EvidenceReasoningModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary="证据已形成。",
            output_payload=_evidence_packet(),
        )
    elif agent_id is AgentId.CARE_STRATEGY:
        output = CareStrategyModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary="照护候选已形成。",
            output_payload=_care_strategy(),
        )
    else:
        output = SafetyReviewModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary="安全复核已完成。",
            output_payload=SafetyDecision(
                verdict=SafetyVerdict.APPROVE,
                review_target_type="work_product",
                review_target_id="target:one",
                review_target_hash="d" * 64,
                fact_snapshot_hash=HASH,
                reviewed_episode_state_revision=3,
                policy_version=PRODUCT_SAFETY_POLICY_VERSION,
                expires_at=NOW + timedelta(days=1),
            ),
        )
    model = StaticModel(
        output,
        provider="bound-provider",
        model_id="bound-model",
        request_id="bound-request",
        input_tokens=9,
    )
    invoker = ProductAgentInvoker(agent_id=agent_id, model=model)

    envelope, record = invoker.invoke(
        caller=AgentId.SLEEP_CARE,
        context=context,
        parent_invocation_id=None,
        target_type=invoker.output_schema.__name__,
        target_id=f"target:{agent_id.value}",
        target_hash_material={"test": agent_id.value},
        skill_id="test-skill",
        skill_version="1.0.0",
        prompt_version="test-skill.prompt.1.0.0",
        agent_version=f"{agent_id.value}.v1",
        policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        compiled_messages=[{"role": "user", "content": "opaque prompt"}],
    )

    assert len(model.calls) == 1
    assert envelope.agent_id is agent_id
    assert record.provider == "bound-provider"
