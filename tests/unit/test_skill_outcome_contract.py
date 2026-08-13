from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

import sleepagent.runtime.registry as skill_registry
from sleepagent.runtime.contracts import (AgentId, EpisodeReceipt, EpisodeStatus, EpisodeType,
    ExecutionMode, InvocationOutcome, SourceScope, SourceScopeKind, stable_hash)
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.registry import (SkillLock, SkillOutcomeStatus, SkillPackage,
    SkillRegistry, SkillResolver, default_skill_packages)
from sleepagent.runtime.results import ProductEpisodeRunResult
from sleepagent.runtime.skill_outcomes import SkillOutcome, bind_skill_outcome

pytestmark = pytest.mark.unit
NOW = datetime(2026, 8, 13, tzinfo=timezone.utc)
def _fixture() -> tuple[Any, ...]:
    registry = SkillRegistry(default_skill_packages())
    bundle, lock = SkillResolver(registry).resolve(
        episode_id="episode:one", episode_type=EpisodeType.MORNING_REVIEW,
        agent_id=AgentId.EVIDENCE_REASONING,
        mandatory_skill_ids=["interpret_scoped_evidence"])
    package = bundle.packages[0]
    invocation = AgentInvocationRecord(
        invocation_id="invocation:one", episode_id=lock.episode_id,
        agent_id=package.owner_agent, agent_version="agent.v1",
        skill_id=package.skill_id, skill_version=package.version,
        skill_package_hash=package.package_hash, skill_lock_hash=lock.lock_hash,
        schema_version="EvidencePacket.v1", prompt_version="prompt.v1",
        policy_version="policy.v1", context_packet_id="context:one",
        context_hash="a" * 64, target_hash="b" * 64,
        provider="test", model_id="model", started_at=NOW, ended_at=NOW,
        latency_ms=0, validation_status="runtime_validated", safe_summary="ok",
    )
    receipt = EpisodeReceipt(
        episode_id=lock.episode_id, episode_type=EpisodeType.MORNING_REVIEW,
        receipt_revision=1, terminal=True, execution_mode=ExecutionMode.INTELLIGENT,
        status=EpisodeStatus.COMPLETE, goal_achieved=True,
        fact_snapshot_id="snapshot:one", fact_snapshot_hash="c" * 64,
        source_scope=SourceScope(kind=SourceScopeKind.GENERAL_KNOWLEDGE, as_of=NOW,
                                 timezone_name="UTC"),
        final_episode_state_revision=1, agent_invocation_ids=[invocation.invocation_id],
        trace_ref="trace:one",
    )
    result = ProductEpisodeRunResult(registry_hash="d" * 64, receipt=receipt,
                                     agent_invocations=[invocation])
    return registry, package, invocation, lock, result

def _bind(package: SkillPackage, invocation: AgentInvocationRecord, lock: SkillLock,
          result: ProductEpisodeRunResult, **updates: Any) -> SkillOutcome:
    result_hash = stable_hash(result)
    values: dict[str, Any] = {
        "result_ref": f"product-result:{result_hash}", "result_hash": result_hash,
        "status": SkillOutcomeStatus.SUCCEEDED, "root_cause": None,
        "reason_codes": ("validation:runtime_validated",)}
    values.update(updates)
    return bind_skill_outcome(invocation=invocation, skill_package=package,
                              skill_lock=lock, result=result, **values)

def test_valid_binding_is_stable_and_content_addressed() -> None:
    _, package, invocation, lock, result = _fixture()
    first = _bind(package, invocation, lock, result)
    assert first == _bind(package, invocation, lock, result)
    assert (first.outcome_id, first.result_terminal, first.result_status) == (
        f"skill-outcome:{first.outcome_hash}", True, EpisodeStatus.COMPLETE)
    assert (first.invocation_outcome, first.invocation_validation_status) == (
        InvocationOutcome.SUCCEEDED, invocation.validation_status)
    assert first.input_refs == ("context-packet:context:one",)
    for field, value, message in (
        ("outcome_hash", "f" * 64, "SkillOutcome hash mismatch"),
        ("outcome_id", "skill-outcome:" + "f" * 64, "SkillOutcome ID mismatch")):
        with pytest.raises(ValidationError, match=message):
            SkillOutcome.model_validate(first.model_dump(mode="json") | {field: value})
