from __future__ import annotations

from enum import Enum

from sleepagent.radar_agent.llm.router import (
    CloudLLMProviderError,
    CloudLLMTimeoutError,
)


class LLMFaultMode(str, Enum):
    NONE = "none"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"


class DeterministicLLMFaultInjector:
    """Deterministic fault injection for fallback and resilience tests."""

    def __init__(self, mode: LLMFaultMode | str) -> None:
        self.mode = LLMFaultMode(mode)
        self.request_attempts = 0

    def before_request(self, *, attempt: int, structured: bool) -> None:
        self.request_attempts = attempt
        if self.mode == LLMFaultMode.UNAVAILABLE:
            raise CloudLLMProviderError("fault injection: provider unavailable")
        if self.mode == LLMFaultMode.TIMEOUT:
            raise CloudLLMTimeoutError("fault injection: request timed out")

    def transform_content(self, content: str, *, structured: bool) -> str:
        if not structured:
            return content
        if self.mode == LLMFaultMode.INVALID_JSON:
            return "{fault-injected-invalid-json"
        if self.mode == LLMFaultMode.INVALID_SCHEMA:
            return "{}"
        return content


__all__ = ["DeterministicLLMFaultInjector", "LLMFaultMode"]
