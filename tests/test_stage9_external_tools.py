from datetime import date

import pytest
from pydantic import ValidationError

from sleepagent.schemas import (
    ActivityLevel,
    ExternalToolContext,
    MealTiming,
    WeatherCondition,
    WeatherContext,
)
from sleepagent.services import (
    MockExternalContextProvider,
    build_external_context_summary,
    build_mock_external_context,
)


def test_mock_external_context_is_deterministic_for_same_inputs() -> None:
    first = build_mock_external_context(
        subject_id="external-subject",
        location="Shanghai",
        context_date=date(2026, 5, 21),
        seed=601,
    )
    second = build_mock_external_context(
        subject_id="external-subject",
        location="Shanghai",
        context_date=date(2026, 5, 21),
        seed=601,
    )

    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
        exclude={"generated_at"}
    )
    assert first.schema_version == "stage9.external_tool_context.v1"
    assert first.source == "mock"
    assert first.subject_id == "external-subject"
    assert first.location == "Shanghai"
    assert first.context_date == date(2026, 5, 21)
    assert "外部mock上下文" in first.summary
    assert "不代表真实外部数据" in first.summary


def test_mock_external_context_changes_with_seed() -> None:
    first = build_mock_external_context(
        subject_id="external-subject",
        context_date=date(2026, 5, 21),
        seed=602,
    )
    second = build_mock_external_context(
        subject_id="external-subject",
        context_date=date(2026, 5, 21),
        seed=603,
    )

    assert first.model_dump(exclude={"generated_at"}) != second.model_dump(
        exclude={"generated_at"}
    )


def test_external_context_provider_exposes_mock_interface() -> None:
    provider = MockExternalContextProvider()

    context = provider.fetch_context(
        subject_id="provider-subject",
        location="mock-bedroom",
        context_date=date(2026, 5, 21),
        seed=604,
    )

    assert isinstance(context, ExternalToolContext)
    assert context.weather.condition in set(WeatherCondition)
    assert context.diet.last_meal_timing in set(MealTiming)
    assert context.lifestyle.activity_level in set(ActivityLevel)
    assert context.weather.note.startswith("Mock weather")
    assert context.diet.note.startswith("Mock diet")
    assert context.lifestyle.note.startswith("Mock lifestyle")


def test_external_context_summary_surfaces_lifestyle_risk_notes() -> None:
    context = build_mock_external_context(
        subject_id="risk-subject",
        context_date=date(2026, 5, 21),
        seed=601,
    )
    context = context.model_copy(
        update={
            "weather": context.weather.model_copy(
                update={"condition": WeatherCondition.HOT}
            ),
            "diet": context.diet.model_copy(
                update={
                    "caffeine_after_noon": True,
                    "alcohol_near_bedtime": True,
                    "heavy_meal_near_bedtime": True,
                }
            ),
            "lifestyle": context.lifestyle.model_copy(
                update={
                    "screen_time_before_bed_minutes": 120,
                    "nap_minutes": 90,
                    "stress_level_0_10": 8,
                }
            ),
        }
    )

    summary = build_external_context_summary(
        weather=context.weather,
        diet=context.diet,
        lifestyle=context.lifestyle,
    )

    assert "极端温度" in summary
    assert "午后咖啡因" in summary
    assert "睡前饮酒" in summary
    assert "较长睡前屏幕时间" in summary
    assert "较长午睡" in summary
    assert "较高压力" in summary


def test_external_tool_schema_rejects_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        WeatherContext(
            condition=WeatherCondition.CLEAR,
            outdoor_temperature_celsius=90,
            humidity_percent=50,
            note="invalid",
        )

    with pytest.raises(ValidationError):
        ExternalToolContext(
            subject_id="schema-subject",
            location="mock",
            context_date=date(2026, 5, 21),
            weather={
                "condition": "clear",
                "outdoor_temperature_celsius": 20,
                "humidity_percent": 50,
                "note": "ok",
            },
            diet={
                "last_meal_timing": "normal",
                "note": "ok",
            },
            lifestyle={
                "activity_level": "low",
                "screen_time_before_bed_minutes": 700,
                "nap_minutes": 0,
                "stress_level_0_10": 3,
                "note": "invalid",
            },
            summary="invalid",
        )