@pytest.mark.parametrize(("field", "value"), [
    ("episode_id", "episode:other"), ("invocation_id", "invocation:other"),
    ("agent_id", AgentId.CARE_STRATEGY), ("skill_id", "other_skill"),
    ("skill_version", "9.9.9"), ("skill_package_hash", "f" * 64), ("safe_summary", "forged"),
    ("validation_status", "unknown"),
])
def test_rejects_invocation_identity_mismatch(field: str, value: Any) -> None:
    _, package, invocation, lock, result = _fixture()
    with pytest.raises(ValueError, match="mismatch|not exactly present|not provable"):
        _bind(package, invocation.model_copy(update={field: value}), lock, result)

def test_rejects_skill_missing_from_lock_and_tampered_lock_hash() -> None:
    _, package, invocation, lock, result = _fixture()
    material = lock.model_dump(mode="json", exclude={"lock_hash"}) | {"package_locks": ()}
    empty = SkillLock(**material, lock_hash=stable_hash(material))
    rebound = invocation.model_copy(update={"skill_lock_hash": empty.lock_hash})
    rebound_result = result.model_copy(update={"agent_invocations": [rebound]})
    with pytest.raises(ValueError, match="not exactly present"):
        _bind(package, rebound, empty, rebound_result)
    with pytest.raises(ValueError, match="SkillLock hash mismatch"):
        _bind(package, invocation, lock.model_copy(update={"lock_hash": "f" * 64}), result)
    cross = result.model_copy(update={
        "receipt": result.receipt.model_copy(update={"episode_id": "episode:other"})})
    with pytest.raises(ValueError, match="invocation and result Episode mismatch"):
        _bind(package, invocation, lock, cross)

@pytest.mark.parametrize(("field", "value"), [
    ("result_ref", "product-result:" + "e" * 64), ("result_hash", "e" * 64),
])
def test_rejects_result_reference_or_hash_mismatch(field: str, value: str) -> None:
    _, package, invocation, lock, result = _fixture()
    with pytest.raises(ValueError, match="result (reference|hash) mismatch"):
        _bind(package, invocation, lock, result, **{field: value})

@pytest.mark.parametrize(("field", "value"), [
    ("reason_codes", tuple(f"reason:{i}" for i in range(21))),
    ("reason_codes", ("UPPER_CASE",)), ("reason_codes", ("x" * 65,)),
    ("reason_codes", ("duplicate",) * 2),
    ("input_refs", tuple(f"source:{i}" for i in range(31))),
    ("input_refs", ("missing-colon",)), ("input_refs", ("source:has space",)),
    ("input_refs", ("source:" + "x" * 250,)), ("input_refs", ("source:duplicate",) * 2),
    ("result_terminal", False), ("invocation_outcome", InvocationOutcome.FAILED),
])
def test_reason_codes_and_input_refs_are_bounded(field: str, value: Any) -> None:
    _, package, invocation, lock, result = _fixture()
    values = _bind(package, invocation, lock, result).model_dump(mode="json") | {field: value}
    with pytest.raises(ValidationError):
        SkillOutcome.model_validate(values)

def test_binding_has_no_prompt_surface_or_registry_mutation() -> None:
    registry, package, invocation, lock, result = _fixture()
    before = registry.snapshot()
    outcome = _bind(package, invocation, lock, result)
    assert registry.snapshot() == before
    assert not hasattr(skill_registry, "SkillOutcome")
    banned = ("prompt", "context", "message", "inject", "compile", "permission", "policy")
    assert not any(token in name.lower() for name in dir(outcome) for token in banned)
