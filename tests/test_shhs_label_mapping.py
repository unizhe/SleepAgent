import pytest

from sleepagent.preprocessing import (
    UnknownSHHSRespiratoryEventError,
    UnknownSleepStageError,
    map_shhs_respiratory_event_counts,
    map_shhs_respiratory_event_label,
    map_shhs_xml_sleep_label,
    map_shhs_xml_sleep_label_counts,
)
from sleepagent.schemas import RespiratoryEventType, SleepStage


def test_maps_shhs_xml_sleep_stage_labels_and_codes() -> None:
    assert map_shhs_xml_sleep_label("Wake|0") == SleepStage.WAKE
    assert map_shhs_xml_sleep_label("Stage 1 sleep|1") == SleepStage.NREM
    assert map_shhs_xml_sleep_label("Stage 2 sleep|2") == SleepStage.NREM
    assert map_shhs_xml_sleep_label("Stage 3 sleep|3") == SleepStage.NREM
    assert map_shhs_xml_sleep_label("Stage 4 sleep|4") == SleepStage.NREM
    assert map_shhs_xml_sleep_label("REM sleep|5") == SleepStage.REM
    assert map_shhs_xml_sleep_label("5") == SleepStage.REM


def test_sleep_label_counts_can_skip_non_stage_xml_vocab() -> None:
    counts = {
        "Wake": 48,
        "Stage 1 sleep": 29,
        "Stage 2 sleep": 68,
        "Stage 3 sleep": 29,
        "REM sleep": 7,
        "Hypopnea": 85,
    }

    assert map_shhs_xml_sleep_label_counts(counts, unknown_policy="ignore") == {
        SleepStage.WAKE: 48,
        SleepStage.NREM: 126,
        SleepStage.REM: 7,
    }


def test_sleep_label_mapping_raises_for_unknown_labels_by_default() -> None:
    with pytest.raises(UnknownSleepStageError):
        map_shhs_xml_sleep_label("Hypopnea")


def test_maps_shhs_respiratory_event_labels_to_mvp_targets() -> None:
    assert (
        map_shhs_respiratory_event_label("Normal breathing")
        == RespiratoryEventType.NORMAL_BREATHING
    )
    assert map_shhs_respiratory_event_label("Hypopnea") == (
        RespiratoryEventType.HYPOPNEA
    )
    assert map_shhs_respiratory_event_label("Obstructive Hypopnea") == (
        RespiratoryEventType.HYPOPNEA
    )
    assert map_shhs_respiratory_event_label("Obstructive apnea|Obstructive Apnea") == (
        RespiratoryEventType.SUSPECTED_APNEA
    )
    assert map_shhs_respiratory_event_label("Central Apnea") == (
        RespiratoryEventType.SUSPECTED_APNEA
    )
    assert map_shhs_respiratory_event_label("Mixed Apnea") == (
        RespiratoryEventType.SUSPECTED_APNEA
    )


def test_respiratory_event_counts_skip_explicit_non_targets() -> None:
    counts = {
        "Arousal": 183,
        "Hypopnea": 85,
        "Obstructive apnea": 2,
        "SpO2 desaturation": 71,
        "Stage 2 sleep": 68,
        "Wake": 48,
    }

    assert map_shhs_respiratory_event_counts(counts, unknown_policy="ignore") == {
        RespiratoryEventType.HYPOPNEA: 85,
        RespiratoryEventType.SUSPECTED_APNEA: 2,
    }


def test_respiratory_event_mapping_raises_for_unmapped_labels_by_default() -> None:
    with pytest.raises(UnknownSHHSRespiratoryEventError):
        map_shhs_respiratory_event_label("Novel Event")
