"""SHHS respiratory EDF signal window extraction for Stage 5."""

from __future__ import annotations

import importlib
import hashlib
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from sleepagent.preprocessing.shhs_respiratory_events import (
    SHHSRespiratoryTrainingWindow,
    SHHSRespiratoryTrainingWindowSequence,
)
from sleepagent.schemas import RespiratoryEventType


SHHS_RESPIRATORY_SIGNAL_MANIFEST_SCHEMA_VERSION = "stage5.respiratory_signal_windows_manifest.v1"
SHHS_RESPIRATORY_DATASET_MANIFEST_SCHEMA_VERSION = "stage5.respiratory_npz_dataset_manifest.v1"
SHHS_RESPIRATORY_SPLIT_MANIFEST_SCHEMA_VERSION = "stage5.respiratory_dataset_split_manifest.v1"
DEFAULT_RESPIRATORY_SIGNAL_CHANNELS = ("THOR RES", "ABDO RES")
DEFAULT_RESPIRATORY_SPLIT_RATIOS = {"train": 0.7, "val": 0.15, "test": 0.15}
RESPIRATORY_DATASET_CLASS_ORDER = (
    RespiratoryEventType.NORMAL_BREATHING,
    RespiratoryEventType.HYPOPNEA,
    RespiratoryEventType.SUSPECTED_APNEA,
)
DEFAULT_NUMBA_CACHE_DIR = Path(tempfile.gettempdir()) / "sleepagent-numba-cache"
DEFAULT_MPLCONFIG_DIR = Path(tempfile.gettempdir()) / "sleepagent-mplconfig"


@dataclass(frozen=True)
class SHHSRespiratorySignalWindow:
    """One labeled respiratory signal window extracted from an EDF file."""

    start_second: float
    duration_seconds: float
    label: RespiratoryEventType
    is_included_in_training: bool
    exclusion_reason: str | None
    channel_names: tuple[str, ...]
    sampling_rate_hz: float
    start_sample: int
    stop_sample: int
    data: Any

    @property
    def n_samples(self) -> int:
        return self.stop_sample - self.start_sample


@dataclass(frozen=True)
class SHHSRespiratorySignalWindowSequence:
    """Labeled respiratory signal windows extracted from one EDF file."""

    edf_path: Path
    source_xml_path: Path
    channel_names: tuple[str, ...]
    sampling_rate_hz: float
    recording_duration_seconds: float
    samples_per_window: int
    windows: list[SHHSRespiratorySignalWindow]
    source_label_sequence: SHHSRespiratoryTrainingWindowSequence

    @property
    def included_class_counts(self) -> dict[RespiratoryEventType, int]:
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
class SHHSRespiratorySignalManifest:
    """JSON-safe manifest for Stage 5 respiratory signal windows."""

    schema_version: str
    generated_at: datetime
    edf_path: Path
    source_xml_path: Path
    channel_names: tuple[str, ...]
    sampling_rate_hz: float
    recording_duration_seconds: float
    total_window_count: int
    included_window_count: int
    excluded_window_count: int
    samples_per_window: int
    included_class_counts: dict[RespiratoryEventType, int]
    excluded_window_counts: dict[str, int]
    notes: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _stringify_manifest_payload(payload)


@dataclass(frozen=True)
class SHHSRespiratoryDatasetManifest:
    """JSON-safe manifest for one local Stage 5 respiratory NPZ dataset."""

    schema_version: str
    generated_at: datetime
    dataset_path: Path
    edf_path: Path
    source_xml_path: Path
    channel_names: tuple[str, ...]
    sampling_rate_hz: float
    samples_per_window: int
    included_only: bool
    window_count: int
    class_order: tuple[RespiratoryEventType, ...]
    class_counts: dict[RespiratoryEventType, int]
    arrays: dict[str, dict[str, Any]]
    notes: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _stringify_manifest_payload(payload)


