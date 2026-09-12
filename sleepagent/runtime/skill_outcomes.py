# 本模块把既有可信 invocation、SkillLock 与终态结果绑定为离线 SkillOutcome。
# 唯一入口是 bind_skill_outcome；它不负责在线学习、Prompt 修改、注册表变更或持久化。
"""Core, offline-only feedback for existing trusted invocation/result evidence.

Failures or denials before an invocation record exists remain pending capture.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    AgentId, EpisodeStatus, FrozenContract, InvocationOutcome, stable_hash,
)
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.registry import RootCauseKind, SkillLock, SkillOutcomeStatus, SkillPackage
from sleepagent.runtime.results import ProductEpisodeRunResult


_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_ReasonCode = Annotated[str, Field(
    min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:-]*$")]
_InputRef = Annotated[str, Field(
    min_length=3, max_length=256, pattern=r"^[a-z][a-z0-9_.-]{0,31}:[^\s]{1,223}$")]
_SUCCEEDED_VALIDATION_STATUSES = frozenset(
    {"accepted_schema_and_capability_policy", "runtime_validated"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


class SkillOutcome(FrozenContract):
    """Content-addressed feedback; never an online prompt or mutation command."""

    outcome_id: str = Field(..., pattern=r"^skill-outcome:[0-9a-f]{64}$")
    outcome_hash: _Sha256
    episode_id: str = Field(..., min_length=1, max_length=256)
    invocation_id: str = Field(..., min_length=1, max_length=256)
    agent_id: AgentId
    skill_id: str = Field(..., min_length=1, max_length=128)
    skill_version: str = Field(..., min_length=1, max_length=64)
    skill_package_hash: _Sha256
    skill_lock_hash: _Sha256
    result_ref: str = Field(..., pattern=r"^product-result:[0-9a-f]{64}$")
    result_hash: _Sha256
    invocation_outcome: InvocationOutcome
    invocation_validation_status: _ReasonCode
    result_terminal: bool
    result_status: EpisodeStatus
    status: SkillOutcomeStatus
    root_cause: RootCauseKind | None = None
    reason_codes: tuple[_ReasonCode, ...] = Field(default=(), max_length=20)
    input_refs: tuple[_InputRef, ...] = Field(default=(), max_length=30)

    # Outcome 必须自证内容寻址身份，避免离线反馈脱离原调用或被改写后继续流转。
    @model_validator(mode="after")
    def validate_identity(self) -> "SkillOutcome":
        _require(len(set(self.reason_codes)) == len(self.reason_codes),
                 "SkillOutcome reason codes must be unique")
        _require(len(set(self.input_refs)) == len(self.input_refs),
                 "SkillOutcome input refs must be unique")
        _require(self.result_ref == f"product-result:{self.result_hash}",
                 "SkillOutcome result reference mismatch")
        waiting = self.result_status in {
            EpisodeStatus.WAITING_USER, EpisodeStatus.WAITING_CONFIRMATION,
        }
        _require(waiting != self.result_terminal, "SkillOutcome terminal state mismatch")
        _require(self.invocation_outcome is InvocationOutcome.SUCCEEDED and
                 self.invocation_validation_status in _SUCCEEDED_VALIDATION_STATUSES,
                 "SkillOutcome invocation outcome is not provable")
        material = self.model_dump(mode="json", exclude={"outcome_id", "outcome_hash"})
        expected = stable_hash(material)
        _require(self.outcome_hash == expected, "SkillOutcome hash mismatch")
        _require(self.outcome_id == f"skill-outcome:{expected}", "SkillOutcome ID mismatch")
        return self


def bind_skill_outcome(
    *,
    invocation: AgentInvocationRecord,
    skill_package: SkillPackage,
    skill_lock: SkillLock,
    result: ProductEpisodeRunResult,
    result_ref: str,
    result_hash: str,
    status: SkillOutcomeStatus,
    root_cause: RootCauseKind | None = None,
    reason_codes: tuple[str, ...] = (),
) -> SkillOutcome:
    """Validate exact runtime lineage and return deterministic offline feedback."""

    # 先校验锁文件和 Skill 包身份，禁止调用方把另一版本或另一 Agent 的结果嫁接进来。
    lock_material = skill_lock.model_dump(mode="json", exclude={"lock_hash"})
    _require(skill_lock.lock_hash == stable_hash(lock_material), "SkillLock hash mismatch")
    package_ref = f"{skill_package.skill_id}@{skill_package.version}:{skill_package.package_hash}"
    invocation_ref = f"{invocation.skill_id}@{invocation.skill_version}:{invocation.skill_package_hash}"
    _require(skill_package.owner_agent == invocation.agent_id and package_ref == invocation_ref,
             "invocation Skill package identity mismatch")
    _require(skill_lock.episode_id == invocation.episode_id,
             "invocation and SkillLock Episode mismatch")
    _require(invocation.skill_lock_hash == skill_lock.lock_hash, "invocation SkillLock hash mismatch")
    _require(skill_lock.package_locks.count(package_ref) == 1,
             "Skill identity is not exactly present in SkillLock")
    _require(invocation.validation_status in _SUCCEEDED_VALIDATION_STATUSES,
             "invocation outcome is not provable")

    # 再把终态结果精确绑定到 invocation；只有可复算的 canonical hash 才能形成 Outcome。
    _require(result.receipt.episode_id == invocation.episode_id,
             "invocation and result Episode mismatch")
    _require(result.receipt.agent_invocation_ids.count(invocation.invocation_id) == 1,
             "result receipt invocation mismatch")
    records = [item for item in result.agent_invocations
               if item.invocation_id == invocation.invocation_id]
    _require(len(records) == 1, "result invocation mismatch")
    _require(records[0] == invocation, "result invocation record mismatch")
    canonical_result_hash = stable_hash(result)
    _require(result_hash == canonical_result_hash, "result hash mismatch")
    _require(result_ref == f"product-result:{canonical_result_hash}", "result reference mismatch")

    material = {
        "episode_id": invocation.episode_id,
        "invocation_id": invocation.invocation_id,
        "agent_id": invocation.agent_id,
        "skill_id": invocation.skill_id,
        "skill_version": invocation.skill_version,
        "skill_package_hash": invocation.skill_package_hash,
        "skill_lock_hash": skill_lock.lock_hash,
        "result_ref": result_ref,
        "result_hash": result_hash,
        "invocation_outcome": InvocationOutcome.SUCCEEDED,
        "invocation_validation_status": invocation.validation_status,
        "result_terminal": result.receipt.terminal,
        "result_status": result.receipt.status,
        "status": status,
        "root_cause": root_cause,
        "reason_codes": tuple(sorted(reason_codes)),
        "input_refs": (f"context-packet:{invocation.context_packet_id}",),
    }
    # Outcome ID 直接由完整治理材料派生，使任何字段变化都产生新的、可审计的身份。
    outcome_hash = stable_hash(material)
    return SkillOutcome(outcome_id=f"skill-outcome:{outcome_hash}",
                        outcome_hash=outcome_hash, **material)


__all__ = ["SkillOutcome", "bind_skill_outcome"]
