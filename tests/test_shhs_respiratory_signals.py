from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sleepagent.preprocessing import (
    DEFAULT_RESPIRATORY_SIGNAL_CHANNELS,
    RESPIRATORY_DATASET_CLASS_ORDER,
    SHHS_RESPIRATORY_DATASET_MANIFEST_SCHEMA_VERSION,
    SHHS_RESPIRATORY_SIGNAL_MANIFEST_SCHEMA_VERSION,
    SHHS_RESPIRATORY_SPLIT_MANIFEST_SCHEMA_VERSION,
    build_shhs_respiratory_dataset_split_manifest,
    build_shhs_respiratory_signal_manifest,
    build_shhs_respiratory_training_windows,
    extract_shhs_respiratory_signal_windows,
    write_shhs_respiratory_signal_dataset_npz,
)
from sleepagent.preprocessing.shhs_respiratory_signals import (
    SHHSRespiratoryDatasetManifest,
)
from sleepagent.schemas import RespiratoryEvent, RespiratoryEventType


class FakeRaw:
    def __init__(self) -> None:
        self.ch_names = ["THOR RES", "ABDO RES", "NEW AIR"]
        self.info = {"sfreq": 125.0}
        self.n_times = 15000
        self.closed = False
        self.get_data_calls: list[dict[str, object]] = []

    def get_data(self, *, picks: list[str], start: int, stop: int) -> list[list[float]]:
        self.get_data_calls.append({"picks": picks, "start": start, "stop": stop})
        return [
            [float(channel_index)] * (stop - start)
            for channel_index, _channel_name in enumerate(picks)
        ]

    def close(self) -> None:
        self.closed = True


class FakeMNE:
    def __init__(self) -> None:
        self.raw = FakeRaw()
        self.calls: list[dict[str, object]] = []
        self.io = SimpleNamespace(read_raw_edf=self.read_raw_edf)

    def read_raw_edf(self, path: str, *, preload: bool, verbose: str) -> FakeRaw:
        self.calls.append({"path": path, "preload": preload, "verbose": verbose})
        return self.raw


