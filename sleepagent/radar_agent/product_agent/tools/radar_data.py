from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import StrictContract
from sleepagent.radar_agent.quality import DataQualityGate, RadarProviderLike
from sleepagent.radar_agent.schemas import RadarDevice, RadarNightSummary


RADAR_DATA_ADAPTER_VERSION = "sleepagent-radar-data-adapter.v1"


class RadarNightEvidenceRequest(StrictContract):
    """Provider-neutral request for one canonical radar night."""

    authorized_radar_device_id: str = Field(..., min_length=1)
    expected_subject_id: str = Field(..., min_length=1)
    night_of: date | datetime | None = None


class RadarNightEvidence(StrictContract):
    """Quality-gated data projection; it intentionally has no Agent identity."""

    adapter_version: str = RADAR_DATA_ADAPTER_VERSION
    device: RadarDevice
    snapshot_count: int = Field(ge=0)
    night_summary: RadarNightSummary
    source_refs: list[str] = Field(default_factory=list)


class CanonicalRadarDeviceStatus(StrictContract):
    """Minimal canonical device state; no provider payload crosses this edge."""

    schema_version: Literal["canonical_radar_device_status.v1"] = (
        "canonical_radar_device_status.v1"
    )
    data_mode: Literal["live", "replay", "unknown"] = "unknown"
    offline: bool = False
    stale: bool = False
    source_refs: list[str] = Field(default_factory=list)


class RadarDataAdapter:
    """Read provider data and normalize it through the canonical quality gate.

    Provider I/O and normalization are a deterministic data boundary.  The
    adapter does not form Evidence claims, route A2A messages, or write state.
    """

    def __init__(
        self,
        provider: RadarProviderLike,
        *,
        quality_gate: DataQualityGate | None = None,
    ) -> None:
        self._provider = provider
        self._quality_gate = quality_gate or DataQualityGate()

    def read_night(self, request: RadarNightEvidenceRequest) -> RadarNightEvidence:
        device_id = request.authorized_radar_device_id
        expected_subject_id = request.expected_subject_id
        device = self._provider.get_device(device_id)
        _validate_device_binding(
            device=device,
            authorized_device_id=device_id,
            expected_subject_id=expected_subject_id,
        )
        provider_report = self._provider.pull_night_report(
            device_id,
            night_of=request.night_of,
        )
        _validate_report_binding(
            report=provider_report,
            authorized_device_id=device_id,
            expected_subject_id=expected_subject_id,
        )
        snapshots = self._provider.pull_snapshots(device_id)
        _validate_snapshot_bindings(
            snapshots=snapshots,
            authorized_device_id=device_id,
            expected_subject_id=expected_subject_id,
        )
        summary = self._quality_gate.run(
            device=device,
            snapshots=snapshots,
            provider_report=provider_report,
            night_of=request.night_of,
        )
        _validate_summary_binding(
            summary=summary,
            authorized_device_id=device_id,
            expected_subject_id=expected_subject_id,
        )
        source_refs = _dedupe(
            [_summary_ref(summary)]
            + [f"snapshot:{snapshot.snapshot_id}" for snapshot in snapshots]
        )
        return RadarNightEvidence(
            device=device,
            snapshot_count=len(snapshots),
            night_summary=summary,
            source_refs=source_refs,
        )


