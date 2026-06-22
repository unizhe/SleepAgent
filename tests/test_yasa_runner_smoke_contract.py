import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleepagent.models import build_yasa_sleep_staging_result
from sleepagent.schemas import SleepStage
from sleepagent.services import (
    EDFSignalInfo,
    STAGE3_YASA_SAMPLE_SCHEMA_VERSION,
    YASARunnerResult,
    build_yasa_runner_payload,
    inspect_edf_signal,
    prepare_yasa_runtime_environment,
    run_yasa_sleep_staging,
)


class FakeRaw:
    def __init__(self) -> None:
        self.ch_names = ["EEG C4-A1", "EOG-L", "Chin EMG"]
        self.info = {"sfreq": 100.0}
        self.n_times = 9000
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeMNE:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.raw = FakeRaw()
        self.io = SimpleNamespace(read_raw_edf=self.read_raw_edf)

    def read_raw_edf(self, path: str, *, preload: bool, verbose: str) -> FakeRaw:
        self.calls.append({"path": path, "preload": preload, "verbose": verbose})
        return self.raw


class FakeYASAHypnogram:
    hypno = ["WAKE", "N2", "REM"]
    sampling_frequency = 1 / 30
    proba = [
        {"WAKE": 0.92, "N1": 0.02, "N2": 0.03, "N3": 0.01, "REM": 0.02},
        {"WAKE": 0.08, "N1": 0.10, "N2": 0.76, "N3": 0.04, "REM": 0.02},
        {"WAKE": 0.03, "N1": 0.02, "N2": 0.02, "N3": 0.01, "REM": 0.92},
    ]


class FakeSleepStaging:
    calls: list[dict[str, object]] = []

    def __init__(
        self,
        raw: FakeRaw,
        *,
        eeg_name: str,
        eog_name: str | None,
        emg_name: str | None,
        metadata: dict[str, object] | None,
    ) -> None:
        self.calls.append(
            {
                "raw": raw,
                "eeg_name": eeg_name,
                "eog_name": eog_name,
                "emg_name": emg_name,
                "metadata": metadata,
            }
        )

    def predict(self) -> FakeYASAHypnogram:
        return FakeYASAHypnogram()


@pytest.fixture(autouse=True)
def clear_fake_yasa_calls() -> None:
    FakeSleepStaging.calls.clear()


def test_inspect_edf_signal_reads_header_contract(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")
    fake_mne = FakeMNE()

    info = inspect_edf_signal(edf_path, mne_module=fake_mne)

    assert info.path == str(edf_path.resolve())
    assert info.channel_names == ["EEG C4-A1", "EOG-L", "Chin EMG"]
    assert info.sampling_rate_hz == pytest.approx(100.0)
    assert info.duration_seconds == pytest.approx(90.0)
    assert info.n_samples == 9000
    assert fake_mne.calls == [
        {"path": str(edf_path.resolve()), "preload": False, "verbose": "ERROR"}
    ]
    assert fake_mne.raw.closed is True


def test_prepare_yasa_runtime_environment_sets_writable_cache_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NUMBA_CACHE_DIR", raising=False)
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)

    prepare_yasa_runtime_environment()

    assert Path(os.environ["NUMBA_CACHE_DIR"]).is_dir()
    assert Path(os.environ["MPLCONFIGDIR"]).is_dir()


def test_run_yasa_sleep_staging_adapts_predictions_to_payload(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")
    fake_mne = FakeMNE()
    fake_yasa = SimpleNamespace(SleepStaging=FakeSleepStaging)

    result = run_yasa_sleep_staging(
        edf_path,
        eeg_name="EEG C4-A1",
        eog_name="EOG-L",
        emg_name="Chin EMG",
        metadata={"age": 70, "male": 1},
        mne_module=fake_mne,
        yasa_module=fake_yasa,
    )
    payload = build_yasa_runner_payload(result)

    assert fake_mne.calls == [
        {"path": str(edf_path.resolve()), "preload": True, "verbose": "ERROR"}
    ]
    assert FakeSleepStaging.calls[0]["eeg_name"] == "EEG C4-A1"
    assert FakeSleepStaging.calls[0]["eog_name"] == "EOG-L"
    assert FakeSleepStaging.calls[0]["emg_name"] == "Chin EMG"
    assert FakeSleepStaging.calls[0]["metadata"] == {"age": 70, "male": 1}
    assert [epoch.stage for epoch in result.staging.epochs] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.REM,
    ]
    assert payload["schema_version"] == STAGE3_YASA_SAMPLE_SCHEMA_VERSION
    assert payload["channels"] == {
        "eeg": "EEG C4-A1",
        "eog": "EOG-L",
        "emg": "Chin EMG",
    }
    assert payload["epoch_count"] == 3
    assert payload["sleep_summary"]["total_recording_minutes"] == pytest.approx(1.5)
    assert payload["sleep_summary"]["total_sleep_time_minutes"] == pytest.approx(1.0)
    assert payload["epochs"][0]["confidence"] == pytest.approx(0.92)
    assert fake_mne.raw.closed is True


def test_run_yasa_sleep_staging_rejects_missing_requested_channel(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")

    with pytest.raises(ValueError, match="was not found"):
        run_yasa_sleep_staging(
            edf_path,
            eeg_name="Missing EEG",
            mne_module=FakeMNE(),
            yasa_module=SimpleNamespace(SleepStaging=FakeSleepStaging),
        )

    assert FakeSleepStaging.calls == []


def test_cli_writes_json_without_real_edf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import scripts.run_yasa_sleep_staging_sample as script

    edf_path = tmp_path / "sample.edf"
    out_path = tmp_path / "stage3" / "sample_yasa_summary.json"
    edf_info = EDFSignalInfo(
        path=str(edf_path),
        channel_names=["EEG", "EOG"],
        sampling_rate_hz=100.0,
        duration_seconds=90.0,
        n_samples=9000,
    )
    staging = build_yasa_sleep_staging_result(
        ["WAKE", "N2", "REM"],
        recording_duration_seconds=90.0,
    )
    runner_result = YASARunnerResult(
        edf_info=edf_info,
        eeg_name="EEG",
        eog_name="EOG",
        emg_name=None,
        staging=staging,
    )
    monkeypatch.setattr(script, "inspect_edf_signal", lambda edf: edf_info)
    monkeypatch.setattr(
        script,
        "run_yasa_sleep_staging",
        lambda *args, **kwargs: runner_result,
    )

    exit_code = script.main(
        [
            "--edf",
            str(edf_path),
            "--eeg",
            "EEG",
            "--eog",
            "EOG",
            "--out",
            str(out_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == STAGE3_YASA_SAMPLE_SCHEMA_VERSION
    assert payload["channels"]["eeg"] == "EEG"
    assert payload["channels"]["eog"] == "EOG"
    assert payload["channels"]["emg"] is None
    assert payload["epoch_count"] == 3
