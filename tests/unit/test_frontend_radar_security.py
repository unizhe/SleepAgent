from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_radar_frontend_uses_same_origin_bff_without_public_product_key() -> None:
    radar_api = (PROJECT_ROOT / "frontend" / "lib" / "radar-api.ts").read_text(
        encoding="utf-8"
    )
    route = (
        PROJECT_ROOT / "frontend" / "app" / "api" / "radar" / "[...path]" / "route.ts"
    ).read_text(encoding="utf-8")

    assert 'const RADAR_API_PROXY_BASE = "/api/radar"' in radar_api
    assert "NEXT_PUBLIC_RADAR_API_BASE_URL" not in radar_api
    assert "SLEEPAGENT_PRODUCT_RADAR_API_KEY" not in radar_api
    assert "X-API-Key" not in radar_api
    assert "SLEEPAGENT_PRODUCT_RADAR_API_KEY" in route
    assert "SLEEPAGENT_RADAR_AGENT_API_KEY" in route
    assert '"X-API-Key": apiKey' in route
    assert "RADAR_AGENT_ACTOR_ID" in route
    assert "RADAR_AGENT_ACTOR_ROLE" in route
    assert "NEXT_PUBLIC_RADAR_API_BASE_URL" not in route


def test_radar_bff_allows_task_api_and_preserves_sse_stream() -> None:
    route = (
        PROJECT_ROOT / "frontend" / "app" / "api" / "radar" / "[...path]" / "route.ts"
    ).read_text(encoding="utf-8")

    assert 'upstreamPath.startsWith("/radar-agent/tasks")' in route
    assert 'upstreamPath === "/radar-agent/chat"' in route
    assert 'request.headers.get("last-event-id")' in route
    assert "upstreamResponse.body" in route
    assert "await upstreamResponse.text()" not in route
    assert 'contentType.includes("text/event-stream")' in route
    assert '"X-Accel-Buffering": "no"' in route


def test_env_example_keeps_radar_live_key_server_side_only() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "SLEEPAGENT_PRODUCT_RADAR_API_KEY=<product-radar-api-key>" in env_example
    assert "SLEEPAGENT_RADAR_AGENT_API_KEY=<radar-agent-api-key>" in env_example
    assert (
        "SLEEPAGENT_RADAR_AGENT_ACTOR_ID=<authenticated-actor-id>"
        in env_example
    )
    assert "NEXT_PUBLIC_RADAR_LIVE_API_ENABLED=<true-or-false>" in env_example
    assert "NEXT_PUBLIC_RADAR_API_KEY" not in env_example
    assert "NEXT_PUBLIC_RADAR_API_BASE_URL" not in env_example
