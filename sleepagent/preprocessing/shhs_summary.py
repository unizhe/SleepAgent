"""Build tiny XML-derived SHHS preprocessing summaries."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from sleepagent.preprocessing.shhs_annotations import (
    SHHSAnnotationInspection,
    inspect_shhs_annotation_xml,
)
from sleepagent.preprocessing.shhs_label_mapping import (
    map_shhs_respiratory_event_counts,
    map_shhs_xml_sleep_label_counts,
)
from sleepagent.preprocessing.shhs_paths import SHHSRecordPaths, build_shhs_record_paths


SHHS_MANIFEST_SCHEMA_VERSION = "stage2.preprocess_manifest.v1"
DEFAULT_MANIFEST_DIR = Path("../data/manifests")

_MANIFEST_REQUIRED_FIELDS = {
    "schema_version",
    "generated_at",
    "source_root",
    "record_count",
    "records",
    "safety_notes",
}
_RECORD_REQUIRED_FIELDS = {
    "record_id",
    "visit",
    "root",
    "edf_path",
    "edf_exists",
    "nsrr_annotation",
    "profusion_annotation",
    "notes",
}
_ANNOTATION_REQUIRED_FIELDS = {
    "path",
    "root_tag",
    "epoch_length_seconds",
    "scored_event_count",
    "sleep_stage_count",
    "event_type_counts",
    "signal_counts",
    "mapped_sleep_stage_counts",
    "mapped_respiratory_event_counts",
}
_REQUIRED_SAFETY_NOTE_SNIPPETS = (
    "must not be committed",
    "EDF signal contents are not read",
    "does not contain training windows",
)


@dataclass(frozen=True)
class SHHSAnnotationSummary:
    """Mapped metadata summary for one SHHS XML annotation file."""

    path: Path
    root_tag: str
    epoch_length_seconds: float | None
    scored_event_count: int
    sleep_stage_count: int
    event_type_counts: dict[str, int]
    signal_counts: dict[str, int]
    mapped_sleep_stage_counts: dict[str, int]
    mapped_respiratory_event_counts: dict[str, int]


@dataclass(frozen=True)
class SHHSPreprocessingSummary:
    """Tiny Stage 2 summary for one local SHHS sample record."""

    record_id: str
    visit: str
    root: Path
    edf_path: Path
    edf_exists: bool
    nsrr_annotation: SHHSAnnotationSummary | None
    profusion_annotation: SHHSAnnotationSummary | None
    notes: list[str]

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return _stringify_paths(payload)


@dataclass(frozen=True)
class SHHSPreprocessingManifest:
    """Minimal Stage 2 manifest for local sample preprocessing summaries."""

    schema_version: str
    generated_at: datetime
    source_root: Path
    record_count: int
    records: list[SHHSPreprocessingSummary]
    safety_notes: list[str]

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return _stringify_paths(payload)


@dataclass(frozen=True)
class SHHSManifestValidationResult:
    """Validation result for a Stage 2 preprocessing manifest payload."""

    is_valid: bool
    errors: list[str]


def build_shhs_preprocessing_summary(
    root: str | Path,
    record_id: str,
    include_profusion: bool = True,
) -> SHHSPreprocessingSummary:
    """Build a tiny XML-derived summary without reading EDF signal contents."""
    record_paths = build_shhs_record_paths(root=root, record_id=record_id)
    return build_shhs_preprocessing_summary_from_paths(
        record_paths,
        include_profusion=include_profusion,
    )


def build_shhs_preprocessing_manifest(
    root: str | Path,
    record_ids: Iterable[str],
    include_profusion: bool = True,
) -> SHHSPreprocessingManifest:
    """Build a minimal local manifest for one or more sample records."""
    resolved_root = Path(root).expanduser().resolve()
    records = [
        build_shhs_preprocessing_summary(
            root=resolved_root,
            record_id=record_id,
            include_profusion=include_profusion,
        )
        for record_id in record_ids
    ]
    return SHHSPreprocessingManifest(
        schema_version=SHHS_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        source_root=resolved_root,
        record_count=len(records),
        records=records,
        safety_notes=[
            "Stage 2 manifest for local smoke-test samples only.",
            "Raw SHHS data, EDF files, XML files, arrays, and parquet outputs must not be committed.",
            "EDF paths are recorded and existence is checked, but EDF signal contents are not read.",
            "This manifest does not contain training windows, model inputs, or derived signal arrays.",
        ],
    )


def write_shhs_preprocessing_manifest(
    manifest: SHHSPreprocessingManifest,
    output_dir: str | Path = DEFAULT_MANIFEST_DIR,
    filename: str | None = None,
) -> Path:
    """Write a local Stage 2 manifest JSON file outside the code repository."""
    resolved_output_dir = Path(output_dir).expanduser().resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = resolved_output_dir / (
        filename or default_shhs_manifest_filename(manifest)
    )
    output_path.write_text(
        json.dumps(manifest.to_json_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def validate_shhs_preprocessing_manifest_payload(
    payload: Mapping[str, Any],
) -> SHHSManifestValidationResult:
    """Validate the minimal Stage 2 manifest shape without touching data files."""
    errors: list[str] = []
    _require_mapping_fields(payload, _MANIFEST_REQUIRED_FIELDS, "manifest", errors)

    schema_version = payload.get("schema_version")
    if schema_version != SHHS_MANIFEST_SCHEMA_VERSION:
        errors.append(
            "manifest.schema_version must be "
            f"{SHHS_MANIFEST_SCHEMA_VERSION!r}, got {schema_version!r}."
        )

    records = payload.get("records")
    if not isinstance(records, list):
        errors.append("manifest.records must be a list.")
        records = []

    record_count = payload.get("record_count")
    if not isinstance(record_count, int):
        errors.append("manifest.record_count must be an integer.")
    elif record_count != len(records):
        errors.append(
            f"manifest.record_count {record_count} does not match "
            f"len(records) {len(records)}."
        )

    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        errors.append("manifest.generated_at must be a non-empty string.")

    source_root = payload.get("source_root")
    if not isinstance(source_root, str) or not source_root:
        errors.append("manifest.source_root must be a non-empty string.")

    _validate_safety_notes(payload.get("safety_notes"), errors)

    for index, record in enumerate(records):
        _validate_manifest_record(record, index, errors)

    return SHHSManifestValidationResult(is_valid=not errors, errors=errors)


def validate_shhs_preprocessing_manifest_file(
    path: str | Path,
) -> SHHSManifestValidationResult:
    """Load and validate a local Stage 2 manifest JSON file."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return SHHSManifestValidationResult(
            is_valid=False,
            errors=[f"Could not read manifest file {manifest_path}: {exc}"],
        )
    except json.JSONDecodeError as exc:
        return SHHSManifestValidationResult(
            is_valid=False,
            errors=[f"Could not parse manifest JSON {manifest_path}: {exc}"],
        )
    if not isinstance(payload, dict):
        return SHHSManifestValidationResult(
            is_valid=False,
            errors=["manifest JSON root must be an object."],
        )
    return validate_shhs_preprocessing_manifest_payload(payload)


