from __future__ import annotations

from types import SimpleNamespace

from sleepagent.workers.product import (
    ReportShadowComparison,
    _source_metric_material,
)


def _source_material() -> tuple[dict[str, object], ...]:
    summary = {
        "schema_version": "product_deterministic_night_summary.v2",
        "sleep_window_start": "2026-07-26T01:35:00+08:00",
        "sleep_window_end": "2026-07-26T02:41:00+08:00",
        "sleep_window_minutes": 66.0,
        "stage_minutes": {"light": 43.0, "deep": 13.0, "rem": 10.0},
        "vital_centers": {"heart_rate": 63.0, "respiratory_rate": 15.0},
        "bed_exit_count": 2,
    }
    source = SimpleNamespace(
        facts=SimpleNamespace(deterministic_night_summary=lambda: summary)
    )
    return _source_metric_material(source)


def test_source_metric_material_uses_shared_v2_window_taxonomy() -> None:
    by_metric = {item["metric_id"]: item for item in _source_material()}

    assert by_metric["bed_exit_count"]["window"] == (
        "authoritative_observation_window"
    )
    assert by_metric["heart_rate_mean"]["window"] == (
        "authoritative_observation_window"
    )
    assert by_metric["respiratory_rate_mean"]["window"] == (
        "authoritative_observation_window"
    )
    assert by_metric["sleep_window_minutes"]["window"] == (
        "effective_sleep_stage_coverage"
    )
    assert by_metric["sleep_stage.light_minutes"]["window"] == (
        "effective_sleep_stage_coverage"
    )
    assert by_metric["sleep_stage_coverage_start_at"] == {
        "metric_id": "sleep_stage_coverage_start_at",
        "value": "2026-07-25T17:35:00+00:00",
        "unit": None,
        "window": "effective_sleep_stage_coverage",
    }
    assert by_metric["sleep_stage_coverage_end_at"] == {
        "metric_id": "sleep_stage_coverage_end_at",
        "value": "2026-07-25T18:41:00+00:00",
        "unit": None,
        "window": "effective_sleep_stage_coverage",
    }


def _comparison_with_metric_material(
    legacy: tuple[dict[str, object], ...],
    shared: tuple[dict[str, object], ...],
) -> ReportShadowComparison:
    return ReportShadowComparison.create(
        legacy_attempt_sha256="a" * 64,
        shared_analysis_sha256="b" * 64,
        category_material={"metric_values_units": (legacy, shared)},
    )


def test_shadow_metric_comparison_detects_real_value_mismatch() -> None:
    source = _source_material()
    shared = tuple(
        {
            **item,
            "value": 3 if item["metric_id"] == "bed_exit_count" else item["value"],
        }
        for item in source
    )

    comparison = _comparison_with_metric_material(source, shared)

    assert comparison.mismatch_categories == ("metric_values_units",)


def test_shadow_metric_comparison_detects_real_unit_mismatch() -> None:
    source = _source_material()
    shared = tuple(
        {
            **item,
            "unit": "hz" if item["metric_id"] == "heart_rate_mean" else item["unit"],
        }
        for item in source
    )

    comparison = _comparison_with_metric_material(source, shared)

    assert comparison.mismatch_categories == ("metric_values_units",)
