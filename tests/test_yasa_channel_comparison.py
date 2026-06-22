import json
from pathlib import Path

import pytest

from sleepagent.services import (
    STAGE3_YASA_CHANNEL_COMPARISON_SCHEMA_VERSION,
    build_yasa_channel_comparison_payload,
    compare_yasa_channel_evaluations,
)


def test_compares_channel_evaluations_by_accuracy_then_tie_breakers(tmp_path: Path) -> None:
    eeg_path = _write_eval(
        tmp_path / "eeg.json",
        accuracy=0.93,
        cohen_kappa=0.88,
        macro_f1=0.92,
        weighted_f1=0.93,
    )
    eegsec_path = _write_eval(
        tmp_path / "eegsec.json",
        accuracy=0.93,
        cohen_kappa=0.89,
        macro_f1=0.91,
        weighted_f1=0.92,
    )

    result = compare_yasa_channel_evaluations(
        {
            "EEG": eeg_path,
            "EEG(sec)": eegsec_path,
        }
    )
    payload = build_yasa_channel_comparison_payload(result)

    assert result.recommended_channel == "EEG(sec)"
    assert payload["schema_version"] == STAGE3_YASA_CHANNEL_COMPARISON_SCHEMA_VERSION
    assert payload["recommended_channel"] == "EEG(sec)"
    assert [candidate["name"] for candidate in payload["candidates"]] == [
        "EEG(sec)",
        "EEG",
    ]
    assert payload["candidates"][0]["metrics"]["cohen_kappa"] == pytest.approx(0.89)


def test_channel_comparison_rejects_invalid_inputs(tmp_path: Path) -> None:
    good_path = _write_eval(tmp_path / "good.json")

    with pytest.raises(ValueError, match="At least two"):
        compare_yasa_channel_evaluations({"EEG": good_path})

    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing metric"):
        compare_yasa_channel_evaluations({"EEG": good_path, "bad": bad_path})


def _write_eval(
    path: Path,
    *,
    accuracy: float = 0.9,
    cohen_kappa: float = 0.8,
    macro_f1: float = 0.85,
    weighted_f1: float = 0.86,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "compared_epoch_count": 10,
                "metrics": {
                    "accuracy": accuracy,
                    "cohen_kappa": cohen_kappa,
                    "macro_f1": macro_f1,
                    "weighted_f1": weighted_f1,
                    "per_class_f1": {"Wake": 0.9, "REM": 0.8, "NREM": 0.85},
                },
            }
        ),
        encoding="utf-8",
    )
    return path