@dataclass(frozen=True)
class SHHSRespiratorySplitRecord:
    """One dataset entry assigned to a Stage 5 split."""

    dataset_path: Path
    split: str
    window_count: int
    class_counts: dict[RespiratoryEventType, int]


@dataclass(frozen=True)
class SHHSRespiratoryDatasetSplitManifest:
    """JSON-safe record-level split manifest for Stage 5 datasets."""

    schema_version: str
    generated_at: datetime
    split_strategy: str
    seed: int
    split_ratios: dict[str, float]
    dataset_count: int
    split_counts: dict[str, int]
    window_counts_by_split: dict[str, int]
    class_counts_by_split: dict[str, dict[RespiratoryEventType, int]]
    records: list[SHHSRespiratorySplitRecord]
    warning_messages: list[str]
    notes: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return _stringify_manifest_payload(payload)


def extract_shhs_respiratory_signal_windows(
    edf_path: str | Path,
    label_sequence: SHHSRespiratoryTrainingWindowSequence,
    *,
    channel_names: tuple[str, ...] = DEFAULT_RESPIRATORY_SIGNAL_CHANNELS,
    max_windows: int | None = None,
    mne_module: ModuleType | None = None,
) -> SHHSRespiratorySignalWindowSequence:
    """Extract EDF respiratory signal windows aligned to Stage 5 labels."""
    prepare_respiratory_signal_runtime_environment()
    resolved_edf_path = _resolve_existing_edf_path(edf_path)
    requested_channels = _validate_channel_names(channel_names)
    if max_windows is not None and max_windows <= 0:
        raise ValueError("max_windows must be greater than 0 when provided.")

    mne = mne_module or _import_module("mne")
    raw = mne.io.read_raw_edf(str(resolved_edf_path), preload=True, verbose="ERROR")
    try:
        available_channels = list(raw.ch_names)
        _validate_requested_channels(
            available_channels=available_channels,
            requested_channels=requested_channels,
        )
        sampling_rate_hz = float(raw.info["sfreq"])
        recording_duration_seconds = int(raw.n_times) / sampling_rate_hz
        _validate_label_sequence_fits_recording(
            label_sequence,
            recording_duration_seconds=recording_duration_seconds,
        )
        samples_per_window = int(
            round(label_sequence.window_duration_seconds * sampling_rate_hz)
        )
        source_windows = label_sequence.windows
        if max_windows is not None:
            source_windows = source_windows[:max_windows]
        signal_windows = [
            _extract_signal_window(
                raw,
                label_window=label_window,
                channel_names=requested_channels,
                sampling_rate_hz=sampling_rate_hz,
            )
            for label_window in source_windows
        ]
        return SHHSRespiratorySignalWindowSequence(
            edf_path=resolved_edf_path,
            source_xml_path=label_sequence.path,
            channel_names=requested_channels,
            sampling_rate_hz=sampling_rate_hz,
            recording_duration_seconds=recording_duration_seconds,
            samples_per_window=samples_per_window,
            windows=signal_windows,
            source_label_sequence=label_sequence,
        )
    finally:
        _close_raw_if_possible(raw)


def build_shhs_respiratory_signal_manifest(
    sequence: SHHSRespiratorySignalWindowSequence,
) -> SHHSRespiratorySignalManifest:
    """Build a JSON-safe manifest without embedding raw signal arrays."""
    total_window_count = len(sequence.windows)
    excluded_window_count = sum(
        1 for window in sequence.windows if not window.is_included_in_training
    )
    return SHHSRespiratorySignalManifest(
        schema_version=SHHS_RESPIRATORY_SIGNAL_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        edf_path=sequence.edf_path,
        source_xml_path=sequence.source_xml_path,
        channel_names=sequence.channel_names,
        sampling_rate_hz=sequence.sampling_rate_hz,
        recording_duration_seconds=sequence.recording_duration_seconds,
        total_window_count=total_window_count,
        included_window_count=total_window_count - excluded_window_count,
        excluded_window_count=excluded_window_count,
        samples_per_window=sequence.samples_per_window,
        included_class_counts=sequence.included_class_counts,
        excluded_window_counts=sequence.excluded_window_counts,
        notes=[
            "Stage 5 respiratory signal window manifest.",
            "Raw EDF signal arrays are not embedded in this JSON payload.",
            "Raw and derived SHHS data must remain outside the code repository.",
        ],
    )


