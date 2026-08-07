from __future__ import annotations

import io

from sleepagent.radar_agent.dynamic.acceptance import EXIT_UNAVAILABLE, main


def test_real_model_acceptance_never_fakes_success_without_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SLEEPAGENT_RADAR_AGENT_LLM_API_KEY", raising=False)
    stdout = io.StringIO()

    exit_code = main([], stdout=stdout)

    assert exit_code == EXIT_UNAVAILABLE
    assert "no intelligent-mode claim was made" in stdout.getvalue()
