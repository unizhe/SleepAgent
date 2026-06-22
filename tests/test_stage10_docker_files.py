from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_defines_real_shhs_experiment_extra() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "experiments = [" in pyproject
    experiments = pyproject.split("experiments = [", maxsplit=1)[1].split(
        "]", maxsplit=1
    )[0]
    for requirement in [
        "numpy>=1.26,<2",
        "pandas>=1.5,<3",
        "scipy>=1.10,<1.14",
        "scikit-learn>=1.2,<1.6",
        "torch>=2.2,<2.3",
        "mne>=1.6,<1.10",
        "yasa>=0.6.5,<0.7",
        "pyEDFlib>=0.1.36,<0.2",
    ]:
        assert f'"{requirement}"' in experiments


def test_server_demo_uses_single_sleepagent_exp_conda_environment() -> None:
    demo = (PROJECT_ROOT / "docs" / "STAGE10_SHHS_DEMO.md").read_text(
        encoding="utf-8"
    )

    assert "conda create --name sleepagent-exp --clone stress" in demo
    assert "conda activate sleepagent-exp" in demo
    assert 'python -m pip install -e ".[postgres]"' in demo
    assert "python -m pip install mne yasa pyedflib" in demo
    assert 'modules = ["sleepagent.models", "torch", "mne", "yasa", "pyedflib"]' in demo
    assert '"numpy": numpy.__version__' in demo
    assert '"torch": torch.__version__' in demo
    assert '"sklearn": sklearn.__version__' in demo
    assert '"yasa": yasa.__version__' in demo


def test_dockerfile_defines_sleepagent_backend_runtime() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "FROM python:3.11-slim" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "COPY backend ./backend" in dockerfile
    assert "COPY sleepagent ./sleepagent" in dockerfile
    assert "ARG SLEEPAGENT_INSTALL_EXTRAS=postgres" in dockerfile
    assert 'python -m pip install ".[${SLEEPAGENT_INSTALL_EXTRAS}]"' in dockerfile
    assert 'CMD ["uvicorn", "backend.main:app"' in dockerfile
    assert "EXPOSE 18000" in dockerfile
    assert "8501" not in dockerfile


def test_frontend_dockerfile_defines_nextjs_runtime() -> None:
    dockerfile = (PROJECT_ROOT / "docker" / "frontend.Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "FROM node:20-slim" in dockerfile
    assert "WORKDIR /app/frontend" in dockerfile
    assert "COPY frontend/package.json frontend/package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL" in dockerfile
    assert "NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE" in dockerfile
    assert "EXPOSE 18510" in dockerfile
    assert 'CMD ["npm", "run", "start"]' in dockerfile


def test_default_compose_defines_backend_and_frontend_services() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "name: sleepagent" in compose
    assert "  backend:" in compose
    assert "dockerfile: docker/Dockerfile" in compose
    assert "- backend.main:app" in compose
    assert '- "18000:18000"' in compose
    assert "SLEEPAGENT_DATA_STORE_DIR: /tmp/sleepagent_stage9_api" in compose
    assert "sleepagent_stage9_store:/tmp/sleepagent_stage9_api" in compose
    assert "service_healthy" in compose
    assert "  frontend:" in compose
    assert "dockerfile: docker/frontend.Dockerfile" in compose
    assert "NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL: http://127.0.0.1:18000" in compose
    assert 'NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE: "false"' in compose
    assert '- "18510:18510"' in compose
    assert "streamlit" not in compose
    assert "frontend/app.py" not in compose


def test_default_compose_does_not_bind_mount_raw_shhs_data() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "../data/raw" not in compose
    assert "polysomnography" not in compose
    assert "SLEEPAGENT_SHHS_ROOT" not in compose


def test_shhs_demo_override_mounts_local_data_read_only() -> None:
    override = (PROJECT_ROOT / "compose.shhs-demo.yaml").read_text(
        encoding="utf-8"
    )

    assert "SLEEPAGENT_SHHS_ROOT: /data/shhs_sample" in override
    assert 'SLEEPAGENT_INSTALL_EXTRAS: "postgres,experiments"' in override
    assert "source: ${SLEEPAGENT_SHHS_ROOT_HOST:-../data/raw/shhs_sample}" in override
    assert "target: /data/shhs_sample" in override
    assert "read_only: true" in override
    assert "  frontend:" not in override


def test_dockerignore_excludes_local_data_and_secrets() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [
        ".env",
        "data/",
        "shhs*.zip",
        "polysomnography/",
        "*.edf",
        "*.xml",
        "*.npz",
        "*.pt",
        "models/checkpoints/",
    ]:
        assert pattern in dockerignore
