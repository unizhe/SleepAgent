"""SHHS sleep-stage epoch extraction for Stage 3 YASA evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from sleepagent.preprocessing.shhs_annotations import SHHSAnnotationError
from sleepagent.preprocessing.shhs_label_mapping import map_shhs_xml_sleep_label
from sleepagent.schemas import SleepEpoch


DEFAULT_SHHS_SLEEP_STAGE_EPOCH_SECONDS = 30.0


@dataclass(frozen=True)
class SHHSSleepStageSequence:
    """Ground-truth sleep-stage epochs extracted from one SHHS XML file."""

    path: Path
    epoch_duration_seconds: float
    epochs: list[SleepEpoch]
    source: str


def extract_shhs_sleep_stage_sequence(xml_path: str | Path) -> SHHSSleepStageSequence:
    """Extract SHHS XML sleep-stage labels as 30-second ``SleepEpoch`` records."""
    path = _resolve_xml_path(xml_path)
    root = _parse_xml(path)
    epoch_duration_seconds = _find_epoch_length(root) or DEFAULT_SHHS_SLEEP_STAGE_EPOCH_SECONDS

    labels = _extract_sleep_stage_block_labels(root)
    source = "SleepStages/SleepStage"
    if not labels:
        labels = _extract_stage_event_labels(root)
        source = "ScoredEvents/ScoredEvent"
    if not labels:
        raise SHHSAnnotationError(f"No SHHS sleep-stage labels found in XML: {path}")

    epochs = build_sleep_epochs_from_shhs_labels(
        labels,
        epoch_duration_seconds=epoch_duration_seconds,
    )
    return SHHSSleepStageSequence(
        path=path,
        epoch_duration_seconds=epoch_duration_seconds,
        epochs=epochs,
        source=source,
    )


def build_sleep_epochs_from_shhs_labels(
    labels: Sequence[object],
    *,
    epoch_duration_seconds: float = DEFAULT_SHHS_SLEEP_STAGE_EPOCH_SECONDS,
) -> list[SleepEpoch]:
    """Map a SHHS epoch label sequence into internal ``SleepEpoch`` objects."""
    epoch_seconds = _validate_positive_float(epoch_duration_seconds, "epoch_duration_seconds")
    if not labels:
        raise ValueError("SHHS sleep-stage labels cannot be empty.")

    epochs: list[SleepEpoch] = []
    for index, label in enumerate(labels):
        stage = map_shhs_xml_sleep_label(label)
        if stage is None:
            continue
        epochs.append(
            SleepEpoch(
                start_second=index * epoch_seconds,
                duration_seconds=epoch_seconds,
                stage=stage,
                confidence=1.0,
            )
        )
    if not epochs:
        raise ValueError("SHHS sleep-stage labels did not contain any usable epochs.")
    return epochs


def _extract_sleep_stage_block_labels(root: ElementTree.Element) -> list[str]:
    return [
        text
        for text in (_clean_text(element.text) for element in root.findall(".//SleepStages/SleepStage"))
        if text is not None
    ]


def _extract_stage_event_labels(root: ElementTree.Element) -> list[str]:
    events: list[tuple[float, float, str]] = []
    epoch_seconds = _find_epoch_length(root) or DEFAULT_SHHS_SLEEP_STAGE_EPOCH_SECONDS
    for event in root.findall(".//ScoredEvents/ScoredEvent"):
        label = _first_available_child_text(event, ("EventConcept", "Name"))
        if label is None:
            continue
        try:
            map_shhs_xml_sleep_label(label)
        except ValueError:
            continue
        start = _parse_optional_float(_first_available_child_text(event, ("Start",)))
        duration = _parse_optional_float(_first_available_child_text(event, ("Duration",)))
        if start is None or duration is None:
            continue
        events.append((start, duration, label))

    labels: list[str] = []
    for start, duration, label in sorted(events):
        start_epoch = round(start / epoch_seconds)
        duration_epochs = max(round(duration / epoch_seconds), 1)
        while len(labels) < start_epoch:
            labels.append("0")
        labels.extend([label] * duration_epochs)
    return labels


def _resolve_xml_path(xml_path: str | Path) -> Path:
    path = Path(xml_path).expanduser().resolve()
    if not path.exists():
        raise SHHSAnnotationError(f"SHHS sleep-stage XML not found: {path}")
    if not path.is_file():
        raise SHHSAnnotationError(f"SHHS sleep-stage XML path is not a file: {path}")
    if path.suffix.lower() != ".xml":
        raise SHHSAnnotationError(f"Expected an .xml file, got: {path}")
    return path


def _parse_xml(path: Path) -> ElementTree.Element:
    try:
        return ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise SHHSAnnotationError(f"Could not parse SHHS sleep-stage XML: {path}") from exc


def _find_epoch_length(root: ElementTree.Element) -> float | None:
    value = _find_first_text(root, "EpochLength")
    return _parse_optional_float(value)


def _find_first_text(root: ElementTree.Element, child_name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == child_name:
            return _clean_text(element.text)
    return None


def _first_available_child_text(
    parent: ElementTree.Element,
    child_names: tuple[str, ...],
) -> str | None:
    allowed_names = set(child_names)
    for child in parent:
        if _local_name(child.tag) in allowed_names:
            text = _clean_text(child.text)
            if text:
                return text
    return None


def _parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SHHSAnnotationError(f"Expected numeric sleep-stage value, got {value!r}.") from exc


def _validate_positive_float(value: float, field_name: str) -> float:
    numeric = float(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return numeric


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", maxsplit=1)[1]
    return tag
