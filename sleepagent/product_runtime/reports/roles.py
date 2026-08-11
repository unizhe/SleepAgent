"""定义角色报告的稳定角色合同，不负责报告渲染。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from sleepagent.product_runtime.schemas import RadarAgentSchema, RoleReportArtifact


# 三角色枚举是调用方共享的稳定角色标识合同。
class ReportRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


REPORT_ROLE_ORDER: tuple[ReportRole, ...] = (
    ReportRole.ELDER,
    ReportRole.FAMILY,
    ReportRole.DOCTOR,
)


# 角色报告集合的稳定 Pydantic 合同；它不构造或渲染报告内容。
class RoleReportBundle(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    reports: list[RoleReportArtifact] = Field(default_factory=list)

    def for_role(self, role: ReportRole) -> RoleReportArtifact | None:
        """按角色查找报告。

        Args:
            role: 要查找的稳定报告角色。

        Returns:
            匹配角色的报告；找不到时返回 ``None``。
        """
        return next((report for report in self.reports if report.role == role.value), None)


__all__ = [
    "REPORT_ROLE_ORDER",
    "ReportRole",
    "RoleReportArtifact",
    "RoleReportBundle",
]
