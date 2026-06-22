from fastapi.testclient import TestClient

from backend.main import app


def test_health_check_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["project"] == "SleepAgent"
    assert payload["stage"] == "project_skeleton"

