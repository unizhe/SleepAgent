from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from sleepagent.observability import log_event, record_error
from sleepagent.product_device.llm import (
    LLM_NOT_CONFIGURED_MESSAGE,
    OpenAICompatibleChatProvider,
    OpenAICompatibleProviderConfig,
    ProductChatProvider,
    ProductLLMNotConfiguredError,
    ProductLLMProviderError,
)
from sleepagent.product_device.schemas import (
    PRODUCT_DIALOGUE_SCHEMA_VERSION,
    RadarAlertEvent,
    RadarDashboardSummary,
    RadarDataQuality,
    RadarDialogueStatus,
    RadarProductDialogueDraft,
    RadarProductDialogueRequest,
    RadarProductDialogueResult,
    RadarSourceMetadata,
    RadarSleepReport,
    RadarVitalSnapshot,
)


PRODUCT_DIALOGUE_BLOCK_MESSAGE = (
    "This product assistant can only explain radar device data, sleep and in-bed "
    "trends, alerts, and lifestyle observations. It cannot provide diagnosis, "
    "medication guidance, emergency triage, or clinical sleep-test claims."
)

ALLOWED_PRODUCT_SCOPE_TERMS = (
    "device",
    "radar",
    "reading",
    "readings",
    "heart rate",
    "breath rate",
    "vital",
    "sleep",
    "bed",
    "in-bed",
    "out of bed",
    "trend",
    "alert",
    "alarm",
    "lifestyle",
    "routine",
    "movement",
    "score",
    "设备",
    "雷达",
    "数据",
    "读数",
    "心率",
    "呼吸",
    "生命体征",
    "睡眠",
    "在床",
    "离床",
    "趋势",
    "告警",
    "报警",
    "提醒",
    "生活方式",
    "作息",
    "活动",
    "体动",
    "睡眠评分",
    "昨晚",
    "今晚",
    "今天",
)
CURRENT_VALUE_TERMS = (
    "current",
    "now",
    "right now",
    "live",
    "latest",
    "real-time",
    "realtime",
    "现在",
    "当前",
    "实时",
    "最新",
    "此刻",
)
DIAGNOSIS_TERMS = (
    "diagnose",
    "diagnosis",
    "diagnosed",
    "you have sleep apnea",
    "obstructive sleep apnea",
    "osa",
    "确诊",
    "诊断",
    "诊断为",
    "患有",
    "睡眠呼吸暂停",
    "阻塞性睡眠呼吸暂停",
)
MEDICATION_TERMS = (
    "medication",
    "medicine",
    "drug",
    "dose",
    "dosage",
    "prescribe",
    "stop taking",
    "start taking",
    "用药",
    "吃药",
    "服药",
    "药物",
    "剂量",
    "处方",
    "停药",
)
EMERGENCY_TRIAGE_TERMS = (
    "911",
    "emergency",
    "er",
    "urgent care",
    "triage",
    "ambulance",
    "chest pain",
    "severe breathing difficulty",
    "急救",
    "急诊",
    "分诊",
    "救护车",
    "胸痛",
    "严重呼吸困难",
    "意识异常",
)
CLINICAL_TEST_TERMS = (
    "psg",
    "polysomnography",
    "sleep study",
    "clinical test",
    "多导睡眠",
    "临床检查",
    "临床检测",
)


class ProductDialogueValidationError(ValueError):
    """Raised when product dialogue JSON or safety validation fails."""


@dataclass(frozen=True)
class ProductDialogueSafetyResult:
    safety_flags: list[str]
    blocked_reasons: list[str]

    @property
    def passed(self) -> bool:
        return not self.blocked_reasons


