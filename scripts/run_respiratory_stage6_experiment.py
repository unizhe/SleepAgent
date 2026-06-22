"""Run the Stage 6 20-record respiratory demo experiment.

This script is intentionally split-friendly because the available local
environments may provide MNE and PyTorch in different conda envs:

- use ``--prepare-only`` with an MNE-capable Python to build per-record NPZs
- use ``--train-only`` with a PyTorch-capable Python to train/evaluate/infer
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sleepagent.models import RespiratoryCnnBiLstmConfig
from sleepagent.services.respiratory_model_gate import evaluate_respiratory_model_gate
from sleepagent.training import (
    evaluate_respiratory_model_outputs,
    infer_respiratory_npz,
    load_respiratory_checkpoint,
    load_respiratory_npz_arrays,
    save_respiratory_checkpoint,
)


STAGE6_EXPERIMENT_SCHEMA_VERSION = "stage6.respiratory_20_record_experiment.v1"
STAGE6_REPORT_AGENT_CONTEXT_SCHEMA_VERSION = (
    "stage6.respiratory_20_record_report_agent_context.v1"
)
DEFAULT_SPLIT_COUNTS = {"train": 14, "val": 3, "test": 3}


def main() -> None:
    args = parse_args()
    validate_args(args)
    if args.context_only:
        summary_path = resolve_summary_path(args)
        summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
        context_path = Path(args.out_dir).expanduser().resolve() / "resp20_report_agent_context.json"
        write_report_agent_context(summary_payload, context_path)
        return
    if not args.prepare_only and not args.train_only:
        args.prepare_only = True
        args.train_only = True
    if args.prepare_only:
        prepare_datasets(args)
    if args.train_only:
        run_experiment(args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stage 6 20-record respiratory training/evaluation/inference demo."
    )
    parser.add_argument("--zip", default="../data/raw/shhs.zip")
    parser.add_argument("--sample-root", default="../data/raw/shhs_sample")
    parser.add_argument("--dataset-dir", default="../data/processed/sleepagent/stage5/resp20")
    parser.add_argument("--out-dir", default="../data/processed/sleepagent/stage6/resp20")
    parser.add_argument("--visit", default="shhs1")
    parser.add_argument("--record-count", type=int, default=20)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--train-only", action="store_true")
    parser.add_argument("--context-only", action="store_true")
    parser.add_argument("--summary-path", default=None)
    parser.add_argument("--overwrite-datasets", action="store_true")
    parser.add_argument("--max-train-batches", type=int, default=None)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.context_only:
        return
    if args.record_count != sum(DEFAULT_SPLIT_COUNTS.values()):
        raise ValueError("Stage 6 demo experiment requires exactly 20 records.")
    if args.epochs <= 0:
        raise ValueError("--epochs must be greater than 0.")
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be greater than 0.")
    if args.learning_rate <= 0:
        raise ValueError("--learning-rate must be greater than 0.")
    if args.max_train_batches is not None and args.max_train_batches <= 0:
        raise ValueError("--max-train-batches must be greater than 0 when provided.")


def resolve_summary_path(args: argparse.Namespace) -> Path:
    if args.summary_path is not None:
        return Path(args.summary_path).expanduser().resolve()
    return Path(args.out_dir).expanduser().resolve() / "resp20_experiment_summary.json"


def prepare_datasets(args: argparse.Namespace) -> list[dict[str, Any]]:
    from sleepagent.preprocessing import (
        build_shhs_zip_record_index,
        build_shhs_respiratory_training_windows_from_xml,
        extract_shhs_respiratory_signal_windows,
        extract_shhs_zip_record,
        select_complete_shhs_zip_records,
        write_shhs_respiratory_signal_dataset_npz,
    )

    zip_path = Path(args.zip).expanduser().resolve()
    sample_root = Path(args.sample_root).expanduser().resolve()
    dataset_dir = Path(args.dataset_dir).expanduser().resolve()
    dataset_dir.mkdir(parents=True, exist_ok=True)

    records = build_shhs_zip_record_index(zip_path)
    selected = select_complete_shhs_zip_records(
        records,
        limit=args.record_count,
        visit=args.visit,
        require_profusion=True,
    )
    if len(selected) != args.record_count:
        raise RuntimeError(
            f"Expected {args.record_count} complete records, found {len(selected)}."
        )

    prepared: list[dict[str, Any]] = []
    for index, record in enumerate(selected, start=1):
        dataset_path = dataset_dir / f"{record.record_id}_resp_windows_included.npz"
        manifest_path = dataset_dir / f"{record.record_id}_resp_windows_manifest.json"
        if dataset_path.exists() and not args.overwrite_datasets:
            print(f"[{index}/{len(selected)}] reuse {record.record_id}: {dataset_path}")
            prepared.append(
                {
                    "record_id": record.record_id,
                    "dataset_path": str(dataset_path),
                    "manifest_path": str(manifest_path) if manifest_path.exists() else None,
                    "reused": True,
                }
            )
            continue

        print(f"[{index}/{len(selected)}] prepare {record.record_id}")
        paths = extract_shhs_zip_record(
            zip_path=zip_path,
            record=record,
            output_root=sample_root,
            include_nsrr=True,
        )
        labels = build_shhs_respiratory_training_windows_from_xml(paths["profusion_xml"])
        sequence = extract_shhs_respiratory_signal_windows(paths["edf"], labels)
        manifest = write_shhs_respiratory_signal_dataset_npz(sequence, dataset_path)
        manifest_payload = manifest.to_json_dict()
        manifest_path.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        prepared.append(
            {
                "record_id": record.record_id,
                "dataset_path": str(dataset_path),
                "manifest_path": str(manifest_path),
                "reused": False,
                "window_count": manifest.window_count,
                "class_counts": {
                    label.value: count
                    for label, count in manifest.class_counts.items()
                },
            }
        )
    split_records = assign_exact_splits(
        [
            {
                "record_id": record["record_id"],
                "dataset_path": record["dataset_path"],
            }
            for record in prepared
        ]
    )
    split_path = dataset_dir / "resp20_split_manifest.json"
    split_path.write_text(
        json.dumps(
            {
                "schema_version": "stage6.respiratory_20_record_split.v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "split_counts": DEFAULT_SPLIT_COUNTS,
                "records": split_records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote split manifest: {split_path}")
    return split_records


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    torch = _import_torch()
    RespiratoryCnnBiLstm = _import_respiratory_model_class()
    dataset_dir = Path(args.dataset_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    split_records = load_or_build_split_records(dataset_dir, record_count=args.record_count)
    split_paths = {
        split: [Path(record["dataset_path"]) for record in split_records if record["split"] == split]
        for split in ("train", "val", "test")
    }
    for split, expected in DEFAULT_SPLIT_COUNTS.items():
        if len(split_paths[split]) != expected:
            raise RuntimeError(f"Expected {expected} {split} records, got {len(split_paths[split])}.")

    torch.manual_seed(args.seed)
    config = RespiratoryCnnBiLstmConfig()
    model = RespiratoryCnnBiLstm(config)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    criterion = torch.nn.CrossEntropyLoss()

    best_val_f1 = -1.0
    best_epoch = 0
    checkpoint_path = out_dir / "best_resp20_checkpoint.pt"
    epochs: list[dict[str, Any]] = []
    for epoch in range(1, args.epochs + 1):
        train_loss = train_one_epoch(
            model,
            split_paths["train"],
            config=config,
            optimizer=optimizer,
            criterion=criterion,
            batch_size=args.batch_size,
            max_batches=args.max_train_batches,
        )
        val_eval = evaluate_records(
            model,
            split_paths["val"],
            config=config,
            batch_size=args.batch_size,
        )
        val_f1 = float(val_eval["metrics"]["f1"] or 0.0)
        if val_f1 > best_val_f1:
            best_val_f1 = val_f1
            best_epoch = epoch
            save_respiratory_checkpoint(
                model,
                checkpoint_path,
                config=config,
                metadata={
                    "experiment": "resp20",
                    "epoch": epoch,
                    "val_f1": val_f1,
                },
            )
        epoch_payload = {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_metrics": val_eval["metrics"],
            "best_epoch": best_epoch,
            "best_val_f1": best_val_f1,
        }
        epochs.append(epoch_payload)
        print(
            f"epoch={epoch} train_loss={train_loss:.6f} "
            f"val_f1={val_f1:.6f} best_epoch={best_epoch}"
        )

    best_checkpoint = load_respiratory_checkpoint(checkpoint_path)
    val_eval = evaluate_records(
        best_checkpoint.model,
        split_paths["val"],
        config=best_checkpoint.config,
        batch_size=args.batch_size,
    )
    test_eval = evaluate_records(
        best_checkpoint.model,
        split_paths["test"],
        config=best_checkpoint.config,
        batch_size=args.batch_size,
    )
    test_summaries = [
        summarize_test_record(
            best_checkpoint.model,
            path,
            config=best_checkpoint.config,
            batch_size=args.batch_size,
        )
        for path in split_paths["test"]
    ]
    predicted_counts = aggregate_predicted_record_counts(test_summaries)
    gate_result = evaluate_respiratory_model_gate(
        metrics=test_eval["metrics"],
        predicted_class_counts=predicted_counts,
        fixed_test_split_passed=True,
        external_holdout_split_passed=False,
    )

    payload = {
        "schema_version": STAGE6_EXPERIMENT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_dir": str(dataset_dir),
        "out_dir": str(out_dir),
        "record_count": args.record_count,
        "split_counts": DEFAULT_SPLIT_COUNTS,
        "epochs_requested": args.epochs,
        "best_epoch": best_epoch,
        "best_checkpoint_path": str(checkpoint_path),
        "records": split_records,
        "epoch_history": epochs,
        "val_metrics": val_eval["metrics"],
        "test_metrics": test_eval["metrics"],
        "respiratory_model_status": gate_result.respiratory_model_status,
        "respiratory_model_gate": gate_result.model_dump(),
        "test_record_summaries": test_summaries,
        "notes": [
            "Stage 6 20-record demo experiment.",
            "Metrics are for demonstration and should not be treated as final model performance.",
        ],
    }
    summary_path = out_dir / "resp20_experiment_summary.json"
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    context_path = out_dir / "resp20_report_agent_context.json"
    write_report_agent_context(payload, context_path)
    print(f"Wrote experiment summary: {summary_path}")
    print(json.dumps(payload["test_metrics"], indent=2, sort_keys=True))
    return payload


def write_report_agent_context(summary_payload: dict[str, Any], path: Path) -> Path:
    context_payload = build_report_agent_context(summary_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(context_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote report/Agent context: {path}")
    return path


def build_report_agent_context(summary_payload: dict[str, Any]) -> dict[str, Any]:
    test_metrics = summary_payload["test_metrics"]
    val_metrics = summary_payload["val_metrics"]
    predicted_counts = aggregate_predicted_test_counts(summary_payload)
    gate_result = evaluate_respiratory_model_gate(
        metrics=test_metrics,
        predicted_class_counts=predicted_counts,
        fixed_test_split_passed=True,
        external_holdout_split_passed=False,
    )
    all_test_predictions_normal = set(predicted_counts) == {"normal_breathing"}
    caution = (
        "The Stage 6 20-record demo model predicted normal_breathing for every "
        "test window, so reports and Agents must not present this checkpoint as "
        "evidence that respiratory abnormality is absent."
        if all_test_predictions_normal
        else "Use this demo checkpoint only as pipeline evidence, not clinical evidence."
    )
    return {
        "schema_version": STAGE6_REPORT_AGENT_CONTEXT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_schema_version": summary_payload["schema_version"],
        "experiment": {
            "record_count": summary_payload["record_count"],
            "split_counts": summary_payload["split_counts"],
            "epochs_requested": summary_payload["epochs_requested"],
            "best_epoch": summary_payload["best_epoch"],
            "best_checkpoint_path": summary_payload["best_checkpoint_path"],
            "dataset_dir": summary_payload["dataset_dir"],
            "out_dir": summary_payload["out_dir"],
        },
        "val_metrics": val_metrics,
        "test_metrics": test_metrics,
        "respiratory_model_status": gate_result.respiratory_model_status,
        "respiratory_model_gate": gate_result.model_dump(),
        "test_predicted_class_counts": predicted_counts,
        "test_record_summaries": [
            {
                "record_id": record["record_id"],
                "window_count": record["window_count"],
                "true_class_counts": record["true_class_counts"],
                "predicted_class_counts": record["predicted_class_counts"],
                "metrics": record["metrics"],
                "first_predictions": record["first_predictions"],
            }
            for record in summary_payload["test_record_summaries"]
        ],
        "report_guidance": {
            "headline": "Stage 6 respiratory demo checkpoint is pipeline-ready but not performance-ready.",
            "caution": caution,
            "suitable_for": [
                "verifying train/evaluate/infer/checkpoint plumbing",
                "grounding report and Agent caveats with real SHHS demo metrics",
                "showing per-record prediction summaries for downstream formatting",
            ],
            "not_suitable_for": [
                "clinical screening",
                "claiming abnormal respiratory events are absent",
                "model-performance benchmarking",
            ],
        },
    }


def aggregate_predicted_test_counts(summary_payload: dict[str, Any]) -> dict[str, int]:
    return aggregate_predicted_record_counts(summary_payload["test_record_summaries"])


def aggregate_predicted_record_counts(
    record_summaries: list[dict[str, Any]],
) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for record in record_summaries:
        counter.update(record["predicted_class_counts"])
    return dict(sorted(counter.items()))


def assign_exact_splits(records: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(records) != sum(DEFAULT_SPLIT_COUNTS.values()):
        raise ValueError("Expected exactly 20 records for the demo split.")
    split_names = (
        ["train"] * DEFAULT_SPLIT_COUNTS["train"]
        + ["val"] * DEFAULT_SPLIT_COUNTS["val"]
        + ["test"] * DEFAULT_SPLIT_COUNTS["test"]
    )
    return [
        {**record, "split": split}
        for record, split in zip(records, split_names, strict=True)
    ]


def load_or_build_split_records(dataset_dir: Path, *, record_count: int) -> list[dict[str, str]]:
    split_path = dataset_dir / "resp20_split_manifest.json"
    if split_path.exists():
        payload = json.loads(split_path.read_text(encoding="utf-8"))
        records = payload["records"]
    else:
        paths = sorted(dataset_dir.glob("*_resp_windows_included.npz"))[:record_count]
        records = assign_exact_splits(
            [
                {
                    "record_id": path.name.replace("_resp_windows_included.npz", ""),
                    "dataset_path": str(path),
                }
                for path in paths
            ]
        )
    if len(records) != record_count:
        raise RuntimeError(f"Expected {record_count} dataset records, got {len(records)}.")
    return records


def train_one_epoch(
    model: Any,
    paths: list[Path],
    *,
    config: RespiratoryCnnBiLstmConfig,
    optimizer: Any,
    criterion: Any,
    batch_size: int,
    max_batches: int | None,
) -> float:
    torch = _import_torch()
    model.train()
    losses: list[float] = []
    batch_count = 0
    for path in paths:
        arrays = load_respiratory_npz_arrays(path, config=config)
        indices = torch.randperm(arrays.window_count)
        for start in range(0, arrays.window_count, batch_size):
            if max_batches is not None and batch_count >= max_batches:
                return sum(losses) / len(losses)
            selected = indices[start : start + batch_size].detach().cpu().numpy()
            windows = torch.as_tensor(arrays.x[selected], dtype=torch.float32)
            labels = torch.as_tensor(arrays.y[selected], dtype=torch.long)
            optimizer.zero_grad()
            logits = model(windows)
            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach().cpu().item()))
            batch_count += 1
    if not losses:
        raise RuntimeError("No training batches were processed.")
    return sum(losses) / len(losses)


def evaluate_records(
    model: Any,
    paths: list[Path],
    *,
    config: RespiratoryCnnBiLstmConfig,
    batch_size: int,
) -> dict[str, Any]:
    torch = _import_torch()
    y_true: list[int] = []
    logits_list: list[Any] = []
    model.eval()
    with torch.no_grad():
        for path in paths:
            arrays = load_respiratory_npz_arrays(path, config=config)
            y_true.extend(int(value) for value in arrays.y)
            for start in range(0, arrays.window_count, batch_size):
                batch = torch.as_tensor(arrays.x[start : start + batch_size], dtype=torch.float32)
                logits_list.append(model(batch).detach().cpu())
    logits = torch.cat(logits_list, dim=0)
    result = evaluate_respiratory_model_outputs(y_true, logits, class_order=config.class_order)
    return {
        "metrics": result.metrics.model_dump(mode="json"),
        "auc_warning_message": result.auc_warning_message,
        "example_count": len(y_true),
    }


def summarize_test_record(
    model: Any,
    path: Path,
    *,
    config: RespiratoryCnnBiLstmConfig,
    batch_size: int,
) -> dict[str, Any]:
    arrays = load_respiratory_npz_arrays(path, config=config)
    inference = infer_respiratory_npz(model, path, config=config, batch_size=batch_size)
    y_pred = [
        config.class_order.index(prediction.predicted_label)
        for prediction in inference.predictions
    ]
    evaluation = evaluate_record_predictions(arrays.y, y_pred, inference, config=config)
    true_counts = Counter(config.class_order[int(value)].value for value in arrays.y)
    pred_counts = Counter(prediction.predicted_label.value for prediction in inference.predictions)
    return {
        "record_id": path.name.replace("_resp_windows_included.npz", ""),
        "dataset_path": str(path),
        "window_count": arrays.window_count,
        "true_class_counts": dict(sorted(true_counts.items())),
        "predicted_class_counts": dict(sorted(pred_counts.items())),
        "metrics": evaluation["metrics"],
        "auc_warning_message": evaluation["auc_warning_message"],
        "first_predictions": [
            {
                "start_second": prediction.start_second,
                "predicted_label": prediction.predicted_label.value,
                "probabilities": prediction.probabilities,
            }
            for prediction in inference.predictions[:5]
        ],
    }


def evaluate_record_predictions(
    y_true: Any,
    y_pred_indices: list[int],
    inference: Any,
    *,
    config: RespiratoryCnnBiLstmConfig,
) -> dict[str, Any]:
    if len(y_pred_indices) != len(inference.predictions):
        raise ValueError("y_pred_indices must match inference prediction count.")
    probability_rows = [
        [prediction.probabilities[label.value] for label in config.class_order]
        for prediction in inference.predictions
    ]
    result = evaluate_respiratory_model_outputs(
        y_true,
        probability_rows,
        class_order=config.class_order,
        from_logits=False,
    )
    return {
        "metrics": result.metrics.model_dump(mode="json"),
        "auc_warning_message": result.auc_warning_message,
    }


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for the Stage 6 respiratory experiment."
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
