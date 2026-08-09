from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    AuthenticatedBinding,
    FactSnapshot,
    InvocationOutcome,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.radar_agent.product_agent.tooling import (
    ProductToolExecutionContext,
    ProductToolExecutor,
)

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    RadarDataAgent,
    TaskContext,
)
from sleepagent.radar_agent.product_agent.tools.radar_data import (
    RadarDataAdapter,
    RadarNightEvidenceRequest,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.quality import DataQualityGate
from sleepagent.radar_agent.schemas import RadarNightSummary
from sleepagent.sleep_domain.contracts import DataMode
from sleepagent.sleep_domain.product_data import ProductRevisionFacts


NIGHT = date(2026, 7, 9)


def test_radar_data_adapter_preserves_canonical_ingest_and_quality_semantics() -> None:
    legacy_provider = ReplayRadarProvider(scenario="normal_night")
    device = legacy_provider.list_devices()[0]
    assert device.bound_subject_id is not None
    legacy = RadarDataAgent(legacy_provider).run(
        ContextPacket(
            task_context=TaskContext(
                task_id="phase3a-radar-data",
                trace_id="trace-phase3a-radar-data",
                purpose="analysis",
            ),
            evidence_packet=EvidencePacket(
                data_quality={"night_of": NIGHT.isoformat()}
            ),
        )
    )
    legacy_summary = RadarNightSummary.model_validate(
        legacy.output_payload["night_summary"]
    )

    result = RadarDataAdapter(legacy_provider).read_night(
        RadarNightEvidenceRequest(
            authorized_radar_device_id=device.radar_device_id,
            expected_subject_id=device.bound_subject_id,
            night_of=NIGHT,
        )
    )

    assert result.device.model_dump(mode="json") == legacy.output_payload["device"]
    assert result.snapshot_count == legacy.output_payload["snapshot_count"]
    assert result.night_summary.model_dump(
        mode="json", exclude={"generated_at"}
    ) == legacy_summary.model_dump(mode="json", exclude={"generated_at"})
    assert result.source_refs == legacy.evidence_refs
    assert not hasattr(result, "agent_name")
    assert not hasattr(result, "next_requests")


def test_radar_data_adapter_keeps_explicit_device_selection() -> None:
    provider = ReplayRadarProvider(scenario="normal_night")
    device = provider.list_devices()[0]
    assert device.bound_subject_id is not None

    result = RadarDataAdapter(provider).read_night(
        RadarNightEvidenceRequest(
            authorized_radar_device_id=device.radar_device_id,
            expected_subject_id=device.bound_subject_id,
            night_of=NIGHT,
        )
    )

    assert result.device.radar_device_id == device.radar_device_id
    assert result.night_summary.radar_device_id == device.radar_device_id
    assert result.night_summary.subject_id == device.bound_subject_id
    assert result.night_summary.night_of == NIGHT


def test_radar_data_adapter_request_requires_authorized_device_and_subject() -> None:
    with pytest.raises(ValueError):
        RadarNightEvidenceRequest(night_of=NIGHT)


def test_radar_data_adapter_never_discovers_an_implicit_default_device() -> None:
    provider = ReplayRadarProvider(scenario="normal_night")
    device = provider.list_devices()[0]
    assert device.bound_subject_id is not None

    result = RadarDataAdapter(_NoDeviceListingProvider(provider)).read_night(
        RadarNightEvidenceRequest(
            authorized_radar_device_id=device.radar_device_id,
            expected_subject_id=device.bound_subject_id,
            night_of=NIGHT,
        )
    )

    assert result.device.radar_device_id == device.radar_device_id


def test_radar_data_adapter_rejects_provider_device_id_mismatch() -> None:
    provider, request = _adapter_fixture()
    other_device_id = "radar-device-from-another-binding"

    with pytest.raises(ValueError, match="device id"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                device_update={"radar_device_id": other_device_id},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_provider_device_subject_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="device subject"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                device_update={"bound_subject_id": "different-subject"},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_unbound_provider_device() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="device subject"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                device_update={"bound_subject_id": None},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_provider_report_device_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="report device"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                report_update={"radar_device_id": "different-device"},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_provider_report_subject_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="report subject"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                report_update={"subject_id": "different-subject"},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_snapshot_device_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="snapshot device"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                snapshot_update={"radar_device_id": "different-device"},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_snapshot_subject_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="snapshot subject"):
        RadarDataAdapter(
            _TamperedRadarProvider(
                provider,
                snapshot_update={"subject_id": "different-subject"},
            )
        ).read_night(request)


def test_radar_data_adapter_rejects_quality_summary_device_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="summary device"):
        RadarDataAdapter(
            provider,
            quality_gate=_TamperedQualityGate(
                {"radar_device_id": "different-device"}
            ),
        ).read_night(request)


