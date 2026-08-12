from __future__ import annotations

from collections.abc import Callable

import pytest

from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.stage2_worker import (
    Stage2InvariantError,
    build_stage2_worker_handlers,
)
from sleepagent.stage4_worker import (
    DELIVERY_QUEUE,
    Stage4Error,
    build_stage4_worker_handlers,
)
from sleepagent.worker_runtime import WorkHandler


pytestmark = pytest.mark.unit


Builder = Callable[[SleepBackendSettings], dict[str, WorkHandler]]


def _settings(
    *,
    model_mode: ModelMode,
    data_mode: DataMode = DataMode.REPLAY,
) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="test-live-model-stage-gates",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=data_mode,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=data_mode,
        namespace_prefixes=(f"{data_mode.value}:",),
        worker_queues=(
            "sleep_command",
            "product_interaction",
            "induction",
            DELIVERY_QUEUE,
            "reconciliation",
        ),
        provider_mode=(
            ProviderMode.LIVE
            if model_mode == ModelMode.LIVE
            else ProviderMode.FAKE
        ),
        model_mode=model_mode,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )


@pytest.mark.parametrize(
    ("builder", "expected_queues"),
    [
        (
            build_stage2_worker_handlers,
            {"sleep_command", "product_interaction"},
        ),
        (
            build_stage4_worker_handlers,
            {"induction", DELIVERY_QUEUE, "reconciliation"},
        ),
    ],
)
def test_replay_stage_handlers_accept_live_product_model_mode(
    builder: Builder,
    expected_queues: set[str],
) -> None:
    handlers = builder(_settings(model_mode=ModelMode.LIVE))

    assert set(handlers) == expected_queues


@pytest.mark.parametrize(
    ("builder", "error_type"),
    [
        (build_stage2_worker_handlers, Stage2InvariantError),
        (build_stage4_worker_handlers, Stage4Error),
    ],
)
def test_replay_stage_handlers_still_reject_disabled_model_mode(
    builder: Builder,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="enabled model mode"):
        builder(_settings(model_mode=ModelMode.DISABLED))


@pytest.mark.parametrize(
    ("builder", "error_type"),
    [
        (build_stage2_worker_handlers, Stage2InvariantError),
        (build_stage4_worker_handlers, Stage4Error),
    ],
)
def test_stage_handlers_still_reject_non_replay_data_mode(
    builder: Builder,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="replay data"):
        builder(
            _settings(
                model_mode=ModelMode.LIVE,
                data_mode=DataMode.LIVE,
            )
        )
