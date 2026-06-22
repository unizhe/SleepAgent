from pathlib import Path

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.services import generate_mock_sleep_report


def test_mock_analysis_json_contract_shape_is_stable() -> None:
    analysis = generate_mock_sleep_analysis(
        record_id="contract-record",
        subject_id="contract-subject",
        duration_hours=0.5,
        seed=101,
    )
    payload = analysis.model_dump(mode="json")

    assert set(payload) == {
        "metadata",
        "epochs",
        "respiratory_events",
        "respiratory_trend",
        "sleep_summary",
        "respiratory_summary",
        "sleep_staging_metrics",
        "respiratory_detection_metrics",
        "risk_level",
        "generated_at",
        "notes",
    }
    assert set(payload["metadata"]) == {
        "record_id",
        "source_dataset",
        "patient",
        "recording_start",
        "duration_seconds",
        "channels",
    }
    assert set(payload["metadata"]["patient"]) == {
        "subject_id",
        "age_years",
        "sex",
        "notes",
    }
    assert set(payload["epochs"][0]) == {
        "start_second",
        "duration_seconds",
        "stage",
        "confidence",
    }
    assert set(payload["respiratory_events"][0]) == {
        "start_second",
        "duration_seconds",
        "event_type",
        "confidence",
        "oxygen_desaturation_percent",
    }
    assert set(payload["respiratory_trend"][0]) == {
        "second",
        "breaths_per_minute",
        "spo2_percent",
    }
    assert set(payload["sleep_summary"]) == {
        "total_recording_minutes",
        "total_sleep_time_minutes",
        "wake_minutes",
        "rem_minutes",
        "nrem_minutes",
        "sleep_efficiency_percent",
    }
    assert set(payload["respiratory_summary"]) == {
        "ahi",
        "normal_breathing_count",
        "hypopnea_count",
        "suspected_apnea_count",
        "mean_respiratory_rate_bpm",
    }
    assert payload["metadata"]["recording_start"] == "2026-01-01T22:00:00Z"
    assert payload["generated_at"] == "2026-01-01T22:30:00Z"
    assert payload["risk_level"] in {"low", "moderate", "high"}
    assert payload["epochs"][0]["stage"] in {"Wake", "REM", "NREM"}
    assert payload["respiratory_events"][0]["event_type"] in {
        "normal_breathing",
        "hypopnea",
        "suspected_apnea",
    }


def test_mock_report_json_contract_shape_is_stable() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=102)
    payload = generate_mock_sleep_report(analysis).model_dump(mode="json")

    assert set(payload) == {
        "summary",
        "elder_report",
        "professional_report",
        "care_suggestions",
        "medical_disclaimer",
        "generated_at",
    }
    assert set(payload["summary"]) == {
        "record_id",
        "subject_id",
        "risk_level",
        "total_recording_minutes",
        "total_sleep_minutes",
        "sleep_efficiency_percent",
        "ahi",
        "hypopnea_count",
        "suspected_apnea_count",
        "mean_respiratory_rate_bpm",
    }
    assert payload["summary"]["total_sleep_minutes"] == (
        analysis.sleep_summary.total_sleep_time_minutes
    )
    assert payload["generated_at"] == analysis.model_dump(mode="json")["generated_at"]
    assert payload["elder_report"]
    assert payload["professional_report"]
    assert payload["care_suggestions"]
    assert "不能替代医生诊断" in payload["medical_disclaimer"]


def test_runtime_code_uses_python310_compatible_utc_imports() -> None:
    project_root = Path(__file__).resolve().parents[1]
    runtime_files = [
        *project_root.joinpath("sleepagent").rglob("*.py"),
        *project_root.joinpath("backend").rglob("*.py"),
        *project_root.joinpath("frontend").rglob("*.py"),
    ]

    for path in runtime_files:
        source = path.read_text(encoding="utf-8")
        assert "from datetime import UTC" not in source
        assert "datetime.UTC" not in source