def default_shhs_manifest_filename(manifest: SHHSPreprocessingManifest) -> str:
    """Return a stable local filename for a manifest."""
    if manifest.record_count == 1 and manifest.records:
        record_part = manifest.records[0].record_id
    else:
        record_part = f"{manifest.record_count}-records"
    generated_part = (
        manifest.generated_at.astimezone(timezone.utc)
        .strftime("%Y%m%dT%H%M%SZ")
    )
    schema_part = manifest.schema_version.replace(".", "_")
    return f"{schema_part}_{record_part}_{generated_part}.json"


def _require_mapping_fields(
    value: Mapping[str, Any],
    required_fields: set[str],
    label: str,
    errors: list[str],
) -> None:
    missing = sorted(required_fields.difference(value))
    for field in missing:
        errors.append(f"{label}.{field} is required.")


def _validate_safety_notes(value: object, errors: list[str]) -> None:
    if not isinstance(value, list) or not all(isinstance(note, str) for note in value):
        errors.append("manifest.safety_notes must be a list of strings.")
        return
    combined_notes = " ".join(value)
    for snippet in _REQUIRED_SAFETY_NOTE_SNIPPETS:
        if snippet not in combined_notes:
            errors.append(f"manifest.safety_notes must mention {snippet!r}.")


def _validate_manifest_record(
    record: object,
    index: int,
    errors: list[str],
) -> None:
    label = f"manifest.records[{index}]"
    if not isinstance(record, dict):
        errors.append(f"{label} must be an object.")
        return
    _require_mapping_fields(record, _RECORD_REQUIRED_FIELDS, label, errors)
    for field in ("record_id", "visit", "root", "edf_path"):
        if field in record and not isinstance(record[field], str):
            errors.append(f"{label}.{field} must be a string.")
    if "edf_exists" in record and not isinstance(record["edf_exists"], bool):
        errors.append(f"{label}.edf_exists must be a boolean.")
    if "notes" in record and not isinstance(record["notes"], list):
        errors.append(f"{label}.notes must be a list.")
    for annotation_field in ("nsrr_annotation", "profusion_annotation"):
        annotation = record.get(annotation_field)
        if annotation is not None:
            _validate_manifest_annotation(annotation, f"{label}.{annotation_field}", errors)


