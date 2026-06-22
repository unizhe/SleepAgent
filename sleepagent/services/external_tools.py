from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from random import Random
from typing import Protocol

from sleepagent.schemas.external_tools import (
    ActivityLevel,
    DietContext,
    ExternalToolContext,
    LifestyleContext,
    MealTiming,
    WeatherCondition,
    WeatherContext,
)


DEFAULT_EXTERNAL_CONTEXT_LOCATION = "mock-city"


class ExternalContextProvider(Protocol):
    def fetch_context(
        self,
        *,
        subject_id: str,
        location: str = DEFAULT_EXTERNAL_CONTEXT_LOCATION,
        context_date: date | None = None,
        seed: int = 42,
    ) -> ExternalToolContext:
        ...


class MockExternalContextProvider:
    """Deterministic local provider for Stage 9 external context tests."""

    def fetch_context(
        self,
        *,
        subject_id: str,
        location: str = DEFAULT_EXTERNAL_CONTEXT_LOCATION,
        context_date: date | None = None,
        seed: int = 42,
    ) -> ExternalToolContext:
        resolved_date = context_date or date.today()
        rng = _build_rng(
            subject_id=subject_id,
            location=location,
            context_date=resolved_date,
            seed=seed,
        )
        weather = _build_mock_weather_context(rng)
        diet = _build_mock_diet_context(rng)
        lifestyle = _build_mock_lifestyle_context(rng)
        return ExternalToolContext(
            subject_id=subject_id,
            location=location,
            context_date=resolved_date,
            weather=weather,
            diet=diet,
            lifestyle=lifestyle,
            summary=build_external_context_summary(
                weather=weather,
                diet=diet,
                lifestyle=lifestyle,
            ),
            generated_at=datetime.now(timezone.utc),
        )


def build_mock_external_context(
    *,
    subject_id: str,
    location: str = DEFAULT_EXTERNAL_CONTEXT_LOCATION,
    context_date: date | None = None,
    seed: int = 42,
) -> ExternalToolContext:
    return MockExternalContextProvider().fetch_context(
        subject_id=subject_id,
        location=location,
        context_date=context_date,
        seed=seed,
    )


def build_external_context_summary(
    *,
    weather: WeatherContext,
    diet: DietContext,
    lifestyle: LifestyleContext,
) -> str:
    risk_notes = _build_context_risk_notes(weather, diet, lifestyle)
    risk_text = "；".join(risk_notes) if risk_notes else "暂无明显生活方式风险提示"
    return (
        f"外部mock上下文：天气{_weather_text(weather.condition)}，"
        f"室外约{weather.outdoor_temperature_celsius:.1f}°C，"
        f"室内约{_optional_temperature_text(weather.indoor_temperature_celsius)}，"
        f"湿度约{weather.humidity_percent:.0f}%。"
        f"饮食：{_meal_timing_text(diet.last_meal_timing)}，"
        f"{'午后摄入咖啡因' if diet.caffeine_after_noon else '午后未记录咖啡因'}，"
        f"{'睡前饮酒' if diet.alcohol_near_bedtime else '未记录睡前饮酒'}。"
        f"生活方式：活动量{_activity_text(lifestyle.activity_level)}，"
        f"睡前屏幕时间约{lifestyle.screen_time_before_bed_minutes}分钟，"
        f"午睡约{lifestyle.nap_minutes}分钟，压力评分"
        f"{lifestyle.stress_level_0_10}/10。{risk_text}。"
        "该上下文来自mock工具，不代表真实外部数据。"
    )


