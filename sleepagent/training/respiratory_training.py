"""Minimal Stage 6 respiratory model training smoke loop."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig
from sleepagent.training.respiratory_dataset import RespiratoryNpzTorchDataset


@dataclass(frozen=True)
class RespiratoryTrainingSmokeResult:
    """Summary from one small respiratory training smoke run."""

    dataset_path: Path
    epoch_count: int
    batch_count: int
    example_count: int
    initial_loss: float
    final_loss: float
    mean_loss: float
    class_counts: dict[str, int]


def train_respiratory_single_epoch_smoke(
    dataset_path: str | Path,
    *,
    model: Any | None = None,
    config: RespiratoryCnnBiLstmConfig | None = None,
    batch_size: int = 8,
    learning_rate: float = 1e-3,
    max_batches: int | None = None,
    seed: int = 7,
) -> RespiratoryTrainingSmokeResult:
    """Run one tiny supervised training epoch for the respiratory model."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0.")
    if learning_rate <= 0:
        raise ValueError("learning_rate must be greater than 0.")
    if max_batches is not None and max_batches <= 0:
        raise ValueError("max_batches must be greater than 0 when provided.")

    torch = _import_torch()
    data = _import_torch_data()

    torch.manual_seed(seed)
    model_config = config or RespiratoryCnnBiLstmConfig()
    dataset = RespiratoryNpzTorchDataset(dataset_path, config=model_config)
    data_loader = data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
    )
    if model is None:
        RespiratoryCnnBiLstm = _import_respiratory_model_class()
        model = RespiratoryCnnBiLstm(model_config)
    model.train()
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    criterion = torch.nn.CrossEntropyLoss()

    losses: list[float] = []
    example_count = 0
    for batch_index, (windows, labels) in enumerate(data_loader):
        if max_batches is not None and batch_index >= max_batches:
            break

        optimizer.zero_grad()
        logits = model(windows)
        loss = criterion(logits, labels)
        loss.backward()
        optimizer.step()

        loss_value = float(loss.detach().cpu().item())
        if not math.isfinite(loss_value):
            raise RuntimeError(f"Training loss is not finite: {loss_value!r}.")
        losses.append(loss_value)
        example_count += int(labels.shape[0])

    if not losses:
        raise RuntimeError("Training smoke loop did not process any batches.")

    return RespiratoryTrainingSmokeResult(
        dataset_path=dataset.arrays.path,
        epoch_count=1,
        batch_count=len(losses),
        example_count=example_count,
        initial_loss=losses[0],
        final_loss=losses[-1],
        mean_loss=sum(losses) / len(losses),
        class_counts={
            label.value: count
            for label, count in dataset.class_counts.items()
        },
    )


def _import_torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for respiratory training. "
            'Install the model extra with: python -m pip install -e ".[model]"'
        ) from exc


def _import_torch_data() -> Any:
    try:
        return importlib.import_module("torch.utils.data")
    except ImportError as exc:
        raise RuntimeError("Could not import torch.utils.data.") from exc


def _import_respiratory_model_class() -> Any:
    try:
        module = importlib.import_module("sleepagent.models.respiratory_cnn_bilstm")
    except ImportError as exc:
        raise RuntimeError("Could not import the respiratory PyTorch model.") from exc
    return module.RespiratoryCnnBiLstm
