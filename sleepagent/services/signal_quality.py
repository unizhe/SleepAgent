from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sleepagent.schemas import AnalysisNodeResult, AnalysisNodeStatus
from sleepagent.services.yasa_runner import EDFSignalInfo


def build_signal_quality_result(
    edf_info: EDFSignalInfo,
    *,
    eeg_channel: str,
    eog_channel: str | None = None,
    emg_channel: str | None = None,
    respiratory_channels: Iterable[str] = (),
    source_paths: dict[str, str] | None = None,
) -> AnalysisNodeResult:
    """Build a conservative EDF header quality check for a real analysis run."""
    required_channels = _compact_channel_names(
        [eeg_channel, eog_channel, emg_channel, *respiratory_channels]
    )
    available_channels = set(edf_info.channel_names)
    missing_channels = [
        channel for channel in required_channels if channel not in available_channels
    ]
    warnings: list[str] = []
    if missing_channels:
        warnings.append(
            "Requested channel(s) missing from EDF header: "
            + ", ".join(missing_channels)
        )
    if edf_info.duration_seconds <= 0:
        warnings.append("EDF duration is not positive.")
    if edf_info.sampling_rate_hz <= 0:
        warnings.append("EDF sampling rate is not positive.")

    status = (
        AnalysisNodeStatus.COMPLETED
        if not warnings
        else AnalysisNodeStatus.SKIPPED_WITH_WARNING
    )
    return AnalysisNodeResult(
        name="QualityCheck",
        status=status,
        payload={
            "channel_names": edf_info.channel_names,
            "requested_channels": required_channels,
            "missing_channels": missing_channels,
            "duration_seconds": edf_info.duration_seconds,
            "sampling_rate_hz": edf_info.sampling_rate_hz,
            "n_samples": edf_info.n_samples,
        },
        warnings=warnings,
        caveats=[
            "Quality check reads EDF header metadata only; it does not validate signal morphology."
        ],
        source_paths=source_paths or {"edf": edf_info.path},
        metrics={
            "duration_seconds": edf_info.duration_seconds,
            "sampling_rate_hz": edf_info.sampling_rate_hz,
            "channel_count": len(edf_info.channel_names),
            "missing_channel_count": len(missing_channels),
        },
        generated_at=datetime.now(timezone.utc),
    )


def build_failed_quality_result(
    *,
    error: str,
    source_paths: dict[str, str] | None = None,
) -> AnalysisNodeResult:
    return AnalysisNodeResult(
        name="QualityCheck",
        status=AnalysisNodeStatus.FAILED,
        warnings=[error],
        caveats=["Real analysis cannot continue without readable EDF metadata."],
        source_paths=source_paths or {},
        generated_at=datetime.now(timezone.utc),
        error=error,
    )


def _compact_channel_names(values: Iterable[str | None]) -> list[str]:
    channels: list[str] = []
    for value in values:
        if value is None:
            continue
        channel = value.strip()
        if channel:
            channels.append(channel)
    return channels
