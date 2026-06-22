from pathlib import Path

import pytest

from sleepagent.preprocessing import (
    RESPIRATORY_LABEL_CONFLICT_RULE,
    RESPIRATORY_NORMAL_RULE,
    SHHSAnnotationError,
    SHHS_RESPIRATORY_WINDOW_MANIFEST_SCHEMA_VERSION,
    build_shhs_respiratory_window_manifest_from_xml,
    build_shhs_respiratory_training_windows,
    build_shhs_respiratory_training_windows_from_xml,
    extract_shhs_respiratory_event_sequence,
)
from sleepagent.schemas import RespiratoryEvent, RespiratoryEventType


def test_extracts_mapped_respiratory_events_from_profusion_xml(tmp_path: Path) -> None:
    xml_path = _write_profusion_xml(tmp_path)

    sequence = extract_shhs_respiratory_event_sequence(xml_path)

    assert sequence.scored_event_count == 3
    assert sequence.mapped_event_count == 2
    assert sequence.ignored_event_count == 1
    assert sequence.target_label_counts == {
        RespiratoryEventType.HYPOPNEA: 1,
        RespiratoryEventType.SUSPECTED_APNEA: 1,
    }
    assert sequence.ignored_label_counts == {"SpO2 desaturation": 1}
    assert sequence.unknown_label_counts == {}
    assert [event.event_type for event in sequence.events] == [
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.SUSPECTED_APNEA,
    ]
    assert sequence.events[0].start_second == pytest.approx(35.0)
    assert sequence.events[0].duration_seconds == pytest.approx(10.0)
    assert sequence.events[1].start_second == pytest.approx(70.0)
    assert sequence.events[1].duration_seconds == pytest.approx(20.0)


def test_builds_30_second_window_labels_from_profusion_xml(tmp_path: Path) -> None:
    xml_path = _write_profusion_xml(tmp_path)

    sequence = build_shhs_respiratory_training_windows_from_xml(xml_path)

    assert sequence.recording_duration_seconds == pytest.approx(120.0)
    assert sequence.window_duration_seconds == pytest.approx(30.0)
    assert [window.start_second for window in sequence.windows] == [
        0.0,
        30.0,
        60.0,
        90.0,
    ]
    assert [window.label for window in sequence.windows] == [
        RespiratoryEventType.NORMAL_BREATHING,
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.SUSPECTED_APNEA,
        RespiratoryEventType.NORMAL_BREATHING,
    ]
    assert [window.is_included_in_training for window in sequence.windows] == [
        False,
        True,
        True,
        False,
    ]
    assert sequence.excluded_window_counts == {"near_abnormal_event": 2}
    assert sequence.windows[1].overlap_seconds_by_label == {
        RespiratoryEventType.HYPOPNEA: 10.0
    }
    assert sequence.windows[2].overlap_seconds_by_label == {
        RespiratoryEventType.SUSPECTED_APNEA: 20.0
    }
    assert sequence.class_counts == {
        RespiratoryEventType.HYPOPNEA: 1,
        RespiratoryEventType.SUSPECTED_APNEA: 1,
    }


def test_event_crossing_window_boundary_labels_both_windows() -> None:
    windows = build_shhs_respiratory_training_windows(
        [
            RespiratoryEvent(
                start_second=25.0,
                duration_seconds=10.0,
                event_type=RespiratoryEventType.HYPOPNEA,
                confidence=1.0,
            )
        ],
        recording_duration_seconds=60.0,
    )

    assert [window.label for window in windows] == [
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.HYPOPNEA,
    ]
    assert windows[0].overlap_seconds_by_label == {
        RespiratoryEventType.HYPOPNEA: 5.0
    }
    assert windows[1].overlap_seconds_by_label == {
        RespiratoryEventType.HYPOPNEA: 5.0
    }


def test_normal_buffer_excludes_nearby_clean_normal_windows() -> None:
    windows = build_shhs_respiratory_training_windows(
        [
            RespiratoryEvent(
                start_second=75.0,
                duration_seconds=10.0,
                event_type=RespiratoryEventType.HYPOPNEA,
                confidence=1.0,
            )
        ],
        recording_duration_seconds=120.0,
    )

    assert [window.label for window in windows] == [
        RespiratoryEventType.NORMAL_BREATHING,
        RespiratoryEventType.NORMAL_BREATHING,
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.NORMAL_BREATHING,
    ]
    assert [window.is_included_in_training for window in windows] == [
        True,
        False,
        True,
        False,
    ]
    assert windows[1].exclusion_reason == "near_abnormal_event"
    assert windows[3].exclusion_reason == "near_abnormal_event"


def test_conflict_rule_uses_larger_overlap_and_ties_to_suspected_apnea() -> None:
    larger_hypopnea_windows = build_shhs_respiratory_training_windows(
        [
            RespiratoryEvent(
                start_second=0.0,
                duration_seconds=10.0,
                event_type=RespiratoryEventType.HYPOPNEA,
                confidence=1.0,
            ),
            RespiratoryEvent(
                start_second=10.0,
                duration_seconds=5.0,
                event_type=RespiratoryEventType.SUSPECTED_APNEA,
                confidence=1.0,
            ),
        ],
        recording_duration_seconds=30.0,
    )
    tied_windows = build_shhs_respiratory_training_windows(
        [
            RespiratoryEvent(
                start_second=0.0,
                duration_seconds=5.0,
                event_type=RespiratoryEventType.HYPOPNEA,
                confidence=1.0,
            ),
            RespiratoryEvent(
                start_second=10.0,
                duration_seconds=5.0,
                event_type=RespiratoryEventType.SUSPECTED_APNEA,
                confidence=1.0,
            ),
        ],
        recording_duration_seconds=30.0,
    )

    assert larger_hypopnea_windows[0].label == RespiratoryEventType.HYPOPNEA
    assert tied_windows[0].label == RespiratoryEventType.SUSPECTED_APNEA