class CanonicalRadarEvidenceTool:
    """Validate already-persisted Product facts at the Tool boundary."""

    @staticmethod
    def read(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical radar facts must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("canonical radar evidence must be an object")
        requested_refs = _dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        snapshot_refs = set(context.fact_snapshot.source_refs)
        if not set(requested_refs).issubset(snapshot_refs):
            raise ValueError("radar evidence refs exceed FactSnapshot scope")
        if data.get("schema_version") == "product_revision_facts.v1":
            from sleepagent.sleep_domain.product_data import ProductRevisionFacts

            facts = ProductRevisionFacts.model_validate(data)
            binding = context.fact_snapshot.binding
            if not _subject_matches_binding(
                canonical_subject=facts.subject_id,
                binding_subject=binding.subject_id,
            ):
                raise ValueError("canonical radar evidence subject mismatch")
            if (
                facts.canonical_data_version
                != context.fact_snapshot.canonical_data_version
            ):
                raise ValueError("canonical radar evidence version mismatch")
            if not requested_refs or set(requested_refs) != set(
                facts.provenance_references
            ):
                raise ValueError(
                    "canonical radar evidence refs must match Product facts"
                )
            scope = context.fact_snapshot.source_scope
            local_sleep_date = date.fromisoformat(facts.local_sleep_date)
            if (
                scope.date_start is None
                or scope.date_end is None
                or not scope.date_start <= local_sleep_date <= scope.date_end
            ):
                raise ValueError(
                    "canonical radar evidence date exceeds FactSnapshot scope"
                )
        return {
            "data": data,
            "source_refs": requested_refs,
        }

    @staticmethod
    def assess_quality(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical quality facts must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("quality assessment must be an object")
        requested_refs = _dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        if not set(requested_refs).issubset(
            set(context.fact_snapshot.source_refs)
        ):
            raise ValueError("quality refs exceed FactSnapshot scope")
        coverage = float(
            data.get("coverage_ratio", arguments.get("coverage_ratio", 0))
        )
        if data.get("schema_version") == (
            "deterministic_quality_assessment.v1"
        ):
            policy_version = str(data.get("policy_version", ""))
            if not policy_version:
                raise ValueError("pinned quality assessment requires policy_version")
            sufficient = data.get("data_sufficiency") == "sufficient"
            usable = bool(
                sufficient
                and data.get("quality_state") != "data_insufficient"
                and not data.get("stale", False)
                and not data.get("offline", False)
                and not data.get("clock_invalid", False)
            )
            return {
                "coverage_ratio": coverage,
                "usable": usable,
                "quality_state": data.get("quality_state"),
                "data_sufficiency": data.get("data_sufficiency"),
                "policy_version": policy_version,
                "reason_codes": list(data.get("reason_codes", [])),
                "source_refs": requested_refs,
            }
        return {
            "coverage_ratio": coverage,
            "usable": coverage >= 0.6,
            "source_refs": requested_refs,
        }

    @staticmethod
    def read_device_status(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical device status must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("canonical device status must be an object")
        requested_refs = _dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        if not set(requested_refs).issubset(
            set(context.fact_snapshot.source_refs)
        ):
            raise ValueError("device status refs exceed FactSnapshot scope")
        return CanonicalRadarDeviceStatus.model_validate(
            {**data, "source_refs": requested_refs}
        ).model_dump(mode="json")


def _summary_ref(summary: RadarNightSummary) -> str:
    return summary.source_report_ref or (
        f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"
    )


def _validate_device_binding(
    *,
    device: RadarDevice,
    authorized_device_id: str,
    expected_subject_id: str,
) -> None:
    if device.radar_device_id != authorized_device_id:
        raise ValueError("Radar provider device id exceeds the authorized binding.")
    if device.bound_subject_id != expected_subject_id:
        raise ValueError("Radar provider device subject does not match the request.")


def _validate_report_binding(
    *,
    report: RadarNightSummary | None,
    authorized_device_id: str,
    expected_subject_id: str,
) -> None:
    if report is None:
        return
    if report.radar_device_id != authorized_device_id:
        raise ValueError("Radar provider report device exceeds the authorized binding.")
    if report.subject_id is not None and report.subject_id != expected_subject_id:
        raise ValueError("Radar provider report subject does not match the request.")


def _validate_snapshot_bindings(
    *,
    snapshots: list[Any],
    authorized_device_id: str,
    expected_subject_id: str,
) -> None:
    for snapshot in snapshots:
        if snapshot.radar_device_id != authorized_device_id:
            raise ValueError(
                "Radar provider snapshot device exceeds the authorized binding."
            )
        if (
            snapshot.subject_id is not None
            and snapshot.subject_id != expected_subject_id
        ):
            raise ValueError(
                "Radar provider snapshot subject does not match the request."
            )


def _validate_summary_binding(
    *,
    summary: RadarNightSummary,
    authorized_device_id: str,
    expected_subject_id: str,
) -> None:
    if summary.radar_device_id != authorized_device_id:
        raise ValueError("Radar quality summary device exceeds the authorized binding.")
    if summary.subject_id != expected_subject_id:
        raise ValueError("Radar quality summary subject does not match the request.")


def _caller_name(context: Any) -> str:
    caller = context.caller
    return caller.value if hasattr(caller, "value") else str(caller)


def _subject_matches_binding(
    *,
    canonical_subject: str,
    binding_subject: str,
) -> bool:
    if not canonical_subject:
        return False
    if binding_subject == canonical_subject:
        return True
    marker = "::subject::"
    if marker not in binding_subject:
        return False
    return binding_subject.split(marker, 1)[1] == canonical_subject


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


__all__ = [
    "RADAR_DATA_ADAPTER_VERSION",
    "CanonicalRadarDeviceStatus",
    "CanonicalRadarEvidenceTool",
    "RadarDataAdapter",
    "RadarNightEvidence",
    "RadarNightEvidenceRequest",
]
