from __future__ import annotations

from typing import Literal, TypeAlias

from sleepagent.radar_agent.product_agent.contracts import AgentId, StrictContract
from sleepagent.radar_agent.product_agent.tools.artifact_rendering import (
    ArtifactRenderResult,
    AudienceRole,
)
from sleepagent.radar_agent.schemas import RiskLevel, RoleReportArtifact


ExpressionTone: TypeAlias = Literal["warm", "concise", "clinical"]


class RoleMaterialExpressionDraft(StrictContract):
    """Tone choice plus an immutable echo of the rendered fact boundary."""

    audience_role: AudienceRole
    tone: ExpressionTone
    claim_ids: list[str]
    evidence_refs: list[str]
    risk_level: RiskLevel
    caveats: list[str]

    @classmethod
    def from_artifact(
        cls,
        artifact: RoleReportArtifact,
        *,
        tone: ExpressionTone,
    ) -> "RoleMaterialExpressionDraft":
        return cls(
            audience_role=artifact.role,
            tone=tone,
            claim_ids=list(artifact.claim_ids),
            evidence_refs=list(artifact.evidence_refs),
            risk_level=artifact.risk_level,
            caveats=list(artifact.caveats),
        )


class SleepCareRoleMaterialSkill:
    """Versioned SleepCare method for protected audience expression only.

    The Skill selects deterministic wording around an already rendered artifact.
    It never accepts new fact text and refuses any drift in claim IDs, Evidence
    references, risk or caveats.  It does not persist, export or publish.
    """

    owner = AgentId.SLEEP_CARE
    skill_version = "1.0.0"

    def __init__(self, *, audience_role: AudienceRole) -> None:
        if audience_role not in {"elder", "family", "doctor"}:
            raise ValueError("unsupported role-material audience")
        self.audience_role = audience_role
        self.skill_id = (
            "draft_doctor_material"
            if audience_role == "doctor"
            else "draft_user_material"
        )

    def draft(
        self,
        rendered: ArtifactRenderResult,
        expression: RoleMaterialExpressionDraft,
    ) -> RoleReportArtifact:
        if type(rendered) is not ArtifactRenderResult:
            raise TypeError("role-material Skill requires ArtifactRenderResult")
        if type(expression) is not RoleMaterialExpressionDraft:
            raise TypeError(
                "role-material Skill requires RoleMaterialExpressionDraft"
            )

        artifact = rendered.artifact
        protected = (
            rendered.audience_role == self.audience_role
            and artifact.role == self.audience_role
            and expression.audience_role == self.audience_role
            and expression.claim_ids == artifact.claim_ids
            and expression.evidence_refs == artifact.evidence_refs
            and expression.risk_level == artifact.risk_level
            and expression.caveats == artifact.caveats
        )
        if not protected:
            raise ValueError(
                "role-material expression cannot modify audience, claim_ids, "
                "evidence_refs, risk, or caveats"
            )

        # Only the presentation prefix/title changes.  Every fact-bearing field
        # remains the exact Tool result, so this method cannot form new facts.
        material = artifact.model_dump(mode="python")
        material.update(
            {
                "title": _tone_title(self.audience_role, expression.tone),
                "content": _tone_content(artifact, expression.tone),
            }
        )
        return RoleReportArtifact.model_validate(material)


def _tone_title(role: AudienceRole, tone: ExpressionTone) -> str:
    titles = {
        "elder": {
            "warm": "昨夜睡眠观察",
            "concise": "昨夜观察摘要",
            "clinical": "昨夜结构化观察",
        },
        "family": {
            "warm": "家属照护观察",
            "concise": "家属照护摘要",
            "clinical": "家属结构化观察",
        },
        "doctor": {
            "warm": "雷达观察摘要",
            "concise": "雷达证据摘要",
            "clinical": "雷达观察证据摘要",
        },
    }
    return titles[role][tone]


def _tone_content(
    artifact: RoleReportArtifact,
    tone: ExpressionTone,
) -> str:
    prefixes = {
        "elder": {
            "warm": "我们根据昨夜设备记录，为您整理了这些观察：",
            "concise": "昨夜设备观察如下：",
            "clinical": "昨夜结构化设备观察如下：",
        },
        "family": {
            "warm": "以下观察可供家属持续关注：",
            "concise": "家属照护观察摘要：",
            "clinical": "家属照护结构化观察：",
        },
        "doctor": {
            "warm": "以下为供审阅的雷达观察事实：",
            "concise": "雷达 canonical 事实摘要：",
            "clinical": (
                "以下为毫米波雷达 canonical 数据与结构化 claim 摘要："
            ),
        },
    }
    lines = artifact.content.splitlines()
    return "\n".join([prefixes[artifact.role][tone], *lines[1:]])


__all__ = [
    "ExpressionTone",
    "RoleMaterialExpressionDraft",
    "SleepCareRoleMaterialSkill",
]
