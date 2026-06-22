from pathlib import Path

from sleepagent.preprocessing import (
    SHHS_MANIFEST_SCHEMA_VERSION,
    build_shhs_preprocessing_manifest,
    build_shhs_preprocessing_summary,
    validate_shhs_preprocessing_manifest_file,
    validate_shhs_preprocessing_manifest_payload,
    write_shhs_preprocessing_manifest,
)


def test_builds_xml_derived_summary_without_requiring_real_edf(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    nsrr_dir = root / "polysomnography" / "annotations-events-nsrr" / "shhs1"
    profusion_dir = (
        root / "polysomnography" / "annotations-events-profusion" / "shhs1"
    )
    nsrr_dir.mkdir(parents=True)
    profusion_dir.mkdir(parents=True)

    (nsrr_dir / "shhs1-200001-nsrr.xml").write_text(
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
      <EventType>Respiratory|Respiratory</EventType>
      <EventConcept>Obstructive apnea|Obstructive Apnea</EventConcept>
      <Start>40</Start>
      <Duration>15</Duration>
      <SignalLocation>NEW AIR</SignalLocation>
    </ScoredEvent>
    <ScoredEvent>
      <EventType>Stages|Stages</EventType>
      <EventConcept>Wake|0</EventConcept>
      <Start>0</Start>
      <Duration>30</Duration>
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
    (profusion_dir / "shhs1-200001-profusion.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <EpochLength>30</EpochLength>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Hypopnea</Name>
      <Start>10</Start>
      <Duration>15</Duration>
      <Input>AIRFLOW</Input>
    </ScoredEvent>
  </ScoredEvents>
  <SleepStages>
    <SleepStage>0</SleepStage>
    <SleepStage>2</SleepStage>
    <SleepStage>5</SleepStage>
  </SleepStages>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )

    summary = build_shhs_preprocessing_summary(root=root, record_id="shhs1-200001")

    assert summary.record_id == "shhs1-200001"
    assert summary.visit == "shhs1"
    assert summary.edf_exists is False
    assert summary.nsrr_annotation is not None
    assert summary.nsrr_annotation.root_tag == "PSGAnnotation"
    assert summary.nsrr_annotation.mapped_sleep_stage_counts == {
        "Wake": 1,
        "REM": 1,
    }
    assert summary.nsrr_annotation.mapped_respiratory_event_counts == {
        "hypopnea": 1,
        "suspected_apnea": 1,
    }
    assert summary.profusion_annotation is not None
    assert summary.profusion_annotation.mapped_sleep_stage_counts == {
        "Wake": 1,
        "NREM": 1,
        "REM": 1,
    }
    assert summary.profusion_annotation.mapped_respiratory_event_counts == {
        "hypopnea": 1
    }
    assert "EDF path existence is checked" in " ".join(summary.notes)


def test_summary_json_dict_stringifies_paths(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    (root / "polysomnography" / "annotations-events-nsrr" / "shhs1").mkdir(
        parents=True
    )
    (root / "polysomnography" / "annotations-events-nsrr" / "shhs1" / "shhs1-1-nsrr.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents />
</PSGAnnotation>
""",
        encoding="utf-8",
    )

    payload = build_shhs_preprocessing_summary(root=root, record_id="shhs1-1").to_json_dict()

    assert isinstance(payload["root"], str)
    assert isinstance(payload["edf_path"], str)
    assert isinstance(payload["nsrr_annotation"], dict)
    assert isinstance(payload["nsrr_annotation"]["path"], str)


def test_manifest_wraps_sample_summaries_with_safety_metadata(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    nsrr_dir = root / "polysomnography" / "annotations-events-nsrr" / "shhs1"
    nsrr_dir.mkdir(parents=True)
    (nsrr_dir / "shhs1-1-nsrr.xml").write_text(
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
  </ScoredEvents>
</PSGAnnotation>
""",
        encoding="utf-8",
    )

    manifest = build_shhs_preprocessing_manifest(root=root, record_ids=["shhs1-1"])
    payload = manifest.to_json_dict()

    assert payload["schema_version"] == SHHS_MANIFEST_SCHEMA_VERSION
    assert payload["record_count"] == 1
    assert isinstance(payload["generated_at"], str)
    assert payload["source_root"] == str(root.resolve())
    assert payload["records"][0]["record_id"] == "shhs1-1"
    assert payload["records"][0]["nsrr_annotation"]["mapped_respiratory_event_counts"] == {
        "hypopnea": 1
    }
    assert any("EDF signal contents are not read" in note for note in payload["safety_notes"])


def test_manifest_writer_saves_json_to_explicit_local_directory(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    nsrr_dir = root / "polysomnography" / "annotations-events-nsrr" / "shhs1"
    nsrr_dir.mkdir(parents=True)
    (nsrr_dir / "shhs1-1-nsrr.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents />
</PSGAnnotation>
""",
        encoding="utf-8",
    )
    manifest = build_shhs_preprocessing_manifest(root=root, record_ids=["shhs1-1"])

    output_path = write_shhs_preprocessing_manifest(
        manifest,
        output_dir=tmp_path / "manifests",
        filename="sample_manifest.json",
    )

    assert output_path == (tmp_path / "manifests" / "sample_manifest.json").resolve()
    assert output_path.exists()
    assert "stage2.preprocess_manifest.v1" in output_path.read_text(encoding="utf-8")


def test_manifest_validation_accepts_valid_payload(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    nsrr_dir = root / "polysomnography" / "annotations-events-nsrr" / "shhs1"
    nsrr_dir.mkdir(parents=True)
    (nsrr_dir / "shhs1-1-nsrr.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
  <ScoredEvents />
</PSGAnnotation>
""",
        encoding="utf-8",
    )
    manifest = build_shhs_preprocessing_manifest(root=root, record_ids=["shhs1-1"])

    result = validate_shhs_preprocessing_manifest_payload(manifest.to_json_dict())

    assert result.is_valid is True
    assert result.errors == []


def test_manifest_validation_reports_schema_and_count_errors(tmp_path: Path) -> None:
    root = tmp_path / "shhs_sample"
    manifest = build_shhs_preprocessing_manifest(root=root, record_ids=[])
    payload = manifest.to_json_dict()
    payload["schema_version"] = "bad"
    payload["record_count"] = 2
    payload["safety_notes"] = ["too short"]

    result = validate_shhs_preprocessing_manifest_payload(payload)

    assert result.is_valid is False
    assert any("schema_version" in error for error in result.errors)
    assert any("record_count" in error for error in result.errors)
    assert any("safety_notes" in error for error in result.errors)


def test_manifest_validation_reports_missing_required_record_fields() -> None:
    result = validate_shhs_preprocessing_manifest_payload(
        {
            "schema_version": SHHS_MANIFEST_SCHEMA_VERSION,
            "generated_at": "2026-01-01T00:00:00Z",
            "source_root": "/tmp/shhs_sample",
            "record_count": 1,
            "records": [{"record_id": "shhs1-1"}],
            "safety_notes": [
                "Raw SHHS data must not be committed.",
                "EDF signal contents are not read.",
                "This manifest does not contain training windows.",
            ],
        }
    )

    assert result.is_valid is False
    assert any("records[0].visit is required" in error for error in result.errors)


def test_manifest_file_validation_reads_json(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version": "bad"}', encoding="utf-8")

    result = validate_shhs_preprocessing_manifest_file(manifest_path)

    assert result.is_valid is False
    assert any("schema_version" in error for error in result.errors)