class ProductDialogueAgent:
    """LLM-backed product dialogue with strict product and safety boundaries."""

    def __init__(
        self,
        *,
        provider: ProductChatProvider | None = None,
        config: OpenAICompatibleProviderConfig | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or OpenAICompatibleProviderConfig()

    def run(
        self,
        request: RadarProductDialogueRequest,
    ) -> RadarProductDialogueResult:
        source_metadata = collect_dialogue_source_metadata(request)
        request_safety = check_product_dialogue_request_safety(request.user_message)
        if not request_safety.passed:
            return _blocked_result(
                request,
                assistant_message=PRODUCT_DIALOGUE_BLOCK_MESSAGE,
                safety_flags=request_safety.safety_flags,
                blocked_reasons=request_safety.blocked_reasons,
                source_metadata=source_metadata,
            )

        data_quality = _request_data_quality(request)
        if _requires_current_values(request.user_message) and data_quality.blocks_current_values:
            return _blocked_result(
                request,
                assistant_message=_quality_block_message(data_quality),
                safety_flags=["data_quality_block"],
                blocked_reasons=data_quality.blocked_reasons,
                caveats=data_quality.caveats,
                source_metadata=source_metadata,
            )

        provider = self.provider or OpenAICompatibleChatProvider(
            api_key_env=self.config.api_key_env,
            base_url=self.config.base_url,
            timeout_seconds=self.config.timeout_seconds,
        )
        if hasattr(provider, "is_configured") and not provider.is_configured:
            log_event(
                "llm_call_skipped",
                source="product_dialogue",
                reason="not_configured",
                model=self.config.model,
            )
            return _llm_not_configured_result(
                request,
                caveats=data_quality.caveats,
                source_metadata=source_metadata,
            )

        try:
            content = provider.create_json_completion(
                messages=build_product_dialogue_messages(request),
                config=self.config,
            )
            draft = validate_product_dialogue_json(content)
        except ProductLLMNotConfiguredError:
            log_event(
                "llm_call_skipped",
                source="product_dialogue",
                reason="not_configured",
                model=self.config.model,
            )
            return _llm_not_configured_result(
                request,
                caveats=data_quality.caveats,
                source_metadata=source_metadata,
            )
        except ProductDialogueValidationError as exc:
            record_error(
                event="llm_output_validation_failure",
                error=exc,
                source="product_dialogue",
                context={"model": self.config.model},
            )
            return _blocked_result(
                request,
                assistant_message=PRODUCT_DIALOGUE_BLOCK_MESSAGE,
                safety_flags=["llm_output_safety_block"],
                blocked_reasons=[str(exc)],
                caveats=data_quality.caveats,
                source_metadata=source_metadata,
            )
        except ProductLLMProviderError as exc:
            record_error(
                event="llm_call_failure",
                error=exc,
                source="product_dialogue",
                context={"model": self.config.model},
            )
            return RadarProductDialogueResult(
                radar_device_id=request.radar_device_id,
                status=RadarDialogueStatus.FAILED,
                assistant_message="Product LLM request failed.",
                caveats=data_quality.caveats,
                source_metadata=source_metadata,
                generated_at=datetime.now(timezone.utc),
            )

        return RadarProductDialogueResult(
            radar_device_id=request.radar_device_id,
            status=RadarDialogueStatus.COMPLETED,
            assistant_message=draft.answer,
            caveats=_dedupe([*data_quality.caveats, *draft.caveats]),
            source_metadata=source_metadata,
            generated_at=datetime.now(timezone.utc),
        )


def build_product_dialogue_messages(
    request: RadarProductDialogueRequest,
) -> list[dict[str, str]]:
    user_payload = {
        "user_message": request.user_message,
        "locale": request.locale,
        "allowed_scope": [
            "radar device data explanation",
            "sleep and in-bed trend observations",
            "alert explanation",
            "lifestyle observation suggestions",
        ],
        "dashboard_summary": _dashboard_context(request.dashboard_summary),
        "recent_observation_summary": _snapshot_batch_summary(
            request.recent_snapshots[-12:]
        ),
        "recent_alerts": [
            _alert_context(alert) for alert in request.recent_alerts[-10:]
        ],
        "required_json_schema": {
            "schema_version": PRODUCT_DIALOGUE_SCHEMA_VERSION,
            "answer": "non-empty string",
            "observations": ["optional non-empty string"],
            "suggested_actions": ["optional non-empty string"],
            "caveats": ["optional non-empty string"],
            "referenced_fields": ["optional non-empty string"],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You are SleepAgent's radar product dialogue assistant. Return JSON "
                "only. Answer only from the provided product context. Stay within "
                "device data explanation, sleep and in-bed trends, alert explanation, "
                "and lifestyle observation suggestions. Do not provide diagnosis, "
                "medication guidance, emergency triage, or clinical sleep-test claims. "
                "If data is stale, missing, invalid, offline, out of bed, or the report "
                "is stale, include a clear caveat."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]


def validate_product_dialogue_json(
    raw_json: str | bytes | dict[str, Any],
) -> RadarProductDialogueDraft:
    try:
        payload = json.loads(raw_json) if isinstance(raw_json, (str, bytes)) else raw_json
        draft = RadarProductDialogueDraft.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ProductDialogueValidationError(
            "Product dialogue output failed JSON validation."
        ) from exc

    safety = check_product_dialogue_output_safety(
        " ".join(
            [
                draft.answer,
                *draft.observations,
                *draft.suggested_actions,
                *draft.caveats,
            ]
        )
    )
    if not safety.passed:
        raise ProductDialogueValidationError(
            "Product dialogue output failed safety validation: "
            + ",".join(safety.blocked_reasons)
        )
    return draft


def check_product_dialogue_request_safety(
    user_message: str,
) -> ProductDialogueSafetyResult:
    return _check_product_dialogue_safety(
        user_message,
        require_allowed_scope=True,
    )


def check_product_dialogue_output_safety(
    answer: str,
) -> ProductDialogueSafetyResult:
    return _check_product_dialogue_safety(
        answer,
        require_allowed_scope=False,
    )


def collect_dialogue_source_metadata(
    request: RadarProductDialogueRequest,
) -> list[RadarSourceMetadata]:
    source_metadata: list[RadarSourceMetadata] = []
    if request.dashboard_summary is not None:
        source_metadata.extend(request.dashboard_summary.source_metadata)
    source_metadata.extend(snapshot.source_metadata for snapshot in request.recent_snapshots)
    source_metadata.extend(alert.source_metadata for alert in request.recent_alerts)
    return _dedupe_source_metadata(source_metadata)


def _check_product_dialogue_safety(
    text: str,
    *,
    require_allowed_scope: bool,
) -> ProductDialogueSafetyResult:
    normalized = text.lower()
    safety_flags: list[str] = []
    blocked_reasons: list[str] = []

    if _has_any(normalized, DIAGNOSIS_TERMS):
        blocked_reasons.append("clinical_diagnosis_not_allowed")
    if _has_any(normalized, MEDICATION_TERMS):
        blocked_reasons.append("medication_guidance_not_allowed")
    if _has_any(normalized, EMERGENCY_TRIAGE_TERMS):
        blocked_reasons.append("emergency_triage_not_allowed")
    if _has_any(normalized, CLINICAL_TEST_TERMS):
        blocked_reasons.append("clinical_sleep_test_claim_not_allowed")
    if require_allowed_scope and not _has_any(normalized, ALLOWED_PRODUCT_SCOPE_TERMS):
        blocked_reasons.append("out_of_scope_product_dialogue")

    if blocked_reasons:
        safety_flags.append("product_dialogue_safety_boundary")
    return ProductDialogueSafetyResult(
        safety_flags=safety_flags,
        blocked_reasons=_dedupe(blocked_reasons),
    )


def _request_data_quality(request: RadarProductDialogueRequest) -> RadarDataQuality:
    if request.dashboard_summary is not None:
        return request.dashboard_summary.data_quality
    if not request.recent_snapshots:
        return RadarDataQuality(
            current_snapshot_available=False,
            partial_sleep_report=True,
            blocks_current_values=True,
            caveats=[
                "No dashboard summary or recent radar vital snapshots were provided."
            ],
            blocked_reasons=["no_product_data_context"],
        )
    return RadarDataQuality(
        partial_sleep_report=True,
        caveats=["No dashboard summary was provided for this product dialogue turn."],
    )


def _requires_current_values(user_message: str) -> bool:
    normalized = user_message.lower()
    has_current_term = _has_any(normalized, CURRENT_VALUE_TERMS)
    has_vital_or_device_term = _has_any(
        normalized,
        (
            "heart rate",
            "breath rate",
            "vital",
            "reading",
            "device",
            "心率",
            "呼吸",
            "生命体征",
            "读数",
            "设备",
            "数据",
        ),
    )
    return has_current_term and has_vital_or_device_term


def _quality_block_message(data_quality: RadarDataQuality) -> str:
    reasons = ", ".join(data_quality.blocked_reasons) or "data_quality_block"
    return (
        "Current radar values are blocked because data quality is insufficient: "
        f"{reasons}. Use historical context only after reviewing the caveats."
    )


def _llm_not_configured_result(
    request: RadarProductDialogueRequest,
    *,
    caveats: list[str],
    source_metadata: list[RadarSourceMetadata],
) -> RadarProductDialogueResult:
    return RadarProductDialogueResult(
        radar_device_id=request.radar_device_id,
        status=RadarDialogueStatus.LLM_NOT_CONFIGURED,
        assistant_message=LLM_NOT_CONFIGURED_MESSAGE,
        caveats=caveats,
        source_metadata=source_metadata,
        generated_at=datetime.now(timezone.utc),
    )


def _blocked_result(
    request: RadarProductDialogueRequest,
    *,
    assistant_message: str,
    safety_flags: list[str],
    blocked_reasons: list[str],
    source_metadata: list[RadarSourceMetadata],
    caveats: list[str] | None = None,
) -> RadarProductDialogueResult:
    return RadarProductDialogueResult(
        radar_device_id=request.radar_device_id,
        status=RadarDialogueStatus.BLOCKED,
        assistant_message=assistant_message,
        safety_flags=safety_flags,
        blocked_reasons=blocked_reasons,
        caveats=caveats or [],
        source_metadata=source_metadata,
        generated_at=datetime.now(timezone.utc),
    )


def _dashboard_context(
    dashboard: RadarDashboardSummary | None,
) -> dict[str, Any] | None:
    if dashboard is None:
        return None
    return {
        "status_line": dashboard.status_line,
        "summary_text": dashboard.summary_text,
        "highlights": dashboard.highlights,
        "trend_observations": dashboard.trend_observations,
        "caveats": dashboard.caveats,
        "blocked_reasons": dashboard.blocked_reasons,
        "data_quality": dashboard.data_quality.model_dump(mode="json"),
        "current_observation_summary": (
            _snapshot_batch_summary([dashboard.current_snapshot])
            if dashboard.current_snapshot is not None
            else None
        ),
        "latest_sleep_report": (
            _sleep_report_context(dashboard.latest_sleep_report)
            if dashboard.latest_sleep_report is not None
            else None
        ),
        "recent_alerts": [
            _alert_context(alert) for alert in dashboard.recent_alerts[-10:]
        ],
    }


def _snapshot_batch_summary(
    snapshots: list[RadarVitalSnapshot],
) -> dict[str, Any]:
    heart_rates = [
        item.heart_rate_bpm for item in snapshots if item.heart_rate_bpm is not None
    ]
    breath_rates = [
        item.breath_rate_bpm
        for item in snapshots
        if item.breath_rate_bpm is not None
    ]
    movements = [
        item.body_movement for item in snapshots if item.body_movement is not None
    ]
    return {
        "sample_count": len(snapshots),
        "window_start_at": snapshots[0].measured_at.isoformat() if snapshots else None,
        "window_end_at": snapshots[-1].measured_at.isoformat() if snapshots else None,
        "mean_heart_rate_bpm": (
            round(sum(heart_rates) / len(heart_rates), 1) if heart_rates else None
        ),
        "mean_breath_rate_bpm": (
            round(sum(breath_rates) / len(breath_rates), 1)
            if breath_rates
            else None
        ),
        "mean_body_movement": (
            round(sum(movements) / len(movements), 2) if movements else None
        ),
        "bed_presence_counts": {
            value: sum(1 for item in snapshots if item.bed_presence.value == value)
            for value in sorted({item.bed_presence.value for item in snapshots})
        },
        "invalid_reading_count": sum(
            len(item.invalid_reading_flags) for item in snapshots
        ),
    }


def _sleep_report_context(report: RadarSleepReport) -> dict[str, Any]:
    return {
        "report_date": report.report_date.isoformat(),
        "sleep_start_at": (
            report.sleep_start_at.isoformat() if report.sleep_start_at else None
        ),
        "sleep_end_at": (
            report.sleep_end_at.isoformat() if report.sleep_end_at else None
        ),
        "total_sleep_minutes": report.total_sleep_minutes,
        "sleep_score": report.sleep_score,
        "deep_sleep_minutes": report.deep_sleep_minutes,
        "light_sleep_minutes": report.light_sleep_minutes,
        "rem_sleep_minutes": report.rem_sleep_minutes,
        "awake_minutes": report.awake_minutes,
        "movement_count": report.movement_count,
        "getup_count": report.getup_count,
    }


def _alert_context(alert: RadarAlertEvent) -> dict[str, Any]:
    return {
        "alert_type": alert.alert_type,
        "severity": alert.severity.value,
        "occurred_at": alert.occurred_at.isoformat(),
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "title": alert.title,
        "message": alert.message,
    }


def _has_any(normalized: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in normalized for term in terms)


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def _dedupe_source_metadata(
    source_metadata: list[RadarSourceMetadata],
) -> list[RadarSourceMetadata]:
    deduped: list[RadarSourceMetadata] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in source_metadata:
        key = (item.vendor, item.raw_event_id, item.vendor_message_id)
        if key in seen:
            continue
        deduped.append(item)
        seen.add(key)
    return deduped
