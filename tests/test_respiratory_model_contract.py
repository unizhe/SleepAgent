import pytest

from sleepagent.models import (
    DEFAULT_RESPIRATORY_CLASS_ORDER,
    DEFAULT_RESPIRATORY_INPUT_CHANNELS,
    DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ,
    DEFAULT_RESPIRATORY_WINDOW_SECONDS,
    RespiratoryCnnBiLstmConfig,
)
from sleepagent.schemas import RespiratoryEventType


def test_respiratory_model_tensor_contract_defaults() -> None:
    config = RespiratoryCnnBiLstmConfig()

    assert DEFAULT_RESPIRATORY_INPUT_CHANNELS == 2
    assert DEFAULT_RESPIRATORY_WINDOW_SECONDS == pytest.approx(30.0)
    assert DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ == pytest.approx(125.0)
    assert config.expected_input_samples == 3750
    assert config.input_shape(batch_size=4) == (4, 2, 3750)
    assert config.output_shape(batch_size=4) == (4, 3)
    assert DEFAULT_RESPIRATORY_CLASS_ORDER == (
        RespiratoryEventType.NORMAL_BREATHING,
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.SUSPECTED_APNEA,
    )
    assert config.class_index(RespiratoryEventType.HYPOPNEA) == 1


def test_respiratory_model_config_rejects_invalid_contracts() -> None:
    with pytest.raises(ValueError, match="input_channels"):
        RespiratoryCnnBiLstmConfig(input_channels=0)

    with pytest.raises(ValueError, match="kernel_size"):
        RespiratoryCnnBiLstmConfig(kernel_size=4)

    with pytest.raises(ValueError, match="duplicate"):
        RespiratoryCnnBiLstmConfig(
            class_order=(
                RespiratoryEventType.NORMAL_BREATHING,
                RespiratoryEventType.NORMAL_BREATHING,
            )
        )
