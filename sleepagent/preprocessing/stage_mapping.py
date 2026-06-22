from typing import Literal

from sleepagent.schemas import SleepStage


StageMappingSource = Literal["auto", "yasa", "shhs"]
UnknownStagePolicy = Literal["raise", "ignore"]


class UnknownSleepStageError(ValueError):
    """Raised when a raw sleep stage label cannot be mapped safely."""


_WAKE_ALIASES = {
    "w",
    "wake",
    "awake",
    "wakefulness",
    "stage w",
    "stage wake",
}

_REM_ALIASES = {
    "r",
    "rem",
    "rem sleep",
    "stage r",
    "stage rem",
    "rapid eye movement",
}

_NREM_ALIASES = {
    "n",
    "nrem",
    "non rem",
    "nonrem",
    "non rapid eye movement",
    "n1",
    "n2",
    "n3",
    "n4",
    "stage 1",
    "stage 1 sleep",
    "stage 2",
    "stage 2 sleep",
    "stage 3",
    "stage 3 sleep",
    "stage 4",
    "stage 4 sleep",
    "s1",
    "s2",
    "s3",
    "s4",
}

_IGNORED_ALIASES = {
    "",
    "?",
    "unknown",
    "unscored",
    "unsure",
    "artifact",
    "artefact",
    "movement",
    "movement time",
    "mt",
}

_YASA_NUMERIC_MAP = {
    0: SleepStage.WAKE,
    1: SleepStage.NREM,
    2: SleepStage.NREM,
    3: SleepStage.NREM,
    4: SleepStage.REM,
}

_SHHS_NUMERIC_MAP = {
    0: SleepStage.WAKE,
    1: SleepStage.NREM,
    2: SleepStage.NREM,
    3: SleepStage.NREM,
    4: SleepStage.NREM,
    5: SleepStage.REM,
}


def map_sleep_stage(
    label: object,
    source: StageMappingSource = "auto",
    unknown_policy: UnknownStagePolicy = "raise",
) -> SleepStage | None:
    """Map a raw sleep stage label into the MVP Wake/REM/NREM taxonomy."""
    if isinstance(label, SleepStage):
        return label

    if source not in {"auto", "yasa", "shhs"}:
        raise ValueError("source must be one of: auto, yasa, shhs.")
    if unknown_policy not in {"raise", "ignore"}:
        raise ValueError("unknown_policy must be one of: raise, ignore.")

    numeric_label = _coerce_integer_label(label)
    if numeric_label is not None:
        mapped = _map_numeric_stage(numeric_label, source=source)
        if mapped is not None:
            return mapped
        return _handle_unknown(label, source=source, unknown_policy=unknown_policy)

    if isinstance(label, str):
        normalized = _normalize_stage_label(label)
        if normalized in _IGNORED_ALIASES:
            return _handle_unknown(label, source=source, unknown_policy=unknown_policy)
        if normalized in _WAKE_ALIASES:
            return SleepStage.WAKE
        if normalized in _REM_ALIASES:
            return SleepStage.REM
        if normalized in _NREM_ALIASES:
            return SleepStage.NREM

    return _handle_unknown(label, source=source, unknown_policy=unknown_policy)


def map_sleep_stages(
    labels: list[object] | tuple[object, ...],
    source: StageMappingSource = "auto",
    unknown_policy: UnknownStagePolicy = "raise",
) -> list[SleepStage]:
    mapped: list[SleepStage] = []
    for label in labels:
        stage = map_sleep_stage(
            label,
            source=source,
            unknown_policy=unknown_policy,
        )
        if stage is not None:
            mapped.append(stage)
    return mapped


def map_yasa_sleep_stage(
    label: object,
    unknown_policy: UnknownStagePolicy = "raise",
) -> SleepStage | None:
    return map_sleep_stage(label, source="yasa", unknown_policy=unknown_policy)


def map_shhs_sleep_stage(
    label: object,
    unknown_policy: UnknownStagePolicy = "raise",
) -> SleepStage | None:
    return map_sleep_stage(label, source="shhs", unknown_policy=unknown_policy)


def _map_numeric_stage(label: int, source: StageMappingSource) -> SleepStage | None:
    if source == "yasa":
        return _YASA_NUMERIC_MAP.get(label)
    if source == "shhs":
        return _SHHS_NUMERIC_MAP.get(label)

    if label == 4:
        raise UnknownSleepStageError(
            "Numeric sleep stage 4 is ambiguous in auto mode. "
            "Use source='yasa' for REM or source='shhs' for NREM."
        )
    if label in {0, 1, 2, 3}:
        return _YASA_NUMERIC_MAP[label]
    if label == 5:
        return SleepStage.REM
    return None


def _coerce_integer_label(label: object) -> int | None:
    if isinstance(label, bool):
        return None
    if isinstance(label, int):
        return label
    if isinstance(label, float) and label.is_integer():
        return int(label)
    if isinstance(label, str):
        stripped = label.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _normalize_stage_label(label: str) -> str:
    normalized = label.strip().lower()
    for old, new in [
        ("_", " "),
        ("-", " "),
        ("/", " "),
        ("  ", " "),
    ]:
        while old in normalized:
            normalized = normalized.replace(old, new)
    return " ".join(normalized.split())


def _handle_unknown(
    label: object,
    source: StageMappingSource,
    unknown_policy: UnknownStagePolicy,
) -> SleepStage | None:
    if unknown_policy == "ignore":
        return None
    raise UnknownSleepStageError(
        f"Cannot map sleep stage label {label!r} using source={source!r}."
    )

