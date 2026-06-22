import json
from pathlib import Path

import pytest

from sleepagent.preprocessing import extract_shhs_sleep_stage_sequence
from sleepagent.schemas import SleepStage
from sleepagent.services import (
    STAGE3_YASA_SHHS_EVALUATION_SCHEMA_VERSION,
    build_yasa_shhs_evaluation_payload,
    evaluate_yasa_summary_against_shhs_xml,
)


def test_extracts_profusion_sleep_stage_block(tmp_path: Path) -> None:
    xml_path = tmp_path / "sample-profusion.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <SleepStages>
    <SleepStage>0</SleepStage>
    <SleepStage>1</SleepStage>
    <SleepStage>5</SleepStage>
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )

    sequence = extract_shhs_sleep_stage_sequence(xml_path)

    assert sequence.source == "SleepStages/SleepStage"
    assert sequence.epoch_duration_seconds == 30.0
    assert [epoch.start_second for epoch in sequence.epochs] == [0.0, 30.0, 60.0]
    assert [epoch.stage for epoch in sequence.epochs] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.REM,
    ]


def test_extracts_stage_scored_events_when_sleep_stage_block_is_absent(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "sample-nsrr.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <EventType>Stages|Stages</EventType>
      <EventConcept>Wake|0</EventConcept>
      <Start>0</Start>
      <Duration>30</Duration>
    </ScoredEvent>
    <ScoredEvent>
      <EventType>Stages|Stages</EventType>
      <EventConcept>Stage 2 sleep|2</EventConcept>
      <Start>30</Start>
      <Duration>60</Duration>
    </ScoredEvent>
    <ScoredEvent>
      <EventType>Respiratory|Respiratory</EventType>
      <EventConcept>Hypopnea|Hypopnea</EventConcept>
      <Start>90</Start>
      <Duration>10</Duration>
    </ScoredEvent>
  </ScoredEvents>
</PSGAnnotation>
""",
        encoding="utf-8",
    )

    sequence = extract_shhs_sleep_stage_sequence(xml_path)

    assert sequence.source == "ScoredEvents/ScoredEvent"
    assert [epoch.stage for epoch in sequence.epochs] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
    ]


def test_evaluates_yasa_summary_against_shhs_xml(tmp_path: Path) -> None:
    yasa_summary_path = tmp_path / "yasa-summary.json"
    shhs_xml_path = tmp_path / "sample-profusion.xml"
    yasa_summary_path.write_text(
        json.dumps(
            {
                "schema_version": "stage3.yasa_staging_sample.v1",
                "epochs": [
                    _epoch(0, SleepStage.WAKE),
                    _epoch(30, SleepStage.NREM),
                    _epoch(60, SleepStage.WAKE),
                ],
                "sleep_summary": {
                    "total_recording_minutes": 1.5,
                    "total_sleep_time_minutes": 0.5,
                    "wake_minutes": 1.0,
                    "rem_minutes": 0.0,
                    "nrem_minutes": 0.5,
                    "sleep_efficiency_percent": 33.33,
                },
            }
        ),
        encoding="utf-8",
    )
    shhs_xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <SleepStages>
    <SleepStage>0</SleepStage>
    <SleepStage>2</SleepStage>
    <SleepStage>5</SleepStage>
    <SleepStage>0</SleepStage>
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )

    result = evaluate_yasa_summary_against_shhs_xml(
        yasa_summary_path=yasa_summary_path,
        shhs_xml_path=shhs_xml_path,
    )
    payload = build_yasa_shhs_evaluation_payload(result)

    assert payload["schema_version"] == STAGE3_YASA_SHHS_EVALUATION_SCHEMA_VERSION
    assert payload["yasa_epoch_count"] == 3
    assert payload["shhs_epoch_count"] == 4
    assert payload["compared_epoch_count"] == 3
    assert payload["dropped_yasa_epochs"] == 0
    assert payload["dropped_shhs_epochs"] == 1
    assert payload["metrics"]["accuracy"] == pytest.approx(2 / 3)
    assert payload["metrics"]["per_class_f1"][SleepStage.WAKE.value] == pytest.approx(2 / 3)
    assert payload["metrics"]["per_class_f1"][SleepStage.NREM.value] == pytest.approx(1.0)
    assert payload["metrics"]["per_class_f1"][SleepStage.REM.value] == pytest.approx(0.0)


def _epoch(start_second: float, stage: SleepStage) -> dict[str, object]:
    return {
        "start_second": start_second,
        "duration_seconds": 30.0,
        "stage": stage.value,
        "confidence": 0.9,
    }
