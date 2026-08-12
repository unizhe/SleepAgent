"""Stage-4 induction, replay delivery, and reconciliation workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from sleepagent.backend_settings import (
    DataMode,
    ModelMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.sleep_domain.episode_v2 import UUID7Generator
from sleepagent.sleep_domain.worker_adapters import (
    B3ClaimInvariantError,
    exact_worker_scope,
    worker_uow_factory,
)
from sleepagent.stage3_worker import EpisodeDateReconciliationHandler
from sleepagent.worker_runtime import (
    InvocationKind,
    LeaseLostError,
    OutcomeUnknownError,
    RetryableWorkError,
    TerminalWorkError,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
)


INDUCTION_QUEUE = "induction"
RECONCILIATION_QUEUE = "reconciliation"
DELIVERY_DESTINATION = "replay_care_notification"
DELIVERY_QUEUE = f"delivery:{DELIVERY_DESTINATION}"
DELIVERY_HANDLER = "deterministic_replay_sink"


class Stage4Error(RuntimeError):
    pass


class Stage4LeaseLost(Stage4Error):
    pass


class InductionWorkHandler:
    def __init__(self, *, id_generator: UUID7Generator | None = None) -> None:
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _operation_scope(
                context,
                queue=INDUCTION_QUEUE,
                operation_type="induction",
            )
            payload = _induction_payload(context.claim.payload)
            result = self._commit(context, scope=scope, payload=payload)
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=result,
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except Stage4LeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="induction_lease_lost_reconciliation_required",
            )
        except (B3ClaimInvariantError, Stage4Error, KeyError, TypeError, ValueError):
            return _terminal("induction_invariant_violation")

    def _commit(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        payload: Mapping[str, str],
    ) -> dict[str, Any]:
        claim = context.claim
        profile_revision_id = self.id_generator()
        receipt_id = self.id_generator()
        event_id = self.id_generator()
        audit_id = self.id_generator()
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT manifest.manifest_id, manifest.manifest_sha256, "
                    "manifest.analysis_revision_id, "
                    "manifest.night_episode_revision_id, "
                    "manifest.source_fact_snapshot_sha256, "
                    "manifest.policy_sha256, manifest.manifest_json, "
                    "analysis.analysis_json ->> 'status', "
                    "attempt.attempt_state, attempt.query_visible, "
                    "operation.policy_sha256 "
                    "FROM public.sleep_domain_operations AS operation "
                    "JOIN public.backend_induction_manifests_v2 AS manifest "
                    "ON manifest.operation_id = operation.operation_id "
                    "AND manifest.namespace_id = operation.namespace_id "
                    "AND manifest.data_mode = operation.data_mode "
                    "AND manifest.namespace_generation = "
                    "operation.namespace_generation "
                    "AND manifest.subject_id = operation.subject_id "
                    "JOIN public.sleep_domain_analysis_revisions AS analysis "
                    "ON analysis.analysis_revision_id = "
                    "manifest.analysis_revision_id "
                    "AND analysis.namespace_id = manifest.namespace_id "
                    "AND analysis.data_mode = manifest.data_mode "
                    "AND analysis.subject_id = manifest.subject_id "
                    "JOIN public.backend_product_attempts AS attempt "
                    "ON attempt.operation_id = "
                    "manifest.source_product_operation_id "
                    "AND attempt.namespace_id = manifest.namespace_id "
                    "AND attempt.data_mode = manifest.data_mode "
                    "AND attempt.namespace_generation = "
                    "manifest.namespace_generation "
                    "AND attempt.subject_id = manifest.subject_id "
                    "WHERE operation.operation_id = %s "
                    "AND operation.operation_type = 'induction' "
                    "AND operation.queue_name = 'induction' "
                    "AND operation.target_resource_id = manifest.manifest_id "
                    "AND operation.status = 'running' "
                    "AND operation.cas_version = %s "
                    "AND operation.lease_generation = %s "
                    "AND operation.fencing_token = %s "
                    "AND operation.worker_instance = %s "
                    "AND operation.lease_expires_at > clock_timestamp() "
                    "FOR UPDATE OF operation, manifest, attempt",
                    (
                        claim.work_id,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise Stage4LeaseLost("induction operation fence was lost")
                stored = {
                    "manifest_id": str(row[0]),
                    "manifest_sha256": str(row[1]),
                    "analysis_revision_id": str(row[2]),
                    "night_episode_revision_id": str(row[3]),
                    "projector_version": "deterministic_personalization.v1",
                }
                if any(payload[name] != value for name, value in stored.items()):
                    raise Stage4Error("induction operation and manifest drifted")
                manifest = _mapping(row[6], "induction manifest")
                if (
                    manifest.get("schema_version") != "induction_manifest.v1"
                    or _sha256(manifest) != str(row[1])
                    or manifest.get("source_fact_snapshot_sha256") != str(row[4])
                    or manifest.get("policy_sha256") != str(row[5])
                    or str(row[7]) not in {"ready", "degraded"}
                    or str(row[8]) != "committed"
                    or row[9] is not True
                    or str(row[10]) != str(row[5])
                ):
                    raise Stage4Error(
                        "induction source is not one committed Product analysis"
                    )
                cursor.execute(
                    "SELECT count(*), array_agg(role ORDER BY role) "
                    "FROM public.sleep_domain_analysis_role_views "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND namespace_generation = %s AND subject_id = %s "
                    "AND analysis_revision_id = %s AND protocol_version >= 2",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.subject_id,
                        stored["analysis_revision_id"],
                    ),
                )
                views = cursor.fetchone()
                if views is None or views != (3, ["doctor", "elder", "family"]):
                    raise Stage4Error(
                        "induction requires the committed three-role projection set"
                    )
                cursor.execute(
                    "SELECT profile_id, current_profile_revision_id, "
                    "current_version, cas_version, profile_json "
                    "FROM public.backend_personalization_profiles_v2 "
                    "WHERE namespace_id = %s AND data_mode = %s "
                    "AND namespace_generation = %s "
                    "AND COALESCE(run_id, '') = COALESCE(%s, '') "
                    "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
                    "AND subject_id = %s FOR UPDATE",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                    ),
                )
                profile_row = cursor.fetchone()
                if profile_row is None:
                    profile_id = self.id_generator()
                    parent_revision_id = None
                    from_version = 0
                    profile_cas = 0
                    prior_refs: list[str] = []
                else:
                    profile_id = str(profile_row[0])
                    parent_revision_id = str(profile_row[1])
                    from_version = int(profile_row[2])
                    profile_cas = int(profile_row[3])
                    prior = _mapping(profile_row[4], "personalization profile")
                    refs = prior.get("inducted_analysis_refs")
                    if (
                        prior.get("schema_version")
                        != "personalization_profile.v1"
                        or not isinstance(refs, list)
                        or any(not isinstance(item, str) for item in refs)
                    ):
                        raise Stage4Error("personalization profile version drifted")
                    prior_refs = list(refs)
                if stored["analysis_revision_id"] in prior_refs:
                    raise Stage4Error("analysis was already inducted")
                to_version = from_version + 1
                profile = {
                    "schema_version": "personalization_profile.v1",
                    "profile_id": profile_id,
                    "current_version": to_version,
                    "inducted_analysis_refs": [
                        *prior_refs[-14:],
                        stored["analysis_revision_id"],
                    ],
                    "last_analysis_status": str(row[7]),
                    "last_night_episode_revision_id": stored[
                        "night_episode_revision_id"
                    ],
                    "projector_version": stored["projector_version"],
                }
                profile_sha256 = _sha256(profile)
                if profile_row is None:
                    cursor.execute(
                        "INSERT INTO public.backend_personalization_profiles_v2 ("
                        "profile_id, namespace_id, data_mode, namespace_generation, "
                        "run_id, arm_id, subject_id, current_profile_revision_id, "
                        "current_version, cas_version, profile_sha256, profile_json, "
                        "created_at, updated_at) VALUES ("
                        "%s, %s, %s, %s, %s, %s, %s, %s, 1, 1, %s, %s::jsonb, "
                        "clock_timestamp(), clock_timestamp())",
                        (
                            profile_id,
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.run_id,
                            scope.arm_id,
                            scope.subject_id,
                            profile_revision_id,
                            profile_sha256,
                            _json(profile),
                        ),
                    )
                else:
                    cursor.execute(
                        "UPDATE public.backend_personalization_profiles_v2 SET "
                        "current_profile_revision_id = %s, "
                        "current_version = %s, cas_version = cas_version + 1, "
                        "profile_sha256 = %s, profile_json = %s::jsonb, "
                        "updated_at = clock_timestamp() "
                        "WHERE profile_id = %s AND cas_version = %s",
                        (
                            profile_revision_id,
                            to_version,
                            profile_sha256,
                            _json(profile),
                            profile_id,
                            profile_cas,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise Stage4Error("personalization profile CAS was lost")
                cursor.execute(
                    "INSERT INTO public.backend_personalization_profile_revisions_v2 ("
                    "profile_revision_id, profile_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "profile_version, parent_profile_revision_id, "
                    "source_analysis_revision_id, manifest_id, profile_sha256, "
                    "profile_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb)",
                    (
                        profile_revision_id,
                        profile_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        to_version,
                        parent_revision_id,
                        stored["analysis_revision_id"],
                        stored["manifest_id"],
                        profile_sha256,
                        _json(profile),
                    ),
                )
                receipt = {
                    "schema_version": "induction_receipt.v1",
                    "receipt_id": receipt_id,
                    "operation_id": claim.work_id,
                    "manifest_id": stored["manifest_id"],
                    "profile_id": profile_id,
                    "profile_revision_id": profile_revision_id,
                    "from_version": from_version,
                    "to_version": to_version,
                    "status": "succeeded",
                }
                cursor.execute(
                    "INSERT INTO public.backend_induction_receipts_v2 ("
                    "receipt_id, operation_id, manifest_id, namespace_id, "
                    "data_mode, namespace_generation, run_id, arm_id, subject_id, "
                    "profile_id, profile_revision_id, from_version, to_version, "
                    "status, receipt_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "'succeeded', %s::jsonb)",
                    (
                        receipt_id,
                        claim.work_id,
                        stored["manifest_id"],
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        profile_id,
                        profile_revision_id,
                        from_version,
                        to_version,
                        _json(receipt),
                    ),
                )
                result = {
                    "schema_version": "induction_result.v1",
                    "manifest_id": stored["manifest_id"],
                    "profile_id": profile_id,
                    "profile_revision_id": profile_revision_id,
                    "profile_version": to_version,
                    "receipt_id": receipt_id,
                }
                _succeed_operation(cursor, context, result)
                event = {
                    "schema_version": "committed_event.v2",
                    "event_type": "PERSONALIZATION_PROFILE_INDUCTED",
                    "operation_id": claim.work_id,
                    "analysis_revision_id": stored["analysis_revision_id"],
                    "profile_id": profile_id,
                    "profile_revision_id": profile_revision_id,
                    "profile_version": to_version,
                    "synthetic_non_release": scope.data_mode == "replay",
                }
                _insert_outbox(
                    cursor,
                    event_id=event_id,
                    scope=scope,
                    event_type=str(event["event_type"]),
                    aggregate_type="PersonalizationProfile",
                    aggregate_id=profile_id,
                    aggregate_version=to_version,
                    operation_id=claim.work_id,
                    event=event,
                )
                _insert_audit(
                    cursor,
                    audit_id=audit_id,
                    scope=scope,
                    policy_sha256=str(row[10]),
                    reason_code="induction_committed",
                    audit={
                        "operation_id": claim.work_id,
                        "manifest_id": stored["manifest_id"],
                        "profile_revision_id": profile_revision_id,
                    },
                )
            finally:
                cursor.close()
            uow.commit()
        return result


class DeterministicReplayDeliverySink:
    """A queryable idempotent receiver used only by replay delivery tests."""

    def __init__(self, *, consumer_id: str = "replay-care-sink.v1") -> None:
        self.consumer_id = consumer_id

    def send(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        intent: Mapping[str, str],
    ) -> tuple[Mapping[str, Any], str]:
        claim = context.claim
        effect_id = "replay-effect:" + hashlib.sha256(
            intent["semantic_effect_key"].encode("utf-8")
        ).hexdigest()
        response = {
            "schema_version": "replay_delivery_sink_result.v1",
            "effect_id": effect_id,
            "semantic_effect_key": intent["semantic_effect_key"],
            "status": "delivered",
        }
        response_sha256 = _sha256(response)
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                # Serialize the deterministic receiver's linearization point by
                # semantic effect key. Besides making concurrent duplicate sends
                # unambiguous, this lock gives the process-fault harness a real
                # boundary: the invocation journal is already ``send_started``
                # while the external effect transaction is still unable to run.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_delivery_effect_lock_name(intent["semantic_effect_key"]),),
                )
                cursor.execute(
                    "SELECT public.sleepagent_stage4_delivery_authority_allows(%s)",
                    (claim.work_id,),
                )
                authority = cursor.fetchone()
                if authority is None or authority[0] is not True:
                    raise TerminalWorkError("delivery_authority_revoked")
                cursor.execute(
                    "SELECT payload_sha256, semantic_effect_key, "
                    "aggregate_type, aggregate_id, aggregate_sequence "
                    "FROM public.backend_delivery_intents "
                    "WHERE delivery_intent_id = %s AND status = 'dispatching' "
                    "AND lease_generation = %s AND fencing_token = %s "
                    "AND worker_instance = %s "
                    "AND lease_expires_at > clock_timestamp() FOR UPDATE",
                    (
                        claim.work_id,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                delivery = cursor.fetchone()
                if (
                    delivery is None
                    or str(delivery[0]) != intent["payload_sha256"]
                    or str(delivery[1]) != intent["semantic_effect_key"]
                ):
                    raise LeaseLostError("delivery dispatch fence was lost")
                cursor.execute(
                    "INSERT INTO public.backend_consumer_inbox ("
                    "consumer_id, delivery_intent_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "semantic_effect_key, payload_sha256, received_at, applied_at, "
                    "result_sha256) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "clock_timestamp(), clock_timestamp(), %s) "
                    "ON CONFLICT (consumer_id, delivery_intent_id) DO NOTHING",
                    (
                        self.consumer_id,
                        claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        intent["semantic_effect_key"],
                        intent["payload_sha256"],
                        response_sha256,
                    ),
                )
                cursor.execute(
                    "INSERT INTO public.backend_replay_delivery_effects_v2 ("
                    "effect_id, consumer_id, delivery_intent_id, namespace_id, "
                    "data_mode, namespace_generation, run_id, arm_id, subject_id, "
                    "semantic_effect_key, payload_sha256, result_sha256, "
                    "result_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb) ON CONFLICT (semantic_effect_key) DO NOTHING",
                    (
                        effect_id,
                        self.consumer_id,
                        claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        intent["semantic_effect_key"],
                        intent["payload_sha256"],
                        response_sha256,
                        _json(response),
                    ),
                )
                cursor.execute(
                    "SELECT effect_id, delivery_intent_id, payload_sha256, "
                    "result_sha256, result_json "
                    "FROM public.backend_replay_delivery_effects_v2 "
                    "WHERE semantic_effect_key = %s",
                    (intent["semantic_effect_key"],),
                )
                stored = cursor.fetchone()
                if (
                    stored is None
                    or str(stored[0]) != effect_id
                    or str(stored[1]) != claim.work_id
                    or str(stored[2]) != intent["payload_sha256"]
                    or str(stored[3]) != response_sha256
                    or _mapping(stored[4], "delivery effect") != response
                ):
                    raise TerminalWorkError("delivery_effect_conflict")
                cursor.execute(
                    "UPDATE public.backend_care_actions_v2 SET "
                    "state = 'active', updated_at = clock_timestamp() "
                    "WHERE care_action_id = %s AND namespace_id = %s "
                    "AND data_mode = %s AND namespace_generation = %s "
                    "AND COALESCE(run_id, '') = COALESCE(%s, '') "
                    "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
                    "AND subject_id = %s "
                    "AND human_decision_id = %s "
                    "AND state IN ('confirmed_pending_delivery', 'active') "
                    "RETURNING state",
                    (
                        intent["care_action_id"],
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        intent["human_decision_id"],
                    ),
                )
                care_action = cursor.fetchone()
                if care_action is None or str(care_action[0]) != "active":
                    raise TerminalWorkError("delivery_care_action_transition_failed")
            finally:
                cursor.close()
            uow.commit()
        return response, effect_id


def _delivery_effect_lock_name(semantic_effect_key: str) -> str:
    if not semantic_effect_key:
        raise Stage4Error("delivery semantic effect key is required")
    return "sleepagent:replay-delivery-effect:" + semantic_effect_key


class ReplayDeliveryWorkHandler:
    def __init__(self, *, sink: DeterministicReplayDeliverySink | None = None) -> None:
        self.sink = sink or DeterministicReplayDeliverySink()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _delivery_scope(context)
            intent = _delivery_payload(context.claim)
            request = {
                "schema_version": "replay_delivery_sink_request.v1",
                **intent,
            }
            response = context.invocation_dispatcher().dispatch(
                invocation_key=(
                    f"replay-delivery:{intent['semantic_effect_key']}:v1"
                ),
                request=request,
                sender=lambda: self.sink.send(
                    context,
                    scope=scope,
                    intent=intent,
                ),
                invocation_kind=InvocationKind.EXTERNAL_SINK,
            )
            if response.get("status") != "delivered":
                raise TerminalWorkError("delivery_sink_contract_invalid")
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=dict(response),
            )
        except RetryableWorkError as exc:
            return WorkResult(
                disposition=WorkDisposition.RETRYABLE,
                error_code=exc.code,
                retry_after_seconds=exc.retry_after_seconds,
            )
        except (OutcomeUnknownError, LeaseLostError) as exc:
            if isinstance(exc, LeaseLostError):
                context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code=getattr(exc, "code", "delivery_outcome_unknown"),
            )
        except TerminalWorkError as exc:
            return _terminal(exc.code)
        except (B3ClaimInvariantError, Stage4Error, KeyError, TypeError, ValueError):
            return _terminal("delivery_invariant_violation")


class DeliveryReconciliationHandler:
    """Resolve ambiguous replay sends by querying the idempotent sink ledger."""

    def __init__(self, *, id_generator: UUID7Generator | None = None) -> None:
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _operation_scope(
                context,
                queue=RECONCILIATION_QUEUE,
                operation_type="delivery_reconciliation",
            )
            payload = _delivery_reconciliation_payload(context.claim.payload)
            result = self._commit(context, scope=scope, payload=payload)
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=result,
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except Stage4LeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="delivery_reconciliation_lease_lost",
            )
        except (B3ClaimInvariantError, Stage4Error, KeyError, TypeError, ValueError):
            return _terminal("delivery_reconciliation_invariant_violation")

    def _commit(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        payload: Mapping[str, str],
    ) -> dict[str, Any]:
        claim = context.claim
        receipt_id = self.id_generator()
        event_id = self.id_generator()
        audit_id = self.id_generator()
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT intent.destination, intent.handler_name, "
                    "intent.semantic_effect_key, intent.payload_sha256, "
                    "intent.status, intent.lease_generation, "
                    "intent.fencing_token, source.policy_sha256, "
                    "invocation.invocation_id, invocation.current_state, "
                    "invocation.request_sha256, invocation.cas_version, "
                    "invocation.lease_generation, invocation.fencing_token "
                    "FROM public.sleep_domain_operations AS operation "
                    "JOIN public.backend_delivery_intents AS intent "
                    "ON intent.delivery_intent_id = operation.target_resource_id "
                    "AND intent.namespace_id = operation.namespace_id "
                    "AND intent.data_mode = operation.data_mode "
                    "AND intent.namespace_generation = operation.namespace_generation "
                    "AND intent.subject_id = operation.subject_id "
                    "JOIN public.sleep_domain_domain_outbox AS event "
                    "ON event.event_id = intent.source_event_id "
                    "AND event.namespace_id = intent.namespace_id "
                    "AND event.data_mode = intent.data_mode "
                    "AND event.subject_id = intent.subject_id "
                    "JOIN public.sleep_domain_operations AS source "
                    "ON source.operation_id = event.operation_id "
                    "AND source.namespace_id = intent.namespace_id "
                    "AND source.data_mode = intent.data_mode "
                    "AND source.subject_id = intent.subject_id "
                    "JOIN public.backend_invocations AS invocation "
                    "ON invocation.operation_id = source.operation_id "
                    "AND invocation.namespace_id = intent.namespace_id "
                    "AND invocation.data_mode = intent.data_mode "
                    "AND invocation.subject_id = intent.subject_id "
                    "AND invocation.invocation_key = %s "
                    "AND invocation.invocation_kind = 'external_sink' "
                    "WHERE operation.operation_id = %s "
                    "AND operation.operation_type = 'delivery_reconciliation' "
                    "AND operation.queue_name = 'reconciliation' "
                    "AND operation.status = 'running' "
                    "AND operation.cas_version = %s "
                    "AND operation.lease_generation = %s "
                    "AND operation.fencing_token = %s "
                    "AND operation.worker_instance = %s "
                    "AND operation.lease_expires_at > clock_timestamp() "
                    "FOR UPDATE OF operation, intent, invocation",
                    (
                        payload["invocation_key"],
                        claim.work_id,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise Stage4LeaseLost("reconciliation operation fence was lost")
                stored = {
                    "destination": str(row[0]),
                    "handler_name": str(row[1]),
                    "semantic_effect_key": str(row[2]),
                    "payload_sha256": str(row[3]),
                }
                if any(payload[name] != value for name, value in stored.items()):
                    raise Stage4Error("delivery reconciliation source drifted")
                if str(row[4]) != "outcome_unknown":
                    raise Stage4Error("delivery is not awaiting reconciliation")

                cursor.execute(
                    "SELECT effect_id, delivery_intent_id, payload_sha256, "
                    "result_sha256, result_json "
                    "FROM public.backend_replay_delivery_effects_v2 "
                    "WHERE semantic_effect_key = %s",
                    (payload["semantic_effect_key"],),
                )
                effect = cursor.fetchone()
                invocation_id = None if row[8] is None else str(row[8])
                invocation_state = None if row[9] is None else str(row[9])
                deterministic_query = (
                    payload["destination"] == DELIVERY_DESTINATION
                    and payload["handler_name"] == DELIVERY_HANDLER
                    and invocation_id is not None
                    and invocation_state
                    in {"send_started", "outcome_possible", "outcome_unknown"}
                )
                if effect is not None and (
                    str(effect[1]) == payload["delivery_intent_id"]
                    and str(effect[2]) == payload["payload_sha256"]
                    and _mapping(effect[4], "delivery effect").get("status")
                    == "delivered"
                ):
                    resolution = "known_delivered"
                    delivery_state = "delivered"
                    evidence_ref = str(effect[0])
                elif effect is None and deterministic_query:
                    resolution = "known_not_delivered"
                    delivery_state = "retry"
                    evidence_ref = (
                        "replay-sink-query:absent:"
                        + payload["semantic_effect_key"]
                    )
                else:
                    resolution = "unknown"
                    delivery_state = "dead_letter"
                    evidence_ref = None

                if invocation_id is not None:
                    cursor.execute(
                        "UPDATE public.backend_invocations SET "
                        "current_state = 'reconciled', "
                        "response_sha256 = CASE WHEN %s = 'known_delivered' "
                        "THEN %s ELSE response_sha256 END, "
                        "cas_version = cas_version + 1, "
                        "updated_at = clock_timestamp() "
                        "WHERE invocation_id = %s AND cas_version = %s "
                        "AND current_state IN ('send_started', "
                        "'outcome_possible', 'outcome_unknown') "
                        "RETURNING cas_version",
                        (
                            resolution,
                            None if effect is None else str(effect[3]),
                            invocation_id,
                            int(row[11]),
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise Stage4Error("invocation reconciliation CAS was lost")
                    _insert_invocation_reconciliation_journal(
                        cursor,
                        event_id=self.id_generator(),
                        scope=scope,
                        invocation_id=invocation_id,
                        from_state=str(row[9]),
                        lease_generation=int(row[12]),
                        fencing_token=str(row[13]),
                        resolution=resolution,
                        evidence_ref=evidence_ref,
                    )

                cursor.execute(
                    "UPDATE public.backend_delivery_intents SET status = %s, "
                    "available_at = CASE WHEN %s = 'retry' "
                    "THEN clock_timestamp() ELSE available_at END, "
                    "delivered_at = CASE WHEN %s = 'delivered' "
                    "THEN clock_timestamp() ELSE delivered_at END, "
                    "dispatch_permit_at = NULL, updated_at = clock_timestamp() "
                    "WHERE delivery_intent_id = %s "
                    "AND status = 'outcome_unknown' RETURNING status",
                    (
                        delivery_state,
                        delivery_state,
                        delivery_state,
                        payload["delivery_intent_id"],
                    ),
                )
                if cursor.fetchone() is None:
                    raise Stage4Error("delivery reconciliation CAS was lost")
                _insert_delivery_reconciliation_journal(
                    cursor,
                    event_id=self.id_generator(),
                    scope=scope,
                    delivery_intent_id=payload["delivery_intent_id"],
                    lease_generation=int(row[5]),
                    fencing_token=None if row[6] is None else str(row[6]),
                    to_state=delivery_state,
                    resolution=resolution,
                    evidence_ref=evidence_ref,
                )
                receipt = {
                    "schema_version": "delivery_reconciliation_receipt.v1",
                    "operation_id": claim.work_id,
                    "delivery_intent_id": payload["delivery_intent_id"],
                    "invocation_id": invocation_id,
                    "resolution": resolution,
                    "provider_evidence_ref": evidence_ref,
                    "delivery_state": delivery_state,
                }
                if invocation_id is None:
                    raise Stage4Error(
                        "ambiguous delivery without invocation cannot be receipted"
                    )
                cursor.execute(
                    "INSERT INTO public.backend_delivery_reconciliation_receipts_v2 ("
                    "reconciliation_receipt_id, operation_id, delivery_intent_id, "
                    "invocation_id, namespace_id, data_mode, namespace_generation, "
                    "run_id, arm_id, subject_id, resolution, provider_evidence_ref, "
                    "receipt_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        receipt_id,
                        claim.work_id,
                        payload["delivery_intent_id"],
                        invocation_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        resolution,
                        evidence_ref,
                        _json(receipt),
                    ),
                )
                result = {
                    "schema_version": "delivery_reconciliation_result.v1",
                    "delivery_intent_id": payload["delivery_intent_id"],
                    "invocation_id": invocation_id,
                    "resolution": resolution,
                    "delivery_state": delivery_state,
                    "receipt_id": receipt_id,
                }
                _succeed_operation(cursor, context, result)
                _insert_outbox(
                    cursor,
                    event_id=event_id,
                    scope=scope,
                    event_type="DELIVERY_RECONCILED",
                    aggregate_type="DeliveryIntent",
                    aggregate_id=payload["delivery_intent_id"],
                    aggregate_version=int(row[5]) + 1,
                    operation_id=claim.work_id,
                    event={
                        "schema_version": "committed_event.v2",
                        "event_type": "DELIVERY_RECONCILED",
                        "delivery_intent_id": payload["delivery_intent_id"],
                        "resolution": resolution,
                        "delivery_state": delivery_state,
                        "synthetic_non_release": scope.data_mode == "replay",
                    },
                )
                _insert_audit(
                    cursor,
                    audit_id=audit_id,
                    scope=scope,
                    policy_sha256=str(row[7]),
                    reason_code="delivery_reconciled",
                    audit={
                        "operation_id": claim.work_id,
                        "delivery_intent_id": payload["delivery_intent_id"],
                        "resolution": resolution,
                    },
                )
            finally:
                cursor.close()
            uow.commit()
        return result


class Stage4ReconciliationRouter:
    def __init__(self) -> None:
        self.date_handler = EpisodeDateReconciliationHandler()
        self.delivery_handler = DeliveryReconciliationHandler()

    def __call__(self, context: WorkContext) -> WorkResult:
        operation_type = context.claim.metadata.get("operation_type")
        if operation_type == "episode_date_reconciliation":
            return self.date_handler(context)
        if operation_type == "delivery_reconciliation":
            return self.delivery_handler(context)
        return _terminal("reconciliation_operation_unsupported")


def build_stage4_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    selected = set(settings.worker_queues).intersection(
        {INDUCTION_QUEUE, DELIVERY_QUEUE, RECONCILIATION_QUEUE}
    )
    if not selected:
        return {}
    if settings.process_role != ProcessRole.WORKER:
        raise Stage4Error("Stage-4 handlers require a worker profile")
    if settings.data_mode != DataMode.REPLAY or settings.model_mode != ModelMode.DETERMINISTIC:
        raise Stage4Error("Stage-4 handlers require deterministic replay")
    handlers: dict[str, WorkHandler] = {}
    if INDUCTION_QUEUE in selected:
        handlers[INDUCTION_QUEUE] = InductionWorkHandler()
    if DELIVERY_QUEUE in selected:
        handlers[DELIVERY_QUEUE] = ReplayDeliveryWorkHandler()
    if RECONCILIATION_QUEUE in selected:
        handlers[RECONCILIATION_QUEUE] = Stage4ReconciliationRouter()
    return handlers


def _operation_scope(
    context: WorkContext,
    *,
    queue: str,
    operation_type: str,
) -> UowScope:
    claim = context.claim
    if (
        claim.queue != queue
        or claim.operation_id != claim.work_id
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("operation_type") != operation_type
        or claim.metadata.get("queue_name") != queue
    ):
        raise Stage4Error("invalid Stage-4 operation claim")
    scope = exact_worker_scope(context, allowed_handler=queue)
    if claim.payload.get("authorization_snapshot") != claim.authorization_snapshot:
        raise Stage4Error("embedded Stage-4 authority snapshot drifted")
    return scope


def _delivery_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != DELIVERY_QUEUE
        or claim.subject_id is None
        or claim.metadata.get("work_kind") != "delivery"
        or claim.metadata.get("handler_name") != DELIVERY_HANDLER
        or claim.metadata.get("destination") != DELIVERY_DESTINATION
    ):
        raise Stage4Error("invalid replay delivery claim")
    scope = context.store.uow_scope_for_claim(claim)
    exact = {
        "namespace_id": claim.namespace_id,
        "namespace_generation": claim.namespace_generation,
        "data_mode": claim.data_mode,
        "run_id": claim.run_id,
        "arm_id": claim.arm_id,
        "subject_id": claim.subject_id,
        "worker_instance": claim.worker_instance,
    }
    if scope.process_role != "worker" or any(
        getattr(scope, name) != value for name, value in exact.items()
    ):
        raise Stage4Error("replay delivery scope drifted")
    return scope


def _induction_payload(value: Mapping[str, object]) -> dict[str, str]:
    expected = {
        "schema_version",
        "manifest_id",
        "manifest_sha256",
        "analysis_revision_id",
        "night_episode_revision_id",
        "projector_version",
        "authorization_snapshot",
    }
    if set(value) != expected or value.get("schema_version") != "induction_operation.v1":
        raise Stage4Error("induction operation version drifted")
    result: dict[str, str] = {}
    for name in expected - {"schema_version", "authorization_snapshot"}:
        item = value.get(name)
        if not isinstance(item, str) or not item:
            raise Stage4Error(f"induction field {name!r} is invalid")
        result[name] = item
    return result


def _delivery_payload(claim: Any) -> dict[str, str]:
    value = claim.payload
    required = {
        "schema_version": "delivery_intent.v2",
        "care_action_id": None,
        "human_decision_id": None,
        "recipient_actor_id": None,
        "recipient_binding_id": None,
        "recipient_role": None,
    }
    if set(value) != {*required, "synthetic_non_release"}:
        raise Stage4Error("delivery intent shape drifted")
    if value.get("schema_version") != required["schema_version"]:
        raise Stage4Error("delivery intent version drifted")
    result = {
        name: str(value[name])
        for name in required
        if name != "schema_version"
        and isinstance(value.get(name), str)
        and str(value[name])
    }
    if len(result) != len(required) - 1 or value.get("synthetic_non_release") is not True:
        raise Stage4Error("delivery intent identity is incomplete")
    result.update(
        {
            "delivery_intent_id": claim.work_id,
            "semantic_effect_key": str(claim.metadata["semantic_effect_key"]),
            "payload_sha256": _sha256(value),
        }
    )
    return result


def _delivery_reconciliation_payload(
    value: Mapping[str, object],
) -> dict[str, str]:
    expected = {
        "schema_version",
        "delivery_intent_id",
        "source_operation_id",
        "destination",
        "handler_name",
        "semantic_effect_key",
        "payload_sha256",
        "invocation_key",
        "trigger_error_code",
        "authorization_snapshot",
    }
    if (
        set(value) != expected
        or value.get("schema_version")
        != "delivery_reconciliation_operation.v1"
    ):
        raise Stage4Error("delivery reconciliation operation version drifted")
    result: dict[str, str] = {}
    for name in expected - {"schema_version", "authorization_snapshot"}:
        item = value.get(name)
        if not isinstance(item, str) or not item:
            raise Stage4Error(
                f"delivery reconciliation field {name!r} is invalid"
            )
        result[name] = item
    if len(result["payload_sha256"]) != 64:
        raise Stage4Error("delivery reconciliation payload hash is invalid")
    return result


def _insert_invocation_reconciliation_journal(
    cursor: Any,
    *,
    event_id: str,
    scope: UowScope,
    invocation_id: str,
    from_state: str,
    lease_generation: int,
    fencing_token: str,
    resolution: str,
    evidence_ref: str | None,
) -> None:
    cursor.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 "
        "FROM public.backend_invocation_journal WHERE invocation_id = %s",
        (invocation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise Stage4Error("invocation journal sequence is unavailable")
    cursor.execute(
        "INSERT INTO public.backend_invocation_journal ("
        "invocation_event_id, invocation_id, namespace_id, data_mode, "
        "namespace_generation, run_id, arm_id, subject_id, sequence, "
        "from_state, to_state, lease_generation, fencing_token, event_json, "
        "occurred_at) VALUES ("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'reconciled', %s, %s, "
        "%s::jsonb, clock_timestamp())",
        (
            event_id,
            invocation_id,
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            int(row[0]),
            from_state,
            lease_generation,
            fencing_token,
            _json(
                {
                    "event": "reconciled",
                    "resolution": resolution,
                    "provider_evidence_ref": evidence_ref,
                }
            ),
        ),
    )


def _insert_delivery_reconciliation_journal(
    cursor: Any,
    *,
    event_id: str,
    scope: UowScope,
    delivery_intent_id: str,
    lease_generation: int,
    fencing_token: str | None,
    to_state: str,
    resolution: str,
    evidence_ref: str | None,
) -> None:
    cursor.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 "
        "FROM public.backend_delivery_journal WHERE delivery_intent_id = %s",
        (delivery_intent_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise Stage4Error("delivery journal sequence is unavailable")
    cursor.execute(
        "INSERT INTO public.backend_delivery_journal ("
        "delivery_event_id, delivery_intent_id, namespace_id, data_mode, "
        "namespace_generation, run_id, arm_id, subject_id, sequence, "
        "from_state, to_state, lease_generation, fencing_token, event_json, "
        "occurred_at) VALUES ("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, 'outcome_unknown', %s, %s, %s, "
        "%s::jsonb, clock_timestamp())",
        (
            event_id,
            delivery_intent_id,
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            int(row[0]),
            to_state,
            lease_generation,
            fencing_token,
            _json(
                {
                    "event": "reconciliation_committed",
                    "resolution": resolution,
                    "provider_evidence_ref": evidence_ref,
                }
            ),
        ),
    )


def _succeed_operation(
    cursor: Any,
    context: WorkContext,
    result: Mapping[str, Any],
) -> None:
    claim = context.claim
    cursor.execute(
        "UPDATE public.sleep_domain_operations SET status = 'succeeded', "
        "outcome_class = 'succeeded', operation_json = operation_json || "
        "jsonb_build_object('result', %s::jsonb), cas_version = cas_version + 1, "
        "updated_at = clock_timestamp(), lease_owner = NULL, "
        "lease_expires_at = NULL, fencing_token = NULL, worker_instance = NULL, "
        "heartbeat_at = NULL WHERE operation_id = %s AND status = 'running' "
        "AND cas_version = %s AND lease_generation = %s AND fencing_token = %s "
        "AND worker_instance = %s AND lease_expires_at > clock_timestamp() "
        "RETURNING cas_version",
        (
            _json(result),
            claim.work_id,
            claim.operation_version,
            claim.lease_generation,
            claim.fencing_token,
            claim.worker_instance,
        ),
    )
    if cursor.fetchone() is None:
        raise Stage4LeaseLost("Stage-4 operation terminal CAS was lost")


def _insert_outbox(
    cursor: Any,
    *,
    event_id: str,
    scope: UowScope,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    operation_id: str,
    event: Mapping[str, Any],
) -> None:
    cursor.execute(
        "INSERT INTO public.sleep_domain_domain_outbox ("
        "event_id, namespace_id, data_mode, event_type, aggregate_type, "
        "aggregate_id, aggregate_version, per_aggregate_sequence, subject_id, "
        "operation_id, status, available_at, event_json, created_at, "
        "protocol_version, namespace_generation, run_id, arm_id) VALUES ("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'committed', "
        "clock_timestamp(), %s::jsonb, clock_timestamp(), 2, %s, %s, %s)",
        (
            event_id,
            scope.namespace_id,
            scope.data_mode,
            event_type,
            aggregate_type,
            aggregate_id,
            aggregate_version,
            aggregate_version,
            scope.subject_id,
            operation_id,
            _json(event),
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
        ),
    )


def _insert_audit(
    cursor: Any,
    *,
    audit_id: str,
    scope: UowScope,
    policy_sha256: str,
    reason_code: str,
    audit: Mapping[str, Any],
) -> None:
    cursor.execute(
        "INSERT INTO public.backend_authorization_audit ("
        "audit_id, namespace_id, data_mode, subject_id, principal_id, actor_id, "
        "binding_id, decision, reason_code, policy_sha256, authorization_epoch, "
        "privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at) VALUES ("
        "%s, %s, %s, %s, %s, NULL, NULL, 'allow', %s, %s, %s, %s, %s, "
        "%s::jsonb, clock_timestamp())",
        (
            audit_id,
            scope.namespace_id,
            scope.data_mode,
            scope.subject_id,
            scope.service_principal_id,
            reason_code,
            policy_sha256,
            scope.authorization_epoch,
            scope.privacy_epoch,
            scope.retrieval_policy_epoch,
            _json(audit),
        ),
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise Stage4Error(f"{label} must be an object")
    return dict(value)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _terminal(code: str) -> WorkResult:
    return WorkResult(disposition=WorkDisposition.TERMINAL, error_code=code)


__all__ = [
    "DELIVERY_DESTINATION",
    "DELIVERY_HANDLER",
    "DELIVERY_QUEUE",
    "DeliveryReconciliationHandler",
    "DeterministicReplayDeliverySink",
    "INDUCTION_QUEUE",
    "InductionWorkHandler",
    "RECONCILIATION_QUEUE",
    "ReplayDeliveryWorkHandler",
    "Stage4ReconciliationRouter",
    "build_stage4_worker_handlers",
]
