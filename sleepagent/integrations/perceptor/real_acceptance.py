"""Read-only audit of a real Perceptor one-night acceptance chain.

The auditor never calls Perceptor, decrypts a raw payload, invokes an Agent, or
mutates a domain resource.  It checks an explicitly live evidence manifest
against already committed projections in the unified authority database.
Automated checks can fail a stage or leave it pending; only an approval bound
to the exact automated-audit digest can promote a passing stage/capability to
VERIFIED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from sleepagent.integrations.perceptor.acceptance import (
    RealAcceptanceStage,
    RealAcceptanceStageVerdict,
    RealAcceptanceStatus,
    RealPerceptorAcceptanceReport,
)
from sleepagent.sleep_domain.contracts import (
    AdapterCapability,
    AnalysisRole,
    AnalysisStatus,
    CapabilityVerificationReceipt,
    CapabilityVerificationStatus,
    DataMode,
    DomainEvent,
    DomainEventType,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeState,
    ObservationType,
    RoleViewStatus,
    Sha256Hex,
    SignatureVerificationState,
    SleepDomainContract,
    SleepObservation,
    VerificationReviewerKind,
)
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    SleepDomainRepository,
)


UTC = timezone.utc
PERCEPTOR_ADAPTER_ID = "perceptor-v1"
REQUIRED_PUSH_OBSERVATION_TYPES = frozenset(
    {
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.MOVEMENT,
        ObservationType.BED_PRESENCE,
    }
)
REQUIRED_ROLE_VIEWS = frozenset(
    {AnalysisRole.ELDER, AnalysisRole.FAMILY, AnalysisRole.DOCTOR}
)
ACCEPTANCE_EVENT_TYPES = frozenset(
    {
        DomainEventType.NIGHT_EPISODE_ANALYZED,
        DomainEventType.NIGHT_EPISODE_REVISED,
        DomainEventType.MORNING_REPORT_READY,
        DomainEventType.AGENT_ANALYSIS_READY,
        DomainEventType.AGENT_ANALYSIS_DEGRADED,
    }
)


class RealAcceptanceEvidenceError(ValueError):
    """The redacted evidence envelope is unsafe or internally inconsistent."""


class IdempotencyProbeKind(str, Enum):
    DUPLICATE_PUSH = "duplicate_push"
    REPORT_REPULL = "report_repull"


class RedactedAcceptanceArtifact(SleepDomainContract):
    """Content-addressed reference only; artifact bytes are stored elsewhere."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["redacted_acceptance_artifact.v1"] = (
        "redacted_acceptance_artifact.v1"
    )
    reference: str = Field(..., min_length=1, max_length=500)
    sha256: Sha256Hex
    captured_at: datetime

    @field_validator("reference")
    @classmethod
    def reference_is_opaque_and_redacted(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise RealAcceptanceEvidenceError(
                "artifact reference must be an opaque URI without whitespace"
            )
        parsed = urlsplit(value)
        if parsed.scheme not in {"evidence", "test-result", "approval", "urn"}:
            raise RealAcceptanceEvidenceError(
                "artifact reference must use an approved opaque URI scheme"
            )
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise RealAcceptanceEvidenceError(
                "artifact reference cannot carry query, fragment, or credentials"
            )
        lowered = value.lower()
        disallowed = (
            "fixture",
            "fake",
            "replay",
            "simulated",
            "bearer ",
            "token=",
            "secret=",
            "private-key",
            "payload-inline",
            "raw-payload",
            "raw_payload",
            "data:",
        )
        if any(marker in lowered for marker in disallowed):
            raise RealAcceptanceEvidenceError(
                "real acceptance cannot reference non-live or sensitive material"
            )
        return value

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise RealAcceptanceEvidenceError("captured_at must be timezone-aware")
        return value


class RealAcceptanceStageEvidence(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_acceptance_stage_evidence.v1"] = (
        "real_acceptance_stage_evidence.v1"
    )
    stage: RealAcceptanceStage
    evidence: tuple[RedactedAcceptanceArtifact, ...] = ()
    test_results: tuple[RedactedAcceptanceArtifact, ...] = ()

    @model_validator(mode="after")
    def artifact_references_are_unique(self) -> "RealAcceptanceStageEvidence":
        items = (*self.evidence, *self.test_results)
        identities = [(item.reference, item.sha256) for item in items]
        if len(identities) != len(set(identities)):
            raise RealAcceptanceEvidenceError(
                "stage evidence cannot repeat an artifact"
            )
        return self


_PROBE_COUNT_KEYS = {
    IdempotencyProbeKind.DUPLICATE_PUSH: frozenset(
        {
            "raw_ingress_records",
            "canonical_observations",
            "night_episodes",
            "night_episode_revisions",
            "domain_events",
        }
    ),
    IdempotencyProbeKind.REPORT_REPULL: frozenset(
        {
            "raw_ingress_records",
            "source_reports",
            "canonical_observations",
            "night_episode_revisions",
            "domain_events",
        }
    ),
}


class ScopedIdempotencyProbe(SleepDomainContract):
    """Redacted before/after proof produced by the real reference-client run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["scoped_idempotency_probe.v1"] = (
        "scoped_idempotency_probe.v1"
    )
    probe_kind: IdempotencyProbeKind
    first_resource_id: str = Field(..., min_length=1)
    repeated_resource_id: str = Field(..., min_length=1)
    counts_before: dict[str, int]
    counts_after: dict[str, int]
    test_result: RedactedAcceptanceArtifact
    performed_at: datetime

    @model_validator(mode="after")
    def proves_stable_scoped_resources(self) -> "ScopedIdempotencyProbe":
        if self.first_resource_id != self.repeated_resource_id:
            raise RealAcceptanceEvidenceError(
                "idempotency probe must return the original resource id"
            )
        expected_keys = _PROBE_COUNT_KEYS[self.probe_kind]
        if set(self.counts_before) != expected_keys:
            raise RealAcceptanceEvidenceError(
                "idempotency probe does not cover the required scoped counts"
            )
        if self.counts_before != self.counts_after:
            raise RealAcceptanceEvidenceError(
                "idempotency probe detected a duplicate committed resource"
            )
        if any(value < 0 for value in self.counts_before.values()):
            raise RealAcceptanceEvidenceError("probe counts cannot be negative")
        if self.performed_at.tzinfo is None or self.performed_at.utcoffset() is None:
            raise RealAcceptanceEvidenceError("performed_at must be timezone-aware")
        return self


class RealPerceptorAcceptanceManifest(SleepDomainContract):
    """Privacy-minimized scope and evidence pointers for one live night."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_perceptor_acceptance_manifest.v1"] = (
        "real_perceptor_acceptance_manifest.v1"
    )
    manifest_id: str = Field(..., min_length=1)
    namespace_id: str = Field(..., min_length=1)
    data_mode: Literal[DataMode.LIVE] = DataMode.LIVE
    adapter_id: Literal["perceptor-v1"] = "perceptor-v1"
    adapter_version: str = Field(..., min_length=1)
    adapter_artifact_sha256: Sha256Hex
    configuration_fingerprint: Sha256Hex
    compatibility_profile_id: str = Field(..., min_length=1)
    compatibility_profile_sha256: Sha256Hex
    device_scope_sha256: Sha256Hex
    local_sleep_date: date
    push_raw_ingress_record_ids: tuple[str, ...] = ()
    source_report_version_ids: tuple[str, ...] = ()
    night_episode_id: str | None = None
    night_episode_revision_id: str | None = None
    stage_evidence: tuple[RealAcceptanceStageEvidence, ...]
    duplicate_push_probe: ScopedIdempotencyProbe | None = None
    report_repull_probe: ScopedIdempotencyProbe | None = None
    created_at: datetime

    @model_validator(mode="after")
    def manifest_is_complete_and_live(self) -> "RealPerceptorAcceptanceManifest":
        if not self.namespace_id.startswith("live:"):
            raise RealAcceptanceEvidenceError(
                "real acceptance requires a live namespace"
            )
        scoped_identifiers = (
            self.manifest_id,
            self.namespace_id,
            self.compatibility_profile_id,
            *self.push_raw_ingress_record_ids,
            *self.source_report_version_ids,
            *(
                ()
                if self.night_episode_id is None
                else (self.night_episode_id,)
            ),
            *(
                ()
                if self.night_episode_revision_id is None
                else (self.night_episode_revision_id,)
            ),
        )
        if any(
            marker in identifier.lower()
            for identifier in scoped_identifiers
            for marker in ("fixture", "fake", "replay", "simulated")
        ):
            raise RealAcceptanceEvidenceError(
                "real acceptance scope cannot cite non-live identifiers"
            )
        stages = [item.stage for item in self.stage_evidence]
        if len(stages) != len(set(stages)) or set(stages) != set(
            RealAcceptanceStage
        ):
            raise RealAcceptanceEvidenceError(
                "manifest requires exactly one evidence entry for every stage"
            )
        for values, label in (
            (self.push_raw_ingress_record_ids, "push raw ids"),
            (self.source_report_version_ids, "source report ids"),
        ):
            if len(values) != len(set(values)):
                raise RealAcceptanceEvidenceError(
                    f"manifest {label} must be unique"
                )
        if self.night_episode_revision_id and not self.night_episode_id:
            raise RealAcceptanceEvidenceError(
                "a NightEpisode revision requires its aggregate id"
            )
        if self.duplicate_push_probe is not None:
            if (
                self.duplicate_push_probe.probe_kind
                != IdempotencyProbeKind.DUPLICATE_PUSH
                or self.duplicate_push_probe.first_resource_id
                not in self.push_raw_ingress_record_ids
            ):
                raise RealAcceptanceEvidenceError(
                    "duplicate push probe is outside the declared raw scope"
                )
        if self.report_repull_probe is not None:
            if (
                self.report_repull_probe.probe_kind
                != IdempotencyProbeKind.REPORT_REPULL
                or self.report_repull_probe.first_resource_id
                not in self.source_report_version_ids
            ):
                raise RealAcceptanceEvidenceError(
                    "report re-pull probe is outside the declared report scope"
                )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RealAcceptanceEvidenceError("created_at must be timezone-aware")
        push_auth_evidence = self.evidence_for(
            RealAcceptanceStage.PUSH_AUTHENTICATION
        ).evidence
        if push_auth_evidence and self.compatibility_profile_sha256 not in {
            item.sha256 for item in push_auth_evidence
        }:
            raise RealAcceptanceEvidenceError(
                "push-auth evidence must content-address the approved "
                "compatibility profile"
            )
        return self

    def evidence_for(
        self,
        stage: RealAcceptanceStage,
    ) -> RealAcceptanceStageEvidence:
        return next(item for item in self.stage_evidence if item.stage == stage)

    def sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class AuthorizedHumanAcceptanceApproval(SleepDomainContract):
    """Human decision bound to one immutable automated audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["authorized_human_acceptance_approval.v1"] = (
        "authorized_human_acceptance_approval.v1"
    )
    approval_id: str = Field(..., min_length=1)
    manifest_sha256: Sha256Hex
    automated_audit_sha256: Sha256Hex
    automated_audit_created_at: datetime
    approved_stages: tuple[RealAcceptanceStage, ...]
    approved_capabilities: tuple[AdapterCapability, ...] = ()
    reviewed_by_actor_id: str = Field(..., min_length=1)
    authorization_reference: RedactedAcceptanceArtifact
    approval_reference: RedactedAcceptanceArtifact
    reviewed_at: datetime

    @model_validator(mode="after")
    def approval_is_bounded(self) -> "AuthorizedHumanAcceptanceApproval":
        if not self.approved_stages:
            raise RealAcceptanceEvidenceError(
                "human approval must name at least one stage"
            )
        if len(self.approved_stages) != len(set(self.approved_stages)):
            raise RealAcceptanceEvidenceError("approved stages must be unique")
        if len(self.approved_capabilities) != len(
            set(self.approved_capabilities)
        ):
            raise RealAcceptanceEvidenceError(
                "approved capabilities must be unique"
            )
        allowed = {
            AdapterCapability.PUSH,
            AdapterCapability.PULL,
            AdapterCapability.SLEEP_REPORT,
        }
        if not set(self.approved_capabilities).issubset(allowed):
            raise RealAcceptanceEvidenceError(
                "approval contains a capability outside the real gate"
            )
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise RealAcceptanceEvidenceError("reviewed_at must be timezone-aware")
        if (
            self.automated_audit_created_at.tzinfo is None
            or self.automated_audit_created_at.utcoffset() is None
        ):
            raise RealAcceptanceEvidenceError(
                "automated_audit_created_at must be timezone-aware"
            )
        if self.reviewed_at < self.automated_audit_created_at:
            raise RealAcceptanceEvidenceError(
                "human approval cannot predate its automated audit"
            )
        return self


class AutomatedFindingStatus(str, Enum):
    PASSED = "passed"
    PENDING = "pending"
    FAILED = "failed"


class RealAcceptanceAutomatedFinding(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_acceptance_automated_finding.v1"] = (
        "real_acceptance_automated_finding.v1"
    )
    stage: RealAcceptanceStage
    status: AutomatedFindingStatus
    passed_check_codes: tuple[str, ...]
    committed_facts_sha256: Sha256Hex
    blocking_reasons: tuple[str, ...] = ()
    failure_code: str | None = None

    @model_validator(mode="after")
    def finding_state_is_coherent(self) -> "RealAcceptanceAutomatedFinding":
        if not self.passed_check_codes:
            raise ValueError("automated finding must enumerate its checks")
        if self.status == AutomatedFindingStatus.PASSED and (
            self.blocking_reasons or self.failure_code
        ):
            raise ValueError("passed finding cannot carry blockers or failure")
        if self.status == AutomatedFindingStatus.PENDING and (
            not self.blocking_reasons or self.failure_code
        ):
            raise ValueError("pending finding requires blockers only")
        if self.status == AutomatedFindingStatus.FAILED and not self.failure_code:
            raise ValueError("failed finding requires a stable failure code")
        return self


class RealPerceptorAutomatedAudit(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_perceptor_automated_audit.v1"] = (
        "real_perceptor_automated_audit.v1"
    )
    manifest_id: str
    manifest_sha256: Sha256Hex
    audit_sha256: Sha256Hex
    findings: tuple[RealAcceptanceAutomatedFinding, ...]
    created_at: datetime

    @model_validator(mode="after")
    def findings_cover_every_stage(self) -> "RealPerceptorAutomatedAudit":
        stages = [item.stage for item in self.findings]
        if len(stages) != len(set(stages)) or set(stages) != set(
            RealAcceptanceStage
        ):
            raise ValueError("automated audit must cover every acceptance stage")
        expected = _audit_sha256(
            manifest_id=self.manifest_id,
            manifest_sha256=self.manifest_sha256,
            findings=self.findings,
        )
        if self.audit_sha256 != expected:
            raise ValueError("automated audit digest does not match its findings")
        return self

    def finding_for(
        self,
        stage: RealAcceptanceStage,
    ) -> RealAcceptanceAutomatedFinding:
        return next(item for item in self.findings if item.stage == stage)


class RealPerceptorAcceptanceAuditBundle(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_perceptor_acceptance_audit_bundle.v1"] = (
        "real_perceptor_acceptance_audit_bundle.v1"
    )
    manifest_id: str
    automated_audit: RealPerceptorAutomatedAudit
    report: RealPerceptorAcceptanceReport

    @model_validator(mode="after")
    def bundle_scope_matches(self) -> "RealPerceptorAcceptanceAuditBundle":
        if (
            self.automated_audit.manifest_id != self.manifest_id
            or self.report.evidence_manifest_sha256
            != self.automated_audit.manifest_sha256
            or self.report.automated_audit_sha256
            != self.automated_audit.audit_sha256
        ):
            raise ValueError("acceptance bundle components have different scopes")
        return self


class DomainEventReader(Protocol):
    def list_domain_events_after(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        delivery_offset: int,
        limit: int,
    ) -> tuple[DomainEvent, ...]: ...

    def has_domain_events_after(
        self,
        namespace: DomainNamespace,
        *,
        subject_id: str,
        delivery_offset: int,
    ) -> bool: ...


@dataclass
class _Finding:
    stage: RealAcceptanceStage
    passed: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    failure_code: str | None = None
    committed_facts: list[object] = field(default_factory=list)

    def ok(self, code: str) -> None:
        if code not in self.passed:
            self.passed.append(code)

    def pending(self, reason: str) -> None:
        if reason not in self.blockers:
            self.blockers.append(reason)

    def fail(self, code: str) -> None:
        if self.failure_code is None:
            self.failure_code = code

    def fact(self, value: object) -> None:
        if hasattr(value, "model_dump"):
            self.committed_facts.append(value.model_dump(mode="json"))
        else:
            self.committed_facts.append(value)

    def finish(self) -> RealAcceptanceAutomatedFinding:
        passed = tuple(self.passed or ["stage_scope_loaded"])
        committed_facts_sha256 = _canonical_sha256(self.committed_facts)
        if self.failure_code:
            return RealAcceptanceAutomatedFinding(
                stage=self.stage,
                status=AutomatedFindingStatus.FAILED,
                passed_check_codes=passed,
                committed_facts_sha256=committed_facts_sha256,
                failure_code=self.failure_code,
            )
        if self.blockers:
            return RealAcceptanceAutomatedFinding(
                stage=self.stage,
                status=AutomatedFindingStatus.PENDING,
                passed_check_codes=passed,
                committed_facts_sha256=committed_facts_sha256,
                blocking_reasons=tuple(self.blockers),
            )
        return RealAcceptanceAutomatedFinding(
            stage=self.stage,
            status=AutomatedFindingStatus.PASSED,
            passed_check_codes=passed,
            committed_facts_sha256=committed_facts_sha256,
        )


@dataclass(frozen=True)
class _AuditContext:
    episode: NightEpisode | None
    revision: NightEpisodeRevision | None
    observations: tuple[SleepObservation, ...]


class RealPerceptorAcceptanceAuditor:
    """Verify a declared real-night scope using committed projections only."""

    def __init__(
        self,
        repository: SleepDomainRepository,
        *,
        event_reader: DomainEventReader | None = None,
    ) -> None:
        self.repository = repository
        self.event_reader = event_reader

    def audit(
        self,
        manifest: RealPerceptorAcceptanceManifest,
        *,
        audited_at: datetime | None = None,
    ) -> RealPerceptorAutomatedAudit:
        created_at = audited_at or datetime.now(tz=UTC)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("audited_at must be timezone-aware")
        namespace = DomainNamespace(manifest.namespace_id, DataMode.LIVE)
        context = self._load_context(namespace, manifest)
        findings = (
            self._audit_transport(namespace, manifest).finish(),
            self._audit_push_authentication(namespace, manifest).finish(),
            self._audit_pull_report(namespace, manifest, context).finish(),
            self._audit_binding(namespace, manifest, context).finish(),
            self._audit_night_episode(manifest, context).finish(),
            self._audit_fast_path(namespace, manifest, context).finish(),
            self._audit_slow_path(namespace, manifest, context).finish(),
            self._audit_api(namespace, manifest, context).finish(),
        )
        manifest_sha256 = manifest.sha256()
        audit_sha256 = _audit_sha256(
            manifest_id=manifest.manifest_id,
            manifest_sha256=manifest_sha256,
            findings=findings,
        )
        return RealPerceptorAutomatedAudit(
            manifest_id=manifest.manifest_id,
            manifest_sha256=manifest_sha256,
            audit_sha256=audit_sha256,
            findings=findings,
            created_at=created_at,
        )

    def build_bundle(
        self,
        manifest: RealPerceptorAcceptanceManifest,
        *,
        approval: AuthorizedHumanAcceptanceApproval | None = None,
        audited_at: datetime | None = None,
    ) -> RealPerceptorAcceptanceAuditBundle:
        effective_audited_at = audited_at
        if effective_audited_at is None and approval is not None:
            effective_audited_at = approval.automated_audit_created_at
        audit = self.audit(manifest, audited_at=effective_audited_at)
        if approval is not None:
            self._validate_approval(manifest, audit, approval)
        report = self._build_report(manifest, audit, approval)
        return RealPerceptorAcceptanceAuditBundle(
            manifest_id=manifest.manifest_id,
            automated_audit=audit,
            report=report,
        )

    def _load_context(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
    ) -> _AuditContext:
        episode = (
            None
            if manifest.night_episode_id is None
            else self.repository.get_night_episode(
                namespace,
                night_episode_id=manifest.night_episode_id,
            )
        )
        revision = (
            None
            if manifest.night_episode_revision_id is None
            else self.repository.get_night_episode_revision(
                namespace,
                night_episode_revision_id=manifest.night_episode_revision_id,
            )
        )
        observations: list[SleepObservation] = []
        if revision is not None:
            for observation_id in revision.observation_ids:
                observation = self.repository.get_observation(
                    namespace,
                    observation_id=observation_id,
                )
                if observation is not None:
                    observations.append(observation)
        return _AuditContext(
            episode=episode,
            revision=revision,
            observations=tuple(observations),
        )

    def _adapter_scope(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        finding: _Finding,
    ) -> None:
        descriptor = self.repository.get_adapter_descriptor(
            namespace,
            adapter_id=PERCEPTOR_ADAPTER_ID,
            adapter_version=manifest.adapter_version,
        )
        if descriptor is None:
            finding.pending("committed_perceptor_adapter_descriptor_unavailable")
            return
        finding.fact(descriptor)
        if (
            descriptor.provider_id != "perceptor"
            or descriptor.adapter_artifact_sha256
            != manifest.adapter_artifact_sha256
            or descriptor.configuration_fingerprint
            != manifest.configuration_fingerprint
            or DataMode.LIVE not in descriptor.supported_data_modes
        ):
            finding.fail("perceptor_adapter_scope_mismatch")
            return
        finding.ok("committed_perceptor_adapter_scope_matches")

    @staticmethod
    def _evidence(
        manifest: RealPerceptorAcceptanceManifest,
        finding: _Finding,
    ) -> None:
        scoped = manifest.evidence_for(finding.stage)
        if not scoped.evidence:
            finding.pending("redacted_real_evidence_unavailable")
        else:
            finding.ok("redacted_real_evidence_content_addressed")
        if not scoped.test_results:
            finding.pending("immutable_acceptance_test_result_unavailable")
        else:
            finding.ok("immutable_acceptance_test_result_content_addressed")

    def _audit_transport(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.TRANSPORT)
        self._adapter_scope(namespace, manifest, finding)
        if not manifest.push_raw_ingress_record_ids:
            finding.pending("real_fastapi_push_raw_scope_unavailable")
        for raw_id in manifest.push_raw_ingress_record_ids:
            raw = self.repository.get_raw_record(
                namespace,
                raw_ingress_record_id=raw_id,
            )
            if raw is None:
                finding.fail("declared_push_raw_record_not_found")
                continue
            finding.fact(raw)
            if raw.data_mode != DataMode.LIVE or raw.provider_id != "perceptor":
                finding.fail("non_live_or_non_perceptor_push_record")
                continue
            if raw.idempotency_version not in {
                "provider-message-id.v1",
                "full-payload-sha256.v1",
            }:
                finding.fail("push_ingress_identity_profile_mismatch")
                continue
            finding.ok("production_fastapi_push_committed")
        if manifest.duplicate_push_probe is None:
            finding.pending("duplicate_push_probe_not_performed")
        else:
            raw = self.repository.get_raw_record(
                namespace,
                raw_ingress_record_id=(
                    manifest.duplicate_push_probe.first_resource_id
                ),
            )
            if raw is None:
                finding.fail("duplicate_push_original_resource_not_found")
            else:
                finding.fact(raw)
                finding.ok("duplicate_push_returned_original_without_new_resources")
        self._evidence(manifest, finding)
        return finding

    def _audit_push_authentication(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.PUSH_AUTHENTICATION)
        self._adapter_scope(namespace, manifest, finding)
        if not manifest.push_raw_ingress_record_ids:
            finding.pending("signed_real_push_scope_unavailable")
        for raw_id in manifest.push_raw_ingress_record_ids:
            raw = self.repository.get_raw_record(
                namespace,
                raw_ingress_record_id=raw_id,
            )
            if raw is None:
                finding.fail("declared_push_auth_record_not_found")
                continue
            finding.fact(raw)
            if (
                raw.signature_verification
                != SignatureVerificationState.VERIFIED
                or raw.signature_profile != manifest.compatibility_profile_id
                or raw.request_signed_at is None
            ):
                finding.fail("real_push_signature_scope_not_verified")
                continue
            finding.ok("real_push_signature_and_time_verified")
        self._evidence(manifest, finding)
        return finding

    def _audit_pull_report(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.PULL_REPORT_NORMALIZATION)
        self._adapter_scope(namespace, manifest, finding)
        if not manifest.source_report_version_ids:
            finding.pending("real_sleep_report_unavailable")
        report_raw_ids: set[str] = set()
        report_hashes: list[str] = []
        for report_id in manifest.source_report_version_ids:
            report = self.repository.get_source_report_version(
                namespace,
                source_report_version_id=report_id,
            )
            if report is None:
                finding.fail("declared_source_report_not_found")
                continue
            finding.fact(
                {
                    "source_report_version_id": report.source_report_version_id,
                    "provider_id": report.provider_id,
                    "provider_account_id": report.provider_account_id,
                    "provider_device_key": report.provider_device_key,
                    "local_report_date": report.local_report_date.isoformat(),
                    "report_version": report.report_version,
                    "content_sha256": report.content_sha256,
                    "raw_ingress_record_id": report.raw_ingress_record_id,
                    "is_empty": report.is_empty,
                    "fetched_at": report.fetched_at.isoformat(),
                }
            )
            if (
                report.provider_id != "perceptor"
                or report.local_report_date != manifest.local_sleep_date
            ):
                finding.fail("source_report_scope_mismatch")
                continue
            if report.is_empty:
                finding.pending("real_sleep_report_is_empty")
            raw = self.repository.get_raw_record(
                namespace,
                raw_ingress_record_id=report.raw_ingress_record_id,
            )
            if (
                raw is None
                or raw.data_mode != DataMode.LIVE
                or raw.provider_id != "perceptor"
                or raw.idempotency_version != "perceptor_pull.v1"
            ):
                finding.fail("source_report_raw_provenance_mismatch")
                continue
            finding.fact(raw)
            report_raw_ids.add(report.raw_ingress_record_id)
            report_hashes.append(report.content_sha256)
            finding.ok("real_source_report_content_version_committed")
        if context.revision is not None and manifest.source_report_version_ids:
            if (
                tuple(context.revision.source_report_references)
                != manifest.source_report_version_ids
                or tuple(context.revision.source_report_sha256)
                != tuple(report_hashes)
            ):
                finding.fail("night_revision_source_report_scope_mismatch")
            else:
                finding.ok("night_revision_pins_exact_source_report_hashes")
        normalized_types = {
            observation.observation_type
            for observation in context.observations
            if observation.provenance.raw_ingress_record_id in report_raw_ids
        }
        if ObservationType.SLEEP_STAGE_INTERVAL not in normalized_types:
            finding.pending("real_sleep_stage_normalization_unavailable")
        else:
            finding.ok("real_sleep_stage_normalization_committed")
        if ObservationType.VENDOR_SLEEP_PROFILE_METRIC not in normalized_types:
            finding.pending("real_sleep_profile_normalization_unavailable")
        else:
            finding.ok("real_sleep_profile_normalization_committed")
        if manifest.report_repull_probe is None:
            finding.pending("report_repull_probe_not_performed")
        else:
            report = self.repository.get_source_report_version(
                namespace,
                source_report_version_id=(
                    manifest.report_repull_probe.first_resource_id
                ),
            )
            if report is None:
                finding.fail("report_repull_original_resource_not_found")
            else:
                finding.fact(
                    {
                        "source_report_version_id": report.source_report_version_id,
                        "content_sha256": report.content_sha256,
                        "report_version": report.report_version,
                    }
                )
                finding.ok("report_repull_returned_original_without_new_resources")
        self._evidence(manifest, finding)
        return finding

    def _audit_binding(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.BINDING)
        if context.episode is None or context.revision is None:
            finding.pending("committed_night_scope_unavailable_for_binding_audit")
            self._evidence(manifest, finding)
            return finding
        if len(context.observations) != len(context.revision.observation_ids):
            finding.fail("night_revision_observation_not_found")
        binding_by_id = {}
        for reference in context.episode.binding_references:
            binding = self.repository.get_device_binding(
                namespace,
                device_binding_id=reference.device_binding_id,
            )
            if binding is None:
                finding.fail("night_episode_binding_not_found")
                continue
            finding.fact(binding)
            if (
                binding.binding_version != reference.binding_version
                or binding.device_id != reference.device_id
                or binding.subject_id != context.episode.subject_id
                or binding.provider_id != "perceptor"
                or binding.data_mode != DataMode.LIVE
            ):
                finding.fail("night_episode_binding_scope_mismatch")
                continue
            binding_by_id[binding.device_binding_id] = binding
        if binding_by_id:
            computed_scope = device_scope_sha256_for_bindings(
                tuple(binding_by_id.values())
            )
            if computed_scope != manifest.device_scope_sha256:
                finding.fail("device_scope_sha256_mismatch")
            else:
                finding.ok("authoritative_device_scope_hash_matches")
        push_types: set[ObservationType] = set()
        for observation in context.observations:
            finding.fact(observation)
            binding = binding_by_id.get(observation.device_binding_id)
            observed_at = (
                observation.measurement_at
                or observation.event_occurred_at
                or observation.received_at
            )
            if (
                binding is None
                or observation.binding_version != binding.binding_version
                or observation.device_id != binding.device_id
                or observation.subject_id != binding.subject_id
                or observation.provenance.provider_id != "perceptor"
                or observation.provenance.adapter_id != PERCEPTOR_ADAPTER_ID
                or observed_at < binding.effective_from
                or (
                    binding.effective_until is not None
                    and observed_at >= binding.effective_until
                )
            ):
                finding.fail("canonical_observation_binding_mismatch")
                continue
            raw = self.repository.get_raw_record(
                namespace,
                raw_ingress_record_id=(
                    observation.provenance.raw_ingress_record_id
                ),
            )
            if (
                raw is None
                or raw.pre_normalization_payload_sha256
                != observation.provenance.raw_payload_sha256
                or raw.provider_account_id
                != observation.provenance.provider_account_id
            ):
                finding.fail("canonical_observation_raw_provenance_mismatch")
                continue
            if (
                observation.provenance.raw_ingress_record_id
                in manifest.push_raw_ingress_record_ids
            ):
                push_types.add(observation.observation_type)
            finding.ok("canonical_observation_binding_and_raw_provenance_match")
        missing_types = REQUIRED_PUSH_OBSERVATION_TYPES - push_types
        if missing_types:
            finding.pending(
                "real_push_missing_required_canonical_types:"
                + ",".join(sorted(item.value for item in missing_types))
            )
        else:
            finding.ok("real_push_has_hr_rr_movement_and_bed_state")
        self._evidence(manifest, finding)
        return finding

    def _audit_night_episode(
        self,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.NIGHT_EPISODE)
        if context.episode is None:
            finding.pending("committed_night_episode_unavailable")
        elif (
            context.episode.data_mode != DataMode.LIVE
            or context.episode.local_sleep_date != manifest.local_sleep_date
        ):
            finding.fail("night_episode_scope_mismatch")
        elif context.episode.state not in {
            NightEpisodeState.ANALYZED,
            NightEpisodeState.CLOSED,
            NightEpisodeState.REVISED,
        }:
            finding.pending("night_episode_not_analyzed_or_closed")
        else:
            finding.fact(context.episode)
            finding.ok("live_night_episode_reached_committed_post_collection_state")
        if context.revision is None:
            finding.pending("committed_night_episode_revision_unavailable")
        elif context.episode is None:
            finding.fail("night_revision_without_declared_episode")
        elif (
            context.revision.data_mode != DataMode.LIVE
            or context.revision.night_episode_id
            != context.episode.night_episode_id
            or context.revision.subject_id != context.episode.subject_id
            or context.episode.current_night_episode_revision_id
            != context.revision.night_episode_revision_id
            or not context.revision.observation_ids
        ):
            finding.fail("current_night_revision_scope_mismatch")
        else:
            finding.fact(context.revision)
            finding.ok("exact_current_night_revision_is_committed")
        self._evidence(manifest, finding)
        return finding

    def _audit_fast_path(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.DETERMINISTIC_FAST_PATH)
        if context.episode is None or context.revision is None:
            finding.pending("exact_night_revision_unavailable_for_fast_path")
            self._evidence(manifest, finding)
            return finding
        quality = self.repository.get_current_quality(
            namespace,
            night_episode_id=context.episode.night_episode_id,
        )
        risk = self.repository.get_current_risk(
            namespace,
            night_episode_id=context.episode.night_episode_id,
        )
        if quality is None or risk is None:
            finding.pending("committed_quality_or_current_risk_unavailable")
        else:
            finding.fact(quality)
            finding.fact(risk)
            expected_observations = set(context.revision.observation_ids)
            scopes = (quality.source_scope, risk.source_scope)
            if any(
                scope.night_episode_id != context.episode.night_episode_id
                or scope.night_episode_revision_id
                != context.revision.night_episode_revision_id
                or not set(scope.observation_ids).issubset(expected_observations)
                for scope in scopes
            ):
                finding.fail("fast_path_source_scope_mismatch")
            elif (
                quality.data_mode != DataMode.LIVE
                or risk.data_mode != DataMode.LIVE
                or quality.subject_id != context.episode.subject_id
                or risk.subject_id != context.episode.subject_id
            ):
                finding.fail("fast_path_live_subject_scope_mismatch")
            else:
                finding.ok("quality_and_current_risk_bind_exact_live_revision")
        self._evidence(manifest, finding)
        return finding

    def _audit_slow_path(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.AGENT_SLOW_PATH)
        if context.revision is None:
            finding.pending("exact_night_revision_unavailable_for_slow_path")
            self._evidence(manifest, finding)
            return finding
        analyses = self.repository.list_analysis_revisions(
            namespace,
            night_episode_revision_id=(
                context.revision.night_episode_revision_id
            ),
        )
        ready = [
            item
            for item in analyses
            if item.status == AnalysisStatus.READY
            and item.execution_mode == "intelligent"
            and item.data_mode == DataMode.LIVE
        ]
        if not ready:
            finding.pending(
                "four_agent_intelligent_analysis_unavailable_or_degraded"
            )
            self._evidence(manifest, finding)
            return finding
        analysis = ready[-1]
        finding.fact(analysis)
        views = self.repository.list_analysis_role_views(
            namespace,
            analysis_revision_id=analysis.analysis_revision_id,
        )
        roles = {
            view.role
            for view in views
            if view.status == RoleViewStatus.READY
            and view.execution_mode == "intelligent"
            and view.data_mode == DataMode.LIVE
            and view.night_episode_revision_id
            == context.revision.night_episode_revision_id
            and view.subject_id == context.revision.subject_id
        }
        for view in views:
            finding.fact(view)
        if roles != REQUIRED_ROLE_VIEWS:
            finding.pending("elder_family_doctor_role_views_not_all_ready")
        elif analysis.result_resource_id is None:
            finding.fail("ready_analysis_missing_result_resource")
        else:
            finding.ok("four_agent_analysis_and_three_role_views_ready")
        self._evidence(manifest, finding)
        return finding

    def _audit_api(
        self,
        namespace: DomainNamespace,
        manifest: RealPerceptorAcceptanceManifest,
        context: _AuditContext,
    ) -> _Finding:
        finding = _Finding(RealAcceptanceStage.API_REFERENCE_CLIENT)
        if context.episode is None or context.revision is None:
            finding.pending("exact_night_revision_unavailable_for_api_audit")
        elif self.event_reader is None:
            finding.pending("domain_event_reader_unavailable")
        else:
            events = self.event_reader.list_domain_events_after(
                namespace,
                subject_id=context.episode.subject_id,
                delivery_offset=0,
                limit=10_000,
            )
            relevant = [
                event
                for event in events
                if event.data_mode == DataMode.LIVE
                and event.night_episode_id == context.episode.night_episode_id
                and event.night_episode_revision_id
                == context.revision.night_episode_revision_id
                and event.event_type in ACCEPTANCE_EVENT_TYPES
            ]
            for event in relevant:
                finding.fact(event)
            if not relevant:
                finding.pending("committed_revision_event_unavailable")
            elif len(events) == 10_000 and self.event_reader.has_domain_events_after(
                namespace,
                subject_id=context.episode.subject_id,
                delivery_offset=events[-1].delivery_offset,
            ):
                finding.pending("domain_event_audit_scope_truncated")
            else:
                finding.ok("committed_revision_event_is_queryable")
        self._evidence(manifest, finding)
        return finding

    @staticmethod
    def _validate_approval(
        manifest: RealPerceptorAcceptanceManifest,
        audit: RealPerceptorAutomatedAudit,
        approval: AuthorizedHumanAcceptanceApproval,
    ) -> None:
        if (
            approval.manifest_sha256 != audit.manifest_sha256
            or approval.automated_audit_sha256 != audit.audit_sha256
            or approval.automated_audit_created_at != audit.created_at
        ):
            raise RealAcceptanceEvidenceError(
                "human approval is not bound to this exact manifest/audit"
            )
        approved_stages = set(approval.approved_stages)
        for stage in approved_stages:
            if (
                audit.finding_for(stage).status
                != AutomatedFindingStatus.PASSED
            ):
                raise RealAcceptanceEvidenceError(
                    "human approval cannot override a pending or failed check"
                )
        capability_stages = {
            AdapterCapability.PUSH: {
                RealAcceptanceStage.TRANSPORT,
                RealAcceptanceStage.PUSH_AUTHENTICATION,
            },
            AdapterCapability.PULL: {
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION
            },
            AdapterCapability.SLEEP_REPORT: {
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION
            },
        }
        for capability in approval.approved_capabilities:
            if not capability_stages[capability].issubset(approved_stages):
                raise RealAcceptanceEvidenceError(
                    "capability approval requires all of its passing stages"
                )
        if approval.reviewed_at < audit.created_at:
            raise RealAcceptanceEvidenceError(
                "human approval cannot predate the automated audit"
            )

    def _build_report(
        self,
        manifest: RealPerceptorAcceptanceManifest,
        audit: RealPerceptorAutomatedAudit,
        approval: AuthorizedHumanAcceptanceApproval | None,
    ) -> RealPerceptorAcceptanceReport:
        capability_stages = {
            AdapterCapability.PUSH: (
                RealAcceptanceStage.TRANSPORT,
                RealAcceptanceStage.PUSH_AUTHENTICATION,
            ),
            AdapterCapability.PULL: (
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION,
            ),
            AdapterCapability.SLEEP_REPORT: (
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION,
            ),
        }
        approved_stages = set(() if approval is None else approval.approved_stages)
        approved_capabilities = set(
            () if approval is None else approval.approved_capabilities
        )
        receipts: dict[AdapterCapability, CapabilityVerificationReceipt] = {}
        for capability, stages in capability_stages.items():
            findings = [audit.finding_for(stage) for stage in stages]
            if any(
                item.status == AutomatedFindingStatus.FAILED
                for item in findings
            ):
                status = CapabilityVerificationStatus.FAILED
            elif (
                approval is not None
                and capability in approved_capabilities
                and all(stage in approved_stages for stage in stages)
                and all(
                    item.status == AutomatedFindingStatus.PASSED
                    for item in findings
                )
            ):
                status = CapabilityVerificationStatus.VERIFIED
            else:
                status = CapabilityVerificationStatus.PENDING
            approval_items = (
                (approval.authorization_reference, approval.approval_reference)
                if status == CapabilityVerificationStatus.VERIFIED
                and approval is not None
                else ()
            )
            evidence_items = _artifacts(
                *(
                    manifest.evidence_for(stage).evidence
                    for stage in stages
                ),
                approval_items,
            )
            test_items = _artifacts(
                *(
                    manifest.evidence_for(stage).test_results
                    for stage in stages
                ),
                self._probe_artifacts(manifest, stages),
            )
            receipt_id = (
                f"capability-receipt:perceptor:{capability.value}:"
                f"{audit.audit_sha256}"
            )
            receipts[capability] = CapabilityVerificationReceipt(
                receipt_id=receipt_id,
                data_mode=DataMode.LIVE,
                adapter_id=PERCEPTOR_ADAPTER_ID,
                adapter_version=manifest.adapter_version,
                adapter_artifact_sha256=manifest.adapter_artifact_sha256,
                configuration_fingerprint=manifest.configuration_fingerprint,
                capability=capability,
                environment="production-acceptance",
                status=status,
                evidence_references=tuple(
                    item.reference for item in evidence_items
                ),
                evidence_sha256=tuple(item.sha256 for item in evidence_items),
                test_result_references=tuple(
                    item.reference for item in test_items
                ),
                test_result_sha256=tuple(item.sha256 for item in test_items),
                conformance_result=(
                    "real_scope_human_approved"
                    if status == CapabilityVerificationStatus.VERIFIED
                    else (
                        "real_scope_failed"
                        if status == CapabilityVerificationStatus.FAILED
                        else "real_scope_pending_human_or_vendor_facts"
                    )
                ),
                reviewer_kind=(
                    VerificationReviewerKind.HUMAN
                    if status == CapabilityVerificationStatus.VERIFIED
                    else VerificationReviewerKind.AUTOMATED
                ),
                reviewed_by_actor_id=(
                    approval.reviewed_by_actor_id
                    if status == CapabilityVerificationStatus.VERIFIED
                    and approval is not None
                    else None
                ),
                reviewed_at=(
                    approval.reviewed_at
                    if status == CapabilityVerificationStatus.VERIFIED
                    and approval is not None
                    else audit.created_at
                ),
            )

        stage_capabilities = {
            RealAcceptanceStage.TRANSPORT: (AdapterCapability.PUSH,),
            RealAcceptanceStage.PUSH_AUTHENTICATION: (
                AdapterCapability.PUSH,
            ),
            RealAcceptanceStage.PULL_REPORT_NORMALIZATION: (
                AdapterCapability.PULL,
                AdapterCapability.SLEEP_REPORT,
            ),
        }
        verdicts: list[RealAcceptanceStageVerdict] = []
        for stage in RealAcceptanceStage:
            finding = audit.finding_for(stage)
            capabilities = stage_capabilities.get(stage, ())
            stage_receipts = tuple(
                receipts[item].receipt_id for item in capabilities
            )
            receipt_statuses = tuple(
                receipts[item].status for item in capabilities
            )
            approved = approval is not None and stage in approved_stages
            if finding.status == AutomatedFindingStatus.FAILED:
                status = RealAcceptanceStatus.FAILED
                blocking_reasons: tuple[str, ...] = ()
                failure_code = finding.failure_code
            elif (
                finding.status == AutomatedFindingStatus.PASSED
                and approved
                and all(
                    item == CapabilityVerificationStatus.VERIFIED
                    for item in receipt_statuses
                )
            ):
                status = RealAcceptanceStatus.VERIFIED
                blocking_reasons = ()
                failure_code = None
            else:
                status = RealAcceptanceStatus.PENDING
                reasons = list(finding.blocking_reasons)
                if finding.status == AutomatedFindingStatus.PASSED and not approved:
                    reasons.append("authorized_human_stage_approval_required")
                if (
                    finding.status == AutomatedFindingStatus.PASSED
                    and approved
                    and any(
                        item != CapabilityVerificationStatus.VERIFIED
                        for item in receipt_statuses
                    )
                ):
                    reasons.append(
                        "authorized_human_capability_approval_required"
                    )
                blocking_reasons = tuple(dict.fromkeys(reasons))
                failure_code = None
            scoped = manifest.evidence_for(stage)
            approval_items = (
                (approval.authorization_reference, approval.approval_reference)
                if status == RealAcceptanceStatus.VERIFIED
                and approval is not None
                else ()
            )
            evidence_items = _artifacts(
                scoped.evidence,
                approval_items,
            )
            test_items = _artifacts(
                scoped.test_results,
                self._probe_artifacts(manifest, (stage,)),
            )
            verdicts.append(
                RealAcceptanceStageVerdict(
                    stage=stage,
                    status=status,
                    namespace_id=manifest.namespace_id,
                    device_scope_sha256=manifest.device_scope_sha256,
                    local_sleep_date=manifest.local_sleep_date,
                    evidence_references=tuple(
                        item.reference for item in evidence_items
                    ),
                    evidence_sha256=tuple(
                        item.sha256 for item in evidence_items
                    ),
                    test_result_references=tuple(
                        item.reference for item in test_items
                    ),
                    test_result_sha256=tuple(
                        item.sha256 for item in test_items
                    ),
                    capability_receipt_ids=stage_receipts,
                    receipt_not_applicable_reason=(
                        None
                        if capabilities
                        else "platform stage is not an AdapterCapability"
                    ),
                    blocking_reasons=blocking_reasons,
                    failure_code=failure_code,
                    reviewer_kind=(
                        VerificationReviewerKind.HUMAN
                        if status == RealAcceptanceStatus.VERIFIED
                        else VerificationReviewerKind.AUTOMATED
                    ),
                    reviewed_by_actor_id=(
                        approval.reviewed_by_actor_id
                        if status == RealAcceptanceStatus.VERIFIED
                        and approval is not None
                        else None
                    ),
                    reviewed_at=(
                        approval.reviewed_at
                        if status == RealAcceptanceStatus.VERIFIED
                        and approval is not None
                        else audit.created_at
                    ),
                )
            )
        report_id = "real-perceptor-acceptance:" + _canonical_sha256(
            {
                "manifest_id": manifest.manifest_id,
                "manifest_sha256": audit.manifest_sha256,
                "audit_sha256": audit.audit_sha256,
                "approval_id": None if approval is None else approval.approval_id,
            }
        )
        duplicate_checked = (
            manifest.duplicate_push_probe is not None
            and audit.finding_for(RealAcceptanceStage.TRANSPORT).status
            == AutomatedFindingStatus.PASSED
        )
        repull_checked = (
            manifest.report_repull_probe is not None
            and audit.finding_for(
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION
            ).status
            == AutomatedFindingStatus.PASSED
        )
        return RealPerceptorAcceptanceReport(
            report_id=report_id,
            namespace_id=manifest.namespace_id,
            adapter_version=manifest.adapter_version,
            evidence_manifest_sha256=audit.manifest_sha256,
            automated_audit_sha256=audit.audit_sha256,
            device_scope_sha256=manifest.device_scope_sha256,
            local_sleep_date=manifest.local_sleep_date,
            verdicts=tuple(verdicts),
            capability_receipts=tuple(receipts.values()),
            duplicate_push_checked=duplicate_checked,
            report_repull_checked=repull_checked,
            created_at=audit.created_at,
        )

    @staticmethod
    def _probe_artifacts(
        manifest: RealPerceptorAcceptanceManifest,
        stages: tuple[RealAcceptanceStage, ...],
    ) -> tuple[RedactedAcceptanceArtifact, ...]:
        artifacts: list[RedactedAcceptanceArtifact] = []
        if (
            RealAcceptanceStage.TRANSPORT in stages
            and manifest.duplicate_push_probe is not None
        ):
            artifacts.append(manifest.duplicate_push_probe.test_result)
        if (
            RealAcceptanceStage.PULL_REPORT_NORMALIZATION in stages
            and manifest.report_repull_probe is not None
        ):
            artifacts.append(manifest.report_repull_probe.test_result)
        return tuple(artifacts)


def device_scope_sha256_for_bindings(bindings: tuple[object, ...]) -> str:
    """Hash the exact live binding/device scope without exporting identifiers."""

    materials = []
    for binding in bindings:
        provider_device = getattr(binding, "provider_device")
        materials.append(
            {
                "provider_id": getattr(binding, "provider_id"),
                "provider_account_id": getattr(binding, "provider_account_id"),
                "device_id": getattr(binding, "device_id"),
                "device_binding_id": getattr(binding, "device_binding_id"),
                "binding_version": getattr(binding, "binding_version"),
                "provider_device": provider_device.model_dump(mode="json"),
            }
        )
    materials.sort(
        key=lambda item: (
            item["provider_id"],
            item["provider_account_id"],
            item["device_id"],
            item["device_binding_id"],
            item["binding_version"],
        )
    )
    return _canonical_sha256(materials)


def _artifacts(
    *groups: tuple[RedactedAcceptanceArtifact, ...],
) -> tuple[RedactedAcceptanceArtifact, ...]:
    by_identity: dict[tuple[str, str], RedactedAcceptanceArtifact] = {}
    for group in groups:
        for item in group:
            by_identity.setdefault((item.reference, item.sha256), item)
    return tuple(by_identity.values())


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audit_sha256(
    *,
    manifest_id: str,
    manifest_sha256: str,
    findings: tuple[RealAcceptanceAutomatedFinding, ...],
) -> str:
    return _canonical_sha256(
        {
            "manifest_id": manifest_id,
            "manifest_sha256": manifest_sha256,
            "findings": [
                item.model_dump(mode="json")
                for item in findings
            ],
        }
    )


__all__ = [
    "AuthorizedHumanAcceptanceApproval",
    "AutomatedFindingStatus",
    "IdempotencyProbeKind",
    "RealAcceptanceAutomatedFinding",
    "RealAcceptanceEvidenceError",
    "RealAcceptanceStageEvidence",
    "RealPerceptorAcceptanceAuditBundle",
    "RealPerceptorAcceptanceAuditor",
    "RealPerceptorAcceptanceManifest",
    "RealPerceptorAutomatedAudit",
    "RedactedAcceptanceArtifact",
    "ScopedIdempotencyProbe",
    "device_scope_sha256_for_bindings",
]
