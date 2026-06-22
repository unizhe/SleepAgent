"""Explicit Stage 2 SHHS XML label mappings for MVP targets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from sleepagent.preprocessing.stage_mapping import (
    UnknownSleepStageError,
    map_shhs_sleep_stage,
)
from sleepagent.schemas import RespiratoryEventType, SleepStage


UnknownSHHSLabelPolicy = Literal["raise", "ignore"]


class UnknownSHHSRespiratoryEventError(ValueError):
    """Raised when an SHHS respiratory label cannot be mapped safely."""


_HYPOPNEA_LABELS = {
    "hypopnea",
    "obstructive hypopnea",
    "central hypopnea",
    "mixed hypopnea",
}

_SUSPECTED_APNEA_LABELS = {
    "apnea",
    "obstructive apnea",
    "obstructive apnoea",
    "central apnea",
    "central apnoea",
    "mixed apnea",
    "mixed apnoea",
}

_NORMAL_BREATHING_LABELS = {
    "normal",
    "normal breathing",
    "normal respiration",
    "no respiratory event",
}

_IGNORED_RESPIRATORY_LABELS = {
    "",
    "arousal",
    "arousal ()",
    "blood pressure artifact",
    "body temperature artifact",
    "bradycardia",
    "distal ph",
    "distal ph artifact",
    "etco2 artifact",
    "limb movement left",
    "limb movement right",
    "periodic breathing",
    "plm left",
    "plm right",
    "proximal ph artifact",
    "recording start time",
    "rem sleep",
    "rera",
    "respiratory artifact",
    "respiratory paradox",
    "spo2 artifact",
    "spo2 desaturation",
    "stage 1 sleep",
    "stage 2 sleep",
    "stage 3 sleep",
    "stage 4 sleep",
    "tachycardia",
    "tcco2 artifact",
    "unsure",
    "wake",
}


def map_shhs_xml_sleep_label(
    label: object,
    unknown_policy: UnknownSHHSLabelPolicy = "raise",
) -> SleepStage | None:
    """Map SHHS XML sleep labels or numeric stage codes to Wake/REM/NREM."""
    if unknown_policy not in {"raise", "ignore"}:
        raise ValueError("unknown_policy must be one of: raise, ignore.")

    candidate = _primary_label(label)
    try:
        return map_shhs_sleep_stage(candidate, unknown_policy=unknown_policy)
    except UnknownSleepStageError:
        if unknown_policy == "ignore":
            return None
        raise


def map_shhs_xml_sleep_label_counts(
    label_counts: Mapping[object, int],
    unknown_policy: UnknownSHHSLabelPolicy = "raise",
) -> dict[SleepStage, int]:
    """Aggregate SHHS XML sleep label counts into the MVP sleep taxonomy."""
    mapped_counts = {stage: 0 for stage in SleepStage}
    for label, count in label_counts.items():
        stage = map_shhs_xml_sleep_label(label, unknown_policy=unknown_policy)
        if stage is not None:
            mapped_counts[stage] += count
    return {stage: count for stage, count in mapped_counts.items() if count > 0}


def map_shhs_respiratory_event_label(
    label: object,
    unknown_policy: UnknownSHHSLabelPolicy = "raise",
) -> RespiratoryEventType | None:
    """Map an SHHS XML event label into the MVP respiratory event taxonomy."""
    if unknown_policy not in {"raise", "ignore"}:
        raise ValueError("unknown_policy must be one of: raise, ignore.")

    normalized = _normalize_event_label(_primary_label(label))
    if normalized in _NORMAL_BREATHING_LABELS:
        return RespiratoryEventType.NORMAL_BREATHING
    if normalized in _HYPOPNEA_LABELS:
        return RespiratoryEventType.HYPOPNEA
    if normalized in _SUSPECTED_APNEA_LABELS:
        return RespiratoryEventType.SUSPECTED_APNEA
    if normalized in _IGNORED_RESPIRATORY_LABELS:
        return _handle_unknown_respiratory_label(label, unknown_policy)
    return _handle_unknown_respiratory_label(label, unknown_policy)


def map_shhs_respiratory_event_counts(
    label_counts: Mapping[object, int],
    unknown_policy: UnknownSHHSLabelPolicy = "raise",
) -> dict[RespiratoryEventType, int]:
    """Aggregate SHHS XML event label counts into MVP respiratory targets."""
    mapped_counts = {event_type: 0 for event_type in RespiratoryEventType}
    for label, count in label_counts.items():
        event_type = map_shhs_respiratory_event_label(
            label,
            unknown_policy=unknown_policy,
        )
        if event_type is not None:
            mapped_counts[event_type] += count
    return {
        event_type: count
        for event_type, count in mapped_counts.items()
        if count > 0
    }


def is_ignored_shhs_respiratory_event_label(label: object) -> bool:
    """Return whether an SHHS respiratory label is an explicit non-target label."""
    return _normalize_event_label(_primary_label(label)) in _IGNORED_RESPIRATORY_LABELS


def _handle_unknown_respiratory_label(
    label: object,
    unknown_policy: UnknownSHHSLabelPolicy,
) -> RespiratoryEventType | None:
    if unknown_policy == "ignore":
        return None
    raise UnknownSHHSRespiratoryEventError(
        f"Cannot map SHHS respiratory event label {label!r}."
    )


def _primary_label(label: object) -> object:
    if isinstance(label, str):
        primary = label.split("|", maxsplit=1)[0].strip()
        return primary if primary else label
    return label


def _normalize_event_label(label: object) -> str:
    normalized = str(label).strip().lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = normalized.replace("/", " ")
    normalized = normalized.replace("(", " ").replace(")", " ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()