def _validate_manifest_annotation(
    annotation: object,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(annotation, dict):
        errors.append(f"{label} must be an object or null.")
        return
    _require_mapping_fields(annotation, _ANNOTATION_REQUIRED_FIELDS, label, errors)
    for field in ("path", "root_tag"):
        if field in annotation and not isinstance(annotation[field], str):
            errors.append(f"{label}.{field} must be a string.")
    for field in ("scored_event_count", "sleep_stage_count"):
        if field in annotation and not isinstance(annotation[field], int):
            errors.append(f"{label}.{field} must be an integer.")
    for field in (
        "event_type_counts",
        "signal_counts",
        "mapped_sleep_stage_counts",
        "mapped_respiratory_event_counts",
    ):
        if field in annotation and not isinstance(annotation[field], dict):
            errors.append(f"{label}.{field} must be an object.")


def build_shhs_preprocessing_summary_from_paths(
    record_paths: SHHSRecordPaths,
    include_profusion: bool = True,
) -> SHHSPreprocessingSummary:
    """Build a summary from already resolved local SHHS record paths."""
    notes: list[str] = [
        "XML-derived Stage 2 summary only.",
        "EDF path existence is checked, but EDF signal contents are not read.",
        "Counts are metadata summaries and are not training windows.",
    ]

    nsrr_summary = _optional_annotation_summary(
        record_paths.nsrr_annotation_path,
        notes=notes,
        role="NSRR annotation",
    )
    profusion_summary = None
    if include_profusion:
        profusion_summary = _optional_annotation_summary(
            record_paths.profusion_annotation_path,
            notes=notes,
            role="Profusion annotation",
        )

    return SHHSPreprocessingSummary(
        record_id=record_paths.record_id,
        visit=record_paths.visit,
        root=record_paths.root,
        edf_path=record_paths.edf_path,
        edf_exists=record_paths.edf_path.exists(),
        nsrr_annotation=nsrr_summary,
        profusion_annotation=profusion_summary,
        notes=notes,
    )


def _optional_annotation_summary(
    path: Path,
    notes: list[str],
    role: str,
) -> SHHSAnnotationSummary | None:
    if not path.exists():
        notes.append(f"{role} missing: {path}")
        return None
    return summarize_shhs_annotation_inspection(inspect_shhs_annotation_xml(path))


def summarize_shhs_annotation_inspection(
    inspection: SHHSAnnotationInspection,
) -> SHHSAnnotationSummary:
    """Convert XML inspection vocabularies into mapped MVP count summaries."""
    sleep_source_counts = (
        inspection.sleep_stage_counts
        if inspection.sleep_stage_counts
        else inspection.event_name_counts
    )
    mapped_sleep_stage_counts = {
        stage.value: count
        for stage, count in map_shhs_xml_sleep_label_counts(
            sleep_source_counts,
            unknown_policy="ignore",
        ).items()
    }
    mapped_respiratory_event_counts = {
        event_type.value: count
        for event_type, count in map_shhs_respiratory_event_counts(
            inspection.event_name_counts,
            unknown_policy="ignore",
        ).items()
    }

    return SHHSAnnotationSummary(
        path=inspection.path,
        root_tag=inspection.root_tag,
        epoch_length_seconds=inspection.epoch_length_seconds,
        scored_event_count=inspection.scored_event_count,
        sleep_stage_count=inspection.sleep_stage_count,
        event_type_counts=inspection.event_type_counts,
        signal_counts=inspection.signal_counts,
        mapped_sleep_stage_counts=mapped_sleep_stage_counts,
        mapped_respiratory_event_counts=mapped_respiratory_event_counts,
    )


def _stringify_paths(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _stringify_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(item) for item in value]
    return value
