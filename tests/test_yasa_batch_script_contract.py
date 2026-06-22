from pathlib import Path

import pytest

from scripts.run_yasa_sleep_staging_batch_from_zip import build_batch_payload
from sleepagent.preprocessing import SHHSZipRecord


def test_build_batch_payload_records_metric_means_and_failures(tmp_path: Path) -> None:
    selected = [
        SHHSZipRecord(
            record_id="shhs1-200001",
            visit="shhs1",
            edf_member="polysomnography/edfs/shhs1/shhs1-200001.edf",
            nsrr_xml_member=None,
            profusion_xml_member=(
                "polysomnography/annotations-events-profusion/shhs1/"
                "shhs1-200001-profusion.xml"
            ),
        ),
        SHHSZipRecord(
            record_id="shhs1-200002",
            visit="shhs1",
            edf_member="polysomnography/edfs/shhs1/shhs1-200002.edf",
            nsrr_xml_member=None,
            profusion_xml_member=(
                "polysomnography/annotations-events-profusion/shhs1/"
                "shhs1-200002-profusion.xml"
            ),
        ),
    ]
    successes = [
        {
            "record_id": "shhs1-200001",
            "metrics": {
                "accuracy": 0.9,
                "cohen_kappa": 0.8,
                "macro_f1": 0.7,
                "weighted_f1": 0.85,
            },
        }
    ]
    failures = [
        {
            "record_id": "shhs1-200002",
            "error_type": "RuntimeError",
            "error": "boom",
        }
    ]

    payload = build_batch_payload(
        zip_path=tmp_path / "shhs.zip",
        sample_root=tmp_path / "sample",
        output_dir=tmp_path / "out",
        selected=selected,
        successes=successes,
        failures=failures,
        eeg="EEG",
        eog="EOG(L)",
        emg="EMG",
    )

    assert payload["selected_record_count"] == 2
    assert payload["success_count"] == 1
    assert payload["failure_count"] == 1
    assert payload["metric_means"]["accuracy"] == pytest.approx(0.9)
    assert payload["metric_means"]["cohen_kappa"] == pytest.approx(0.8)
    assert payload["failures"][0]["record_id"] == "shhs1-200002"
    assert payload["channels"] == {"eeg": "EEG", "eog": "EOG(L)", "emg": "EMG"}