def _build_mock_weather_context(rng: Random) -> WeatherContext:
    condition = rng.choice(list(WeatherCondition))
    if condition == WeatherCondition.COLD:
        outdoor_temperature = rng.uniform(-5, 10)
    elif condition == WeatherCondition.HOT:
        outdoor_temperature = rng.uniform(30, 38)
    else:
        outdoor_temperature = rng.uniform(12, 29)
    indoor_temperature = min(max(outdoor_temperature + rng.uniform(-4, 8), 16), 30)
    humidity = rng.uniform(30, 85)
    return WeatherContext(
        condition=condition,
        outdoor_temperature_celsius=round(outdoor_temperature, 1),
        indoor_temperature_celsius=round(indoor_temperature, 1),
        humidity_percent=round(humidity, 1),
        note="Mock weather and temperature context for local Stage 9 development.",
    )


def _build_mock_diet_context(rng: Random) -> DietContext:
    meal_timing = rng.choice(list(MealTiming))
    caffeine_after_noon = rng.random() < 0.35
    alcohol_near_bedtime = rng.random() < 0.15
    heavy_meal_near_bedtime = meal_timing == MealTiming.LATE or rng.random() < 0.2
    return DietContext(
        last_meal_timing=meal_timing,
        caffeine_after_noon=caffeine_after_noon,
        alcohol_near_bedtime=alcohol_near_bedtime,
        heavy_meal_near_bedtime=heavy_meal_near_bedtime,
        note="Mock diet context; no food diary or wearable integration is used.",
    )


def _build_mock_lifestyle_context(rng: Random) -> LifestyleContext:
    activity_level = rng.choice(list(ActivityLevel))
    return LifestyleContext(
        activity_level=activity_level,
        screen_time_before_bed_minutes=rng.randint(0, 180),
        nap_minutes=rng.choice([0, 15, 30, 45, 60, 90]),
        stress_level_0_10=rng.randint(0, 10),
        note="Mock lifestyle context; no real phone, diet, or wearable data is used.",
    )


def _build_context_risk_notes(
    weather: WeatherContext,
    diet: DietContext,
    lifestyle: LifestyleContext,
) -> list[str]:
    notes: list[str] = []
    if weather.condition in {WeatherCondition.COLD, WeatherCondition.HOT}:
        notes.append("极端温度可能影响睡眠舒适度")
    if diet.caffeine_after_noon:
        notes.append("午后咖啡因可能影响入睡")
    if diet.alcohol_near_bedtime:
        notes.append("睡前饮酒可能加重睡眠呼吸问题")
    if diet.heavy_meal_near_bedtime:
        notes.append("临睡前过饱可能影响睡眠质量")
    if lifestyle.screen_time_before_bed_minutes >= 90:
        notes.append("较长睡前屏幕时间可能影响入睡")
    if lifestyle.nap_minutes >= 60:
        notes.append("较长午睡可能影响夜间睡眠连续性")
    if lifestyle.stress_level_0_10 >= 7:
        notes.append("较高压力可能影响睡眠质量")
    return notes


def _build_rng(
    *,
    subject_id: str,
    location: str,
    context_date: date,
    seed: int,
) -> Random:
    seed_text = f"{subject_id}|{location}|{context_date.isoformat()}|{seed}"
    seed_value = int.from_bytes(sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    return Random(seed_value)


def _weather_text(condition: WeatherCondition) -> str:
    mapping = {
        WeatherCondition.CLEAR: "晴朗",
        WeatherCondition.CLOUDY: "多云",
        WeatherCondition.RAINY: "降雨",
        WeatherCondition.COLD: "偏冷",
        WeatherCondition.HOT: "偏热",
    }
    return mapping[condition]


def _meal_timing_text(meal_timing: MealTiming) -> str:
    mapping = {
        MealTiming.EARLY: "晚餐较早",
        MealTiming.NORMAL: "晚餐时间常规",
        MealTiming.LATE: "晚餐偏晚",
        MealTiming.UNKNOWN: "晚餐时间未知",
    }
    return mapping[meal_timing]


def _activity_text(activity_level: ActivityLevel) -> str:
    mapping = {
        ActivityLevel.LOW: "偏低",
        ActivityLevel.MODERATE: "中等",
        ActivityLevel.HIGH: "较高",
    }
    return mapping[activity_level]


def _optional_temperature_text(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.1f}°C"
