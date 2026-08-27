# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Deterministic quality, alert correlation and CurrentRisk fast path.

This module deliberately contains no Agent/LLM dependency and no physiological
threshold. Vendor alerts remain source-qualified operational signals unless an
injected rule carries an immutable approved domain review.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Mapping, cast
from uuid import uuid4

from sleepagent.domain.contracts import (
    AlertCorrelationOutcome,
    AlertCorrelationReceipt,
    AlertLifecycleState,
    BedExitPayload,
    BedPresencePayload,
    CurrentRisk,
    DataSufficiency,
    DeterministicQualityAssessment,
    DeterministicQualityPolicy,
    DeterministicRiskPolicy,
    DeterministicSourceScope,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    DomainEvent,
    DomainNamespace,
    DomainEventType,
    DomainRuleReviewStatus,
    FastPathEventPolicy,
    FastPathReceiptOutcome,
    FastPathSignalProjection,
    FastPathSignalReceipt,
    FastPathSignalType,
    MissingIntervalPayload,
    MissingState,
    MissingnessState,
    NightEpisode,
    QualityState,
    RiskState,
    SleepObservation,
    TimezoneStatus,
    VendorAlertInstance,
    VendorAlertPayload,
)


class LifecycleBusyError(RuntimeError):
    """The subject-level lifecycle lease is owned by another worker."""


def _quality_outcome(
    *,
    observations_present: bool,
    coverage_ratio: float,
    explicit_missing_count: int,
    invalid_count: int,
    stale: bool,
    offline: bool,
    clock_invalid: bool,
    policy: DeterministicQualityPolicy,
) -> tuple[QualityState, DataSufficiency]:
    """Map existing policy dimensions to sufficient/partial/unusable states."""

    unusable = bool(
        not observations_present
        or coverage_ratio < policy.partial_coverage_ratio
        or stale
        or offline
        or clock_invalid
    )
    if unusable:
        return QualityState.DATA_INSUFFICIENT, DataSufficiency.DATA_INSUFFICIENT
    degraded = bool(
        coverage_ratio < policy.minimum_coverage_ratio
        or explicit_missing_count
        or invalid_count
    )
    if degraded:
        return QualityState.PARTIAL, DataSufficiency.PARTIAL
    return QualityState.SUFFICIENT, DataSufficiency.SUFFICIENT


@dataclass(frozen=True)
class DeterministicFastPathResult:
    quality: DeterministicQualityAssessment
    current_risk: CurrentRisk
    projections: tuple[FastPathSignalProjection, ...]
    alert_receipts: tuple[AlertCorrelationReceipt, ...]
    signal_receipts: tuple[FastPathSignalReceipt, ...]
    events: tuple[DomainEvent, ...]
    created: bool