def write_shhs_respiratory_signal_dataset_npz(
    sequence: SHHSRespiratorySignalWindowSequence,
    output_path: str | Path,
    *,
    included_only: bool = True,
    dtype: str = "float32",
) -> SHHSRespiratoryDatasetManifest:
    """Write a local NPZ dataset for Stage 5 respiratory signal windows."""
    np = _import_numpy()
    selected_windows = [
        window
        for window in sequence.windows
        if window.is_included_in_training or not included_only
    ]
    if not selected_windows:
        raise ValueError("No signal windows selected for dataset writing.")

    x = np.stack([np.asarray(window.data, dtype=dtype) for window in selected_windows])
    expected_shape = (
        len(selected_windows),
        len(sequence.channel_names),
        sequence.samples_per_window,
    )
    if tuple(x.shape) != expected_shape:
        raise ValueError(
            f"Signal window array shape {tuple(x.shape)} does not match "
            f"expected {expected_shape}."
        )
    y = np.asarray(
        [_class_index(window.label) for window in selected_windows],
        dtype="int64",
    )
    start_seconds = np.asarray(
        [window.start_second for window in selected_windows],
        dtype="float64",
    )
    included_mask = np.asarray(
        [window.is_included_in_training for window in selected_windows],
        dtype="bool",
    )

    resolved_output_path = Path(output_path).expanduser().resolve()
    if resolved_output_path.suffix.lower() != ".npz":
        raise ValueError("Stage 5 respiratory dataset output path must end with .npz.")
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        resolved_output_path,
        x=x,
        y=y,
        start_seconds=start_seconds,
        included_mask=included_mask,
        class_order=np.asarray(
            [label.value for label in RESPIRATORY_DATASET_CLASS_ORDER],
            dtype="U32",
        ),
        channel_names=np.asarray(sequence.channel_names, dtype="U64"),
    )
    class_counts = _class_counts_for_windows(selected_windows)
    return SHHSRespiratoryDatasetManifest(
        schema_version=SHHS_RESPIRATORY_DATASET_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        dataset_path=resolved_output_path,
        edf_path=sequence.edf_path,
        source_xml_path=sequence.source_xml_path,
        channel_names=sequence.channel_names,
        sampling_rate_hz=sequence.sampling_rate_hz,
        samples_per_window=sequence.samples_per_window,
        included_only=included_only,
        window_count=len(selected_windows),
        class_order=RESPIRATORY_DATASET_CLASS_ORDER,
        class_counts=class_counts,
        arrays={
            "x": {"shape": list(x.shape), "dtype": str(x.dtype)},
            "y": {"shape": list(y.shape), "dtype": str(y.dtype)},
            "start_seconds": {
                "shape": list(start_seconds.shape),
                "dtype": str(start_seconds.dtype),
            },
            "included_mask": {
                "shape": list(included_mask.shape),
                "dtype": str(included_mask.dtype),
            },
        },
        notes=[
            "Stage 5 local respiratory NPZ dataset.",
            "This derived dataset must remain outside the code repository.",
            "Class indices follow class_order in this manifest.",
        ],
    )


