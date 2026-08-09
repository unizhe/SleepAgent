from __future__ import annotations

import subprocess
import sys

import pytest

from sleepagent.demo_cli import DemoCliError, verify_backend


pytestmark = pytest.mark.unit


class Client:
    def __init__(self, *, watermarked: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.watermarked = watermarked

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        watermark = {
            "data_mode": "replay" if self.watermarked else "live",
            "synthetic_non_release": self.watermarked,
        }
        if path == "/livez":
            return {"status": "alive"}
        if path == "/demo/v1/clock":
            return {**watermark, "generation": 1}
        if path == "/demo/v1/seed":
            return {**watermark, "operation_id": "operation-1", "generation": 1}
        if path == "/demo/v1/trace":
            return {
                **watermark,
                "generation": 1,
                "entries": [
                    {
                        "operation_id": "operation-1",
                        "state": "succeeded",
                    }
                ],
            }
        raise AssertionError(path)


def test_verifier_uses_only_http_surface_and_replay_watermarks() -> None:
    client = Client()

    result = verify_backend(
        client,  # type: ignore[arg-type]
        scenario_id="golden-15-night",
        model="deterministic",
        wait_seconds=0.1,
    )

    assert result["verified"] is True
    assert result["synthetic_non_release"] is True
    assert [path for _, path, _ in client.calls] == [
        "/livez",
        "/demo/v1/clock",
        "/demo/v1/seed",
        "/demo/v1/trace",
    ]
    seed_payload = client.calls[2][2]["payload"]
    assert set(seed_payload) == {"artifact_family", "scenario_id", "batch_size"}
    assert "expected" not in seed_payload
    assert "actions" not in seed_payload


def test_verifier_rejects_response_without_non_release_watermark() -> None:
    with pytest.raises(DemoCliError, match="watermark"):
        verify_backend(
            Client(watermarked=False),  # type: ignore[arg-type]
            scenario_id="golden-15-night",
            model="deterministic",
            wait_seconds=0.1,
        )


def test_demo_cli_help_is_available_in_clean_module_execution() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "sleepagent.demo_cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "sleepagent-demo" in completed.stdout
