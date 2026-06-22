import subprocess
import sys
from pathlib import Path


def test_stage6_smoke_script_help_runs_without_model_extra() -> None:
    script_path = Path("scripts/run_respiratory_stage6_smoke.py")

    completed = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Stage 6 respiratory" in completed.stdout
    assert "--dataset-path" in completed.stdout
