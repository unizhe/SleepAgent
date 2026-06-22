from __future__ import annotations

try:
    import torch
    from torch import Tensor, nn
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without model extra.
    raise ModuleNotFoundError(
        "PyTorch is required for RespiratoryCnnBiLstm. "
        'Install the model extra with: python -m pip install -e ".[model]"'
    ) from exc

from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig


class RespiratoryCnnBiLstm(nn.Module):
    """Stage 4 1D-CNN + BiLSTM skeleton for respiratory event classification."""

    def __init__(self, config: RespiratoryCnnBiLstmConfig | None = None) -> None:
        super().__init__()
        self.config = config or RespiratoryCnnBiLstmConfig()

        cnn_layers: list[nn.Module] = []
        in_channels = self.config.input_channels
        for out_channels in self.config.cnn_channels:
            cnn_layers.extend(
                [
                    nn.Conv1d(
                        in_channels=in_channels,
                        out_channels=out_channels,
                        kernel_size=self.config.kernel_size,
                        padding=self.config.kernel_size // 2,
                    ),
                    nn.BatchNorm1d(out_channels),
                    nn.ReLU(),
                    nn.MaxPool1d(kernel_size=2),
                ]
            )
            in_channels = out_channels

        self.feature_extractor = nn.Sequential(*cnn_layers)
        self.sequence_model = nn.LSTM(
            input_size=in_channels,
            hidden_size=self.config.lstm_hidden_size,
            num_layers=self.config.lstm_layers,
            batch_first=True,
            bidirectional=True,
            dropout=self.config.dropout if self.config.lstm_layers > 1 else 0.0,
        )
        self.classifier = nn.Linear(
            self.config.lstm_hidden_size * 2,
            self.config.num_classes,
        )

    def forward(self, respiratory_window: Tensor) -> Tensor:
        """Return raw logits with shape ``(batch_size, num_classes)``."""
        self._validate_forward_input(respiratory_window)

        features = self.feature_extractor(respiratory_window)
        sequence = features.transpose(1, 2)
        sequence_output, _ = self.sequence_model(sequence)
        pooled = torch.mean(sequence_output, dim=1)
        return self.classifier(pooled)

    def _validate_forward_input(self, respiratory_window: Tensor) -> None:
        if respiratory_window.ndim != 3:
            raise ValueError(
                "respiratory_window must have shape "
                "(batch_size, input_channels, samples)."
            )
        if respiratory_window.shape[1] != self.config.input_channels:
            raise ValueError(
                "respiratory_window channel dimension does not match "
                f"config.input_channels={self.config.input_channels}."
            )
        if respiratory_window.shape[2] < self.config.minimum_input_samples:
            raise ValueError(
                "respiratory_window sample dimension is too short for the CNN "
                f"pooling stack; expected at least {self.config.minimum_input_samples}."
            )
        if not torch.is_floating_point(respiratory_window):
            raise ValueError("respiratory_window must be a floating-point tensor.")