def test_extracts_signal_windows_aligned_to_label_windows(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")
    label_sequence = _label_sequence(tmp_path)
    fake_mne = FakeMNE()

    sequence = extract_shhs_respiratory_signal_windows(
        edf_path,
        label_sequence,
        mne_module=fake_mne,
    )
    manifest = build_shhs_respiratory_signal_manifest(sequence).to_json_dict()

    assert sequence.edf_path == edf_path.resolve()
    assert sequence.source_xml_path == label_sequence.path
    assert sequence.channel_names == DEFAULT_RESPIRATORY_SIGNAL_CHANNELS
    assert sequence.sampling_rate_hz == pytest.approx(125.0)
    assert sequence.samples_per_window == 3750
    assert [window.start_sample for window in sequence.windows] == [0, 3750, 7500]
    assert [window.stop_sample for window in sequence.windows] == [3750, 7500, 11250]
    assert [window.label for window in sequence.windows] == [
        RespiratoryEventType.NORMAL_BREATHING,
        RespiratoryEventType.HYPOPNEA,
        RespiratoryEventType.NORMAL_BREATHING,
    ]
    assert [window.is_included_in_training for window in sequence.windows] == [
        False,
        True,
        False,
    ]
    assert fake_mne.calls == [
        {"path": str(edf_path.resolve()), "preload": True, "verbose": "ERROR"}
    ]
    assert fake_mne.raw.get_data_calls[0] == {
        "picks": ["THOR RES", "ABDO RES"],
        "start": 0,
        "stop": 3750,
    }
    assert fake_mne.raw.closed is True
    assert manifest["schema_version"] == SHHS_RESPIRATORY_SIGNAL_MANIFEST_SCHEMA_VERSION
    assert manifest["channel_names"] == ["THOR RES", "ABDO RES"]
    assert manifest["total_window_count"] == 3
    assert manifest["included_window_count"] == 1
    assert manifest["excluded_window_count"] == 2
    assert manifest["samples_per_window"] == 3750
    assert manifest["included_class_counts"] == {"hypopnea": 1}
    assert manifest["excluded_window_counts"] == {"near_abnormal_event": 2}
    assert "Raw EDF signal arrays are not embedded" in " ".join(manifest["notes"])


def test_signal_extraction_rejects_missing_channel(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")

    with pytest.raises(ValueError, match="were not found"):
        extract_shhs_respiratory_signal_windows(
            edf_path,
            _label_sequence(tmp_path),
            channel_names=("MISSING",),
            mne_module=FakeMNE(),
        )


def test_signal_extraction_supports_smoke_window_limit(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")

    sequence = extract_shhs_respiratory_signal_windows(
        edf_path,
        _label_sequence(tmp_path),
        max_windows=2,
        mne_module=FakeMNE(),
    )

    assert len(sequence.windows) == 2


def test_writes_included_signal_windows_to_npz_dataset(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")
    sequence = extract_shhs_respiratory_signal_windows(
        edf_path,
        _label_sequence(tmp_path),
        mne_module=FakeMNE(),
    )

    manifest = write_shhs_respiratory_signal_dataset_npz(
        sequence,
        tmp_path / "processed" / "sample_resp_windows.npz",
    )
    payload = manifest.to_json_dict()

    assert payload["schema_version"] == SHHS_RESPIRATORY_DATASET_MANIFEST_SCHEMA_VERSION
    assert payload["included_only"] is True
    assert payload["window_count"] == 1
    assert payload["class_order"] == [
        event_type.value for event_type in RESPIRATORY_DATASET_CLASS_ORDER
    ]
    assert payload["class_counts"] == {"hypopnea": 1}
    assert payload["arrays"]["x"] == {"shape": [1, 2, 3750], "dtype": "float32"}
    assert payload["arrays"]["y"] == {"shape": [1], "dtype": "int64"}
    assert "code repository" in " ".join(payload["notes"])

    import numpy as np

    with np.load(payload["dataset_path"]) as dataset:
        assert dataset["x"].shape == (1, 2, 3750)
        assert dataset["y"].tolist() == [1]
        assert dataset["class_order"].tolist() == [
            "normal_breathing",
            "hypopnea",
            "suspected_apnea",
        ]
        assert dataset["channel_names"].tolist() == ["THOR RES", "ABDO RES"]


def test_dataset_writer_can_include_excluded_windows(tmp_path: Path) -> None:
    edf_path = tmp_path / "sample.edf"
    edf_path.write_bytes(b"fake-edf")
    sequence = extract_shhs_respiratory_signal_windows(
        edf_path,
        _label_sequence(tmp_path),
        mne_module=FakeMNE(),
    )

    manifest = write_shhs_respiratory_signal_dataset_npz(
        sequence,
        tmp_path / "all_windows.npz",
        included_only=False,
    )

    assert manifest.window_count == 3
    assert manifest.class_counts == {
        RespiratoryEventType.NORMAL_BREATHING: 2,
        RespiratoryEventType.HYPOPNEA: 1,
    }


def test_builds_record_level_split_manifest(tmp_path: Path) -> None:
    manifests = [
        _dataset_manifest(tmp_path, f"shhs1-{index:06d}", window_count=10 + index)
        for index in range(1, 6)
    ]

    split_manifest = build_shhs_respiratory_dataset_split_manifest(
        manifests,
        seed=7,
    )
    payload = split_manifest.to_json_dict()

    assert payload["schema_version"] == SHHS_RESPIRATORY_SPLIT_MANIFEST_SCHEMA_VERSION
    assert payload["split_strategy"] == "record_level_stable_hash"
    assert payload["seed"] == 7
    assert payload["dataset_count"] == 5
    assert payload["split_counts"] == {"train": 3, "val": 1, "test": 1}
    assert sum(payload["window_counts_by_split"].values()) == sum(
        manifest.window_count for manifest in manifests
    )
    assert payload["class_counts_by_split"]["train"]
    assert payload["records"] == build_shhs_respiratory_dataset_split_manifest(
        manifests,
        seed=7,
    ).to_json_dict()["records"]
    assert "record level" in " ".join(payload["notes"])


def test_single_record_split_manifest_warns_and_uses_train_only(tmp_path: Path) -> None:
    manifest = _dataset_manifest(tmp_path, "shhs1-200001", window_count=1015)

    split_manifest = build_shhs_respiratory_dataset_split_manifest([manifest])
    payload = split_manifest.to_json_dict()

    assert payload["split_counts"] == {"train": 1, "val": 0, "test": 0}
    assert payload["window_counts_by_split"] == {"train": 1015, "val": 0, "test": 0}
    assert payload["records"][0]["split"] == "train"
    assert payload["warning_messages"]


def test_split_manifest_rejects_invalid_ratios(tmp_path: Path) -> None:
    manifest = _dataset_manifest(tmp_path, "shhs1-200001", window_count=10)

    with pytest.raises(ValueError, match="sum to 1.0"):
        build_shhs_respiratory_dataset_split_manifest(
            [manifest],
            split_ratios={"train": 0.8, "val": 0.2, "test": 0.2},
        )


def _label_sequence(tmp_path: Path):
    from sleepagent.preprocessing.shhs_respiratory_events import (
        SHHSRespiratoryEventSequence,
        SHHSRespiratoryTrainingWindowSequence,
    )

    events = [
        RespiratoryEvent(
            start_second=35.0,
            duration_seconds=10.0,
            event_type=RespiratoryEventType.HYPOPNEA,
            confidence=1.0,
        )
    ]
    windows = build_shhs_respiratory_training_windows(
        events,
        recording_duration_seconds=90.0,
    )
    return SHHSRespiratoryTrainingWindowSequence(
        path=(tmp_path / "sample.xml").resolve(),
        recording_duration_seconds=90.0,
        window_duration_seconds=30.0,
        stride_seconds=30.0,
        minimum_event_overlap_seconds=1.0,
        normal_exclusion_buffer_seconds=30.0,
        windows=windows,
        event_sequence=SHHSRespiratoryEventSequence(
            path=(tmp_path / "sample.xml").resolve(),
            events=events,
            scored_event_count=1,
            mapped_event_count=1,
            ignored_event_count=0,
            target_label_counts={RespiratoryEventType.HYPOPNEA: 1},
            ignored_label_counts={},
            unknown_label_counts={},
        ),
    )


def _dataset_manifest(
    tmp_path: Path,
    record_id: str,
    *,
    window_count: int,
) -> SHHSRespiratoryDatasetManifest:
    return SHHSRespiratoryDatasetManifest(
        schema_version=SHHS_RESPIRATORY_DATASET_MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc),
        dataset_path=(tmp_path / f"{record_id}.npz").resolve(),
        edf_path=(tmp_path / f"{record_id}.edf").resolve(),
        source_xml_path=(tmp_path / f"{record_id}.xml").resolve(),
        channel_names=("THOR RES", "ABDO RES"),
        sampling_rate_hz=125.0,
        samples_per_window=3750,
        included_only=True,
        window_count=window_count,
        class_order=RESPIRATORY_DATASET_CLASS_ORDER,
        class_counts={
            RespiratoryEventType.NORMAL_BREATHING: max(window_count - 2, 0),
            RespiratoryEventType.HYPOPNEA: 1,
            RespiratoryEventType.SUSPECTED_APNEA: 1,
        },
        arrays={
            "x": {"shape": [window_count, 2, 3750], "dtype": "float32"},
            "y": {"shape": [window_count], "dtype": "int64"},
        },
        notes=["test manifest"],
    )
