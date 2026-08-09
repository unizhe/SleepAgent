from __future__ import annotations

from datetime import datetime

from sleepagent.product_device.dashboard_tool import RadarDashboardProjectionTool
from sleepagent.product_device.schemas import (
    RadarAlertEvent,
    RadarDashboardSummary,
    RadarDevice,
    RadarSleepReport,
    RadarVitalSnapshot,
)


class RadarProductAgent:
    """Retired Agent identity retained as a Phase 3B parity reference only."""

    def __init__(self, tool: RadarDashboardProjectionTool | None = None) -> None:
        self._tool = tool or RadarDashboardProjectionTool()

    def run(
        self,
        *,
        device: RadarDevice,
        recent_snapshots: list[RadarVitalSnapshot],
        latest_sleep_report: RadarSleepReport | None = None,
        recent_alerts: list[RadarAlertEvent] | None = None,
        now: datetime | None = None,
    ) -> RadarDashboardSummary:
        return self._tool.run(
            device=device,
            recent_snapshots=recent_snapshots,
            latest_sleep_report=latest_sleep_report,
            recent_alerts=recent_alerts,
            now=now,
        )

    def build_dashboard_summary(
        self,
        *,
        device: RadarDevice,
        recent_snapshots: list[RadarVitalSnapshot],
        latest_sleep_report: RadarSleepReport | None = None,
        recent_alerts: list[RadarAlertEvent] | None = None,
        now: datetime | None = None,
    ) -> RadarDashboardSummary:
        return self.run(
            device=device,
            recent_snapshots=recent_snapshots,
            latest_sleep_report=latest_sleep_report,
            recent_alerts=recent_alerts,
            now=now,
        )
