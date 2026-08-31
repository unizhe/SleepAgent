"""Neutral reporting facts shared by deterministic data and runtime tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.runtime.policies import TrendSignalLevel


class TrendRiskSignal(BaseModel):
    """Accepted, source-bound trend classification without Agent prose."""

    model_config = ConfigDict(extra="forbid")

    risk_level: TrendSignalLevel
    confidence: float = Field(default=0, ge=0, le=1)
    source_refs: tuple[str, ...] = ()


__all__ = ["TrendRiskSignal"]

