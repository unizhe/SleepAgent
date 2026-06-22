import pytest

from sleepagent.preprocessing import (
    UnknownSleepStageError,
    map_shhs_sleep_stage,
    map_sleep_stage,
    map_sleep_stages,
    map_yasa_sleep_stage,
)
from sleepagent.schemas import SleepStage


def test_maps_common_string_aliases_to_three_stage_taxonomy() -> None:
    labels = [
        "Wake",
        "W",
        "REM",
        "R",
        "N1",
        "stage 2 sleep",
        "Stage_4",
    ]

    assert map_sleep_stages(labels) == [
        SleepStage.WAKE,
        SleepStage.WAKE,
        SleepStage.REM,
        SleepStage.REM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
    ]


def test_maps_yasa_numeric_convention() -> None:
    assert [map_yasa_sleep_stage(label) for label in [0, 1, 2, 3, 4]] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.REM,
    ]


def test_maps_shhs_numeric_convention() -> None:
    assert [map_shhs_sleep_stage(label) for label in [0, 1, 2, 3, 4, 5]] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.REM,
    ]


def test_auto_mode_rejects_ambiguous_numeric_stage_four() -> None:
    with pytest.raises(UnknownSleepStageError, match="ambiguous"):
        map_sleep_stage(4)


def test_unknown_policy_ignore_skips_unscored_labels() -> None:
    assert map_sleep_stages(
        ["Wake", "artifact", "REM", "unscored"],
        unknown_policy="ignore",
    ) == [SleepStage.WAKE, SleepStage.REM]

