from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import StoredAnalysisRecord
from sleepagent.services import (
    ANALYSIS_RECORDS_FILENAME,
    REPORT_RECORDS_FILENAME,
    LocalJsonlSleepDataRepository,
    build_stored_analysis_record,
    generate_mock_sleep_report,
)


def test_stored_analysis_record_rejects_mismatched_metadata() -> None:
    analysis = generate_mock_sleep_analysis(
        record_id="stage9-record",
        subject_id="stage9-subject",
        duration_hours=0.5,
        seed=301,
    )

    with pytest.raises(ValidationError):
        StoredAnalysisRecord(
            analysis_id="analysis-stage9",
            record_id="different-record",
            subject_id="stage9-subject",
            source_dataset=analysis.metadata.source_dataset,
            risk_level=analysis.risk_level,
            generated_at=analysis.generated_at,
            analysis=analysis,
        )


def test_build_stored_analysis_record_derives_search_fields() -> None:
    analysis = generate_mock_sleep_analysis(
        record_id="stage9/id with spaces",
        subject_id="stage9-subject",
        duration_hours=0.5,
        seed=302,
    )
    stored_at = datetime(2026, 2, 1, tzinfo=timezone.utc)

    record = build_stored_analysis_record(analysis, stored_at=stored_at)

    assert record.analysis_id == "analysis-stage9-id-with-spaces-20260101T223000Z"
    assert record.record_id == analysis.metadata.record_id
    assert record.subject_id == analysis.metadata.patient.subject_id
    assert record.source_dataset == "mock"
    assert record.risk_level == analysis.risk_level
    assert record.stored_at == stored_at


def test_local_jsonl_repository_saves_lists_and_loads_latest(tmp_path) -> None:
    repository = LocalJsonlSleepDataRepository(tmp_path)
    first_analysis = generate_mock_sleep_analysis(
        record_id="stage9-record-1",
        subject_id="stage9-subject",
        duration_hours=0.5,
        seed=303,
    )
    second_analysis = generate_mock_sleep_analysis(
        record_id="stage9-record-2",
        subject_id="stage9-subject",
        duration_hours=1.0,
        seed=304,
    )
    other_subject_analysis = generate_mock_sleep_analysis(
        record_id="stage9-record-3",
        subject_id="other-subject",
        duration_hours=0.5,
        seed=305,
    )

    first_record = repository.save_analysis(first_analysis)
    second_record = repository.save_analysis(second_analysis)
    repository.save_analysis(other_subject_analysis)
    report_record = repository.save_report(
        generate_mock_sleep_report(second_analysis),
        analysis_id=second_record.analysis_id,
    )

    assert tmp_path.joinpath(ANALYSIS_RECORDS_FILENAME).exists()
    assert tmp_path.joinpath(REPORT_RECORDS_FILENAME).exists()
    assert len(repository.list_analysis_records()) == 3
    assert repository.list_analysis_records(record_id="stage9-record-1") == [
        first_record
    ]
    assert len(repository.list_analysis_records(subject_id="stage9-subject")) == 2
    assert repository.get_latest_analysis_record(
        subject_id="stage9-subject"
    ) == second_record
    assert repository.get_latest_report_record(
        subject_id="stage9-subject"
    ) == report_record
    assert report_record.analysis_id == second_record.analysis_id
    assert repository.get_latest_analysis_record(subject_id="missing") is None
