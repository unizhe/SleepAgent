"""Stage 6 NPZ dataset loading for respiratory model training."""

from __future__ import annotations

import importlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleepagent.metrics.respiratory import map_respiratory_event_type
from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig
from sleepagent.schemas import RespiratoryEventType


REQUIRED_RESPIRATORY_NPZ_ARRAYS = ("x", "y", "class_order")


@dataclass(frozen=True)
class RespiratoryNpzArrays:
    """Validated Stage 5 respiratory NPZ arrays for Stage 6 model code."""

    path: Path
    x: Any
    y: Any
    class_order: tuple[RespiratoryEventType, ...]
    channel_names: tuple[str, ...]
    start_seconds: Any | None = None
    included_mask: Any | None = None

    @property
    def window_count(self) -> int:
        return int(self.x.shape[0])

    @property
    def input_channels(self) -> int:
        return int(self.x.shape[1])

    @property
    def samples_per_window(self) -> int:
        return int(self.x.shape[2])

    @property
    def class_counts(self) -> dict[RespiratoryEventType, int]:
        counts = Counter(
            self.label_for_index(int(label_index))
            for label_index in self.y
        )
        return {
            label: counts[label]
            for label in self.class_order
            if counts[label] > 0
        }

    def label_for_index(self, label_index: int) -> RespiratoryEventType:
        try:
            return self.class_order[label_index]
        except IndexError as exc:
            raise ValueError(f"Label index {label_index} is outside class_order.") from exc


class RespiratoryNpzTorchDataset:
    """Torch Dataset-compatible wrapper around a Stage 5 respiratory NPZ file.

    The class avoids importing PyTorch at module import time so the rest of the
    project stays usable without the optional ``sleepagent[model]`` extra.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        config: RespiratoryCnnBiLstmConfig | None = None,
    ) -> None:
        self.config = config or RespiratoryCnnBiLstmConfig()
        self.arrays = load_respiratory_npz_arrays(path, config=self.config)

    def __len__(self) -> int:
        return self.arrays.window_count

    def __getitem__(self, index: int) -> tuple[Any, Any]:
        if index < 0:
            index = len(self) + index
        if index < 0 or index >= len(self):
            raise IndexError(index)

        torch = _import_torch()
        window = torch.as_tensor(self.arrays.x[index], dtype=torch.float32)
        label = torch.as_tensor(int(self.arrays.y[index]), dtype=torch.long)
        return window, label

    @property
    def class_order(self) -> tuple[RespiratoryEventType, ...]:
        return self.arrays.class_order

    @property
    def class_counts(self) -> dict[RespiratoryEventType, int]:
        return self.arrays.class_counts


def load_respiratory_npz_arrays(
    path: str | Path,
    *,
    config: RespiratoryCnnBiLstmConfig | None = None,
) -> RespiratoryNpzArrays:
    """Load and validate a Stage 5 respiratory NPZ dataset."""
    np = _import_numpy()
    resolved_path = _resolve_existing_npz_path(path)
    model_config = config or RespiratoryCnnBiLstmConfig()

    with np.load(resolved_path, allow_pickle=False) as dataset:
        _validate_required_arrays(dataset.files)
        x = np.asarray(dataset["x"], dtype=np.float32)
        y = np.asarray(dataset["y"], dtype=np.int64)
        class_order = _decode_class_order(dataset["class_order"])
        channel_names = (
            _decode_string_array(dataset["channel_names"])
            if "channel_names" in dataset
            else ()
        )
        start_seconds = (
            np.asarray(dataset["start_seconds"], dtype=np.float64)
            if "start_seconds" in dataset
            else None
        )
        included_mask = (
            np.asarray(dataset["included_mask"], dtype=np.bool_)
            if "included_mask" in dataset
            else None
        )

    arrays = RespiratoryNpzArrays(
        path=resolved_path,
        x=x,
        y=y,
        class_order=class_order,
        channel_names=channel_names,
        start_seconds=start_seconds,
        included_mask=included_mask,
    )
    _validate_arrays(arrays, config=model_config)
    return arrays


def _validate_required_arrays(files: list[str]) -> None:
    missing = [
        array_name
        for array_name in REQUIRED_RESPIRATORY_NPZ_ARRAYS
        if array_name not in files
    ]
    if missing:
        raise ValueError(f"Respiratory NPZ dataset is missing arrays: {missing}.")


def _validate_arrays(
    arrays: RespiratoryNpzArrays,
    *,
    config: RespiratoryCnnBiLstmConfig,
) -> None:
    if arrays.x.ndim != 3:
        raise ValueError(
            "Respiratory NPZ array 'x' must have shape "
            "(windows, channels, samples)."
        )
    if arrays.y.ndim != 1:
        raise ValueError("Respiratory NPZ array 'y' must have shape (windows,).")
    if arrays.window_count == 0:
        raise ValueError("Respiratory NPZ dataset cannot be empty.")
    if arrays.y.shape[0] != arrays.window_count:
        raise ValueError(
            "Respiratory NPZ arrays 'x' and 'y' must have the same window count."
        )
    if arrays.input_channels != config.input_channels:
        raise ValueError(
            "Respiratory NPZ channel count does not match "
            f"config.input_channels={config.input_channels}."
        )
    if arrays.samples_per_window != config.expected_input_samples:
        raise ValueError(
            "Respiratory NPZ sample count does not match "
            f"config.expected_input_samples={config.expected_input_samples}."
        )
    if arrays.class_order != config.class_order:
        raise ValueError("Respiratory NPZ class_order does not match the model config.")
    if (
        arrays.start_seconds is not None
        and arrays.start_seconds.shape[0] != arrays.window_count
    ):
        raise ValueError("Respiratory NPZ array 'start_seconds' must match window count.")
    if (
        arrays.included_mask is not None
        and arrays.included_mask.shape[0] != arrays.window_count
    ):
        raise ValueError("Respiratory NPZ array 'included_mask' must match window count.")

    min_label = int(arrays.y.min())
    max_label = int(arrays.y.max())
    if min_label < 0 or max_label >= len(arrays.class_order):
        raise ValueError("Respiratory NPZ labels must be valid class_order indices.")


def _decode_class_order(values: Any) -> tuple[RespiratoryEventType, ...]:
    class_order = tuple(
        map_respiratory_event_type(str(value))
        for value in values.tolist()
    )
    if not class_order:
        raise ValueError("Respiratory NPZ class_order cannot be empty.")
    if len(set(class_order)) != len(class_order):
        raise ValueError("Respiratory NPZ class_order cannot contain duplicates.")
    return class_order


def _decode_string_array(values: Any) -> tuple[str, ...]:
    return tuple(str(value) for value in values.tolist())


def _resolve_existing_npz_path(path: str | Path) -> Path:
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"Respiratory NPZ dataset does not exist: {resolved_path}"
        )
    if not resolved_path.is_file():
        raise ValueError(f"Respiratory NPZ path is not a file: {resolved_path}")
    if resolved_path.suffix.lower() != ".npz":
        raise ValueError(f"Expected a .npz respiratory dataset, got: {resolved_path}")
    return resolved_path


def _import_numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required to load Stage 5 respiratory NPZ datasets."
        ) from exc


def _import_torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required to read items from RespiratoryNpzTorchDataset. "
            'Install the model extra with: python -m pip install -e ".[model]"'
        ) from exc
