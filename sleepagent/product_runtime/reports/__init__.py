"""角色报告合同的稳定公共 façade。

``rendering`` 是内部实现，刻意不从此处公开导出。
"""

from sleepagent.product_runtime.reports.roles import (
    REPORT_ROLE_ORDER,
    ReportRole,
    RoleReportArtifact,
    RoleReportBundle,
)

__all__ = [
    "REPORT_ROLE_ORDER",
    "ReportRole",
    "RoleReportArtifact",
    "RoleReportBundle",
]