class DeterministicFastPathService:
    def __init__(
        self,
        repository: Any,
        *,
        quality_policies: Mapping[str, DeterministicQualityPolicy],
        risk_policies: Mapping[str, DeterministicRiskPolicy],
        event_policies: Mapping[str, FastPathEventPolicy],
        worker_id: str = "deterministic-fast-path",
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.quality_policies = MappingProxyType(dict(quality_policies))
        self.risk_policies = MappingProxyType(dict(risk_policies))
        self.event_policies = MappingProxyType(dict(event_policies))
        self.worker_id = worker_id
        if lease_seconds < 1:
            raise ValueError("fast-path lease_seconds must be positive")
        self.lease_seconds = lease_seconds

    def evaluate(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        evaluation_id: str,
        assessed_at: datetime,
    ) -> DeterministicFastPathResult:
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=night_episode_id,
        )
        if episode is None:
            raise KeyError(f"NightEpisode not found: {night_episode_id}")
        quality_policy = self._pinned_quality_policy(episode)
        risk_policy = self._pinned_risk_policy(episode)
        event_policy = self._pinned_event_policy(episode)
        assessment_id = self._stable_id(
            "quality-assessment",
            namespace.namespace_id,
            night_episode_id,
            evaluation_id,
        )
        existing = self.repository.get_quality_assessment(
            namespace,
            assessment_id=assessment_id,
        )
        if existing is not None:
            risk = self.repository.get_current_risk(
                namespace,
                night_episode_id=night_episode_id,
            )
            if risk is None:
                raise RuntimeError(
                    "quality assessment exists without a current risk projection"
                )
            existing_projections = tuple(
                projection
                for signal_type in FastPathSignalType
                if (
                    projection
                    := self.repository.get_fast_path_signal_projection(
                        namespace,
                        night_episode_id=night_episode_id,
                        signal_type=signal_type.value,
                    )
                )
                is not None
            )
            return DeterministicFastPathResult(
                quality=existing,
                current_risk=risk,
                projections=existing_projections,
                alert_receipts=(),
                signal_receipts=(),
                events=(),
                created=False,
            )

        lease = self.repository.acquire_subject_lifecycle_lease(
            namespace,
            subject_id=episode.subject_id,
            lease_owner=self.worker_id,
            lease_token=f"{self.worker_id}:{uuid4()}",
            now=assessed_at,
            lease_duration=timedelta(seconds=self.lease_seconds),
        )
        if lease is None:
            raise LifecycleBusyError(
                f"subject deterministic fast path is leased: {episode.subject_id}"
            )
        try:
            observations = self._load_observations(namespace, episode)
            quality = self._current_authoritative_quality(
                namespace,
                episode,
                observations,
                policy=quality_policy,
            )
            persist_quality = quality is None
            if quality is None:
                quality = self._assess_quality(
                    namespace,
                    episode,
                    observations,
                    evaluation_id=evaluation_id,
                    assessed_at=assessed_at,
                    policy=quality_policy,
                )
            alert_instances, alert_receipts = self._correlate_alerts(
                namespace,
                episode,
                observations,
                persisted_at=assessed_at,
            )
            risk = self._assess_risk(
                namespace,
                episode,
                observations,
                quality,
                alert_instances,
                evaluation_id=evaluation_id,
                assessed_at=assessed_at,
                policy=risk_policy,
            )
            states = self._observed_signal_states(
                observations,
                quality,
                risk,
            )
            projections: list[FastPathSignalProjection] = []
            signal_receipts: list[FastPathSignalReceipt] = []
            events: list[DomainEvent] = []
            next_event_sequence = self.repository.next_domain_event_sequence(
                namespace,
                aggregate_type="NightEpisode",
                aggregate_id=episode.night_episode_id,
            )
            for signal_type in FastPathSignalType:
                current = self.repository.get_fast_path_signal_projection(
                    namespace,
                    night_episode_id=episode.night_episode_id,
                    signal_type=signal_type.value,
                )
                projection, receipt, emit = self._advance_signal(
                    episode,
                    current=current,
                    signal_type=signal_type,
                    observed_state=states[signal_type],
                    evaluation_id=evaluation_id,
                    observed_at=risk.observed_at,
                    persisted_at=assessed_at,
                    policy=event_policy,
                )
                projections.append(projection)
                signal_receipts.append(receipt)
                if emit:
                    events.append(
                        self._signal_event(
                            namespace,
                            episode,
                            projection,
                            receipt,
                            assessed_at=assessed_at,
                            sequence=next_event_sequence,
                        )
                    )
                    next_event_sequence += 1
            created = self.repository.commit_deterministic_fast_path(
                namespace,
                quality=quality,
                persist_quality=persist_quality,
                risk=risk,
                alert_instances=alert_instances,
                alert_receipts=alert_receipts,
                signal_projections=tuple(projections),
                signal_receipts=tuple(signal_receipts),
                events=tuple(events),
            )
            return DeterministicFastPathResult(
                quality=quality,
                current_risk=risk,
                projections=tuple(projections),
                alert_receipts=alert_receipts,
                signal_receipts=tuple(signal_receipts),
                events=tuple(events),
                created=created,
            )
        finally:
            self.repository.release_subject_lifecycle_lease(namespace, lease)

    def _current_authoritative_quality(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        *,
        policy: DeterministicQualityPolicy,
    ) -> DeterministicQualityAssessment | None:
        """Reuse an exact acquisition-owned quality projection read-only."""

        quality = self.repository.get_current_quality(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        if quality is None:
            return None
        current_revision = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        scope = quality.source_scope
        if (
            quality.subject_id != episode.subject_id
            or quality.data_mode != namespace.data_mode
            or quality.night_episode_id != episode.night_episode_id
            or quality.policy_version != policy.policy_version
            or scope.night_episode_id != episode.night_episode_id
            or scope.night_episode_revision_id
            != current_revision.current_revision_id
            or set(scope.observation_ids)
            != {item.observation_id for item in observations}
        ):
            return None
        return quality

    def _assess_quality(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        *,
        evaluation_id: str,
        assessed_at: datetime,
        policy: DeterministicQualityPolicy,
    ) -> DeterministicQualityAssessment:
        reference_end = min(
            assessed_at,
            episode.collection_end_at or assessed_at,
        )
        if reference_end <= episode.collection_start_at:
            reference_end = episode.collection_start_at + timedelta(seconds=1)
        duration_seconds = (
            reference_end - episode.collection_start_at
        ).total_seconds()
        expected_bins = max(
            1,
            math.ceil(duration_seconds / policy.coverage_cadence_seconds),
        )
        coverage_types = set(policy.coverage_observation_types)
        covered_bins: set[int] = set()
        explicit_missing = 0
        invalid = 0
        clock_invalid = False
        latest_observed_at: datetime | None = None
        latest_connectivity: tuple[datetime, DeviceConnectivityState] | None = None
        for observation in observations:
            observed_at = self._observation_time(observation)
            flags = set(observation.quality.quality_flags)
            if (
                flags.intersection(policy.clock_invalid_flags)
                or observation.timezone_status == TimezoneStatus.TIMEZONE_UNKNOWN
            ):
                clock_invalid = True
            if observation.quality.missing_state in {
                MissingState.MISSING,
                MissingState.INVALID,
            }:
                invalid += 1
            if isinstance(observation.payload, MissingIntervalPayload):
                explicit_missing += 1
            if observed_at is None:
                clock_invalid = True
                continue
            if observed_at > assessed_at:
                clock_invalid = True
                continue
            if latest_observed_at is None or observed_at > latest_observed_at:
                latest_observed_at = observed_at
            if isinstance(observation.payload, DeviceConnectivityPayload):
                if (
                    latest_connectivity is None
                    or observed_at > latest_connectivity[0]
                ):
                    latest_connectivity = (
                        observed_at,
                        observation.payload.state,
                    )
            if (
                observation.observation_type in coverage_types
                and observation.quality.missing_state == MissingState.PRESENT
                and episode.collection_start_at <= observed_at <= reference_end
            ):
                index = int(
                    (
                        observed_at - episode.collection_start_at
                    ).total_seconds()
                    // policy.coverage_cadence_seconds
                )
                covered_bins.add(min(max(index, 0), expected_bins - 1))
        coverage_ratio = min(1.0, len(covered_bins) / expected_bins)
        age_seconds = (
            float("inf")
            if latest_observed_at is None
            else max(0.0, (reference_end - latest_observed_at).total_seconds())
        )
        stale = age_seconds > policy.stale_after_seconds
        offline = (
            age_seconds > policy.offline_after_seconds
            or (
                latest_connectivity is not None
                and latest_connectivity[1] == DeviceConnectivityState.OFFLINE
            )
        )
        reasons: set[str] = set()
        if not observations:
            reasons.add("no_canonical_observations")
        if coverage_ratio < policy.minimum_coverage_ratio:
            reasons.add("coverage_below_minimum")
        if coverage_ratio < policy.partial_coverage_ratio:
            reasons.add("coverage_below_partial_floor")
        if explicit_missing:
            reasons.add("explicit_missing_interval")
        if invalid:
            reasons.add("invalid_or_missing_observation")
        if stale:
            reasons.add("stale_observation_stream")
        if offline:
            reasons.add("device_offline_or_stream_gap")
        if clock_invalid:
            reasons.add("clock_or_timezone_invalid")
        quality_state, sufficiency = _quality_outcome(
            observations_present=bool(observations),
            coverage_ratio=coverage_ratio,
            explicit_missing_count=explicit_missing,
            invalid_count=invalid,
            stale=stale,
            offline=offline,
            clock_invalid=clock_invalid,
            policy=policy,
        )
        missingness = (
            MissingnessState.UNKNOWN
            if not observations
            else (
                MissingnessState.GAPS_PRESENT
                if explicit_missing or invalid or coverage_ratio < 1.0
                else MissingnessState.COMPLETE
            )
        )
        scope = self._source_scope(
            namespace,
            episode,
            observations,
            window_end=reference_end,
        )
        return DeterministicQualityAssessment(
            assessment_id=self._stable_id(
                "quality-assessment",
                namespace.namespace_id,
                episode.night_episode_id,
                evaluation_id,
            ),
            data_mode=namespace.data_mode,
            subject_id=episode.subject_id,
            night_episode_id=episode.night_episode_id,
            quality_state=quality_state,
            data_sufficiency=sufficiency,
            missingness_state=missingness,
            coverage_ratio=coverage_ratio,
            expected_bin_count=expected_bins,
            covered_bin_count=len(covered_bins),
            explicit_missing_interval_count=explicit_missing,
            invalid_observation_count=invalid,
            stale=stale,
            offline=offline,
            clock_invalid=clock_invalid,
            latest_observed_at=latest_observed_at,
            source_scope=scope,
            policy_version=policy.policy_version,
            reason_codes=tuple(sorted(reasons)) or ("quality_sufficient",),
            assessed_at=assessed_at,
        )

    def _correlate_alerts(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        *,
        persisted_at: datetime,
    ) -> tuple[
        tuple[VendorAlertInstance, ...],
        tuple[AlertCorrelationReceipt, ...],
    ]:
        instances = {
            instance.alert_instance_id: instance
            for instance in self.repository.list_vendor_alert_instances(
                namespace,
                night_episode_id=episode.night_episode_id,
            )
        }
        by_vendor_id = {
            (
                instance.provider_id,
                instance.provider_account_id,
                instance.device_id,
                instance.vendor_alert_instance_id,
            ): instance.alert_instance_id
            for instance in instances.values()
        }
        updates: dict[str, VendorAlertInstance] = {}
        receipts: list[AlertCorrelationReceipt] = []
        alerts = [
            observation
            for observation in observations
            if isinstance(observation.payload, VendorAlertPayload)
        ]
        alerts.sort(
            key=lambda item: (
                self._observation_time(item) or item.received_at,
                item.observation_id,
            )
        )
        for observation in alerts:
            if self.repository.get_alert_correlation_receipt(
                namespace,
                observation_id=observation.observation_id,
            ) is not None:
                continue
            # 过滤器已经把序列收窄为厂商告警；显式 cast 让静态检查器
            # 与这个运行时判定保持一致，避免把其他 observation union 当告警读取。
            payload = cast(VendorAlertPayload, observation.payload)
            observed_at = self._observation_time(observation) or observation.received_at
            vendor_id = payload.vendor_alert_instance_id
            matched_id: str | None = None
            if payload.lifecycle_state == AlertLifecycleState.ACTIVE:
                if not vendor_id:
                    outcome = AlertCorrelationOutcome.UNKEYED_SIGNAL
                else:
                    key = (
                        observation.provenance.provider_id,
                        observation.provenance.provider_account_id,
                        observation.device_id,
                        vendor_id,
                    )
                    matched_id = by_vendor_id.get(key)
                    if matched_id is not None:
                        outcome = AlertCorrelationOutcome.DUPLICATE_OPEN
                    else:
                        matched_id = self._stable_id(
                            "vendor-alert-instance",
                            namespace.namespace_id,
                            *key,
                        )
                        instance = VendorAlertInstance(
                            alert_instance_id=matched_id,
                            data_mode=namespace.data_mode,
                            subject_id=episode.subject_id,
                            night_episode_id=episode.night_episode_id,
                            provider_id=observation.provenance.provider_id,
                            provider_account_id=(
                                observation.provenance.provider_account_id
                            ),
                            device_id=observation.device_id,
                            vendor_alert_instance_id=vendor_id,
                            alert_code=payload.alert_code,
                            opened_by_observation_id=observation.observation_id,
                            opened_at=observed_at,
                        )
                        instances[matched_id] = instance
                        updates[matched_id] = instance
                        by_vendor_id[key] = matched_id
                        outcome = AlertCorrelationOutcome.OPENED
            elif payload.lifecycle_state in {
                AlertLifecycleState.STOPPED,
                AlertLifecycleState.ORPHAN_STOP,
            }:
                if vendor_id:
                    key = (
                        observation.provenance.provider_id,
                        observation.provenance.provider_account_id,
                        observation.device_id,
                        vendor_id,
                    )
                    candidate_id = by_vendor_id.get(key)
                    candidate = (
                        None
                        if candidate_id is None
                        else instances[candidate_id]
                    )
                    if candidate is not None and candidate.closed_at is None:
                        matched_id = candidate.alert_instance_id
                        closed = candidate.model_copy(
                            update={
                                "closed_by_observation_id": (
                                    observation.observation_id
                                ),
                                "closed_at": observed_at,
                                "cas_version": candidate.cas_version + 1,
                            }
                        )
                        instances[matched_id] = closed
                        updates[matched_id] = closed
                        outcome = (
                            AlertCorrelationOutcome.CLOSED_UNIQUE_INSTANCE
                        )
                    else:
                        outcome = AlertCorrelationOutcome.ORPHAN_STOP
                else:
                    outcome = AlertCorrelationOutcome.ORPHAN_STOP
            else:
                outcome = AlertCorrelationOutcome.UNKEYED_SIGNAL
            receipts.append(
                AlertCorrelationReceipt(
                    receipt_id=self._stable_id(
                        "alert-correlation",
                        namespace.namespace_id,
                        observation.observation_id,
                    ),
                    data_mode=namespace.data_mode,
                    subject_id=episode.subject_id,
                    night_episode_id=episode.night_episode_id,
                    observation_id=observation.observation_id,
                    vendor_alert_instance_id=vendor_id,
                    alert_code=payload.alert_code,
                    outcome=outcome,
                    matched_alert_instance_id=matched_id,
                    occurred_at=observed_at,
                    persisted_at=persisted_at,
                )
            )
        return tuple(updates.values()), tuple(receipts)

    def _assess_risk(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        quality: DeterministicQualityAssessment,
        alert_updates: tuple[VendorAlertInstance, ...],
        *,
        evaluation_id: str,
        assessed_at: datetime,
        policy: DeterministicRiskPolicy,
    ) -> CurrentRisk:
        stored = {
            instance.alert_instance_id: instance
            for instance in self.repository.list_vendor_alert_instances(
                namespace,
                night_episode_id=episode.night_episode_id,
            )
        }
        stored.update(
            {instance.alert_instance_id: instance for instance in alert_updates}
        )
        open_instances = tuple(
            sorted(
                (
                    instance
                    for instance in stored.values()
                    if instance.closed_at is None
                ),
                key=lambda item: item.alert_instance_id,
            )
        )
        unkeyed_active = tuple(
            observation
            for observation in observations
            if isinstance(observation.payload, VendorAlertPayload)
            and (
                observation.payload.lifecycle_state
                == AlertLifecycleState.UNKNOWN
                or (
                    observation.payload.lifecycle_state
                    == AlertLifecycleState.ACTIVE
                    and not observation.payload.vendor_alert_instance_id
                )
            )
        )
        rules = {
            (rule.provider_id, rule.alert_code): rule
            for rule in policy.vendor_alert_rules
        }
        pending: set[str] = set()
        approved = []
        active_codes = {
            (instance.provider_id, instance.alert_code)
            for instance in open_instances
        }
        active_codes.update(
            (
                observation.provenance.provider_id,
                cast(VendorAlertPayload, observation.payload).alert_code,
            )
            for observation in unkeyed_active
        )
        for provider_id, alert_code in active_codes:
            rule = rules.get((provider_id, alert_code))
            if rule is None:
                pending.add(
                    f"PENDING_DOMAIN_REVIEW:{provider_id}:{alert_code}"
                )
            elif rule.review_status == DomainRuleReviewStatus.PENDING_DOMAIN_REVIEW:
                pending.add(rule.rule_id)
            else:
                approved.append(rule)

        reasons: set[str] = set(quality.reason_codes)
        health_escalation = False
        if quality.data_sufficiency != DataSufficiency.SUFFICIENT:
            risk_state = RiskState.UNKNOWN
            reasons.add("risk_unknown_due_to_data_insufficient")
        elif any(rule.risk_state == RiskState.REVIEWED_SIGNAL for rule in approved):
            risk_state = RiskState.REVIEWED_SIGNAL
            health_escalation = any(
                rule.health_escalation_allowed
                for rule in approved
                if rule.risk_state == RiskState.REVIEWED_SIGNAL
            )
            reasons.add("approved_deterministic_rule_matched")
        elif active_codes:
            risk_state = RiskState.OPERATIONAL_REVIEW
            reasons.add("vendor_alert_signal_not_diagnosis")
            if pending:
                reasons.add("alert_mapping_pending_domain_review")
        else:
            risk_state = RiskState.NO_REVIEWED_SIGNAL
            reasons.add("no_reviewed_signal_in_source_scope")
            reasons.add("not_an_all_clear_conclusion")
        observed_at = (
            quality.latest_observed_at or quality.source_scope.window_end_at
        )
        return CurrentRisk(
            current_risk_id=self._stable_id(
                "current-risk",
                namespace.namespace_id,
                episode.night_episode_id,
                evaluation_id,
            ),
            data_mode=namespace.data_mode,
            subject_id=episode.subject_id,
            night_episode_id=episode.night_episode_id,
            risk_state=risk_state,
            data_sufficiency=quality.data_sufficiency,
            source_scope=quality.source_scope,
            policy_version=policy.policy_version,
            observed_at=observed_at,
            reason_codes=tuple(sorted(reasons)),
            active_vendor_alert_instance_ids=tuple(
                instance.alert_instance_id for instance in open_instances
            ),
            pending_domain_review_rule_ids=tuple(sorted(pending)),
            health_escalation_allowed=health_escalation,
            updated_at=assessed_at,
        )

    def _observed_signal_states(
        self,
        observations: tuple[SleepObservation, ...],
        quality: DeterministicQualityAssessment,
        risk: CurrentRisk,
    ) -> dict[FastPathSignalType, str]:
        bed_state = "unknown"
        bed_time: datetime | None = None
        for observation in observations:
            observed_at = self._observation_time(observation)
            if observed_at is None:
                continue
            state: str | None = None
            if isinstance(observation.payload, BedPresencePayload):
                state = observation.payload.state.value
            elif isinstance(observation.payload, BedExitPayload):
                state = "out_of_bed"
            if state is not None and (
                bed_time is None or observed_at >= bed_time
            ):
                bed_time = observed_at
                bed_state = state
        offline_state = (
            "offline"
            if quality.offline
            else ("unknown" if quality.stale else "online")
        )
        return {
            FastPathSignalType.RISK: risk.risk_state.value,
            FastPathSignalType.BED: bed_state,
            FastPathSignalType.OFFLINE: offline_state,
            FastPathSignalType.QUALITY: quality.quality_state.value,
        }

    def _advance_signal(
        self,
        episode: NightEpisode,
        *,
        current: FastPathSignalProjection | None,
        signal_type: FastPathSignalType,
        observed_state: str,
        evaluation_id: str,
        observed_at: datetime,
        persisted_at: datetime,
        policy: FastPathEventPolicy,
    ) -> tuple[
        FastPathSignalProjection,
        FastPathSignalReceipt,
        bool,
    ]:
        previous_state = None if current is None else current.current_state
        if current is None:
            projection = FastPathSignalProjection(
                data_mode=episode.data_mode,
                subject_id=episode.subject_id,
                night_episode_id=episode.night_episode_id,
                signal_type=signal_type,
                current_state=observed_state,
                last_emitted_at=persisted_at,
                cooldown_until=persisted_at
                + timedelta(seconds=policy.cooldown_seconds),
                cas_version=1,
                policy_version=policy.policy_version,
                updated_at=persisted_at,
            )
            outcome = FastPathReceiptOutcome.EMITTED
            reason = "initial_signal_state"
            emit = True
        elif observed_state == current.current_state:
            if (
                current.cooldown_until is None
                or persisted_at >= current.cooldown_until
            ):
                projection = current.model_copy(
                    update={
                        "candidate_state": None,
                        "candidate_count": 0,
                        "suppressed_repeat_count": 0,
                        "last_emitted_at": persisted_at,
                        "cooldown_until": persisted_at
                        + timedelta(seconds=policy.cooldown_seconds),
                        "cas_version": current.cas_version + 1,
                        "updated_at": persisted_at,
                    }
                )
                outcome = FastPathReceiptOutcome.EMITTED
                reason = "cooldown_reminder"
                emit = True
            else:
                projection = current.model_copy(
                    update={
                        "candidate_state": None,
                        "candidate_count": 0,
                        "suppressed_repeat_count": (
                            current.suppressed_repeat_count + 1
                        ),
                        "cas_version": current.cas_version + 1,
                        "updated_at": persisted_at,
                    }
                )
                outcome = FastPathReceiptOutcome.SUPPRESSED_REPEAT
                reason = "duplicate_within_cooldown"
                emit = False
        else:
            adverse = self._is_adverse(signal_type, observed_state)
            required = (
                policy.bed_hysteresis_observations
                if signal_type == FastPathSignalType.BED
                else policy.recovery_hysteresis_observations
            )
            candidate_count = (
                current.candidate_count + 1
                if current.candidate_state == observed_state
                else 1
            )
            if adverse or candidate_count >= required:
                projection = current.model_copy(
                    update={
                        "current_state": observed_state,
                        "candidate_state": None,
                        "candidate_count": 0,
                        "suppressed_repeat_count": 0,
                        "last_emitted_at": persisted_at,
                        "cooldown_until": persisted_at
                        + timedelta(seconds=policy.cooldown_seconds),
                        "cas_version": current.cas_version + 1,
                        "policy_version": policy.policy_version,
                        "updated_at": persisted_at,
                    }
                )
                outcome = FastPathReceiptOutcome.EMITTED
                reason = (
                    "adverse_transition_immediate"
                    if adverse
                    else "hysteresis_satisfied"
                )
                emit = True
            else:
                projection = current.model_copy(
                    update={
                        "candidate_state": observed_state,
                        "candidate_count": candidate_count,
                        "cas_version": current.cas_version + 1,
                        "policy_version": policy.policy_version,
                        "updated_at": persisted_at,
                    }
                )
                outcome = FastPathReceiptOutcome.SUPPRESSED_HYSTERESIS
                reason = "recovery_or_bed_hysteresis_pending"
                emit = False
        receipt = FastPathSignalReceipt(
            receipt_id=self._stable_id(
                "fast-path-signal-receipt",
                episode.night_episode_id,
                signal_type.value,
                evaluation_id,
            ),
            evaluation_id=evaluation_id,
            data_mode=episode.data_mode,
            subject_id=episode.subject_id,
            night_episode_id=episode.night_episode_id,
            signal_type=signal_type,
            observed_state=observed_state,
            previous_state=previous_state,
            outcome=outcome,
            suppressed_repeat_count=projection.suppressed_repeat_count,
            reason_code=reason,
            policy_version=policy.policy_version,
            observed_at=observed_at,
            persisted_at=persisted_at,
        )
        return projection, receipt, emit

    def _signal_event(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        projection: FastPathSignalProjection,
        receipt: FastPathSignalReceipt,
        *,
        assessed_at: datetime,
        sequence: int,
    ) -> DomainEvent:
        event_type = {
            FastPathSignalType.RISK: DomainEventType.RISK_STATE_CHANGED,
            FastPathSignalType.BED: DomainEventType.BED_STATE_CHANGED,
            FastPathSignalType.OFFLINE: (
                DomainEventType.DEVICE_ONLINE
                if projection.current_state == "online"
                else DomainEventType.DEVICE_OFFLINE
            ),
            FastPathSignalType.QUALITY: (
                DomainEventType.DATA_QUALITY_INSUFFICIENT
                if projection.current_state == QualityState.DATA_INSUFFICIENT.value
                else DomainEventType.DATA_QUALITY_RECOVERED
            ),
        }[projection.signal_type]
        return DomainEvent(
            event_id=self._stable_id(
                "domain-event",
                namespace.namespace_id,
                episode.night_episode_id,
                projection.signal_type.value,
                receipt.evaluation_id,
            ),
            event_type=event_type,
            event_version="1",
            data_mode=namespace.data_mode,
            aggregate_type="NightEpisode",
            aggregate_id=episode.night_episode_id,
            aggregate_version=projection.cas_version,
            per_aggregate_sequence=sequence,
            delivery_offset=1,
            subject_id=episode.subject_id,
            night_episode_id=episode.night_episode_id,
            event_occurred_at=receipt.observed_at,
            persisted_at=assessed_at,
            correlation_id=f"fast-path:{receipt.evaluation_id}",
            causation_id=receipt.receipt_id,
            attributes={
                "signal_type": projection.signal_type.value,
                "state": projection.current_state,
                "policy_version": projection.policy_version,
                "reminder": receipt.reason_code == "cooldown_reminder",
                "suppressed_repeat_count": receipt.suppressed_repeat_count,
            },
        )

    def _source_scope(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
        observations: tuple[SleepObservation, ...],
        *,
        window_end: datetime,
    ) -> DeterministicSourceScope:
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=episode.night_episode_id,
        )
        return DeterministicSourceScope(
            night_episode_id=episode.night_episode_id,
            night_episode_revision_id=pointer.current_revision_id,
            observation_ids=tuple(
                sorted(observation.observation_id for observation in observations)
            ),
            observation_types=tuple(
                sorted(
                    {observation.observation_type for observation in observations},
                    key=lambda item: item.value,
                )
            ),
            device_binding_ids=tuple(
                sorted(
                    {observation.device_binding_id for observation in observations}
                )
            ),
            window_start_at=episode.collection_start_at,
            window_end_at=window_end,
        )

    def _load_observations(
        self,
        namespace: DomainNamespace,
        episode: NightEpisode,
    ) -> tuple[SleepObservation, ...]:
        observations: list[SleepObservation] = []
        for observation_id in episode.observation_ids:
            observation = self.repository.get_observation(
                namespace,
                observation_id=observation_id,
            )
            if observation is None:
                raise ValueError(
                    f"NightEpisode observation is missing: {observation_id}"
                )
            if observation.subject_id != episode.subject_id:
                raise ValueError("NightEpisode observation subject mismatch")
            observations.append(observation)
        observations.sort(
            key=lambda item: (
                self._observation_time(item) or item.received_at,
                item.observation_id,
            )
        )
        return tuple(observations)

    def _pinned_quality_policy(
        self,
        episode: NightEpisode,
    ) -> DeterministicQualityPolicy:
        version = episode.pinned_policy_versions.get("quality")
        if not version or version not in self.quality_policies:
            raise ValueError("NightEpisode pins an unavailable quality policy")
        return self.quality_policies[version]

    def _pinned_risk_policy(
        self,
        episode: NightEpisode,
    ) -> DeterministicRiskPolicy:
        version = episode.pinned_policy_versions.get("risk")
        if not version or version not in self.risk_policies:
            raise ValueError("NightEpisode pins an unavailable risk policy")
        return self.risk_policies[version]

    def _pinned_event_policy(
        self,
        episode: NightEpisode,
    ) -> FastPathEventPolicy:
        version = episode.pinned_policy_versions.get("fast_path_event")
        if not version or version not in self.event_policies:
            raise ValueError("NightEpisode pins an unavailable event policy")
        return self.event_policies[version]

    @staticmethod
    def _observation_time(
        observation: SleepObservation,
    ) -> datetime | None:
        return observation.event_occurred_at or observation.measurement_at

    @staticmethod
    def _is_adverse(
        signal_type: FastPathSignalType,
        state: str,
    ) -> bool:
        return {
            FastPathSignalType.RISK: state
            in {
                RiskState.UNKNOWN.value,
                RiskState.OPERATIONAL_REVIEW.value,
                RiskState.REVIEWED_SIGNAL.value,
            },
            FastPathSignalType.BED: False,
            FastPathSignalType.OFFLINE: state != "online",
            FastPathSignalType.QUALITY: state
            == QualityState.DATA_INSUFFICIENT.value,
        }[signal_type]

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"


__all__ = [
    "DeterministicFastPathResult",
    "DeterministicFastPathService",
]
