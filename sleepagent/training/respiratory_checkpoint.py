"""Stage 6 respiratory model checkpoint helpers."""

from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sleepagent.metrics.respiratory import map_respiratory_event_type
from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig
from sleepagent.schemas import RespiratoryEventType


RESPIRATORY_CHECKPOINT_SCHEMA_VERSION = "stage6.respiratory_checkpoint.v1"


@dataclass(frozen=True)
class RespiratoryCheckpoint:
    """Loaded respiratory checkpoint metadata and model."""

    path: Path
    model: Any
    config: RespiratoryCnnBiLstmConfig
    metadata: dict[str, Any]
    schema_version: str


def save_respiratory_checkpoint(
    model: Any,
    path: str | Path,
    *,
    config: RespiratoryCnnBiLstmConfig | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Save a respiratory model state_dict with its config and metadata."""
    torch = _import_torch()
    model_config = _resolve_config(model, config=config)
    resolved_path = Path(path).expanduser().resolve()
    if resolved_path.suffix.lower() not in {".pt", ".pth"}:
        raise ValueError("Respiratory checkpoint path must end with .pt or .pth.")
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": RESPIRATORY_CHECKPOINT_SCHEMA_VERSION,
        "config": _config_to_json_dict(model_config),
        "metadata": dict(metadata or {}),
        "state_dict": model.state_dict(),
    }
    torch.save(payload, resolved_path)
    return resolved_path


def load_respiratory_checkpoint(
    path: str | Path,
    *,
    map_location: str = "cpu",
) -> RespiratoryCheckpoint:
    """Load a respiratory checkpoint and rebuild the PyTorch model."""
    torch = _import_torch()
    RespiratoryCnnBiLstm = _import_respiratory_model_class()
    resolved_path = _resolve_existing_checkpoint_path(path)
    payload = torch.load(resolved_path, map_location=map_location)
    if not isinstance(payload, dict):
        raise ValueError("Respiratory checkpoint payload must be a dictionary.")
    schema_version = payload.get("schema_version")
    if schema_version != RESPIRATORY_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported respiratory checkpoint schema_version: "
            f"{schema_version!r}."
        )
    if "state_dict" not in payload:
        raise ValueError("Respiratory checkpoint is missing state_dict.")

    config = _config_from_json_dict(payload.get("config", {}))
    model = RespiratoryCnnBiLstm(config)
    model.load_state_dict(payload["state_dict"])
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Respiratory checkpoint metadata must be a dictionary.")
    return RespiratoryCheckpoint(
        path=resolved_path,
        model=model,
        config=config,
        metadata=metadata,
        schema_version=schema_version,
    )


def _config_to_json_dict(config: RespiratoryCnnBiLstmConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["cnn_channels"] = list(config.cnn_channels)
    payload["class_order"] = [label.value for label in config.class_order]
    return payload


def _config_from_json_dict(payload: Any) -> RespiratoryCnnBiLstmConfig:
    if not isinstance(payload, dict):
        raise ValueError("Respiratory checkpoint config must be a dictionary.")
    config_kwargs = dict(payload)
    if "cnn_channels" in config_kwargs:
        config_kwargs["cnn_channels"] = tuple(int(value) for value in config_kwargs["cnn_channels"])
    if "class_order" in config_kwargs:
        config_kwargs["class_order"] = tuple(
            _map_checkpoint_class_label(value)
            for value in config_kwargs["class_order"]
        )
    return RespiratoryCnnBiLstmConfig(**config_kwargs)


def _map_checkpoint_class_label(value: Any) -> RespiratoryEventType:
    try:
        return map_respiratory_event_type(value)
    except ValueError as exc:
        raise ValueError(f"Invalid checkpoint class label {value!r}.") from exc


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


def _resolve_existing_checkpoint_path(path: str | Path) -> Path:
    resolved_path = Path(path).expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Respiratory checkpoint does not exist: {resolved_path}")
    if not resolved_path.is_file():
        raise ValueError(f"Respiratory checkpoint path is not a file: {resolved_path}")
    if resolved_path.suffix.lower() not in {".pt", ".pth"}:
        raise ValueError("Respiratory checkpoint path must end with .pt or .pth.")
    return resolved_path


def _import_torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for respiratory checkpoints. "
            'Install the model extra with: python -m pip install -e ".[model]"'
        ) from exc


def _import_respiratory_model_class() -> Any:
    try:
        module = importlib.import_module("sleepagent.models.respiratory_cnn_bilstm")
    except ImportError as exc:
        raise RuntimeError("Could not import the respiratory PyTorch model.") from exc
    return module.RespiratoryCnnBiLstm
