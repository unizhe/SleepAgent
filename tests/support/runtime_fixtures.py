"""Canonical backend process test helpers."""

import sleepagent.process as backend_runtime_module


def reset_backend_runtime_state() -> None:
    """在隔离测试之间重置进程单例防护。"""

    with backend_runtime_module._ACTIVE_LOCK:
        backend_runtime_module._ACTIVE_RUNTIME_ID = None


__all__ = ["reset_backend_runtime_state"]
