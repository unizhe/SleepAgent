"""SHHS respiratory-event extraction and fixed-window label construction."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from sleepagent.preprocessing.shhs_annotations import SHHSAnnotationError
from sleepagent.preprocessing.shhs_label_mapping import (
    UnknownSHHSLabelPolicy,
    is_ignored_shhs_respiratory_event_label,
    map_shhs_respiratory_event_label,
)
from sleepagent.schemas import RespiratoryEvent, RespiratoryEventType


SHHS_RESPIRATORY_WINDOW_MANIFEST_SCHEMA_VERSION = "stage5.respiratory_windows_manifest.v1"
DEFAULT_RESPIRATORY_WINDOW_SECONDS = 30.0
DEFAULT_RESPIRATORY_WINDOW_STRIDE_SECONDS = DEFAULT_RESPIRATORY_WINDOW_SECONDS
DEFAULT_MIN_RESPIRATORY_EVENT_OVERLAP_SECONDS = 1.0
DEFAULT_NORMAL_EXCLUSION_BUFFER_SECONDS = 30.0
DEFAULT_RESPIRATORY_EVENT_CONFIDENCE = 1.0
RESPIRATORY_LABEL_CONFLICT_RULE = "largest_abnormal_overlap_seconds_tie_suspected_apnea"
RESPIRATORY_NORMAL_RULE = "no_abnormal_overlap_and_outside_abnormal_event_buffer"
_ABNORMAL_LABEL_PRIORITY = (
    RespiratoryEventType.SUSPECTED_APNEA,
    RespiratoryEventType.HYPOPNEA,
)
_EXCLUSION_NEAR_ABNORMAL_EVENT = "near_abnormal_event"


@dataclass(frozen=True)
class SHHSRespiratoryEventSequence:
    """Mapped respiratory events extracted from one SHHS XML annotation file."""

    path: Path
    events: list[RespiratoryEvent]
    scored_event_count: int
    mapped_event_count: int
    ignored_event_count: int
    target_label_counts: dict[RespiratoryEventType, int]
    ignored_label_counts: dict[str, int]
    unknown_label_counts: dict[str, int]


@dataclass(frozen=True)
class SHHSRespiratoryTrainingWindow:
    """One fixed-duration respiratory training window and its target label."""

    start_second: float
    duration_seconds: float
    label: RespiratoryEventType
    overlap_seconds_by_label: dict[RespiratoryEventType, float]
    source_event_count: int
    is_included_in_training: bool = True
    exclusion_reason: str | None = None

    @property
    def end_second(self) -> float:
        return self.start_second + self.duration_seconds


@dataclass(frozen=True)
class SHHSRespiratoryTrainingWindowSequence:
    """Respiratory training windows built from one SHHS XML annotation file."""

    path: Path
    recording_duration_seconds: float
    window_duration_seconds: float
    stride_seconds: float
    minimum_event_overlap_seconds: float
    normal_exclusion_buffer_seconds: float
    windows: list[SHHSRespiratoryTrainingWindow]
    event_sequence: SHHSRespiratoryEventSequence

    @property
    def class_counts(self) -> dict[RespiratoryEventType, int]:
        counts: Counter[RespiratoryEventType] = Counter(
            window.label for window in self.windows if window.is_included_in_training
        )
        return {
            label: counts[label]
            for label in RespiratoryEventType
            if counts[label] > 0
        }

    @property
    def excluded_window_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter(
            window.exclusion_reason
            for window in self.windows
            if not window.is_included_in_training and window.exclusion_reason is not None
        )
        return dict(sorted(counts.items()))


@dataclass(frozen=True)
class SHHSRespiratoryWindowManifest:
    """Stage 5 XML-only respiratory window label manifest contract."""

    schema_version: str
    generated_at: datetime
    source_xml_path: Path
    recording_duration_seconds: float
    window_duration_seconds: float
    stride_seconds: float
    minimum_event_overlap_seconds: float
    normal_exclusion_buffer_seconds: float
    label_conflict_rule: str
    normal_rule: str
    unknown_policy: UnknownSHHSLabelPolicy
    scored_event_count: int
    mapped_event_count: int
    ignored_event_count: int
    target_label_counts: dict[RespiratoryEventType, int]
    ignored_label_counts: dict[str, int]
    unknown_label_counts: dict[str, int]
    total_window_count: int
    included_window_count: int
    excluded_window_count: int
    included_class_counts: dict[RespiratoryEventType, int]
    excluded_window_counts: dict[str, int]
    warning_messages: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _stringify_manifest_payload(payload)


def extract_shhs_respiratory_event_sequence(
    xml_path: str | Path,
    *,
    unknown_policy: UnknownSHHSLabelPolicy = "ignore",
) -> SHHSRespiratoryEventSequence:
    """Extract mapped respiratory events from an SHHS XML annotation file."""
    path = _resolve_xml_path(xml_path)
    root = _parse_xml(path)

    events: list[RespiratoryEvent] = []
    scored_event_count = 0
    ignored_label_counts: Counter[str] = Counter()
    unknown_label_counts: Counter[str] = Counter()
    target_label_counts: Counter[RespiratoryEventType] = Counter()
    for scored_event in root.findall(".//ScoredEvents/ScoredEvent"):
        scored_event_count += 1
        label = _first_available_child_text(scored_event, ("EventConcept", "Name"))
        if label is None:
            if unknown_policy == "raise":
                raise SHHSAnnotationError("Respiratory event is missing EventConcept/Name.")
            unknown_label_counts["<missing>"] += 1
            continue
        label_key = _primary_xml_label(label)
        event_type = map_shhs_respiratory_event_label(
            label,
            unknown_policy=unknown_policy,
        )
        if event_type is None:
            if is_ignored_shhs_respiratory_event_label(label):
                ignored_label_counts[label_key] += 1
            else:
                unknown_label_counts[label_key] += 1
            continue

        start_second = _parse_required_float(
            _first_available_child_text(scored_event, ("Start",)),
            "Start",
        )
        duration_seconds = _parse_required_float(
            _first_available_child_text(scored_event, ("Duration",)),
            "Duration",
        )
        if start_second < 0:
            raise SHHSAnnotationError(
                f"Respiratory event Start must be >= 0, got {start_second}."
            )
        if duration_seconds <= 0:
            raise SHHSAnnotationError(
                f"Respiratory event Duration must be > 0, got {duration_seconds}."
            )
        events.append(
            RespiratoryEvent(
                start_second=start_second,
                duration_seconds=duration_seconds,
                event_type=event_type,
                confidence=DEFAULT_RESPIRATORY_EVENT_CONFIDENCE,
            )
        )
        target_label_counts[event_type] += 1

    return SHHSRespiratoryEventSequence(
        path=path,
        events=sorted(
            events,
            key=lambda event: (event.start_second, event.duration_seconds),
        ),
        scored_event_count=scored_event_count,
        mapped_event_count=len(events),
        ignored_event_count=sum(ignored_label_counts.values()) + sum(unknown_label_counts.values()),
        target_label_counts={
            label: target_label_counts[label]
            for label in RespiratoryEventType
            if target_label_counts[label] > 0
        },
        ignored_label_counts=dict(sorted(ignored_label_counts.items())),
        unknown_label_counts=dict(sorted(unknown_label_counts.items())),
    )


def build_shhs_respiratory_training_windows_from_xml(
    xml_path: str | Path,
    *,
    recording_duration_seconds: float | None = None,
    window_duration_seconds: float = DEFAULT_RESPIRATORY_WINDOW_SECONDS,
    stride_seconds: float = DEFAULT_RESPIRATORY_WINDOW_STRIDE_SECONDS,
    minimum_event_overlap_seconds: float = DEFAULT_MIN_RESPIRATORY_EVENT_OVERLAP_SECONDS,
    normal_exclusion_buffer_seconds: float = DEFAULT_NORMAL_EXCLUSION_BUFFER_SECONDS,
    unknown_policy: UnknownSHHSLabelPolicy = "ignore",
) -> SHHSRespiratoryTrainingWindowSequence:
    """Build fixed-duration respiratory labels from SHHS XML annotations."""
    path = _resolve_xml_path(xml_path)
    root = _parse_xml(path)
    duration_seconds = (
        _validate_positive_float(recording_duration_seconds, "recording_duration_seconds")
        if recording_duration_seconds is not None
        else _infer_recording_duration_seconds(root)
    )
    event_sequence = extract_shhs_respiratory_event_sequence(
        path,
        unknown_policy=unknown_policy,
    )
    windows = build_shhs_respiratory_training_windows(
        event_sequence.events,
        recording_duration_seconds=duration_seconds,
        window_duration_seconds=window_duration_seconds,
        stride_seconds=stride_seconds,
        minimum_event_overlap_seconds=minimum_event_overlap_seconds,
        normal_exclusion_buffer_seconds=normal_exclusion_buffer_seconds,
    )
    return SHHSRespiratoryTrainingWindowSequence(
        path=path,
        recording_duration_seconds=duration_seconds,
        window_duration_seconds=window_duration_seconds,
        stride_seconds=stride_seconds,
        minimum_event_overlap_seconds=minimum_event_overlap_seconds,
        normal_exclusion_buffer_seconds=normal_exclusion_buffer_seconds,
        windows=windows,
        event_sequence=event_sequence,
    )


def build_shhs_respiratory_training_windows(
    events: list[RespiratoryEvent],
    *,
    recording_duration_seconds: float,
    window_duration_seconds: float = DEFAULT_RESPIRATORY_WINDOW_SECONDS,
    stride_seconds: float = DEFAULT_RESPIRATORY_WINDOW_STRIDE_SECONDS,
    minimum_event_overlap_seconds: float = DEFAULT_MIN_RESPIRATORY_EVENT_OVERLAP_SECONDS,
    normal_exclusion_buffer_seconds: float = DEFAULT_NORMAL_EXCLUSION_BUFFER_SECONDS,
) -> list[SHHSRespiratoryTrainingWindow]:
    """Assign fixed windows to normal/hypopnea/suspected-apnea targets."""
    recording_seconds = _validate_positive_float(
        recording_duration_seconds,
        "recording_duration_seconds",
    )
    window_seconds = _validate_positive_float(
        window_duration_seconds,
        "window_duration_seconds",
    )
    stride = _validate_positive_float(stride_seconds, "stride_seconds")
    min_overlap = _validate_positive_float(
        minimum_event_overlap_seconds,
        "minimum_event_overlap_seconds",
    )
    normal_buffer = _validate_non_negative_float(
        normal_exclusion_buffer_seconds,
        "normal_exclusion_buffer_seconds",
    )
    if window_seconds > recording_seconds:
        raise ValueError("window_duration_seconds cannot exceed recording_duration_seconds.")

    windows: list[SHHSRespiratoryTrainingWindow] = []
    start_second = 0.0
    epsilon = 1e-9
    while start_second + window_seconds <= recording_seconds + epsilon:
        end_second = start_second + window_seconds
        overlap_seconds_by_label = _sum_event_overlaps_by_label(
            events,
            start_second=start_second,
            end_second=end_second,
        )
        label = _select_window_label(
            overlap_seconds_by_label,
            minimum_event_overlap_seconds=min_overlap,
        )
        exclusion_reason = None
        is_included = True
        if label == RespiratoryEventType.NORMAL_BREATHING:
            nearest_event_distance = _minimum_distance_to_events(
                events,
                start_second=start_second,
                end_second=end_second,
            )
            if nearest_event_distance is not None and nearest_event_distance <= normal_buffer:
                is_included = False
                exclusion_reason = _EXCLUSION_NEAR_ABNORMAL_EVENT
        source_event_count = sum(
            1
            for event in events
            if _interval_overlap_seconds(
                start_second,
                end_second,
                event.start_second,
                event.end_second,
            )
            > 0
        )
        windows.append(
            SHHSRespiratoryTrainingWindow(
                start_second=start_second,
                duration_seconds=window_seconds,
                label=label,
                overlap_seconds_by_label=overlap_seconds_by_label,
                source_event_count=source_event_count,
                is_included_in_training=is_included,
                exclusion_reason=exclusion_reason,
            )
        )
        start_second += stride

    if not windows:
        raise ValueError("No full respiratory windows could be generated.")
    return windows


def build_shhs_respiratory_window_manifest(
    sequence: SHHSRespiratoryTrainingWindowSequence,
    *,
    unknown_policy: UnknownSHHSLabelPolicy = "ignore",
) -> SHHSRespiratoryWindowManifest:
    """Build a JSON-safe Stage 5 XML label/window manifest summary."""
    total_window_count = len(sequence.windows)
    excluded_window_count = sum(
        1 for window in sequence.windows if not window.is_included_in_training
    )
    warning_messages: list[str] = []
    if sequence.event_sequence.unknown_label_counts:
        warning_messages.append(
            "Unknown SHHS respiratory XML labels were skipped; review "
            "unknown_label_counts before using these windows for training."
        )
    if excluded_window_count:
        warning_messages.append(
            "Some candidate normal windows were excluded because they are within "
            "the configured abnormal-event buffer."
        )
    return SHHSRespiratoryWindowManifest(
        schema_version=SHHS_RESPIRATORY_WINDOW_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        source_xml_path=sequence.path,
        recording_duration_seconds=sequence.recording_duration_seconds,
        window_duration_seconds=sequence.window_duration_seconds,
        stride_seconds=sequence.stride_seconds,
        minimum_event_overlap_seconds=sequence.minimum_event_overlap_seconds,
        normal_exclusion_buffer_seconds=sequence.normal_exclusion_buffer_seconds,
        label_conflict_rule=RESPIRATORY_LABEL_CONFLICT_RULE,
        normal_rule=RESPIRATORY_NORMAL_RULE,
        unknown_policy=unknown_policy,
        scored_event_count=sequence.event_sequence.scored_event_count,
        mapped_event_count=sequence.event_sequence.mapped_event_count,
        ignored_event_count=sequence.event_sequence.ignored_event_count,
        target_label_counts=sequence.event_sequence.target_label_counts,
        ignored_label_counts=sequence.event_sequence.ignored_label_counts,
        unknown_label_counts=sequence.event_sequence.unknown_label_counts,
        total_window_count=total_window_count,
        included_window_count=total_window_count - excluded_window_count,
        excluded_window_count=excluded_window_count,
        included_class_counts=sequence.class_counts,
        excluded_window_counts=sequence.excluded_window_counts,
        warning_messages=warning_messages,
    )


def build_shhs_respiratory_window_manifest_from_xml(
    xml_path: str | Path,
    *,
    recording_duration_seconds: float | None = None,
    window_duration_seconds: float = DEFAULT_RESPIRATORY_WINDOW_SECONDS,
    stride_seconds: float = DEFAULT_RESPIRATORY_WINDOW_STRIDE_SECONDS,
    minimum_event_overlap_seconds: float = DEFAULT_MIN_RESPIRATORY_EVENT_OVERLAP_SECONDS,
    normal_exclusion_buffer_seconds: float = DEFAULT_NORMAL_EXCLUSION_BUFFER_SECONDS,
    unknown_policy: UnknownSHHSLabelPolicy = "ignore",
) -> SHHSRespiratoryWindowManifest:
    """Build the Stage 5 XML label/window manifest directly from an XML file."""
    sequence = build_shhs_respiratory_training_windows_from_xml(
        xml_path,
        recording_duration_seconds=recording_duration_seconds,
        window_duration_seconds=window_duration_seconds,
        stride_seconds=stride_seconds,
        minimum_event_overlap_seconds=minimum_event_overlap_seconds,
        normal_exclusion_buffer_seconds=normal_exclusion_buffer_seconds,
        unknown_policy=unknown_policy,
    )
    return build_shhs_respiratory_window_manifest(
        sequence,
        unknown_policy=unknown_policy,
    )


def _sum_event_overlaps_by_label(
    events: list[RespiratoryEvent],
    *,
    start_second: float,
    end_second: float,
) -> dict[RespiratoryEventType, float]:
    overlaps = {label: 0.0 for label in RespiratoryEventType}
    for event in events:
        overlap_seconds = _interval_overlap_seconds(
            start_second,
            end_second,
            event.start_second,
            event.end_second,
        )
        if overlap_seconds > 0:
            overlaps[event.event_type] += overlap_seconds
    return {label: seconds for label, seconds in overlaps.items() if seconds > 0}


def _select_window_label(
    overlap_seconds_by_label: dict[RespiratoryEventType, float],
    *,
    minimum_event_overlap_seconds: float,
) -> RespiratoryEventType:
    abnormal_candidates = [
        label
        for label in _ABNORMAL_LABEL_PRIORITY
        if overlap_seconds_by_label.get(label, 0.0) >= minimum_event_overlap_seconds
    ]
    if abnormal_candidates:
        return max(
            abnormal_candidates,
            key=lambda label: (
                overlap_seconds_by_label[label],
                -_ABNORMAL_LABEL_PRIORITY.index(label),
            ),
        )
    return RespiratoryEventType.NORMAL_BREATHING


def _interval_overlap_seconds(
    start_a: float,
    end_a: float,
    start_b: float,
    end_b: float,
) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def _minimum_distance_to_events(
    events: list[RespiratoryEvent],
    *,
    start_second: float,
    end_second: float,
) -> float | None:
    if not events:
        return None
    distances = []
    for event in events:
        if _interval_overlap_seconds(
            start_second,
            end_second,
            event.start_second,
            event.end_second,
        ) > 0:
            distances.append(0.0)
        elif end_second <= event.start_second:
            distances.append(event.start_second - end_second)
        else:
            distances.append(start_second - event.end_second)
    return min(distances)


def _infer_recording_duration_seconds(root: ElementTree.Element) -> float:
    epoch_length_seconds = _parse_optional_float(_find_first_text(root, "EpochLength"))
    sleep_stage_count = len(root.findall(".//SleepStages/SleepStage"))
    if epoch_length_seconds is not None and sleep_stage_count > 0:
        return _validate_positive_float(
            sleep_stage_count * epoch_length_seconds,
            "recording_duration_seconds",
        )

    max_event_end = 0.0
    for event in root.findall(".//ScoredEvents/ScoredEvent"):
        start_second = _parse_optional_float(_first_available_child_text(event, ("Start",)))
        duration_seconds = _parse_optional_float(
            _first_available_child_text(event, ("Duration",))
        )
        if start_second is None or duration_seconds is None:
            continue
        max_event_end = max(max_event_end, start_second + duration_seconds)
    if max_event_end > 0:
        return max_event_end
    raise SHHSAnnotationError(
        "Could not infer recording duration from SHHS XML; pass recording_duration_seconds."
    )


def _resolve_xml_path(xml_path: str | Path) -> Path:
    path = Path(xml_path).expanduser().resolve()
    if not path.exists():
        raise SHHSAnnotationError(f"SHHS respiratory XML not found: {path}")
    if not path.is_file():
        raise SHHSAnnotationError(f"SHHS respiratory XML path is not a file: {path}")
    if path.suffix.lower() != ".xml":
        raise SHHSAnnotationError(f"Expected an .xml file, got: {path}")
    return path


def _parse_xml(path: Path) -> ElementTree.Element:
    try:
        return ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise SHHSAnnotationError(f"Could not parse SHHS respiratory XML: {path}") from exc


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


def _parse_required_float(value: str | None, field_name: str) -> float:
    if value is None:
        raise SHHSAnnotationError(f"Respiratory event is missing {field_name}.")
    parsed = _parse_optional_float(value)
    if parsed is None:
        raise SHHSAnnotationError(f"Respiratory event is missing {field_name}.")
    return parsed


def _parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SHHSAnnotationError(f"Expected numeric SHHS respiratory value, got {value!r}.") from exc


def _validate_positive_float(value: float | None, field_name: str) -> float:
    if value is None:
        raise ValueError(f"{field_name} must be provided.")
    numeric = float(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return numeric


def _validate_non_negative_float(value: float | None, field_name: str) -> float:
    if value is None:
        raise ValueError(f"{field_name} must be provided.")
    numeric = float(value)
    if numeric < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0.")
    return numeric


def _primary_xml_label(label: object) -> str:
    if isinstance(label, str):
        primary = label.split("|", maxsplit=1)[0].strip()
        return primary or label.strip()
    return str(label)


def _stringify_manifest_payload(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, RespiratoryEventType):
        return value.value
    if isinstance(value, dict):
        return {
            _stringify_manifest_payload(key): _stringify_manifest_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_stringify_manifest_payload(item) for item in value]
    return value


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", maxsplit=1)[1]
    return tag
