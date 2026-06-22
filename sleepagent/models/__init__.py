"""Model components for sleep staging and respiratory event detection."""

from sleepagent.models.respiratory_contract import (
    DEFAULT_RESPIRATORY_CLASS_ORDER,
    DEFAULT_RESPIRATORY_INPUT_CHANNELS,
    DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ,
    DEFAULT_RESPIRATORY_WINDOW_SECONDS,
    RespiratoryCnnBiLstmConfig,
)
from sleepagent.models.yasa_staging import (
    DEFAULT_CONFIDENCE_WITHOUT_PROBA,
    DEFAULT_YASA_EPOCH_SECONDS,
    YASASleepStagingResult,
    build_sleep_epochs_from_yasa_labels,
    build_yasa_sleep_staging_result,
    extract_yasa_confidences,
    extract_yasa_stage_labels,
    infer_yasa_epoch_duration_seconds,
    summarize_sleep_epochs,
)


def __getattr__(name: str) -> object:
    if name == "RespiratoryCnnBiLstm":
        from sleepagent.models.respiratory_cnn_bilstm import RespiratoryCnnBiLstm

        return RespiratoryCnnBiLstm
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_CONFIDENCE_WITHOUT_PROBA",
    "DEFAULT_RESPIRATORY_CLASS_ORDER",
    "DEFAULT_RESPIRATORY_INPUT_CHANNELS",
    "DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ",
    "DEFAULT_RESPIRATORY_WINDOW_SECONDS",
    "DEFAULT_YASA_EPOCH_SECONDS",
    "RespiratoryCnnBiLstmConfig",
    "YASASleepStagingResult",
    "build_sleep_epochs_from_yasa_labels",
    "build_yasa_sleep_staging_result",
    "extract_yasa_confidences",
    "extract_yasa_stage_labels",
    "infer_yasa_epoch_duration_seconds",
    "summarize_sleep_epochs",
]
