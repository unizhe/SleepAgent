from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.schemas import RespiratoryEventType
from sleepagent.training import infer_respiratory_npz, infer_respiratory_window


NORMAL = RespiratoryEventType.NORMAL_BREATHING
HYPOPNEA = RespiratoryEventType.HYPOPNEA
APNEA = RespiratoryEventType.SUSPECTED_APNEA


class FixedLogitModel(torch.nn.Module):
    def __init__(
        self,
        config: RespiratoryCnnBiLstmConfig,
        logits: torch.Tensor,
    ) -> None:
        super().__init__()
        self.config = config
        self.logits = logits
        self.offset = 0

    def forward(self, batch: torch.Tensor) -> torch.Tensor:
        stop = self.offset + batch.shape[0]
        batch_logits = self.logits[self.offset:stop]
        self.offset = stop
        return batch_logits


def test_infers_single_window_prediction() -> None:
    config = _small_config()
    model = FixedLogitModel(config, torch.tensor([[0.1, 3.0, 0.2]]))
    window = np.zeros((2, 8), dtype=np.float32)

    prediction = infer_respiratory_window(model, window, start_second=30.0)

    assert prediction.predicted_label == HYPOPNEA
    assert prediction.start_second == pytest.approx(30.0)
    assert set(prediction.probabilities) == {
        "normal_breathing",
        "hypopnea",
        "suspected_apnea",
    }
    assert sum(prediction.probabilities.values()) == pytest.approx(1.0)


def test_infers_all_npz_windows_in_batches(tmp_path: Path) -> None:
    config = _small_config()
    dataset_path = _write_npz(tmp_path)
    logits = torch.tensor(
        [
            [4.0, 0.1, 0.0],
            [0.1, 3.0, 0.2],
            [0.0, 0.2, 4.0],
        ]
    )
    model = FixedLogitModel(config, logits)

    result = infer_respiratory_npz(
        model,
        dataset_path,
        batch_size=2,
    )

    assert result.source_path == dataset_path.resolve()
    assert result.class_order == (NORMAL, HYPOPNEA, APNEA)
    assert [prediction.predicted_label for prediction in result.predictions] == [
        NORMAL,
        HYPOPNEA,
        APNEA,
    ]
    assert [prediction.start_second for prediction in result.predictions] == [
        0.0,
        30.0,
        60.0,
    ]


def test_inference_restores_training_mode_after_call() -> None:
    config = _small_config()
    model = FixedLogitModel(config, torch.tensor([[0.1, 3.0, 0.2]]))
    model.train()

    infer_respiratory_window(model, np.zeros((2, 8), dtype=np.float32))

    assert model.training is True


def test_inference_rejects_invalid_inputs(tmp_path: Path) -> None:
    config = _small_config()
    model = FixedLogitModel(config, torch.tensor([[0.1, 3.0, 0.2]]))

    with pytest.raises(ValueError, match="shape"):
        infer_respiratory_window(model, np.zeros((1, 2, 8), dtype=np.float32))

    with pytest.raises(ValueError, match="channel"):
        infer_respiratory_window(model, np.zeros((3, 8), dtype=np.float32))

    with pytest.raises(ValueError, match="sample"):
        infer_respiratory_window(model, np.zeros((2, 7), dtype=np.float32))

    wrong_class_model = FixedLogitModel(config, torch.tensor([[0.1, 3.0]]))
    with pytest.raises(ValueError, match="class dimension"):
        infer_respiratory_window(wrong_class_model, np.zeros((2, 8), dtype=np.float32))

    with pytest.raises(ValueError, match="batch_size"):
        infer_respiratory_npz(model, _write_npz(tmp_path), batch_size=0)


def _small_config() -> RespiratoryCnnBiLstmConfig:
    return RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=2.0,
    )


def _write_npz(tmp_path: Path) -> Path:
    dataset_path = tmp_path / "resp_windows.npz"
    np.savez_compressed(
        dataset_path,
        x=np.zeros((3, 2, 8), dtype=np.float32),
        y=np.asarray([0, 1, 2], dtype=np.int64),
        start_seconds=np.asarray([0.0, 30.0, 60.0], dtype=np.float64),
        included_mask=np.ones(3, dtype=np.bool_),
        class_order=np.asarray(
            ["normal_breathing", "hypopnea", "suspected_apnea"],
            dtype="U32",
        ),
        channel_names=np.asarray(["THOR RES", "ABDO RES"], dtype="U64"),
    )
    return dataset_path
