import json
from pathlib import Path

import pytest

from scripts.analyze_yasa_batch_metrics import main as analyze_batch_main
from sleepagent.services import (
    STAGE3_YASA_BATCH_ANALYSIS_SCHEMA_VERSION,
    analyze_yasa_batch_summary,
    build_yasa_batch_analysis_payload,
)


def test_analyzes_batch_metrics_and_flags_low_records(tmp_path: Path) -> None:
    batch_path = _write_batch_summary(tmp_path / "batch.json")

    result = analyze_yasa_batch_summary(batch_path, lowest_limit=2)
    payload = build_yasa_batch_analysis_payload(result)

    assert payload["schema_version"] == STAGE3_YASA_BATCH_ANALYSIS_SCHEMA_VERSION
    assert payload["success_count"] == 3
    assert payload["failure_count"] == 1
    assert payload["metric_summary"]["accuracy"]["mean"] == pytest.approx(0.89)
    assert payload["metric_summary"]["accuracy"]["median"] == pytest.approx(0.91)
    assert payload["per_class_f1_summary"]["REM"]["min"] == pytest.approx(0.61)
    assert [record["record_id"] for record in payload["lowest_records"]] == [
        "shhs1-200002",
        "shhs1-200003",
    ]
    assert [record["record_id"] for record in payload["records_below_thresholds"]] == [
        "shhs1-200002",
    ]
    assert payload["records_below_thresholds"][0]["flags"]["accuracy"]["threshold"] == 0.9
    assert payload["failures"][0]["record_id"] == "shhs1-200004"


def test_batch_analysis_cli_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    batch_path = _write_batch_summary(tmp_path / "batch.json")
    output_path = tmp_path / "analysis.json"

    exit_code = analyze_batch_main([str(batch_path), "--out", str(output_path)])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == STAGE3_YASA_BATCH_ANALYSIS_SCHEMA_VERSION
    assert payload["lowest_records"][0]["record_id"] == "shhs1-200002"
    assert "Metric distributions:" in capsys.readouterr().out


def _write_batch_summary(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "successes": [
                    _success(
                        "shhs1-200001",
                        accuracy=0.96,
                        cohen_kappa=0.91,
                        macro_f1=0.94,
                        weighted_f1=0.96,
                        wake_f1=0.95,
                        rem_f1=0.94,
                        nrem_f1=0.96,
                    ),
                    _success(
                        "shhs1-200002",
                        accuracy=0.80,
                        cohen_kappa=0.62,
                        macro_f1=0.77,
                        weighted_f1=0.81,
                        wake_f1=0.82,
                        rem_f1=0.61,
                        nrem_f1=0.88,
                    ),
                    _success(
                        "shhs1-200003",
                        accuracy=0.91,
                        cohen_kappa=0.84,
                        macro_f1=0.89,
                        weighted_f1=0.91,
                        wake_f1=0.90,
                        rem_f1=0.87,
                        nrem_f1=0.92,
                    ),
                ],
                "failures": [
                    {
                        "record_id": "shhs1-200004",
                        "error_type": "RuntimeError",
                        "error": "boom",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _success(
    record_id: str,
    *,
    accuracy: float,
    cohen_kappa: float,
    macro_f1: float,
    weighted_f1: float,
    wake_f1: float,
    rem_f1: float,
    nrem_f1: float,
) -> dict:
    return {
        "record_id": record_id,
        "evaluation_path": f"/tmp/{record_id}.json",
        "compared_epoch_count": 100,
        "metrics": {
            "accuracy": accuracy,
            "cohen_kappa": cohen_kappa,
            "macro_f1": macro_f1,
            "weighted_f1": weighted_f1,
            "per_class_f1": {
                "Wake": wake_f1,
                "REM": rem_f1,
                "NREM": nrem_f1,
            },
        },
    }
