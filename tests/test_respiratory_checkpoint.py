from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.models.respiratory_cnn_bilstm import RespiratoryCnnBiLstm
from sleepagent.training import (
    RESPIRATORY_CHECKPOINT_SCHEMA_VERSION,
    infer_respiratory_window,
    load_respiratory_checkpoint,
    save_respiratory_checkpoint,
)


def test_saves_and_loads_respiratory_checkpoint_for_inference(tmp_path: Path) -> None:
    torch.manual_seed(7)
    config = RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=8.0,
        cnn_channels=(4,),
        lstm_hidden_size=4,
    )
    model = RespiratoryCnnBiLstm(config)
    window = torch.zeros((2, 32), dtype=torch.float32)
    before = infer_respiratory_window(model, window)

    checkpoint_path = save_respiratory_checkpoint(
        model,
        tmp_path / "resp_model.pt",
        config=config,
        metadata={"record_id": "tiny-smoke"},
    )
    checkpoint = load_respiratory_checkpoint(checkpoint_path)
    after = infer_respiratory_window(checkpoint.model, window, config=checkpoint.config)

    assert checkpoint.schema_version == RESPIRATORY_CHECKPOINT_SCHEMA_VERSION
    assert checkpoint.path == checkpoint_path
    assert checkpoint.metadata == {"record_id": "tiny-smoke"}
    assert checkpoint.config == config
    assert after.predicted_label == before.predicted_label
    assert after.probabilities == pytest.approx(before.probabilities)


def test_checkpoint_rejects_invalid_suffix(tmp_path: Path) -> None:
    config = RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=8.0,
        cnn_channels=(4,),
        lstm_hidden_size=4,
    )
    model = RespiratoryCnnBiLstm(config)

    with pytest.raises(ValueError, match=".pt or .pth"):
        save_respiratory_checkpoint(model, tmp_path / "resp_model.bin", config=config)
