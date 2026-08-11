from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sleepagent.sleep_api.auth import AuthoritativeRoleBinding
from sleepagent.sleep_api.contracts import PublicActorRole
from sleepagent.sleep_api.service import SleepApiOperationWorker

from reference_client import (
    Ed25519ActorSigner,
    FileEventStateStore,
    HttpResponse,
    SleepApiV1Client,
)
from tests.integration.test_sleep_api_v1 import (
    AUDIENCE,
    ISSUER,
    ROLE_SCOPES,
    SERVICE_NEW,
    _actor_id,
    _authorization_id,
    _environment,
)


class AsgiTestTransport:
    def __init__(self, client) -> None:
        self.client = client

    def request(self, *, method, url, headers, body):
        parsed = urlsplit(url)
        if method == "POST":
            assert body
        response = self.client.request(
            method,
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
            headers=dict(headers),
            content=body,
            follow_redirects=False,
        )
        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )


def _reference_client(environment, state_path: Path) -> SleepApiV1Client:
    role = PublicActorRole.ELDER
    return SleepApiV1Client(
        base_url="https://sleep-api.example",
        service_credential=SERVICE_NEW,
        signer=Ed25519ActorSigner(
            issuer=ISSUER,
            audience=AUDIENCE,
            key_id="new",
            actor_id=_actor_id(role),
            subject_id="elder-1",
            role=role.value,
            scopes=tuple(sorted(ROLE_SCOPES[role])),
            private_key=environment.private_keys["new"],
        ),
        event_state_store=FileEventStateStore(state_path),
        transport=AsgiTestTransport(TestClient(environment.app())),
        now_factory=environment.clock,
    )


def test_reference_client_is_independent_and_never_models_chat_context() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "reference_client"
        / "sleep_api_v1_client.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any("sleepagent" in ast.unparse(node).lower() for node in imports)
    lowered = source.lower()
    for forbidden in ("chat_context", "conversation_history", "full_chat"):
        assert forbidden not in lowered


def test_reference_client_commands_queries_poll_dedupe_resync_and_revocation(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "client.sqlite3", seed=True)
    environment.runtime = replace(
        environment.runtime,
        event_cursor_ttl=timedelta(minutes=1),
    )
    state_path = tmp_path / "client-state.json"
    client = _reference_client(environment, state_path)

    episodes = client.list_night_episodes()
    episode_id = episodes["items"][0]["night_episode_id"]
    assert client.get_lifecycle()["subject_id"] == "elder-1"
    assert client.get_current_risk()["night_episode_id"] == episode_id
    assert client.get_role_view(episode_id)["role"] == "elder"

    accepted = client.request_reanalysis(
        night_episode_id=episode_id,
        idempotency_key="reference-reanalysis-1",
        reason="reference client integration",
    )
    worker = SleepApiOperationWorker(environment.runtime)
    assert worker.run_once()
    operation = client.await_operation(
        accepted["operation_id"],
        timeout_seconds=1,
        interval_seconds=0.01,
    )
    assert operation["status"] in {"succeeded", "failed"}

    first = client.poll_events(limit=1)
    assert len(first) == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    first_cursor = state["subjects"]["elder-1"]["cursor"]
    second = client.poll_events(limit=1)
    assert len(second) <= 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["subjects"]["elder-1"]["cursor"] = first_cursor
    state_path.write_text(json.dumps(state), encoding="utf-8")
    restarted_client = _reference_client(environment, state_path)
    assert restarted_client.poll_events(limit=1) == ()

    environment.clock.advance(timedelta(minutes=2))
    restarted_client.poll_events(limit=1)
    refreshed = json.loads(state_path.read_text(encoding="utf-8"))
    assert refreshed["subjects"]["elder-1"]["snapshots"]["lifecycle"]

    scopes = ROLE_SCOPES[PublicActorRole.ELDER]
    environment.authority.put(
        AuthoritativeRoleBinding(
            authorization_id=_authorization_id(PublicActorRole.ELDER),
            actor_id=_actor_id(PublicActorRole.ELDER),
            subject_id="elder-1",
            role=PublicActorRole.ELDER,
            scopes=scopes,
            authorization_epoch=2,
            active=False,
        )
    )
    assert restarted_client.poll_events(limit=1) == ()
    revoked = json.loads(state_path.read_text(encoding="utf-8"))
    subject_state = revoked["subjects"]["elder-1"]
    assert subject_state["snapshots"] == {}
    assert subject_state["event_ids"] == []
    assert subject_state["revocation"]["remote_recall_possible"] is False
