"""Lightweight SHHS XML annotation inspection helpers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


class SHHSAnnotationError(ValueError):
    """Raised when an SHHS XML annotation cannot be inspected safely."""


@dataclass(frozen=True)
class SHHSAnnotationInspection:
    """Small metadata summary for one local SHHS XML annotation file."""

    path: Path
    root_tag: str
    epoch_length_seconds: float | None
    scored_event_count: int
    sleep_stage_count: int
    event_type_counts: dict[str, int]
    event_name_counts: dict[str, int]
    signal_counts: dict[str, int]
    sleep_stage_counts: dict[str, int]


def inspect_shhs_annotation_xml(path: str | Path) -> SHHSAnnotationInspection:
    """Inspect a local SHHS XML annotation file without reading EDF contents."""
    xml_path = Path(path).expanduser().resolve()
    if not xml_path.exists():
        raise SHHSAnnotationError(f"SHHS annotation XML not found: {xml_path}")
    if not xml_path.is_file():
        raise SHHSAnnotationError(f"SHHS annotation XML path is not a file: {xml_path}")
    if xml_path.suffix.lower() != ".xml":
        raise SHHSAnnotationError(f"Expected an .xml file, got: {xml_path}")

    try:
        root = ElementTree.parse(xml_path).getroot()
    except ElementTree.ParseError as exc:
        raise SHHSAnnotationError(f"Could not parse SHHS annotation XML: {xml_path}") from exc

    scored_events = [
        element
        for element in root.findall(".//ScoredEvents/ScoredEvent")
        if _local_name(element.tag) == "ScoredEvent"
    ]
    sleep_stages = [
        _clean_text(element.text)
        for element in root.findall(".//SleepStages/SleepStage")
        if _clean_text(element.text)
    ]

    return SHHSAnnotationInspection(
        path=xml_path,
        root_tag=_local_name(root.tag),
        epoch_length_seconds=_parse_optional_float(_find_first_text(root, "EpochLength")),
        scored_event_count=len(scored_events),
        sleep_stage_count=len(sleep_stages),
        event_type_counts=_sorted_counts(
            _event_type_label(event) for event in scored_events
        ),
        event_name_counts=_sorted_counts(
            _event_name_label(event) for event in scored_events
        ),
        signal_counts=_sorted_counts(_signal_label(event) for event in scored_events),
        sleep_stage_counts=_sorted_counts(sleep_stages),
    )


def _event_type_label(scored_event: ElementTree.Element) -> str | None:
    return _first_available_child_text(scored_event, ("EventType",))


def _event_name_label(scored_event: ElementTree.Element) -> str | None:
    raw_label = _first_available_child_text(scored_event, ("EventConcept", "Name"))
    if raw_label is None:
        return None
    primary_label = raw_label.split("|", maxsplit=1)[0].strip()
    return primary_label or raw_label


def _signal_label(scored_event: ElementTree.Element) -> str | None:
    return _first_available_child_text(scored_event, ("SignalLocation", "Input"))


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


def _sorted_counts(values: object) -> dict[str, int]:
    counter: Counter[str] = Counter(
        str(value) for value in values if value is not None and str(value) != ""
    )
    return dict(sorted(counter.items()))


def _parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SHHSAnnotationError(f"Expected numeric EpochLength, got {value!r}.") from exc


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", maxsplit=1)[1]
    return tag