def build_shhs_respiratory_dataset_split_manifest(
    dataset_manifests: list[SHHSRespiratoryDatasetManifest],
    *,
    seed: int = 42,
    split_ratios: dict[str, float] | None = None,
) -> SHHSRespiratoryDatasetSplitManifest:
    """Build a deterministic record-level train/val/test split manifest."""
    ratios = split_ratios or DEFAULT_RESPIRATORY_SPLIT_RATIOS
    _validate_split_ratios(ratios)
    if not dataset_manifests:
        raise ValueError("dataset_manifests cannot be empty.")

    sorted_manifests = sorted(
        dataset_manifests,
        key=lambda manifest: _stable_split_key(manifest.dataset_path, seed=seed),
    )
    split_names = _assign_record_level_splits(len(sorted_manifests), ratios)
    records = [
        SHHSRespiratorySplitRecord(
            dataset_path=manifest.dataset_path,
            split=split_name,
            window_count=manifest.window_count,
            class_counts=manifest.class_counts,
        )
        for manifest, split_name in zip(sorted_manifests, split_names)
    ]
    split_counts: Counter[str] = Counter(record.split for record in records)
    window_counts: Counter[str] = Counter()
    class_counts_by_split: dict[str, Counter[RespiratoryEventType]] = {
        split: Counter() for split in ("train", "val", "test")
    }
    for record in records:
        window_counts[record.split] += record.window_count
        for label, count in record.class_counts.items():
            class_counts_by_split[record.split][label] += count
    warning_messages = []
    if len(sorted_manifests) < 3:
        warning_messages.append(
            "Fewer than 3 record-level datasets are available; validation/test "
            "splits are empty and this split is for smoke testing only."
        )
    return SHHSRespiratoryDatasetSplitManifest(
        schema_version=SHHS_RESPIRATORY_SPLIT_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        split_strategy="record_level_stable_hash",
        seed=seed,
        split_ratios=dict(ratios),
        dataset_count=len(sorted_manifests),
        split_counts={split: split_counts.get(split, 0) for split in ("train", "val", "test")},
        window_counts_by_split={
            split: window_counts.get(split, 0) for split in ("train", "val", "test")
        },
        class_counts_by_split={
            split: {
                label: counter[label]
                for label in RESPIRATORY_DATASET_CLASS_ORDER
                if counter[label] > 0
            }
            for split, counter in class_counts_by_split.items()
        },
        records=records,
        warning_messages=warning_messages,
        notes=[
            "Stage 5 respiratory dataset split manifest.",
            "Splits are assigned at dataset/record level to reduce window leakage.",
            "Training code should read dataset_path entries rather than raw EDF files.",
        ],
    )


def prepare_respiratory_signal_runtime_environment() -> None:
    """Set writable cache directories before importing MNE dependencies."""
    numba_cache_dir = Path(
        os.environ.setdefault("NUMBA_CACHE_DIR", str(DEFAULT_NUMBA_CACHE_DIR))
    )
    mpl_config_dir = Path(
        os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_MPLCONFIG_DIR))
    )
    numba_cache_dir.mkdir(parents=True, exist_ok=True)
    mpl_config_dir.mkdir(parents=True, exist_ok=True)


def _extract_signal_window(
    raw: Any,
    *,
    label_window: SHHSRespiratoryTrainingWindow,
    channel_names: tuple[str, ...],
    sampling_rate_hz: float,
) -> SHHSRespiratorySignalWindow:
    start_sample = int(round(label_window.start_second * sampling_rate_hz))
    stop_sample = int(round(label_window.end_second * sampling_rate_hz))
    data = raw.get_data(
        picks=list(channel_names),
        start=start_sample,
        stop=stop_sample,
    )
    return SHHSRespiratorySignalWindow(
        start_second=label_window.start_second,
        duration_seconds=label_window.duration_seconds,
        label=label_window.label,
        is_included_in_training=label_window.is_included_in_training,
        exclusion_reason=label_window.exclusion_reason,
        channel_names=channel_names,
        sampling_rate_hz=sampling_rate_hz,
        start_sample=start_sample,
        stop_sample=stop_sample,
        data=data,
    )


