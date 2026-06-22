from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sleepagent.metrics import compute_sleep_staging_metrics
from sleepagent.preprocessing import StageMappingSource, UnknownStagePolicy, map_yasa_sleep_stage
from sleepagent.schemas import SleepEpoch, SleepStage, SleepStageSummary, SleepStagingMetrics


DEFAULT_YASA_EPOCH_SECONDS = 30.0
DEFAULT_CONFIDENCE_WITHOUT_PROBA = 1.0


@dataclass(frozen=True)
class YASASleepStagingResult:
    """YASA sleep staging output normalized to SleepAgent schemas."""

    epochs: list[SleepEpoch]
    sleep_summary: SleepStageSummary
    sleep_staging_metrics: SleepStagingMetrics | None = None


def build_yasa_sleep_staging_result(
    yasa_output: object,
    *,
    reference_stages: Sequence[object] | None = None,
    reference_source: StageMappingSource = "auto",
    epoch_duration_seconds: float | None = None,
    recording_duration_seconds: float | None = None,
    unknown_policy: UnknownStagePolicy = "raise",
    default_confidence: float = DEFAULT_CONFIDENCE_WITHOUT_PROBA,
) -> YASASleepStagingResult:
    """Convert a YASA hypnogram-like output into internal staging schemas.

    ``yasa_output`` may be a real ``yasa.Hypnogram`` or a plain sequence using
    YASA's 5-stage convention: 0/Wake, 1-3/NREM, and 4/REM.
    """
    raw_labels = extract_yasa_stage_labels(yasa_output)
    epoch_seconds = infer_yasa_epoch_duration_seconds(
        yasa_output,
        epoch_duration_seconds=epoch_duration_seconds,
    )
    confidences = extract_yasa_confidences(
        yasa_output,
        expected_length=len(raw_labels),
        default_confidence=default_confidence,
    )
    epochs = build_sleep_epochs_from_yasa_labels(
        raw_labels,
        confidences=confidences,
        epoch_duration_seconds=epoch_seconds,
        unknown_policy=unknown_policy,
    )
    total_duration_seconds = (
        _validate_positive_float(recording_duration_seconds, "recording_duration_seconds")
        if recording_duration_seconds is not None
        else len(raw_labels) * epoch_seconds
    )
    sleep_summary = summarize_sleep_epochs(
        epochs,
        total_duration_seconds=total_duration_seconds,
    )
    metrics = None
    if reference_stages is not None:
        metrics = compute_sleep_staging_metrics(
            reference_stages,
            [epoch.stage for epoch in epochs],
            true_source=reference_source,
            pred_source="auto",
        )

    return YASASleepStagingResult(
        epochs=epochs,
        sleep_summary=sleep_summary,
        sleep_staging_metrics=metrics,
    )


def build_sleep_epochs_from_yasa_labels(
    labels: Sequence[object],
    *,
    confidences: Sequence[float] | None = None,
    epoch_duration_seconds: float = DEFAULT_YASA_EPOCH_SECONDS,
    unknown_policy: UnknownStagePolicy = "raise",
) -> list[SleepEpoch]:
    """Map YASA stage labels into ``SleepEpoch`` objects."""
    raw_labels = _as_list(labels, field_name="labels")
    if not raw_labels:
        raise ValueError("YASA sleep stage labels cannot be empty.")

    epoch_seconds = _validate_positive_float(epoch_duration_seconds, "epoch_duration_seconds")
    confidence_values = (
        list(confidences)
        if confidences is not None
        else [DEFAULT_CONFIDENCE_WITHOUT_PROBA] * len(raw_labels)
    )
    if len(confidence_values) != len(raw_labels):
        raise ValueError("confidences must have the same length as labels.")

    epochs: list[SleepEpoch] = []
    for index, raw_label in enumerate(raw_labels):
        stage = map_yasa_sleep_stage(raw_label, unknown_policy=unknown_policy)
        if stage is None:
            continue
        epochs.append(
            SleepEpoch(
                start_second=index * epoch_seconds,
                duration_seconds=epoch_seconds,
                stage=stage,
                confidence=_validate_confidence(confidence_values[index]),
            )
        )

    if not epochs:
        raise ValueError("YASA sleep stage labels did not contain any usable epochs.")
    return epochs


