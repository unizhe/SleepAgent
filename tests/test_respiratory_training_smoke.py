from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.training import train_respiratory_single_epoch_smoke


def test_single_epoch_training_smoke_processes_batches(tmp_path: Path) -> None:
    dataset_path = _write_npz(tmp_path)
    config = RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=8.0,
        cnn_channels=(4,),
        lstm_hidden_size=4,
    )

    result = train_respiratory_single_epoch_smoke(
        dataset_path,
        config=config,
        batch_size=2,
        learning_rate=1e-3,
        max_batches=2,
        seed=11,
    )

    assert result.dataset_path == dataset_path.resolve()
    assert result.epoch_count == 1
    assert result.batch_count == 2
    assert result.example_count == 4
    assert result.initial_loss >= 0.0
    assert result.final_loss >= 0.0
    assert result.mean_loss >= 0.0
    assert result.class_counts == {
        "normal_breathing": 2,
        "hypopnea": 2,
        "suspected_apnea": 2,
    }


def test_single_epoch_training_smoke_rejects_invalid_args(tmp_path: Path) -> None:
    dataset_path = _write_npz(tmp_path)
    config = RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=8.0,
        cnn_channels=(4,),
        lstm_hidden_size=4,
    )

    with pytest.raises(ValueError, match="batch_size"):
        train_respiratory_single_epoch_smoke(
            dataset_path,
            config=config,
            batch_size=0,
        )

    with pytest.raises(ValueError, match="learning_rate"):
        train_respiratory_single_epoch_smoke(
            dataset_path,
            config=config,
            learning_rate=0.0,
        )


def _write_npz(tmp_path: Path) -> Path:
    dataset_path = tmp_path / "resp_windows.npz"
    rng = np.random.default_rng(7)
    np.savez_compressed(
        dataset_path,
        x=rng.normal(size=(6, 2, 32)).astype("float32"),
        y=np.asarray([0, 1, 2, 0, 1, 2], dtype=np.int64),
        start_seconds=np.arange(6, dtype=np.float64) * 30.0,
        included_mask=np.ones(6, dtype=np.bool_),
        class_order=np.asarray(
            ["normal_breathing", "hypopnea", "suspected_apnea"],
            dtype="U32",
        ),
        channel_names=np.asarray(["THOR RES", "ABDO RES"], dtype="U64"),
    )
    return dataset_path
