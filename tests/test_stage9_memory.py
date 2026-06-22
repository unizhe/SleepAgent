import pytest

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.services import (
    LocalJsonlSleepDataRepository,
    build_dialogue_context_from_memory,
    compress_long_term_memory,
    compress_memory_from_repository,
    generate_mock_sleep_report,
)


def test_compress_long_term_memory_builds_dialogue_history_summary(tmp_path) -> None:
    first = generate_mock_sleep_analysis(
        record_id="memory-record-1",
        subject_id="memory-subject",
        duration_hours=0.5,
        seed=401,
        abnormal_event_rate_per_hour=2.0,
    )
    second = generate_mock_sleep_analysis(
        record_id="memory-record-2",
        subject_id="memory-subject",
        duration_hours=1.0,
        seed=402,
        abnormal_event_rate_per_hour=8.0,
    )
    third = generate_mock_sleep_analysis(
        record_id="memory-record-3",
        subject_id="memory-subject",
        duration_hours=1.5,
        seed=403,
        abnormal_event_rate_per_hour=20.0,
    )
    repository = LocalJsonlSleepDataRepository(tmp_path)
    first_record = repository.save_analysis(first)
    second_record = repository.save_analysis(second)
    third_record = repository.save_analysis(third)
    report_record = repository.save_report(
        generate_mock_sleep_report(third),
        analysis_id=third_record.analysis_id,
    )

    memory = compress_long_term_memory(
        [first_record, second_record, third_record],
        report_records=[report_record],
        max_records=2,
        generated_at=third.generated_at,
    )

    assert memory.schema_version == "stage9.long_term_memory_summary.v1"
    assert memory.subject_id == "memory-subject"
    assert memory.record_count == 2
    assert memory.record_ids == ["memory-record-3", "memory-record-2"]
    assert memory.source_analysis_ids == [
        third_record.analysis_id,
        second_record.analysis_id,
    ]
    assert memory.source_report_ids == [report_record.report_id]
    assert memory.latest_risk_level == third.risk_level
    assert memory.latest_ahi == third.respiratory_summary.ahi
    assert memory.max_ahi >= memory.latest_ahi
    assert "最近2次睡眠记录" in memory.history_summary
    assert "平均AHI" in memory.history_summary
    assert "该长期记忆仅用于对话上下文" in memory.history_summary

    dialogue_context = build_dialogue_context_from_memory(
        memory,
        user_preferences=["面向家属解释"],
        recent_questions=["AHI 是否升高"],
    )

    assert dialogue_context.history_summary == memory.history_summary
    assert dialogue_context.user_preferences == ["面向家属解释"]
    assert dialogue_context.recent_questions == ["AHI 是否升高"]


def test_compress_memory_from_repository_returns_none_without_history(tmp_path) -> None:
    repository = LocalJsonlSleepDataRepository(tmp_path)

    assert compress_memory_from_repository(
        repository,
        subject_id="missing-subject",
    ) is None


def test_compress_memory_from_repository_uses_stored_subject_history(tmp_path) -> None:
    repository = LocalJsonlSleepDataRepository(tmp_path)
    analysis = generate_mock_sleep_analysis(
        record_id="memory-repo-record",
        subject_id="memory-repo-subject",
        duration_hours=0.5,
        seed=404,
    )
    analysis_record = repository.save_analysis(analysis)
    report_record = repository.save_report(
        generate_mock_sleep_report(analysis),
        analysis_id=analysis_record.analysis_id,
    )

    memory = compress_memory_from_repository(
        repository,
        subject_id="memory-repo-subject",
    )

    assert memory is not None
    assert memory.record_ids == ["memory-repo-record"]
    assert memory.source_report_ids == [report_record.report_id]
    assert "memory-repo-subject" in memory.history_summary


def test_compress_long_term_memory_rejects_invalid_inputs(tmp_path) -> None:
    analysis = generate_mock_sleep_analysis(
        record_id="memory-record",
        subject_id="memory-subject",
        duration_hours=0.5,
        seed=405,
    )
    other_subject_analysis = generate_mock_sleep_analysis(
        record_id="other-memory-record",
        subject_id="other-memory-subject",
        duration_hours=0.5,
        seed=406,
    )
    repository = LocalJsonlSleepDataRepository(tmp_path)
    record = repository.save_analysis(analysis)
    other_subject_record = repository.save_analysis(other_subject_analysis)

    with pytest.raises(ValueError, match="at least one"):
        compress_long_term_memory([])

    with pytest.raises(ValueError, match="greater than 0"):
        compress_long_term_memory([record], max_records=0)

    with pytest.raises(ValueError, match="one subject"):
        compress_long_term_memory([record, other_subject_record])