def summarize_sleep_epochs(
    epochs: Sequence[SleepEpoch],
    *,
    total_duration_seconds: float | None = None,
) -> SleepStageSummary:
    """Summarize internal sleep epochs using the MVP Wake/REM/NREM taxonomy."""
    epoch_list = list(epochs)
    if not epoch_list:
        raise ValueError("epochs cannot be empty.")

    observed_seconds = sum(epoch.duration_seconds for epoch in epoch_list)
    duration_seconds = (
        _validate_positive_float(total_duration_seconds, "total_duration_seconds")
        if total_duration_seconds is not None
        else observed_seconds
    )
    if duration_seconds < observed_seconds:
        raise ValueError("total_duration_seconds cannot be shorter than epoch durations.")

    wake_seconds = sum(
        epoch.duration_seconds for epoch in epoch_list if epoch.stage == SleepStage.WAKE
    )
    rem_seconds = sum(
        epoch.duration_seconds for epoch in epoch_list if epoch.stage == SleepStage.REM
    )
    nrem_seconds = sum(
        epoch.duration_seconds for epoch in epoch_list if epoch.stage == SleepStage.NREM
    )
    sleep_seconds = rem_seconds + nrem_seconds

    return SleepStageSummary(
        total_recording_minutes=round(duration_seconds / 60, 2),
        total_sleep_time_minutes=round(sleep_seconds / 60, 2),
        wake_minutes=round(wake_seconds / 60, 2),
        rem_minutes=round(rem_seconds / 60, 2),
        nrem_minutes=round(nrem_seconds / 60, 2),
        sleep_efficiency_percent=round((sleep_seconds / duration_seconds) * 100, 2),
    )


def extract_yasa_stage_labels(yasa_output: object) -> list[object]:
    """Extract raw stage labels from a YASA hypnogram-like object or sequence."""
    raw_hypnogram = getattr(yasa_output, "hypno", yasa_output)
    labels = _as_list(raw_hypnogram, field_name="yasa_output")
    if not labels:
        raise ValueError("YASA output cannot be empty.")
    return labels


def extract_yasa_confidences(
    yasa_output: object,
    *,
    expected_length: int,
    default_confidence: float = DEFAULT_CONFIDENCE_WITHOUT_PROBA,
) -> list[float]:
    """Return per-epoch confidence from YASA probabilities when available."""
    if expected_length <= 0:
        raise ValueError("expected_length must be greater than 0.")

    proba = getattr(yasa_output, "proba", None)
    if proba is None:
        confidence = _validate_confidence(default_confidence)
        return [confidence] * expected_length

    confidence_values = _max_probability_per_row(proba)
    if len(confidence_values) != expected_length:
        raise ValueError("YASA probability rows must match the number of stage labels.")
    return [_validate_confidence(value) for value in confidence_values]


def infer_yasa_epoch_duration_seconds(
    yasa_output: object,
    *,
    epoch_duration_seconds: float | None = None,
) -> float:
    """Infer epoch duration from YASA metadata, defaulting to 30 seconds."""
    if epoch_duration_seconds is not None:
        return _validate_positive_float(epoch_duration_seconds, "epoch_duration_seconds")

    sampling_frequency = getattr(yasa_output, "sampling_frequency", None)
    if sampling_frequency is not None:
        sampling_frequency = _validate_positive_float(sampling_frequency, "sampling_frequency")
        return 1.0 / sampling_frequency

    freq = getattr(yasa_output, "freq", None)
    if isinstance(freq, str):
        return _parse_frequency_seconds(freq)

    return DEFAULT_YASA_EPOCH_SECONDS


def _max_probability_per_row(proba: object) -> list[float]:
    if hasattr(proba, "max"):
        try:
            row_max = proba.max(axis=1)
        except TypeError:
            row_max = None
        if row_max is not None:
            return [float(value) for value in _as_list(row_max, field_name="proba.max(axis=1)")]

    rows = _as_list(proba, field_name="proba")
    values: list[float] = []
    for row in rows:
        if isinstance(row, Mapping):
            row_values = list(row.values())
        else:
            row_values = _as_list(row, field_name="proba row")
        if not row_values:
            raise ValueError("YASA probability rows cannot be empty.")
        values.append(max(float(value) for value in row_values))
    return values


def _parse_frequency_seconds(freq: str) -> float:
    normalized = freq.strip().lower()
    if not normalized:
        raise ValueError("freq cannot be empty.")

    units = [
        ("seconds", 1.0),
        ("second", 1.0),
        ("secs", 1.0),
        ("sec", 1.0),
        ("s", 1.0),
        ("minutes", 60.0),
        ("minute", 60.0),
        ("mins", 60.0),
        ("min", 60.0),
        ("m", 60.0),
        ("hours", 3600.0),
        ("hour", 3600.0),
        ("hrs", 3600.0),
        ("hr", 3600.0),
        ("h", 3600.0),
    ]
    for suffix, multiplier in units:
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)].strip() or "1"
            return _validate_positive_float(float(number) * multiplier, "freq")
    return _validate_positive_float(float(normalized), "freq")


def _as_list(values: object, *, field_name: str) -> list[object]:
    if isinstance(values, (str, bytes)) or values is None:
        raise ValueError(f"{field_name} must be a non-string sequence.")

    if hasattr(values, "tolist"):
        return list(values.tolist())
    if hasattr(values, "to_list"):
        return list(values.to_list())
    if isinstance(values, Sequence):
        return list(values)

    try:
        return list(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field_name} must be sequence-like.") from exc


def _validate_confidence(value: float) -> float:
    confidence = float(value)
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence values must be between 0 and 1.")
    return confidence


def _validate_positive_float(value: float, field_name: str) -> float:
    numeric = float(value)
    if numeric <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return numeric
