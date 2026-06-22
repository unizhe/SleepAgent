"""Stage 6 respiratory model inference helpers."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig
from sleepagent.schemas import RespiratoryEventType
from sleepagent.training.respiratory_dataset import load_respiratory_npz_arrays


@dataclass(frozen=True)
class RespiratoryPrediction:
    """One decoded respiratory model prediction."""

    predicted_label: RespiratoryEventType
    probabilities: dict[str, float]
    start_second: float | None = None


@dataclass(frozen=True)
class RespiratoryInferenceResult:
    """Predictions from one respiratory inference call."""

    predictions: tuple[RespiratoryPrediction, ...]
    class_order: tuple[RespiratoryEventType, ...]
    source_path: Path | None = None


def infer_respiratory_window(
    model: Any,
    window: Any,
    *,
    config: RespiratoryCnnBiLstmConfig | None = None,
    start_second: float | None = None,
) -> RespiratoryPrediction:
    """Run inference on one channel-first respiratory window."""
    torch = _import_torch()
    model_config = _resolve_config(model, config=config)
    window_tensor = torch.as_tensor(window, dtype=torch.float32)
    if window_tensor.ndim != 2:
        raise ValueError("window must have shape (input_channels, samples).")
    _validate_window_shape(window_tensor.shape, config=model_config)

    logits = _run_model_logits(model, window_tensor.unsqueeze(0), torch=torch)
    predictions = _decode_logits(
        logits,
        class_order=model_config.class_order,
        torch=torch,
        start_seconds=(start_second,),
    )
    return predictions[0]


def infer_respiratory_npz(
    model: Any,
    dataset_path: str | Path,
    *,
    config: RespiratoryCnnBiLstmConfig | None = None,
    batch_size: int = 32,
) -> RespiratoryInferenceResult:
    """Run inference over every window in a Stage 5 respiratory NPZ dataset."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0.")

    torch = _import_torch()
    model_config = _resolve_config(model, config=config)
    arrays = load_respiratory_npz_arrays(dataset_path, config=model_config)
    predictions: list[RespiratoryPrediction] = []

    for start_index in range(0, arrays.window_count, batch_size):
        stop_index = min(start_index + batch_size, arrays.window_count)
        batch = torch.as_tensor(arrays.x[start_index:stop_index], dtype=torch.float32)
        logits = _run_model_logits(model, batch, torch=torch)
        start_seconds = (
            arrays.start_seconds[start_index:stop_index].tolist()
            if arrays.start_seconds is not None
            else None
        )
        predictions.extend(
            _decode_logits(
                logits,
                class_order=model_config.class_order,
                torch=torch,
                start_seconds=start_seconds,
            )
        )

    return RespiratoryInferenceResult(
        predictions=tuple(predictions),
        class_order=model_config.class_order,
        source_path=arrays.path,
    )


def _run_model_logits(model: Any, batch: Any, *, torch: Any) -> Any:
    was_training = bool(getattr(model, "training", False))
    eval_method = getattr(model, "eval", None)
    train_method = getattr(model, "train", None)
    if callable(eval_method):
        eval_method()
    try:
        with torch.no_grad():
            logits = model(batch)
    finally:
        if was_training and callable(train_method):
            train_method()
    if logits.ndim != 2:
        raise ValueError("model output logits must have shape (examples, classes).")
    return logits


def _decode_logits(
    logits: Any,
    *,
    class_order: Sequence[RespiratoryEventType],
    torch: Any,
    start_seconds: Sequence[float | None] | None = None,
) -> list[RespiratoryPrediction]:
    probabilities = torch.softmax(logits, dim=1)
    if probabilities.shape[1] != len(class_order):
        raise ValueError("model output class dimension must match class_order length.")

    predicted_indices = torch.argmax(probabilities, dim=1).detach().cpu().tolist()
    probability_rows = probabilities.detach().cpu().tolist()
    resolved_start_seconds = (
        tuple(start_seconds)
        if start_seconds is not None
        else (None,) * len(predicted_indices)
    )
    if len(resolved_start_seconds) != len(predicted_indices):
        raise ValueError("start_seconds must match prediction count.")
    return [
        RespiratoryPrediction(
            predicted_label=class_order[int(predicted_index)],
            probabilities={
                label.value: float(probability)
                for label, probability in zip(class_order, row, strict=True)
            },
            start_second=(
                None
                if start_second is None
                else float(start_second)
            ),
        )
        for predicted_index, row, start_second in zip(
            predicted_indices,
            probability_rows,
            resolved_start_seconds,
            strict=True,
        )
    ]


def _resolve_config(
    model: Any,
    *,
    config: RespiratoryCnnBiLstmConfig | None,
) -> RespiratoryCnnBiLstmConfig:
    if config is not None:
        return config
    model_config = getattr(model, "config", None)
    if isinstance(model_config, RespiratoryCnnBiLstmConfig):
        return model_config
    return RespiratoryCnnBiLstmConfig()


def _validate_window_shape(
    shape: Sequence[int],
    *,
    config: RespiratoryCnnBiLstmConfig,
) -> None:
    if int(shape[0]) != config.input_channels:
        raise ValueError(
            "window channel count does not match "
            f"config.input_channels={config.input_channels}."
        )
    if int(shape[1]) != config.expected_input_samples:
        raise ValueError(
            "window sample count does not match "
            f"config.expected_input_samples={config.expected_input_samples}."
        )


def _import_torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for respiratory inference. "
            'Install the model extra with: python -m pip install -e ".[model]"'
        ) from exc
