"""Run a tiny Stage 6 respiratory train/evaluate/infer smoke pipeline."""

from __future__ import annotations

import argparse
import json
import tempfile
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.training import (
    evaluate_respiratory_model_outputs,
    infer_respiratory_npz,
    load_respiratory_checkpoint,
    load_respiratory_npz_arrays,
    save_respiratory_checkpoint,
    train_respiratory_single_epoch_smoke,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a tiny Stage 6 respiratory train/evaluate/infer smoke."
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help="Optional Stage 5 NPZ dataset. Defaults to a generated tiny /tmp NPZ.",
    )
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-batches", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=None,
        help="Optional checkpoint output path. Defaults to a generated /tmp .pt file.",
    )
    args = parser.parse_args()

    torch = _import_torch()
    RespiratoryCnnBiLstm = _import_respiratory_model_class()

    dataset_path = args.dataset_path
    if dataset_path is None:
        dataset_path = _write_tiny_npz(seed=args.seed)
        config = RespiratoryCnnBiLstmConfig(
            window_duration_seconds=4.0,
            sampling_rate_hz=8.0,
            cnn_channels=(4,),
            lstm_hidden_size=4,
        )
    else:
        config = RespiratoryCnnBiLstmConfig()

    torch.manual_seed(args.seed)
    model = RespiratoryCnnBiLstm(config)
    train_result = train_respiratory_single_epoch_smoke(
        dataset_path,
        model=model,
        config=config,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        max_batches=args.max_batches,
        seed=args.seed,
    )
    checkpoint_path = args.checkpoint_path or (
        Path(tempfile.gettempdir()) / "sleepagent_stage6_cli_smoke_checkpoint.pt"
    )
    checkpoint_path = save_respiratory_checkpoint(
        model,
        checkpoint_path,
        config=config,
        metadata={
            "script": Path(__file__).name,
            "dataset_path": str(Path(dataset_path).resolve()),
        },
    )
    loaded_checkpoint = load_respiratory_checkpoint(checkpoint_path)

    arrays = load_respiratory_npz_arrays(dataset_path, config=config)
    with torch.no_grad():
        logits = loaded_checkpoint.model(torch.as_tensor(arrays.x, dtype=torch.float32))
    evaluation_result = evaluate_respiratory_model_outputs(arrays.y, logits)
    inference_result = infer_respiratory_npz(
        loaded_checkpoint.model,
        dataset_path,
        config=loaded_checkpoint.config,
        batch_size=args.batch_size,
    )

    payload = {
        "schema_version": "stage6.respiratory_smoke.v1",
        "dataset_path": str(Path(dataset_path).resolve()),
        "checkpoint": {
            "path": str(checkpoint_path),
            "schema_version": loaded_checkpoint.schema_version,
            "config": asdict(loaded_checkpoint.config),
            "metadata": loaded_checkpoint.metadata,
        },
        "train": {
            "epoch_count": train_result.epoch_count,
            "batch_count": train_result.batch_count,
            "example_count": train_result.example_count,
            "initial_loss": train_result.initial_loss,
            "final_loss": train_result.final_loss,
            "mean_loss": train_result.mean_loss,
            "class_counts": train_result.class_counts,
        },
        "evaluation": {
            "metrics": evaluation_result.metrics.model_dump(mode="json"),
            "auc_warning_message": evaluation_result.auc_warning_message,
        },
        "inference": {
            "prediction_count": len(inference_result.predictions),
            "first_predictions": [
                {
                    "start_second": prediction.start_second,
                    "predicted_label": prediction.predicted_label.value,
                    "probabilities": prediction.probabilities,
                }
                for prediction in inference_result.predictions[:5]
            ],
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _write_tiny_npz(*, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    dataset_path = Path(tempfile.gettempdir()) / "sleepagent_stage6_cli_smoke.npz"
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


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for the Stage 6 respiratory smoke script. "
            'Install the model extra with: python -m pip install -e ".[model]"'
        ) from exc
    return torch


def _import_respiratory_model_class() -> Any:
    try:
        from sleepagent.models.respiratory_cnn_bilstm import RespiratoryCnnBiLstm
    except ImportError as exc:
        raise RuntimeError("Could not import the respiratory PyTorch model.") from exc
    return RespiratoryCnnBiLstm


if __name__ == "__main__":
    main()
