from __future__ import annotations

import io

import pytest

from sleepagent.radar_agent.dynamic.acceptance import (
    EXIT_UNAVAILABLE,
    main,
    run_real_model_acceptance,
)


def test_dynamic_acceptance_entrypoint_is_retired() -> None:
    stdout = io.StringIO()

    exit_code = main([], stdout=stdout)

    assert exit_code == EXIT_UNAVAILABLE
    assert "dynamic Agent acceptance runtime is retired" in stdout.getvalue()
    with pytest.raises(RuntimeError, match="dynamic Agent execution is retired"):
        run_real_model_acceptance()