def test_radar_data_adapter_rejects_quality_summary_subject_mismatch() -> None:
    provider, request = _adapter_fixture()

    with pytest.raises(ValueError, match="summary subject"):
        RadarDataAdapter(
            provider,
            quality_gate=_TamperedQualityGate(
                {"subject_id": "different-subject"}
            ),
        ).read_night(request)


def test_product_night_evidence_tool_rejects_cross_subject_canonical_facts() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_night_evidence",
        {
            "data": _product_facts(subject_id="different-subject").model_dump(
                mode="json"
            ),
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome == InvocationOutcome.FAILED
    assert result.receipt.error_code == "ValueError"


def test_product_night_evidence_accepts_explicit_namespaced_subject_binding() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_night_evidence",
        {
            "data": _product_facts().model_dump(mode="json"),
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(
                binding_subject="tenant-live::subject::elder-phase3a"
            ),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED


def test_agent_cannot_promote_caller_supplied_generic_radar_payload() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_night_evidence",
        {
            "data": {"total_sleep_minutes": 999},
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_agent_cannot_submit_even_well_formed_canonical_radar_facts() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_night_evidence",
        {
            "data": _product_facts().model_dump(mode="json"),
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_runtime_canonical_radar_facts_require_complete_typed_payload() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_night_evidence",
        {
            "data": {
                "schema_version": "product_revision_facts.v1",
                "subject_id": "elder-phase3a",
                "canonical_data_version": "a" * 64,
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_quality_tool_preserves_pinned_fail_closed_assessment() -> None:
    result = ProductToolExecutor().execute(
        "radar.assess_data_quality",
        {
            "coverage_ratio": 0.99,
            "data": {
                "schema_version": "deterministic_quality_assessment.v1",
                "policy_version": "quality-policy-reviewed.v3",
                "quality_state": "data_insufficient",
                "data_sufficiency": "data_insufficient",
                "stale": False,
                "offline": True,
                "clock_invalid": False,
                "reason_codes": ["device_offline"],
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome == InvocationOutcome.SUCCEEDED
    assert result.receipt.output["usable"] is False
    assert result.receipt.output["policy_version"] == (
        "quality-policy-reviewed.v3"
    )
    assert result.receipt.output["reason_codes"] == ["device_offline"]


def test_agent_cannot_self_attest_pinned_quality_policy() -> None:
    result = ProductToolExecutor().execute(
        "radar.assess_data_quality",
        {
            "data": {
                "schema_version": "deterministic_quality_assessment.v1",
                "policy_version": "caller-invented.v1",
                "quality_state": "good",
                "data_sufficiency": "sufficient",
                "coverage_ratio": 1.0,
            },
            "source_refs": ["night_episode_revision:live:1"],
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "PermissionError"


def test_quality_tool_rejects_source_outside_fact_snapshot() -> None:
    result = ProductToolExecutor().execute(
        "radar.assess_data_quality",
        {
            "coverage_ratio": 0.95,
            "source_refs": ["quality:not-authorized"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_device_status_tool_uses_typed_canonical_projection() -> None:
    facts = _product_facts()
    result = ProductToolExecutor().execute(
        "radar.get_device_status",
        facts.tool_inputs()["radar.get_device_status"],
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
            episode_id="episode-phase3a-radar-data",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output == {
        "schema_version": "canonical_radar_device_status.v1",
        "data_mode": "live",
        "offline": False,
        "stale": False,
        "source_refs": ["night_episode_revision:live:1"],
    }


def test_product_device_status_rejects_unbound_source() -> None:
    result = ProductToolExecutor().execute(
        "radar.get_device_status",
        {
            "data": {
                "schema_version": "canonical_radar_device_status.v1",
                "data_mode": "live",
                "offline": False,
                "stale": False,
            },
            "source_refs": ["device-status:not-authorized"],
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def _adapter_fixture() -> tuple[
    ReplayRadarProvider,
    RadarNightEvidenceRequest,
]:
    provider = ReplayRadarProvider(scenario="normal_night")
    device = provider.list_devices()[0]
    assert device.bound_subject_id is not None
    return provider, RadarNightEvidenceRequest(
        authorized_radar_device_id=device.radar_device_id,
        expected_subject_id=device.bound_subject_id,
        night_of=NIGHT,
    )


class _NoDeviceListingProvider:
    def __init__(self, delegate: ReplayRadarProvider) -> None:
        self._delegate = delegate

    def list_devices(self):
        raise AssertionError("RadarDataAdapter must not discover a default device")

    def get_device(self, radar_device_id: str):
        return self._delegate.get_device(radar_device_id)

    def pull_snapshots(self, radar_device_id: str, **kwargs):
        return self._delegate.pull_snapshots(radar_device_id, **kwargs)

    def pull_night_report(self, radar_device_id: str, **kwargs):
        return self._delegate.pull_night_report(radar_device_id, **kwargs)


class _TamperedRadarProvider(_NoDeviceListingProvider):
    def __init__(
        self,
        delegate: ReplayRadarProvider,
        *,
        device_update: dict[str, object] | None = None,
        report_update: dict[str, object] | None = None,
        snapshot_update: dict[str, object] | None = None,
    ) -> None:
        super().__init__(delegate)
        self._device_update = device_update or {}
        self._report_update = report_update or {}
        self._snapshot_update = snapshot_update or {}

    def get_device(self, radar_device_id: str):
        return super().get_device(radar_device_id).model_copy(
            update=self._device_update
        )

    def pull_night_report(self, radar_device_id: str, **kwargs):
        report = super().pull_night_report(radar_device_id, **kwargs)
        if report is None:
            return None
        return report.model_copy(update=self._report_update)

    def pull_snapshots(self, radar_device_id: str, **kwargs):
        snapshots = super().pull_snapshots(radar_device_id, **kwargs)
        if not snapshots or not self._snapshot_update:
            return snapshots
        return [
            snapshots[0].model_copy(update=self._snapshot_update),
            *snapshots[1:],
        ]


class _TamperedQualityGate(DataQualityGate):
    def __init__(self, summary_update: dict[str, object]) -> None:
        super().__init__()
        self._summary_update = summary_update

    def run(self, **kwargs):
        return super().run(**kwargs).model_copy(update=self._summary_update)


def _snapshot(*, binding_subject: str = "elder-phase3a") -> FactSnapshot:
    as_of = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    return FactSnapshot.create(
        fact_snapshot_id="phase3a-radar-snapshot",
        binding=AuthenticatedBinding(
            actor_id="elder-phase3a",
            subject_id=binding_subject,
            role="elder",
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=as_of,
            timezone_name="Asia/Shanghai",
            date_start=NIGHT,
            date_end=NIGHT,
            valid_night_count=1,
        ),
        canonical_data_version="a" * 64,
        source_refs=("night_episode_revision:live:1",),
        created_at=as_of,
    )


def _product_facts(
    *,
    subject_id: str = "elder-phase3a",
) -> ProductRevisionFacts:
    return ProductRevisionFacts(
        night_episode_id="night-phase3a",
        night_episode_revision_id="revision-phase3a",
        night_episode_revision_number=1,
        subject_id=subject_id,
        data_mode=DataMode.LIVE,
        timezone_name="Asia/Shanghai",
        local_sleep_date=NIGHT.isoformat(),
        data_sufficiency="sufficient",
        canonical_observations=(),
        deterministic_quality={
            "schema_version": "deterministic_quality_assessment.v1",
            "policy_version": "quality-policy-reviewed.v3",
            "coverage_ratio": 0.9,
            "data_sufficiency": "sufficient",
            "quality_state": "good",
        },
        deterministic_risk={
            "risk_state": "no_reviewed_signal",
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        conflict_summaries=(),
        provenance_references=("night_episode_revision:live:1",),
        canonical_data_version="a" * 64,
    )
