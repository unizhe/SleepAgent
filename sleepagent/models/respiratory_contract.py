from dataclasses import dataclass

from sleepagent.schemas import RespiratoryEventType


DEFAULT_RESPIRATORY_WINDOW_SECONDS = 30.0
DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ = 125.0
DEFAULT_RESPIRATORY_INPUT_CHANNELS = 2
DEFAULT_RESPIRATORY_CNN_CHANNELS = (16, 32)
DEFAULT_RESPIRATORY_KERNEL_SIZE = 7
DEFAULT_RESPIRATORY_LSTM_HIDDEN_SIZE = 32
DEFAULT_RESPIRATORY_LSTM_LAYERS = 1
DEFAULT_RESPIRATORY_DROPOUT = 0.0

DEFAULT_RESPIRATORY_CLASS_ORDER: tuple[RespiratoryEventType, ...] = (
    RespiratoryEventType.NORMAL_BREATHING,
    RespiratoryEventType.HYPOPNEA,
    RespiratoryEventType.SUSPECTED_APNEA,
)


@dataclass(frozen=True)
class RespiratoryCnnBiLstmConfig:
    """Minimal Stage 4 tensor and architecture contract for respiratory windows.

    Input tensors are channel-first 1D windows with shape
    ``(batch_size, input_channels, samples)``. The model emits raw class logits
    with shape ``(batch_size, num_classes)`` using ``class_order``.
    """

    input_channels: int = DEFAULT_RESPIRATORY_INPUT_CHANNELS
    window_duration_seconds: float = DEFAULT_RESPIRATORY_WINDOW_SECONDS
    sampling_rate_hz: float = DEFAULT_RESPIRATORY_SAMPLING_RATE_HZ
    cnn_channels: tuple[int, ...] = DEFAULT_RESPIRATORY_CNN_CHANNELS
    kernel_size: int = DEFAULT_RESPIRATORY_KERNEL_SIZE
    lstm_hidden_size: int = DEFAULT_RESPIRATORY_LSTM_HIDDEN_SIZE
    lstm_layers: int = DEFAULT_RESPIRATORY_LSTM_LAYERS
    dropout: float = DEFAULT_RESPIRATORY_DROPOUT
    class_order: tuple[RespiratoryEventType, ...] = DEFAULT_RESPIRATORY_CLASS_ORDER

    def __post_init__(self) -> None:
        _validate_positive_int(self.input_channels, "input_channels")
        _validate_positive_float(self.window_duration_seconds, "window_duration_seconds")
        _validate_positive_float(self.sampling_rate_hz, "sampling_rate_hz")
        _validate_positive_int(self.kernel_size, "kernel_size")
        if self.kernel_size % 2 == 0:
            raise ValueError("kernel_size must be odd so padding preserves sequence length.")
        _validate_positive_int(self.lstm_hidden_size, "lstm_hidden_size")
        _validate_positive_int(self.lstm_layers, "lstm_layers")
        if self.dropout < 0 or self.dropout >= 1:
            raise ValueError("dropout must be greater than or equal to 0 and less than 1.")
        if not self.cnn_channels:
            raise ValueError("cnn_channels cannot be empty for the 1D-CNN skeleton.")
        for channel_count in self.cnn_channels:
            _validate_positive_int(channel_count, "cnn_channels")
        if not self.class_order:
            raise ValueError("class_order cannot be empty.")
        if len(set(self.class_order)) != len(self.class_order):
            raise ValueError("class_order cannot contain duplicate labels.")
        if self.expected_input_samples < self.minimum_input_samples:
            raise ValueError("default window is shorter than the CNN pooling stack requires.")

    @property
    def expected_input_samples(self) -> int:
        return int(round(self.window_duration_seconds * self.sampling_rate_hz))

    @property
    def minimum_input_samples(self) -> int:
        return 2 ** len(self.cnn_channels)

    @property
    def num_classes(self) -> int:
        return len(self.class_order)

    def input_shape(self, batch_size: int) -> tuple[int, int, int]:
        _validate_positive_int(batch_size, "batch_size")
        return (batch_size, self.input_channels, self.expected_input_samples)

    def output_shape(self, batch_size: int) -> tuple[int, int]:
        _validate_positive_int(batch_size, "batch_size")
        return (batch_size, self.num_classes)

    def class_index(self, event_type: RespiratoryEventType) -> int:
        return self.class_order.index(event_type)


def _validate_positive_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _validate_positive_float(value: float, field_name: str) -> None:
    if not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
