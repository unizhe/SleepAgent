from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_openapi_snapshots_match_canonical_factory() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/generate_openapi_snapshots.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_target_physically_removes_replay_oracles() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    production = dockerfile.split("FROM runtime-base AS production", 1)[1]

    assert "sleepagent/simulation/fixtures" in production
    assert "sleepagent/simulation/replay" in production
    assert "sleepagent/simulation/replay_seed_registry.json" in production
    assert "find /usr/local" not in production


def test_core_imports_survive_exact_production_replay_cleanup(tmp_path: Path) -> None:
    staged_root = tmp_path / "production-site"
    ignore_bytecode = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(
        ROOT / "sleepagent",
        staged_root / "sleepagent",
        ignore=ignore_bytecode,
    )
    shutil.copytree(
        ROOT / "backend",
        staged_root / "backend",
        ignore=ignore_bytecode,
    )

    shutil.rmtree(staged_root / "sleepagent" / "simulation" / "fixtures")
    shutil.rmtree(staged_root / "sleepagent" / "simulation" / "replay")
    (staged_root / "sleepagent" / "simulation" / "replay_seed_registry.json").unlink()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(staged_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sleepagent.retention; "
                "import sleepagent.backend_app; "
                "import sleepagent.worker_runtime"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr


def test_runtime_source_tree_has_no_verifier_golden_module() -> None:
    replay_package = ROOT / "sleepagent" / "simulation" / "replay"

    assert not (replay_package / "goldens.py").exists()
    assert not (replay_package / "workflow_goldens.json").exists()


def test_reference_clients_are_server_independent() -> None:
    sleep_client = (
        ROOT / "reference_client" / "sleep_api_v1_client.py"
    ).read_text(encoding="utf-8")
    assert "from sleepagent" not in sleep_client
    assert "import sleepagent" not in sleep_client


def test_verifier_scripts_do_not_put_credentials_on_argv() -> None:
    for name in (
        "verify_backend_first_slice.sh",
        "verify_backend_stage3.sh",
        "verify_backend_stage4.sh",
        "verify_backend_stage5.sh",
    ):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "--demo-token" not in source
        assert "--service-credential" not in source
        assert "--actor-private-key" not in source


def test_process_fault_scripts_cover_locked_kill_and_database_restart_windows() -> None:
    first_slice = (ROOT / "scripts" / "verify_backend_first_slice.sh").read_text(
        encoding="utf-8"
    )
    stage4 = (ROOT / "scripts" / "verify_backend_stage4.sh").read_text(
        encoding="utf-8"
    )
    probe = (ROOT / "scripts" / "backend_process_fault_probe.py").read_text(
        encoding="utf-8"
    )

    assert "restart-postgres-during-root" in first_slice
    assert 'restart postgres' in first_slice
    assert "wait-root-active" in first_slice
    assert "SLEEPAGENT_TEST_WORKER_QUEUES=replay_journey" in first_slice
    assert "--force-recreate --wait worker" in first_slice
    assert "wait-delivery-send-started" in stage4
    assert 'kill -s SIGKILL worker' in stage4
    assert "hold-delivery-effect-lock" in stage4
    assert "pg_advisory_lock" in probe
    assert "current_state" in probe and "send_started" in probe