def _validate_label_sequence_fits_recording(
    label_sequence: SHHSRespiratoryTrainingWindowSequence,
    *,
    recording_duration_seconds: float,
) -> None:
    if not label_sequence.windows:
        raise ValueError("label_sequence must contain at least one window.")
    last_window_end = max(window.end_second for window in label_sequence.windows)
    if last_window_end > recording_duration_seconds + 1e-6:
        raise ValueError(
            "Label windows extend beyond EDF recording duration: "
            f"{last_window_end} > {recording_duration_seconds}."
        )


def _validate_channel_names(channel_names: tuple[str, ...]) -> tuple[str, ...]:
    if not channel_names:
        raise ValueError("At least one respiratory signal channel is required.")
    normalized = tuple(channel.strip() for channel in channel_names if channel.strip())
    if len(normalized) != len(channel_names):
        raise ValueError("Respiratory signal channel names cannot be empty.")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Respiratory signal channel names cannot contain duplicates.")
    return normalized


def _validate_requested_channels(
    *,
    available_channels: list[str],
    requested_channels: tuple[str, ...],
) -> None:
    missing = [channel for channel in requested_channels if channel not in available_channels]
    if missing:
        raise ValueError(
            f"Requested respiratory channels were not found in EDF: {missing}. "
            f"Available channels: {available_channels}"
        )


def _resolve_existing_edf_path(edf_path: str | Path) -> Path:
    path = Path(edf_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"EDF file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"EDF path is not a file: {path}")
    if path.suffix.lower() != ".edf":
        raise ValueError(f"Expected an .edf file, got: {path}")
    return path


def _import_module(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Could not import {module_name!r}. Install the dependency or provide "
            "a fake module in tests."
        ) from exc


def _close_raw_if_possible(raw: Any) -> None:
    close = getattr(raw, "close", None)
    if callable(close):
        close()


def _class_index(label: RespiratoryEventType) -> int:
    return RESPIRATORY_DATASET_CLASS_ORDER.index(label)


def _class_counts_for_windows(
    windows: list[SHHSRespiratorySignalWindow],
) -> dict[RespiratoryEventType, int]:
    counts: Counter[RespiratoryEventType] = Counter(window.label for window in windows)
    return {
        label: counts[label]
        for label in RESPIRATORY_DATASET_CLASS_ORDER
        if counts[label] > 0
    }


def _assign_record_level_splits(
    dataset_count: int,
    split_ratios: dict[str, float],
) -> list[str]:
    if dataset_count < 3:
        return ["train"] * dataset_count
    val_count = max(1, round(dataset_count * split_ratios["val"]))
    test_count = max(1, round(dataset_count * split_ratios["test"]))
    if val_count + test_count >= dataset_count:
        val_count = 1
        test_count = 1
    train_count = dataset_count - val_count - test_count
    return ["train"] * train_count + ["val"] * val_count + ["test"] * test_count


def _validate_split_ratios(split_ratios: dict[str, float]) -> None:
    if set(split_ratios) != {"train", "val", "test"}:
        raise ValueError("split_ratios must contain exactly train, val, and test.")
    for split, ratio in split_ratios.items():
        if ratio < 0 or ratio > 1:
            raise ValueError(f"split ratio for {split!r} must be between 0 and 1.")
    if abs(sum(split_ratios.values()) - 1.0) > 1e-9:
        raise ValueError("split_ratios must sum to 1.0.")


def _stable_split_key(path: Path, *, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{path}".encode("utf-8")).hexdigest()


def _import_numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "Could not import 'numpy'. Install numpy to write Stage 5 NPZ datasets."
        ) from exc


def _stringify_manifest_payload(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, RespiratoryEventType):
        return value.value
    if isinstance(value, tuple):
        return [_stringify_manifest_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            _stringify_manifest_payload(key): _stringify_manifest_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_stringify_manifest_payload(item) for item in value]
    return value
