import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_respiratory_stage6_experiment import (
    DEFAULT_SPLIT_COUNTS,
    STAGE6_REPORT_AGENT_CONTEXT_SCHEMA_VERSION,
    assign_exact_splits,
    build_report_agent_context,
    validate_args,
)


def test_stage6_experiment_script_help_runs_without_model_extra() -> None:
    script_path = Path("scripts/run_respiratory_stage6_experiment.py")

    completed = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "20-record respiratory" in completed.stdout
    assert "--prepare-only" in completed.stdout
    assert "--train-only" in completed.stdout


def test_assign_exact_splits_uses_fixed_demo_counts() -> None:
    records = [
        {"record_id": f"shhs1-{index:06d}", "dataset_path": f"record-{index}.npz"}
        for index in range(1, 21)
    ]

    split_records = assign_exact_splits(records)
    split_counts = {
        split: sum(record["split"] == split for record in split_records)
        for split in DEFAULT_SPLIT_COUNTS
    }

    assert split_counts == DEFAULT_SPLIT_COUNTS
    assert split_records[0]["split"] == "train"
    assert split_records[13]["split"] == "train"
    assert split_records[14]["split"] == "val"
    assert split_records[17]["split"] == "test"


def test_assign_exact_splits_rejects_non_20_records() -> None:
    with pytest.raises(ValueError, match="exactly 20"):
        assign_exact_splits([{"record_id": "one", "dataset_path": "one.npz"}])


def test_validate_args_rejects_non_demo_record_count() -> None:
    args = argparse.Namespace(
        record_count=19,
        epochs=5,
        batch_size=64,
        learning_rate=1e-3,
        max_train_batches=None,
        context_only=False,
    )

    with pytest.raises(ValueError, match="exactly 20"):
        validate_args(args)


def test_build_report_agent_context_warns_about_normal_only_predictions() -> None:
    payload = {
        "schema_version": "stage6.respiratory_20_record_experiment.v1",
        "record_count": 20,
        "split_counts": DEFAULT_SPLIT_COUNTS,
        "epochs_requested": 5,
        "best_epoch": 1,
        "best_checkpoint_path": "best.pt",
        "dataset_dir": "datasets",
        "out_dir": "outputs",
        "val_metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
        "test_metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
        "test_record_summaries": [
            {
                "record_id": "shhs1-200018",
                "window_count": 3,
                "true_class_counts": {"normal_breathing": 2, "hypopnea": 1},
                "predicted_class_counts": {"normal_breathing": 3},
                "metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
                "first_predictions": [],
            }
        ],
    }

    context = build_report_agent_context(payload)

    assert context["schema_version"] == STAGE6_REPORT_AGENT_CONTEXT_SCHEMA_VERSION
    assert context["respiratory_model_status"] == "not_validated_for_risk_conclusion"
    assert "all_normal_prediction_collapse" in context["respiratory_model_gate"]["reasons"]
    assert context["test_predicted_class_counts"] == {"normal_breathing": 3}
    assert "must not present" in context["report_guidance"]["caution"]
