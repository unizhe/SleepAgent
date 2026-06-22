from pathlib import Path

import numpy as np
import pytest

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.schemas import RespiratoryEventType
from sleepagent.training import (
    RespiratoryNpzTorchDataset,
    load_respiratory_npz_arrays,
)


NORMAL = RespiratoryEventType.NORMAL_BREATHING
HYPOPNEA = RespiratoryEventType.HYPOPNEA
APNEA = RespiratoryEventType.SUSPECTED_APNEA


def test_loads_stage5_npz_arrays_for_model_contract(tmp_path: Path) -> None:
    dataset_path = _write_npz(tmp_path)
    config = _small_config()

    arrays = load_respiratory_npz_arrays(dataset_path, config=config)

    assert arrays.path == dataset_path.resolve()
    assert arrays.window_count == 3
    assert arrays.input_channels == 2
    assert arrays.samples_per_window == 8
    assert arrays.x.dtype == np.float32
    assert arrays.y.dtype == np.int64
    assert arrays.class_order == (NORMAL, HYPOPNEA, APNEA)
    assert arrays.channel_names == ("THOR RES", "ABDO RES")
    assert arrays.class_counts == {NORMAL: 1, HYPOPNEA: 1, APNEA: 1}
    assert arrays.label_for_index(2) == APNEA


def test_torch_dataset_returns_one_window_and_label(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    dataset_path = _write_npz(
        tmp_path,
        x=np.arange(3 * 2 * 8, dtype=np.float64).reshape(3, 2, 8),
        y=np.asarray([0, 1, 2], dtype=np.int32),
    )

    dataset = RespiratoryNpzTorchDataset(dataset_path, config=_small_config())
    window, label = dataset[1]

    assert len(dataset) == 3
    assert window.shape == (2, 8)
    assert window.dtype == torch.float32
    assert label.dtype == torch.long
    assert label.item() == 1
    assert dataset.class_counts == {NORMAL: 1, HYPOPNEA: 1, APNEA: 1}


def test_loader_rejects_xy_length_mismatch(tmp_path: Path) -> None:
    dataset_path = _write_npz(tmp_path, y=np.asarray([0, 1], dtype=np.int64))

    with pytest.raises(ValueError, match="same window count"):
        load_respiratory_npz_arrays(dataset_path, config=_small_config())


def test_loader_rejects_start_seconds_length_mismatch(tmp_path: Path) -> None:
    dataset_path = _write_npz(
        tmp_path,
        start_seconds=np.asarray([0.0, 30.0], dtype=np.float64),
    )

    with pytest.raises(ValueError, match="start_seconds"):
        load_respiratory_npz_arrays(dataset_path, config=_small_config())


def test_loader_rejects_included_mask_length_mismatch(tmp_path: Path) -> None:
    dataset_path = _write_npz(
        tmp_path,
        included_mask=np.asarray([True, True], dtype=np.bool_),
    )

    with pytest.raises(ValueError, match="included_mask"):
        load_respiratory_npz_arrays(dataset_path, config=_small_config())


def test_loader_rejects_class_order_mismatch(tmp_path: Path) -> None:
    dataset_path = _write_npz(
        tmp_path,
        class_order=np.asarray(
            ["normal_breathing", "suspected_apnea", "hypopnea"],
            dtype="U32",
        ),
    )

    with pytest.raises(ValueError, match="class_order"):
        load_respiratory_npz_arrays(dataset_path, config=_small_config())


def test_loader_rejects_invalid_label_index(tmp_path: Path) -> None:
    dataset_path = _write_npz(tmp_path, y=np.asarray([0, 1, 3], dtype=np.int64))

    with pytest.raises(ValueError, match="valid class_order indices"):
        load_respiratory_npz_arrays(dataset_path, config=_small_config())


def _small_config() -> RespiratoryCnnBiLstmConfig:
    return RespiratoryCnnBiLstmConfig(
        window_duration_seconds=4.0,
        sampling_rate_hz=2.0,
    )


def _write_npz(
    tmp_path: Path,
    *,
    x: np.ndarray | None = None,
    y: np.ndarray | None = None,
    start_seconds: np.ndarray | None = None,
    included_mask: np.ndarray | None = None,
    class_order: np.ndarray | None = None,
) -> Path:
    dataset_path = tmp_path / "resp_windows.npz"
    np.savez_compressed(
        dataset_path,
        x=x
        if x is not None
        else np.arange(3 * 2 * 8, dtype=np.float32).reshape(3, 2, 8),
        y=y if y is not None else np.asarray([0, 1, 2], dtype=np.int64),
        start_seconds=start_seconds
        if start_seconds is not None
        else np.asarray([0.0, 30.0, 60.0], dtype=np.float64),
        included_mask=included_mask
        if included_mask is not None
        else np.asarray([True, True, True], dtype=np.bool_),
        class_order=class_order
        if class_order is not None
        else np.asarray(
            ["normal_breathing", "hypopnea", "suspected_apnea"],
            dtype="U32",
        ),
        channel_names=np.asarray(["THOR RES", "ABDO RES"], dtype="U64"),
    )
    return dataset_path
