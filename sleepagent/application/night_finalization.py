"""Deterministic night-data finalization, separate from episode date state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.domain.episodes import UUID7Generator
from sleepagent.observability import log_event
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope


UTC = timezone.utc


class NightFinalizationState(str, Enum):
    OPEN = "open"
    SOFT_FINALIZED = "soft_finalized"
    HARD_FINALIZED = "hard_finalized"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class NightFinalizationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = "night-finalization.v1"
    wake_grace_seconds: int = Field(default=7_200, ge=0, le=86_400)
    maximum_wait_seconds: int = Field(default=86_400, ge=60, le=604_800)
    minimum_observation_count: int = Field(default=1, ge=0)

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class NightFinalizationRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "night_finalization_revision.v1"
    night_finalization_revision_id: str
    night_finalization_id: str
    finalization_revision_number: int = Field(ge=1)
    parent_finalization_revision_id: str | None = None
    night_episode_id: str
    source_night_episode_revision_id: str
    source_report_version_id: str | None = None
    state: NightFinalizationState
    provisional: bool
    coverage_status: str
    coverage_caveat: str | None = None
    revision_cause: str
    material_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    reanalysis_operation_id: str | None = None
    created_at: datetime


class NightFinalizationPending(RuntimeError):
    pass


class NightFinalizationConflict(RuntimeError):
    pass


class NightFinalizationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        policy: NightFinalizationPolicy | None = None,
        now_factory: Any = lambda: datetime.now(tz=UTC),
        id_generator: Any | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.policy = policy or NightFinalizationPolicy()
        self.now_factory = now_factory
        self.id_generator = id_generator or UUID7Generator()

    def finalize_latest_for_binding(
        self,
        scope: UowScope,
        *,
        device_binding_id: str,
        evaluated_at: datetime | None = None,
    ) -> NightFinalizationRevision:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT episode.night_episode_id
                    FROM public.sleep_domain_night_episodes AS episode
                    WHERE episode.namespace_id = %s AND episode.data_mode = %s
                      AND episode.namespace_generation = %s
                      AND episode.subject_id = %s
                      AND episode.current_revision_id IS NOT NULL
                      AND EXISTS (
                        SELECT 1
                        FROM public.sleep_domain_episode_observation_memberships
                          AS member
                        WHERE member.namespace_id = episode.namespace_id
                          AND member.data_mode = episode.data_mode
                          AND member.night_episode_id = episode.night_episode_id
                          AND member.device_binding_id = %s
                      )
                    ORDER BY episode.episode_local_date DESC NULLS LAST,
                      episode.updated_at DESC
                    LIMIT 1
                    """,
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.subject_id,
                        device_binding_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise NightFinalizationPending("binding has no finalizable night")
        return self.finalize(
            scope,
            night_episode_id=str(row[0]),
            evaluated_at=evaluated_at,
        )

    def finalize(
        self,
        scope: UowScope,
        *,
        night_episode_id: str,
        evaluated_at: datetime | None = None,
    ) -> NightFinalizationRevision:
        if scope.process_role != "worker" or scope.subject_id is None:
            raise PermissionError("night finalization requires exact worker scope")
        now = evaluated_at or self.now_factory()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                evidence = self._load_evidence(cursor, scope, night_episode_id)
                decision = _decide(evidence, now=now, policy=self.policy)
                if decision is None:
                    raise NightFinalizationPending(
                        "night has not reached a deterministic finalization gate"
                    )
                result = self._persist(cursor, scope, evidence, decision, now)
                uow.commit()
            except NightFinalizationPending:
                raise
            except Exception as exc:
                raise NightFinalizationConflict(
                    "night finalization transition was rejected"
                ) from exc
            finally:
                cursor.close()
        log_event(
            "night_finalization_transition",
            night_episode_id=result.night_episode_id,
            finalization_revision_id=result.night_finalization_revision_id,
            revision_number=result.finalization_revision_number,
            state=result.state.value,
            revision_cause=result.revision_cause,
        )
        if result.reanalysis_operation_id is not None:
            log_event(
                "night_finalization_report_handoff",
                finalization_revision_id=result.night_finalization_revision_id,
                operation_id=result.reanalysis_operation_id,
                handoff_kind=(
                    "initial_report"
                    if result.parent_finalization_revision_id is None
                    else "late_reanalysis"
                ),
            )
        return result

    def _load_evidence(
        self, cursor: Any, scope: UowScope, night_episode_id: str
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT episode.current_revision_id,
              episode.current_revision_number, episode.date_conflict,
              episode.timezone_name, episode.episode_local_date,
              episode.deterministic_close_deadline_at,
              revision.revision_json,
              (SELECT count(*)
               FROM public.sleep_domain_episode_observation_memberships AS member
               WHERE member.namespace_id = episode.namespace_id
                 AND member.data_mode = episode.data_mode
                 AND member.night_episode_id = episode.night_episode_id),
              report.source_report_version_id,
              report.is_empty
            FROM public.sleep_domain_night_episodes AS episode
            JOIN public.sleep_domain_night_episode_revisions AS revision
              ON revision.night_episode_revision_id = episode.current_revision_id
             AND revision.namespace_id = episode.namespace_id
             AND revision.data_mode = episode.data_mode
            LEFT JOIN LATERAL (
              SELECT source.source_report_version_id, source.is_empty
              FROM public.sleep_domain_source_reports AS source
              JOIN public.sleep_domain_episode_observation_memberships AS member
                ON member.namespace_id = episode.namespace_id
               AND member.data_mode = episode.data_mode
               AND member.night_episode_id = episode.night_episode_id
              JOIN public.sleep_domain_device_bindings AS binding
                ON binding.device_binding_id = member.device_binding_id
               AND binding.namespace_id = member.namespace_id
               AND binding.data_mode = member.data_mode
               AND binding.binding_version = member.binding_version
              JOIN public.sleep_domain_device_identities AS identity
                ON identity.namespace_id = binding.namespace_id
               AND identity.data_mode = binding.data_mode
               AND identity.device_id = binding.device_id
              WHERE source.namespace_id = episode.namespace_id
                AND source.data_mode = episode.data_mode
                AND source.provider_id = binding.provider_id
                AND source.provider_account_id = binding.provider_account_id
                AND source.provider_device_key IN (
                  identity.provider_device_key,
                  'sha256:' || encode(digest(convert_to(
                    episode.namespace_generation::text || chr(31) ||
                    identity.provider_device_key,
                    'UTF8'
                  ), 'sha256'), 'hex')
                )
                AND source.local_report_date = episode.episode_local_date
              ORDER BY source.report_version DESC
              LIMIT 1
            ) AS report ON TRUE
            WHERE episode.night_episode_id = %s
              AND episode.namespace_id = %s AND episode.data_mode = %s
              AND episode.namespace_generation = %s
              AND episode.subject_id = %s
            FOR UPDATE OF episode
            """,
            (
                night_episode_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.subject_id,
            ),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None or row[4] is None:
            raise NightFinalizationPending(
                "night episode has no committed date/revision authority"
            )
        return {
            "night_episode_id": night_episode_id,
            "episode_revision_id": str(row[0]),
            "episode_revision_number": int(row[1]),
            "date_conflict": bool(row[2]),
            "timezone_name": str(row[3]),
            "local_sleep_date": row[4],
            "deadline_at": row[5],
            "revision_json": _json_object(row[6]),
            "observation_count": int(row[7]),
            "source_report_version_id": None if row[8] is None else str(row[8]),
            "source_report_is_empty": None if row[9] is None else bool(row[9]),
        }

    def _persist(
        self,
        cursor: Any,
        scope: UowScope,
        evidence: dict[str, Any],
        decision: dict[str, Any],
        now: datetime,
    ) -> NightFinalizationRevision:
        finalization_id = _identifier(
            "night-finalization", scope.namespace_id, evidence["night_episode_id"]
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalizations (
              night_finalization_id, namespace_id, data_mode,
              namespace_generation, run_id, arm_id, subject_id,
              night_episode_id, state, policy_version, policy_sha256,
              created_at, updated_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s
            ) ON CONFLICT (night_finalization_id) DO NOTHING
            """,
            (
                finalization_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                evidence["night_episode_id"],
                self.policy.policy_version,
                self.policy.sha256,
                now,
                now,
            ),
        )
        cursor.execute(
            """
            SELECT state, current_finalization_revision_id,
              current_revision_number, cas_version, policy_sha256
            FROM public.sleep_domain_night_finalizations
            WHERE night_finalization_id = %s
            FOR UPDATE
            """,
            (finalization_id,),
        )
        aggregate = cursor.fetchone()
        if aggregate is None or str(aggregate[4]) != self.policy.sha256:
            raise NightFinalizationConflict("finalization policy conflict")
        parent_id = None if aggregate[1] is None else str(aggregate[1])
        revision_number = 1 if aggregate[2] is None else int(aggregate[2]) + 1
        material = {
            "episode_revision_id": evidence["episode_revision_id"],
            "source_report_version_id": evidence["source_report_version_id"],
            "state": decision["state"],
            "coverage_status": decision["coverage_status"],
            "policy_sha256": self.policy.sha256,
        }
        material_json = json.dumps(material, sort_keys=True, separators=(",", ":"))
        material_sha = hashlib.sha256(material_json.encode()).hexdigest()
        cursor.execute(
            """
            SELECT finalization_json
            FROM public.sleep_domain_night_finalization_revisions
            WHERE night_finalization_id = %s AND material_sha256 = %s
            """,
            (finalization_id, material_sha),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return NightFinalizationRevision.model_validate(_json_object(existing[0]))

        revision_id = _identifier(
            "night-finalization-revision", finalization_id, material_sha
        )
        revision_cause = decision["revision_cause"]
        if parent_id is not None and decision["state"] != "reconciliation_required":
            revision_cause = "late_material_evidence"
        analysis_handoff_id = None
        if decision["state"] in {
            "soft_finalized",
            "hard_finalized",
        }:
            analysis_handoff_id = self._insert_analysis_handoff(
                cursor,
                scope,
                evidence=evidence,
                finalization_revision_id=revision_id,
                initial_finalization=parent_id is None,
                now=now,
            )
        payload = NightFinalizationRevision(
            night_finalization_revision_id=revision_id,
            night_finalization_id=finalization_id,
            finalization_revision_number=revision_number,
            parent_finalization_revision_id=parent_id,
            night_episode_id=evidence["night_episode_id"],
            source_night_episode_revision_id=evidence["episode_revision_id"],
            source_report_version_id=evidence["source_report_version_id"],
            state=NightFinalizationState(decision["state"]),
            provisional=bool(decision["provisional"]),
            coverage_status=str(decision["coverage_status"]),
            coverage_caveat=decision["coverage_caveat"],
            revision_cause=revision_cause,
            material_sha256=material_sha,
            # Migration 017 named this linkage for the late-data case. It is
            # also the existing durable FK for the initial analysis handoff.
            reanalysis_operation_id=analysis_handoff_id,
            created_at=now,
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalization_revisions (
              night_finalization_revision_id, night_finalization_id,
              namespace_id, data_mode, namespace_generation, run_id, arm_id,
              subject_id, night_episode_id, finalization_revision_number,
              parent_finalization_revision_id,
              source_night_episode_revision_id, source_report_version_id,
              state, provisional, coverage_status, coverage_caveat,
              revision_cause, material_sha256, finalization_json,
              reanalysis_operation_id, created_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
            )
            """,
            (
                revision_id,
                finalization_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                evidence["night_episode_id"],
                revision_number,
                parent_id,
                evidence["episode_revision_id"],
                evidence["source_report_version_id"],
                decision["state"],
                decision["provisional"],
                decision["coverage_status"],
                decision["coverage_caveat"],
                revision_cause,
                material_sha,
                payload.model_dump_json(),
                analysis_handoff_id,
                now,
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_finalizations
            SET state = %s, current_finalization_revision_id = %s,
                current_revision_number = %s, cas_version = cas_version + 1,
                updated_at = %s
            WHERE night_finalization_id = %s AND cas_version = %s
            """,
            (
                decision["state"],
                revision_id,
                revision_number,
                now,
                finalization_id,
                int(aggregate[3]),
            ),
        )
        if cursor.rowcount != 1:
            raise NightFinalizationConflict("finalization CAS conflict")
        return payload

    def _insert_analysis_handoff(
        self,
        cursor: Any,
        scope: UowScope,
        *,
        evidence: dict[str, Any],
        finalization_revision_id: str,
        initial_finalization: bool,
        now: datetime,
    ) -> str:
        operation_id = str(self.id_generator())
        workload = {
            "schema_version": "workload_authorization_snapshot.v1",
            "workload_principal_id": scope.service_principal_id,
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "data_mode": scope.data_mode,
            "run_id": scope.run_id,
            "arm_id": scope.arm_id,
            "subject_id": scope.subject_id,
            "purpose": scope.purpose,
            "allowed_handler": "fast_path",
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
        }
        operation = {
            "schema_version": "backend_operation.v2",
            "trigger": "night_finalization",
            "finalization_handoff_kind": (
                "initial_report" if initial_finalization else "late_reanalysis"
            ),
            "night_episode_id": evidence["night_episode_id"],
            "night_episode_revision_id": evidence["episode_revision_id"],
            "night_finalization_revision_id": finalization_revision_id,
            "authorization_snapshot": workload,
        }
        semantic = _identifier(
            "finalization-analysis-handoff-semantic", finalization_revision_id
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_operations (
              operation_id, namespace_id, data_mode, operation_type,
              subject_id, service_principal_id, actor_id,
              target_resource_id, target_resource_key, idempotency_key,
              request_sha256, status, attempt_count, cas_version,
              operation_json, created_at, updated_at, protocol_version,
              namespace_generation, run_id, arm_id, id_scheme, origin_kind,
              semantic_key, queue_name, priority, available_at, max_attempts,
              workload_authorization_snapshot_json, policy_sha256
            ) VALUES (
              %s, %s, %s, 'fast_path', %s, %s, NULL, %s, %s, %s, %s,
              'pending', 0, 0, %s::jsonb, %s, %s, 2, %s, %s, %s,
              'uuidv7', 'system', %s, 'fast_path', 80, %s, 5,
              %s::jsonb, %s
            ) ON CONFLICT (operation_id) DO NOTHING
            """,
            (
                operation_id,
                scope.namespace_id,
                scope.data_mode,
                scope.subject_id,
                scope.service_principal_id,
                evidence["night_episode_id"],
                evidence["episode_revision_id"],
                semantic,
                hashlib.sha256(
                    json.dumps(operation, sort_keys=True).encode()
                ).hexdigest(),
                json.dumps(operation, sort_keys=True, separators=(",", ":")),
                now,
                now,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                semantic,
                now,
                json.dumps(workload, sort_keys=True, separators=(",", ":")),
                self.policy.sha256,
            ),
        )
        return operation_id


def _decide(
    evidence: dict[str, Any],
    *,
    now: datetime,
    policy: NightFinalizationPolicy,
) -> dict[str, Any] | None:
    if evidence["date_conflict"]:
        return {
            "state": "reconciliation_required",
            "provisional": False,
            "coverage_status": "data_insufficient",
            "coverage_caveat": "Episode date authority is in conflict.",
            "revision_cause": "reconciliation_conflict",
        }
    has_vendor_report = (
        evidence["source_report_version_id"] is not None
        and evidence["source_report_is_empty"] is False
    )
    if has_vendor_report:
        return {
            "state": "hard_finalized",
            "provisional": False,
            "coverage_status": "complete",
            "coverage_caveat": None,
            "revision_cause": "vendor_report_reconciled",
        }
    deadline = evidence["deadline_at"]
    if now >= deadline + timedelta(seconds=policy.maximum_wait_seconds):
        sufficient = evidence["observation_count"] >= policy.minimum_observation_count
        return {
            "state": "hard_finalized",
            "provisional": False,
            "coverage_status": "partial" if sufficient else "data_insufficient",
            "coverage_caveat": (
                "Maximum vendor-report wait elapsed; hard finalization uses "
                "bounded available evidence."
            ),
            "revision_cause": "maximum_wait_elapsed",
        }
    if (
        now >= deadline + timedelta(seconds=policy.wake_grace_seconds)
        and evidence["observation_count"] >= policy.minimum_observation_count
    ):
        return {
            "state": "soft_finalized",
            "provisional": True,
            "coverage_status": "partial",
            "coverage_caveat": (
                "Vendor SleepReport is pending; coverage is provisional."
            ),
            "revision_cause": "wake_grace_elapsed",
        }
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"{prefix}:{digest}"


__all__ = [
    "NightFinalizationConflict",
    "NightFinalizationPending",
    "NightFinalizationPolicy",
    "NightFinalizationRevision",
    "NightFinalizationService",
    "NightFinalizationState",
]
