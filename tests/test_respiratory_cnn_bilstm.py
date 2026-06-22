import pytest

torch = pytest.importorskip("torch")

from sleepagent.models.respiratory_cnn_bilstm import RespiratoryCnnBiLstm
from sleepagent.models.respiratory_contract import RespiratoryCnnBiLstmConfig


def test_respiratory_cnn_bilstm_forward_smoke() -> None:
    torch.manual_seed(7)
    config = RespiratoryCnnBiLstmConfig()
    model = RespiratoryCnnBiLstm(config)
    respiratory_window = torch.randn(config.input_shape(batch_size=3))

    logits = model(respiratory_window)
    probabilities = torch.softmax(logits, dim=1)

    assert logits.shape == config.output_shape(batch_size=3)
    assert torch.is_floating_point(logits)
    assert torch.allclose(
        probabilities.sum(dim=1),
        torch.ones(3),
        atol=1e-6,
    )


def test_respiratory_cnn_bilstm_rejects_invalid_forward_input() -> None:
    config = RespiratoryCnnBiLstmConfig()
    model = RespiratoryCnnBiLstm(config)

    with pytest.raises(ValueError, match="shape"):
        model(torch.randn(config.input_channels, config.expected_input_samples))

    with pytest.raises(ValueError, match="channel"):
        model(torch.randn(2, config.input_channels + 1, config.expected_input_samples))

    with pytest.raises(ValueError, match="floating-point"):
        model(torch.ones(config.input_shape(batch_size=2), dtype=torch.int64))
