from __future__ import annotations

import importlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from sleepagent.models import YASASleepStagingResult, build_yasa_sleep_staging_result


STAGE3_YASA_SAMPLE_SCHEMA_VERSION = "stage3.yasa_staging_sample.v1"
DEFAULT_NUMBA_CACHE_DIR = Path(tempfile.gettempdir()) / "sleepagent-numba-cache"
DEFAULT_MPLCONFIG_DIR = Path(tempfile.gettempdir()) / "sleepagent-mplconfig"


@dataclass(frozen=True)
class EDFSignalInfo:
    """Lightweight EDF metadata for Stage 3 smoke runs."""

    path: str
    channel_names: list[str]
    sampling_rate_hz: float
    duration_seconds: float
    n_samples: int


@dataclass(frozen=True)
class YASARunnerResult:
    """Result of running YASA and adapting its output to SleepAgent schemas."""

    edf_info: EDFSignalInfo
    eeg_name: str
    eog_name: str | None
    emg_name: str | None
    staging: YASASleepStagingResult


def inspect_edf_signal(
    edf_path: str | Path,
    *,
    mne_module: ModuleType | None = None,
) -> EDFSignalInfo:
    """Read EDF header metadata without running sleep staging."""
    prepare_yasa_runtime_environment()
    path = _resolve_existing_edf_path(edf_path)
    mne = mne_module or _import_module("mne")
    raw = mne.io.read_raw_edf(str(path), preload=False, verbose="ERROR")
    try:
        return _edf_info_from_raw(path=path, raw=raw)
    finally:
        _close_raw_if_possible(raw)


def run_yasa_sleep_staging(
    edf_path: str | Path,
    *,
    eeg_name: str,
    eog_name: str | None = None,
    emg_name: str | None = None,
    metadata: dict[str, Any] | None = None,
    preload: bool = True,
    mne_module: ModuleType | None = None,
    yasa_module: ModuleType | None = None,
    yasa_src: str | Path | None = None,
) -> YASARunnerResult:
    """Run YASA SleepStaging on one authorized local EDF sample."""
    prepare_yasa_runtime_environment()
    path = _resolve_existing_edf_path(edf_path)
    mne = mne_module or _import_module("mne")
    yasa = yasa_module or _import_yasa(yasa_src=yasa_src)

    raw = mne.io.read_raw_edf(str(path), preload=preload, verbose="ERROR")
    try:
        edf_info = _edf_info_from_raw(path=path, raw=raw)
        _validate_requested_channels(
            available_channels=edf_info.channel_names,
            eeg_name=eeg_name,
            eog_name=eog_name,
            emg_name=emg_name,
        )
        sleep_staging = yasa.SleepStaging(
            raw,
            eeg_name=eeg_name,
            eog_name=_normalize_optional_channel(eog_name),
            emg_name=_normalize_optional_channel(emg_name),
            metadata=metadata,
        )
        yasa_hypnogram = sleep_staging.predict()
        staging = build_yasa_sleep_staging_result(
            yasa_hypnogram,
            recording_duration_seconds=edf_info.duration_seconds,
        )
        return YASARunnerResult(
            edf_info=edf_info,
            eeg_name=eeg_name,
            eog_name=_normalize_optional_channel(eog_name),
            emg_name=_normalize_optional_channel(emg_name),
            staging=staging,
        )
    finally:
        _close_raw_if_possible(raw)


def build_yasa_runner_payload(result: YASARunnerResult) -> dict[str, Any]:
    """Build a JSON-safe Stage 3 smoke output payload."""
    metrics = result.staging.sleep_staging_metrics
    return {
        "schema_version": STAGE3_YASA_SAMPLE_SCHEMA_VERSION,
        "edf": {
            "path": result.edf_info.path,
            "channel_names": result.edf_info.channel_names,
            "sampling_rate_hz": result.edf_info.sampling_rate_hz,
            "duration_seconds": result.edf_info.duration_seconds,
            "n_samples": result.edf_info.n_samples,
        },
        "channels": {
            "eeg": result.eeg_name,
            "eog": result.eog_name,
            "emg": result.emg_name,
        },
        "epoch_count": len(result.staging.epochs),
        "epochs": [epoch.model_dump(mode="json") for epoch in result.staging.epochs],
        "sleep_summary": result.staging.sleep_summary.model_dump(mode="json"),
        "sleep_staging_metrics": (
            metrics.model_dump(mode="json") if metrics is not None else None
        ),
        "notes": [
            "Stage 3 local YASA smoke output.",
            "EDF data are referenced by path only and are not embedded in this JSON.",
            "YASA predictions are assistive research outputs and require scorer review.",
        ],
    }


def prepare_yasa_runtime_environment() -> None:
    """Set writable cache directories before importing MNE/YASA dependencies."""
    numba_cache_dir = Path(
        os.environ.setdefault("NUMBA_CACHE_DIR", str(DEFAULT_NUMBA_CACHE_DIR))
    )
    mpl_config_dir = Path(
        os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_MPLCONFIG_DIR))
    )
    numba_cache_dir.mkdir(parents=True, exist_ok=True)
    mpl_config_dir.mkdir(parents=True, exist_ok=True)


def _edf_info_from_raw(path: Path, raw: Any) -> EDFSignalInfo:
    sampling_rate_hz = float(raw.info["sfreq"])
    n_samples = int(raw.n_times)
    return EDFSignalInfo(
        path=str(path),
        channel_names=list(raw.ch_names),
        sampling_rate_hz=sampling_rate_hz,
        duration_seconds=n_samples / sampling_rate_hz,
        n_samples=n_samples,
    )


def _validate_requested_channels(
    *,
    available_channels: list[str],
    eeg_name: str,
    eog_name: str | None,
    emg_name: str | None,
) -> None:
    requested = {
        "eeg": eeg_name,
        "eog": _normalize_optional_channel(eog_name),
        "emg": _normalize_optional_channel(emg_name),
    }
    for role, channel_name in requested.items():
        if channel_name is None:
            continue
        if channel_name not in available_channels:
            raise ValueError(
                f"Requested {role} channel {channel_name!r} was not found in EDF. "
                f"Available channels: {available_channels}"
            )


def _normalize_optional_channel(channel_name: str | None) -> str | None:
    if channel_name is None:
        return None
    stripped = channel_name.strip()
    return stripped or None


def _resolve_existing_edf_path(edf_path: str | Path) -> Path:
    path = Path(edf_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"EDF file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"EDF path is not a file: {path}")
    return path


def _import_yasa(*, yasa_src: str | Path | None = None) -> ModuleType:
    if yasa_src is not None:
        src_path = str(Path(yasa_src).expanduser().resolve())
        if src_path not in sys.path:
            sys.path.insert(0, src_path)
    return _import_module("yasa")


def _import_module(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Could not import {module_name!r}. Install the dependency or provide a local "
            "source path when supported by the caller."
        ) from exc


def _close_raw_if_possible(raw: Any) -> None:
    close = getattr(raw, "close", None)
    if callable(close):
        close()
