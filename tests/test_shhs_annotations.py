from pathlib import Path

import pytest

from sleepagent.preprocessing import SHHSAnnotationError, inspect_shhs_annotation_xml


def test_inspects_synthetic_nsrr_annotation_xml(tmp_path: Path) -> None:
    xml_path = tmp_path / "shhs1-200001-nsrr.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <EventType>Respiratory|Respiratory</EventType>
      <EventConcept>Hypopnea|Hypopnea</EventConcept>
      <Start>10</Start>
      <Duration>15</Duration>
      <SignalLocation>NEW AIR</SignalLocation>
    </ScoredEvent>
    <ScoredEvent>
      <EventType>Stages|Stages</EventType>
      <EventConcept>REM sleep|5</EventConcept>
      <Start>30</Start>
      <Duration>30</Duration>
    </ScoredEvent>
  </ScoredEvents>
</PSGAnnotation>
""",
        encoding="utf-8",
    )

    inspection = inspect_shhs_annotation_xml(xml_path)

    assert inspection.root_tag == "PSGAnnotation"
    assert inspection.epoch_length_seconds == 30.0
    assert inspection.scored_event_count == 2
    assert inspection.sleep_stage_count == 0
    assert inspection.event_type_counts == {
        "Respiratory|Respiratory": 1,
        "Stages|Stages": 1,
    }
    assert inspection.event_name_counts == {"Hypopnea": 1, "REM sleep": 1}
    assert inspection.signal_counts == {"NEW AIR": 1}


def test_inspects_synthetic_profusion_annotation_xml(tmp_path: Path) -> None:
    xml_path = tmp_path / "shhs1-200001-profusion.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Obstructive Apnea</Name>
      <Start>10</Start>
      <Duration>20</Duration>
      <Input>AIRFLOW</Input>
    </ScoredEvent>
    <ScoredEvent>
      <Name>SpO2 desaturation</Name>
      <Start>40</Start>
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

    inspection = inspect_shhs_annotation_xml(xml_path)

    assert inspection.root_tag == "CMPStudyConfig"
    assert inspection.epoch_length_seconds == 30.0
    assert inspection.scored_event_count == 2
    assert inspection.sleep_stage_count == 4
    assert inspection.event_type_counts == {}
    assert inspection.event_name_counts == {
        "Obstructive Apnea": 1,
        "SpO2 desaturation": 1,
    }
    assert inspection.signal_counts == {"AIRFLOW": 1, "SAO2": 1}
    assert inspection.sleep_stage_counts == {"0": 1, "2": 1, "5": 2}


def test_annotation_inspection_rejects_missing_or_non_xml_paths(tmp_path: Path) -> None:
    with pytest.raises(SHHSAnnotationError, match="not found"):
        inspect_shhs_annotation_xml(tmp_path / "missing.xml")

    text_path = tmp_path / "annotation.txt"
    text_path.write_text("<xml />", encoding="utf-8")
    with pytest.raises(SHHSAnnotationError, match=".xml"):
        inspect_shhs_annotation_xml(text_path)