def test_builds_windows_with_explicit_recording_duration_for_nsrr_xml(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "shhs1-1-nsrr.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <EventType>Respiratory|Respiratory</EventType>
      <EventConcept>Hypopnea|Hypopnea</EventConcept>
      <Start>5</Start>
      <Duration>10</Duration>
      <SignalLocation>NEW AIR</SignalLocation>
    </ScoredEvent>
  </ScoredEvents>
</PSGAnnotation>
""",
        encoding="utf-8",
    )

    sequence = build_shhs_respiratory_training_windows_from_xml(
        xml_path,
        recording_duration_seconds=60.0,
    )

    assert len(sequence.windows) == 2
    assert [window.label for window in sequence.windows] == [
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.NORMAL_BREATHING,
    ]
    assert sequence.windows[1].is_included_in_training is False
    assert sequence.windows[1].exclusion_reason == "near_abnormal_event"


def test_unknown_policy_can_raise_for_unreviewed_xml_vocab(tmp_path: Path) -> None:
    xml_path = tmp_path / "shhs1-1-profusion.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Novel Respiratory Event</Name>
      <Start>10</Start>
      <Duration>20</Duration>
    </ScoredEvent>
  </ScoredEvents>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Novel Respiratory Event"):
        extract_shhs_respiratory_event_sequence(xml_path, unknown_policy="raise")


def test_manifest_contract_records_rules_and_label_warnings(tmp_path: Path) -> None:
    xml_path = tmp_path / "shhs1-1-profusion.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Hypopnea</Name>
      <Start>35</Start>
      <Duration>10</Duration>
    </ScoredEvent>
    <ScoredEvent>
      <Name>Novel Respiratory Event</Name>
      <Start>75</Start>
      <Duration>10</Duration>
    </ScoredEvent>
    <ScoredEvent>
      <Name>SpO2 desaturation</Name>
      <Start>80</Start>
      <Duration>10</Duration>
    </ScoredEvent>
  </ScoredEvents>
  <SleepStages>
    <SleepStage>0</SleepStage>
    <SleepStage>2</SleepStage>
    <SleepStage>5</SleepStage>
    <SleepStage>5</SleepStage>
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )

    manifest = build_shhs_respiratory_window_manifest_from_xml(xml_path)
    payload = manifest.to_json_dict()

    assert payload["schema_version"] == SHHS_RESPIRATORY_WINDOW_MANIFEST_SCHEMA_VERSION
    assert payload["label_conflict_rule"] == RESPIRATORY_LABEL_CONFLICT_RULE
    assert payload["normal_rule"] == RESPIRATORY_NORMAL_RULE
    assert payload["minimum_event_overlap_seconds"] == 1.0
    assert payload["normal_exclusion_buffer_seconds"] == 30.0
    assert payload["unknown_policy"] == "ignore"
    assert payload["target_label_counts"] == {"hypopnea": 1}
    assert payload["ignored_label_counts"] == {"SpO2 desaturation": 1}
    assert payload["unknown_label_counts"] == {"Novel Respiratory Event": 1}
    assert payload["included_class_counts"] == {
        "normal_breathing": 1,
        "hypopnea": 1,
    }
    assert payload["excluded_window_counts"] == {"near_abnormal_event": 2}
    assert payload["included_window_count"] == 2
    assert payload["excluded_window_count"] == 2
    assert len(payload["warning_messages"]) == 2


def test_rejects_xml_without_duration_hint(tmp_path: Path) -> None:
    xml_path = tmp_path / "empty.xml"
    xml_path.write_text("<CMPStudyConfig><ScoredEvents /></CMPStudyConfig>", encoding="utf-8")

    with pytest.raises(SHHSAnnotationError, match="recording duration"):
        build_shhs_respiratory_training_windows_from_xml(xml_path)


def _write_profusion_xml(tmp_path: Path) -> Path:
    xml_path = tmp_path / "shhs1-1-profusion.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Hypopnea</Name>
      <Start>35</Start>
      <Duration>10</Duration>
      <Input>AIRFLOW</Input>
    </ScoredEvent>
    <ScoredEvent>
      <Name>Obstructive Apnea</Name>
      <Start>70</Start>
      <Duration>20</Duration>
      <Input>AIRFLOW</Input>
    </ScoredEvent>
    <ScoredEvent>
      <Name>SpO2 desaturation</Name>
      <Start>75</Start>
      <Duration>20</Duration>
      <Input>SAO2</Input>
    </ScoredEvent>
  </ScoredEvents>
  <SleepStages>
    <SleepStage>0</SleepStage>
    <SleepStage>2</SleepStage>
    <SleepStage>5</SleepStage>
    <SleepStage>5</SleepStage>
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )
    return xml_path
