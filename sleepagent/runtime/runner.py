# 本模块编排唯一的 Product Episode 四智能体执行与安全终态。
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sleepagent.runtime.agents import (
    ProductAgentRoster,
    RuntimeAgentPort,
)
from sleepagent.runtime.agent_invocation_coordinator import (
    AgentInvocationCoordinator,
    ProviderInputBudgetLedger,
)
from sleepagent.runtime.contracts import (
    AgentEnvelope,
    AgentId,
    CareActionCandidate,
    CareStrategy,
    CommunicationDraft,
    CrossAgentRequest,
    CrossAgentRequestType,
    EpisodeReceipt,
    EpisodePlan,
    EpisodeStatus,
    EpisodeType,
    EvidencePacket,
    ExecutionMode,
    InvocationOutcome,
    OnlineRiskLevel,
    SafetyDecision,
    SafetyVerdict,
    ToolReceipt,
    WorkProductKind,
    stable_hash,
)
from sleepagent.runtime.episode import (
    EvaluationDecision,
    ProductEpisodeRuntime,
)
from sleepagent.runtime.episode_result_finalizer import (
    EpisodeResultFinalizer,
)
from sleepagent.runtime.governance import (
    AcceptanceError,
    AcceptedWorkProduct,
    CareActionCatalog,
    InMemoryCareContextStore,
    PublicationError,
    accept_care,
    accept_communication,
    accept_evidence,
    accept_safety,
    build_cold_start_receipt,
    publication_postflight,
    safety_trigger_reasons,
)
from sleepagent.runtime.cold_start import (
    ResponseMode,
    degraded_boundary_sentence,
)
from sleepagent.runtime.registry import (
    EPISODE_DEFINITIONS,
    product_agent_manifest,
)
from sleepagent.runtime.publication_service import (
    PublicationIndeterminateError,
    PublicationService,
)
from sleepagent.runtime.registry import (
    AgentProfile,
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
)
from sleepagent.runtime.results import (
    PRODUCT_EPISODE_RESULT_SCHEMA_VERSION,
    PRODUCT_EPISODE_RUNNER_VERSION,
    PendingConfirmationTarget,
    PendingUserInputTarget,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
    ProductUserFactResponse,
    doctor_safety_checkpoint as _doctor_safety_checkpoint,
    effective_audience_role as _effective_audience_role,
    product_episode_request_hash,
    uses_doctor_material_semantics as _uses_doctor_material_semantics,
)
from sleepagent.runtime.contracts import (
    FactSnapshotRevalidator,
    ProductEpisodeResultStore,
    ProductToolExecutionContext,
    ProductToolExecutorPort,
    PublicationPublisher,
)
from sleepagent.runtime.tool_execution_coordinator import (
    ToolExecutionCoordinator,
    serialized_episode_execution,
)
from sleepagent.runtime.tools import CareCoordinationPolicyResult
from sleepagent.runtime.memory import (
    InMemoryLongitudinalResultStore,
    LongitudinalMemoryService,
)
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.reports import (
    ElderNarrative,
    ElderNarrativeRequest,
    ElderNarrativeState,
    SharedAnalysisRunRequest,
    SharedNightAnalysis,
)


LOGGER = logging.getLogger(__name__)


class AgentInteractionRequired(RuntimeError):
    def __init__(
        self,
        status: EpisodeStatus,
        reason_code: str,
        request: CrossAgentRequest | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.status = status
        self.reason_code = reason_code
        self.request = request


class InMemoryProductEpisodeResultStore(InMemoryLongitudinalResultStore):
    """Product-facing name for the atomic longitudinal reference store."""


class _DetachedNarrativeRuntime:
    """One-call runtime view for an independently durable elder narrative."""

    def __init__(self, shared: SharedNightAnalysis) -> None:
        self.episode_state_revision = max(
            item.episode_state_revision
            for item in (
                shared.evidence,
                *(() if shared.care is None else (shared.care,)),
                *(() if shared.safety is None else (shared.safety,)),
            )
        )
        self.invocation_records: list[AgentInvocationRecord] = []
        self.accepted_work_products = shared.accepted_products()
        self.plan: EpisodePlan | None = None

    def record_agent_invocation(self, record: AgentInvocationRecord) -> None:
        if self.invocation_records:
            raise RuntimeError("elder narrative permits exactly one Agent invocation")
        if record.agent_id is not AgentId.SLEEP_CARE:
            raise RuntimeError("elder narrative permits only SleepCare")
        self.invocation_records.append(record)

    def record_tool_call(self) -> None:
        raise RuntimeError("elder narrative cannot invoke Tools")


class ProductEpisodeRunner:
    """Execute minimal scenario paths over exactly four Agent identities."""

    def __init__(
        self,
        *,
        agent_roster: ProductAgentRoster,
        care_catalog: CareActionCatalog,
        care_store: InMemoryCareContextStore,
        skill_registry: SkillRegistry,
        agent_invocation_coordinator: AgentInvocationCoordinator,
        tool_execution_coordinator: ToolExecutionCoordinator,
        publication_service: PublicationService,
        episode_result_finalizer: EpisodeResultFinalizer,
    ) -> None:
        if any(
            agent.skill_registry.snapshot() != skill_registry.snapshot()
            for agent in agent_roster
        ):
            raise ValueError("Agent roster and Runner Skill registries differ")
        if agent_invocation_coordinator.skill_resolver.registry is not skill_registry:
            raise ValueError("Runner Skill resolver must use the injected registry")
        if set(agent_invocation_coordinator.agent_profiles) != set(AgentId):
            raise ValueError("Runner requires one profile for each concrete Agent")
        if (
            agent_invocation_coordinator.longitudinal_memory
            is not publication_service.longitudinal_memory
        ):
            raise ValueError("Runner invocation/Memory graph is inconsistent")
        if (
            agent_invocation_coordinator.tool_execution_coordinator
            is not tool_execution_coordinator
        ):
            raise ValueError("Runner invocation/Tool graph is inconsistent")
        if (
            agent_invocation_coordinator.longitudinal_memory.repository
            is not episode_result_finalizer.result_store
        ):
            raise ValueError("Runner Memory/result-store graph is inconsistent")
        if (
            publication_service.result_store
            is not episode_result_finalizer.result_store
        ):
            raise ValueError("Runner publication/result-store graph is inconsistent")
        if (
            episode_result_finalizer.tool_execution_coordinator
            is not tool_execution_coordinator
        ):
            raise ValueError("Runner finalizer/Tool graph is inconsistent")
        if (
            episode_result_finalizer.provider_input_budget_ledger
            is not agent_invocation_coordinator.provider_input_budget
        ):
            raise ValueError("Runner finalizer/provider-ledger graph is inconsistent")
        self.agent_roster = agent_roster
        self.care_catalog = care_catalog
        self.care_store = care_store
        self.skill_registry = skill_registry
        self.agent_invocation_coordinator = agent_invocation_coordinator
        self.tool_execution_coordinator = tool_execution_coordinator
        self.publication_service = publication_service
        self.episode_result_finalizer = episode_result_finalizer

    @property
    def tool_executor(self) -> ProductToolExecutorPort:
        return self.tool_execution_coordinator.executor

    @property
    def publisher(self) -> PublicationPublisher:
        return self.publication_service.publisher

    @property
    def result_store(self) -> ProductEpisodeResultStore:
        return self.episode_result_finalizer.result_store

    @property
    def longitudinal_memory(self) -> LongitudinalMemoryService:
        return self.agent_invocation_coordinator.longitudinal_memory

    @property
    def fact_snapshot_revalidator(self) -> FactSnapshotRevalidator | None:
        return self.publication_service.revalidator

    @property
    def skill_resolver(self) -> SkillResolver:
        return self.agent_invocation_coordinator.skill_resolver

    @property
    def prompt_compiler(self) -> PromptCompiler:
        return self.agent_invocation_coordinator.prompt_compiler

    @property
    def agent_profiles(self) -> Mapping[AgentId, AgentProfile]:
        return self.agent_invocation_coordinator.agent_profiles

    @property
    def provider_input_budget(self) -> ProviderInputBudgetLedger:
        return self.agent_invocation_coordinator.provider_input_budget

    @serialized_episode_execution
    def analyze_shared(
        self,
        request: SharedAnalysisRunRequest,
    ) -> SharedNightAnalysis:
        """Run one role-neutral reasoning pass and stop before communication."""

        runtime_request = request.runtime_request
        if runtime_request.episode_type is EpisodeType.DATA_QUALITY_RECOVERY:
            raise AcceptanceError("shared analysis cannot run data-quality recovery")

        preflight_receipts: list[ToolReceipt] = []
        if runtime_request.runtime_readiness_decisions:
            preflight_receipts.append(
                build_cold_start_receipt(
                    snapshot=runtime_request.fact_snapshot,
                    decisions=runtime_request.runtime_readiness_decisions,
                    capability_receipts=runtime_request.runtime_capability_receipts,
                    observed_at=datetime.now(timezone.utc),
                )
            )
        runtime = ProductEpisodeRuntime(
            episode_id=runtime_request.episode_id,
            fact_snapshot=runtime_request.fact_snapshot,
            sleepcare_agent=self.agent_roster.sleepcare,
            skill_registry=self.skill_registry,
        )
        envelopes: list[AgentEnvelope] = []
        tool_receipts: list[ToolReceipt] = list(preflight_receipts)
        evidence: AcceptedWorkProduct | None = None
        care: AcceptedWorkProduct | None = None
        safety: AcceptedWorkProduct | None = None
        try:
            required, checkpoints = self._request_requirements(runtime_request)
            plan = runtime.create_plan(
                episode_type=runtime_request.episode_type,
                objective=runtime_request.objective,
                required_work_products=required,
                required_safety_checkpoints=checkpoints,
            )
            for _ in preflight_receipts:
                runtime.record_tool_call()
            registered_receipts = self._run_registered_tools(
                runtime_request,
                runtime,
            )
            tool_receipts.extend(registered_receipts)
            failed_required = [
                receipt
                for receipt in registered_receipts
                if receipt.outcome != InvocationOutcome.SUCCEEDED
            ]
            if failed_required:
                raise AcceptanceError(
                    f"required Tool failed: {failed_required[0].tool_name}"
                )

            deterministic_risk_reasons: list[str] = []
            policy_routed_care_required = False
            planned = set(plan.required_work_products) | set(
                plan.conditional_work_products
            )
            if WorkProductKind.EVIDENCE_PACKET in planned:
                envelope, evidence = self._invoke_and_accept(
                    request=runtime_request,
                    runtime=runtime,
                    agent=self.agent_roster.evidence_reasoning,
                    tool_receipts=tool_receipts,
                    accepted_evidence=None,
                    accepted_care=None,
                    safety_target=None,
                )
                envelopes.append(envelope)
                risk_receipts: list[ToolReceipt] = []
                if (
                    "risk.classify_signal"
                    in EPISODE_DEFINITIONS[
                        runtime_request.episode_type
                    ].required_tools
                ):
                    risk_arguments = dict(
                        runtime_request.tool_inputs.get(
                            "risk.classify_signal",
                            {},
                        )
                    )
                    trend_signals = tuple(
                        risk_arguments.pop("trend_signals", ())
                    )
                    trend_observation = risk_arguments.pop(
                        "trend_observation",
                        None,
                    )
                    risk_arguments["accepted_evidence_ref"] = (
                        evidence.work_product_ref
                    )
                    risk_arguments["accepted_claims"] = evidence.payload.get(
                        "claims",
                        [],
                    )
                    risk_result = self.tool_execution_coordinator.execute(
                        "risk.classify_signal",
                        risk_arguments,
                        context=ProductToolExecutionContext(
                            caller="runtime",
                            fact_snapshot=runtime_request.fact_snapshot,
                            episode_id=runtime_request.episode_id,
                            authorization_scope=(
                                runtime_request.fact_snapshot.binding.authorization_scope
                            ),
                        ),
                        runtime=runtime,
                        record_call=True,
                    )
                    tool_receipts.append(risk_result.receipt)
                    if risk_result.receipt.outcome != InvocationOutcome.SUCCEEDED:
                        raise AcceptanceError("Safety risk classification failed")
                    risk_receipts.append(risk_result.receipt)
                    if bool(
                        risk_result.receipt.output.get("safety_required")
                    ) or (
                        risk_result.receipt.output.get("risk_level")
                        == OnlineRiskLevel.ESCALATE.value
                    ):
                        deterministic_risk_reasons.extend(
                            risk_result.receipt.output.get(
                                "reason_codes",
                                ("deterministic_risk_escalate",),
                            )
                        )
                    if trend_signals:
                        trend_result = self.tool_execution_coordinator.execute(
                            "risk.classify_signal",
                            {
                                "observation": trend_observation,
                                "trend_signals": trend_signals,
                            },
                            context=ProductToolExecutionContext(
                                caller="runtime",
                                fact_snapshot=runtime_request.fact_snapshot,
                                episode_id=runtime_request.episode_id,
                                authorization_scope=(
                                    runtime_request.fact_snapshot.binding.authorization_scope
                                ),
                            ),
                            runtime=runtime,
                            record_call=True,
                        )
                        tool_receipts.append(trend_result.receipt)
                        if (
                            trend_result.receipt.outcome
                            != InvocationOutcome.SUCCEEDED
                        ):
                            raise AcceptanceError(
                                "Longitudinal risk classification failed"
                            )
                        risk_receipts.append(trend_result.receipt)
                    if (
                        "coordination.read_policy"
                        in EPISODE_DEFINITIONS[
                            runtime_request.episode_type
                        ].required_tools
                    ):
                        policy_routed_care_required = (
                            self._care_required_by_policy(
                                request=runtime_request,
                                runtime=runtime,
                                evidence=evidence,
                                risk_receipts=tuple(risk_receipts),
                                tool_receipts=tool_receipts,
                            )
                        )

            care_planned = WorkProductKind.CARE_STRATEGY in planned
            care_policy_routed = (
                "coordination.read_policy"
                in EPISODE_DEFINITIONS[
                    runtime_request.episode_type
                ].required_tools
            )
            should_run_care = (
                policy_routed_care_required
                if care_policy_routed
                else care_planned
            )
            if should_run_care:
                if evidence is None:
                    raise AcceptanceError("Care cannot run without accepted Evidence")
                envelope, care = self._invoke_and_accept(
                    request=runtime_request,
                    runtime=runtime,
                    agent=self.agent_roster.care_strategy,
                    tool_receipts=tool_receipts,
                    accepted_evidence=evidence,
                    accepted_care=None,
                    safety_target=None,
                )
                envelopes.append(envelope)
                evidence = runtime.accepted_work_products.get(
                    WorkProductKind.EVIDENCE_PACKET,
                    evidence,
                )

            if evidence is None:
                raise AcceptanceError("shared analysis requires accepted Evidence")
            review_target = care or evidence
            trigger_reasons = safety_trigger_reasons(
                target=review_target,
                episode_type=runtime_request.episode_type.value,
                external_action=False,
                doctor_material=False,
            )
            if deterministic_risk_reasons:
                trigger_reasons = sorted(
                    {
                        *trigger_reasons,
                        "deterministic_risk_escalate",
                        *deterministic_risk_reasons,
                    }
                )
            safety_planned = WorkProductKind.SAFETY_DECISION in planned
            shared_safety_required = bool(trigger_reasons or safety_planned)
            doctor_failure_codes: tuple[str, ...] = ()
            safety_attempted = False
            if shared_safety_required:
                safety_attempted = True
                try:
                    safety, safety_envelopes, review_target = self._safety_loop(
                        request=runtime_request,
                        runtime=runtime,
                        target=review_target,
                        evidence=evidence,
                        care=care,
                        tool_receipts=tool_receipts,
                        reasons=(
                            trigger_reasons
                            or ["registry_required_safety"]
                        ),
                        evaluate_result=False,
                        return_non_approve=True,
                    )
                    envelopes.extend(safety_envelopes)
                    if review_target.agent_id is AgentId.EVIDENCE_REASONING:
                        evidence = review_target
                    elif review_target.agent_id is AgentId.CARE_STRATEGY:
                        care = review_target
                except Exception:
                    runtime.accepted_work_products.pop(
                        WorkProductKind.SAFETY_DECISION,
                        None,
                    )
                    safety = None
                    doctor_failure_codes = ("DOCTOR_SAFETY_UNAVAILABLE",)

            if safety is None and not safety_attempted:
                try:
                    safety, safety_envelopes, review_target = self._safety_loop(
                        request=runtime_request,
                        runtime=runtime,
                        target=care or evidence,
                        evidence=evidence,
                        care=care,
                        tool_receipts=tool_receipts,
                        reasons=["doctor_projection_safety"],
                        evaluate_result=False,
                        return_non_approve=True,
                    )
                    envelopes.extend(safety_envelopes)
                    if review_target.agent_id is AgentId.EVIDENCE_REASONING:
                        evidence = review_target
                    elif review_target.agent_id is AgentId.CARE_STRATEGY:
                        care = review_target
                except Exception:
                    runtime.accepted_work_products.pop(
                        WorkProductKind.SAFETY_DECISION,
                        None,
                    )
                    safety = None
                    doctor_failure_codes = ("DOCTOR_SAFETY_UNAVAILABLE",)

            doctor_projection_allowed = False
            if safety is not None:
                safety_decision = SafetyDecision.model_validate(safety.payload)
                doctor_projection_allowed = (
                    safety_decision.verdict is SafetyVerdict.APPROVE
                )
                if not doctor_projection_allowed:
                    doctor_failure_codes = (
                        "DOCTOR_SAFETY_NOT_APPROVED",
                    )

            packet = EvidencePacket.model_validate(evidence.payload)
            summary_lines = tuple(claim.statement for claim in packet.claims)
            if care is not None:
                strategy = CareStrategy.model_validate(care.payload)
                if strategy.primary_action is not None:
                    summary_lines = (*summary_lines, strategy.primary_action.title)
            if not summary_lines:
                summary_lines = ("当前没有足够证据形成健康趋势结论。",)
            return SharedNightAnalysis.create(
                source=request.source,
                registry_hash=stable_hash(product_agent_manifest()),
                runtime_request=runtime_request,
                summary_lines=summary_lines,
                evidence=evidence,
                care=care,
                safety=safety,
                doctor_projection_allowed=doctor_projection_allowed,
                doctor_failure_codes=doctor_failure_codes,
                envelopes=tuple(envelopes),
                agent_invocations=tuple(runtime.invocation_records),
                tool_receipts=tuple(tool_receipts),
            )
        finally:
            self._release_transient_episode_state(runtime_request.episode_id)

    @serialized_episode_execution
    def render_elder_narrative(
        self,
        request: ElderNarrativeRequest,
    ) -> ElderNarrative:
        """Run one bounded SleepCare content plan over accepted shared facts."""

        runtime_request = request.runtime_request
        runtime = _DetachedNarrativeRuntime(request.shared_analysis)
        invocation: AgentInvocationRecord | None = None
        fallback_text = request.elder_projection.text
        assert fallback_text is not None
        try:
            turn = self.agent_invocation_coordinator.invoke_turn(
                request=runtime_request,
                runtime=runtime,
                agent=self.agent_roster.sleepcare,
                tool_receipts=[],
                accepted_evidence=request.shared_analysis.evidence,
                safety_target=None,
            )
            invocation = runtime.invocation_records[0]
            envelope = turn.envelope
            if envelope.tool_requests or envelope.collaboration_requests:
                raise AcceptanceError("elder narrative requests are disallowed")
            accepted = accept_communication(
                envelope,
                snapshot=runtime_request.fact_snapshot,
                accepted_evidence=request.shared_analysis.evidence,
                accepted_care=request.shared_analysis.care,
                reviewed_knowledge_refs=set(),
                reviewed_knowledge_payloads={},
                require_personal_grounding=runtime_request.personalized,
                authenticated_user_text="",
                expected_audience_role="elder",
            )
            draft = CommunicationDraft.model_validate(accepted.payload)
            if draft.memory_change_candidates:
                raise AcceptanceError("elder narrative cannot propose Memory changes")
            return ElderNarrative(
                state=ElderNarrativeState.READY,
                source_shared_analysis_sha256=(
                    request.shared_analysis.shared_analysis_sha256
                ),
                source_projection_sha256=(
                    request.source_projection_identity_sha256
                ),
                render_identity_sha256=request.render_identity_sha256,
                text=draft.text,
                communication=draft,
                invocation=invocation,
            )
        except Exception as exc:
            if runtime.invocation_records:
                invocation = runtime.invocation_records[0]
            return ElderNarrative(
                state=ElderNarrativeState.FALLBACK,
                source_shared_analysis_sha256=(
                    request.shared_analysis.shared_analysis_sha256
                ),
                source_projection_sha256=(
                    request.source_projection_identity_sha256
                ),
                render_identity_sha256=request.render_identity_sha256,
                text=fallback_text,
                invocation=invocation,
                failure_codes=(self._elder_narrative_failure_code(exc),),
            )
        finally:
            self._release_transient_episode_state(runtime_request.episode_id)

    def _release_transient_episode_state(self, episode_id: str) -> None:
        try:
            self.tool_execution_coordinator.release_episode(episode_id)
        except Exception:
            LOGGER.exception(
                "shared Product Tool cache cleanup failed for Episode %s",
                episode_id,
            )
        try:
            self.provider_input_budget.release_episode(episode_id)
        except Exception:
            LOGGER.exception(
                "shared provider-ledger cleanup failed for Episode %s",
                episode_id,
            )

    @staticmethod
    def _elder_narrative_failure_code(exc: Exception) -> str:
        if isinstance(exc, AcceptanceError):
            return "ELDER_NARRATIVE_REQUEST_REJECTED"
        if isinstance(exc, (TypeError, ValueError)):
            return "ELDER_NARRATIVE_INVALID"
        return "ELDER_NARRATIVE_UNAVAILABLE"

    @serialized_episode_execution
    def run(self, request: ProductEpisodeRunRequest) -> ProductEpisodeRunResult:
        urgent = self._urgent_preflight(request)
        if urgent is not None:
            return self.episode_result_finalizer.finalize(
                urgent,
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        if request.episode_type == EpisodeType.DATA_QUALITY_RECOVERY:
            return self.episode_result_finalizer.finalize(
                self._data_quality_recovery(request),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )

        preflight_receipts: list[ToolReceipt] = []
        if request.runtime_readiness_decisions:
            preflight_receipts.insert(
                0,
                build_cold_start_receipt(
                    snapshot=request.fact_snapshot,
                    decisions=request.runtime_readiness_decisions,
                    capability_receipts=request.runtime_capability_receipts,
                    observed_at=datetime.now(timezone.utc),
                ),
            )
        runtime = ProductEpisodeRuntime(
            episode_id=request.episode_id,
            fact_snapshot=request.fact_snapshot,
            sleepcare_agent=self.agent_roster.sleepcare,
            skill_registry=self.skill_registry,
        )
        envelopes: list[AgentEnvelope] = []
        tool_receipts: list[ToolReceipt] = list(preflight_receipts)
        pending_confirmations: list[PendingConfirmationTarget] = []
        try:
            required, checkpoints = self._request_requirements(request)
            plan = runtime.create_plan(
                episode_type=request.episode_type,
                objective=request.objective,
                required_work_products=required,
                required_safety_checkpoints=checkpoints,
            )
        except AgentInteractionRequired as exc:
            receipt = runtime.finish(
                status=exc.status,
                failure_codes=[exc.reason_code],
                tool_receipt_ids=[
                    item.tool_invocation_id for item in tool_receipts
                ],
            )
            return self.episode_result_finalizer.finalize(
                ProductEpisodeRunResult(
                    registry_hash=stable_hash(product_agent_manifest()),
                    continuation_request_hash=product_episode_request_hash(
                        request
                    ),
                    receipt=receipt,
                    envelopes=envelopes,
                    agent_invocations=list(runtime.invocation_records),
                    tool_receipts=tool_receipts,
                    accepted_work_products=list(
                        runtime.accepted_work_products.values()
                    ),
                    pending_user_input=self._pending_user_input(
                        request=request,
                        interaction=exc,
                    ),
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        except Exception as exc:
            return self.episode_result_finalizer.finalize(
                self._degraded(
                    request,
                    runtime,
                    failure_code=f"sleepcare_planning_failed:{type(exc).__name__}",
                    tool_receipts=tool_receipts,
                    envelopes=envelopes,
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )

        for _ in preflight_receipts:
            runtime.record_tool_call()
        registered_receipts = self._run_registered_tools(request, runtime)
        tool_receipts.extend(registered_receipts)
        failed_required = [
            receipt
            for receipt in registered_receipts
            if receipt.outcome != InvocationOutcome.SUCCEEDED
        ]
        if failed_required:
            failed_tool = failed_required[0].tool_name
            return self.episode_result_finalizer.finalize(
                self._degraded(
                    request,
                    runtime,
                    failure_code=f"required_tool_failed:{failed_tool}",
                    tool_receipts=tool_receipts,
                    envelopes=envelopes,
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        evidence: AcceptedWorkProduct | None = None
        care: AcceptedWorkProduct | None = None
        safety: AcceptedWorkProduct | None = None
        communication: AcceptedWorkProduct | None = None
        deterministic_risk_reasons: list[str] = []
        policy_routed_care_required = False

        try:
            if WorkProductKind.EVIDENCE_PACKET in (
                set(plan.required_work_products) | set(plan.conditional_work_products)
            ):
                envelope, accepted = self._invoke_and_accept(
                    request=request,
                    runtime=runtime,
                    agent=self.agent_roster.evidence_reasoning,
                    tool_receipts=tool_receipts,
                    accepted_evidence=None,
                    accepted_care=None,
                    safety_target=None,
                )
                envelopes.append(envelope)
                evidence = accepted
                self._evaluate(runtime, WorkProductKind.EVIDENCE_PACKET, evidence)
                if (
                    "risk.classify_signal"
                    in EPISODE_DEFINITIONS[request.episode_type].required_tools
                ):
                    risk_arguments = dict(
                        request.tool_inputs.get("risk.classify_signal", {})
                    )
                    trend_signals = tuple(
                        risk_arguments.pop("trend_signals", ())
                    )
                    trend_observation = risk_arguments.pop(
                        "trend_observation", None
                    )
                    risk_arguments["accepted_evidence_ref"] = (
                        evidence.work_product_ref
                    )
                    risk_arguments["accepted_claims"] = evidence.payload.get(
                        "claims", []
                    )
                    risk_result = self.tool_execution_coordinator.execute(
                        "risk.classify_signal",
                        risk_arguments,
                        context=ProductToolExecutionContext(
                            caller="runtime",
                            fact_snapshot=request.fact_snapshot,
                            episode_id=request.episode_id,
                            authorization_scope=(
                                request.fact_snapshot.binding.authorization_scope
                            ),
                        ),
                        runtime=runtime,
                        record_call=True,
                    )
                    tool_receipts.append(risk_result.receipt)
                    if (
                        risk_result.receipt.outcome
                        != InvocationOutcome.SUCCEEDED
                    ):
                        raise AcceptanceError(
                            "Safety risk classification failed"
                        )
                    if bool(
                        risk_result.receipt.output.get("safety_required")
                    ) or (
                        risk_result.receipt.output.get("risk_level")
                        == OnlineRiskLevel.ESCALATE.value
                    ):
                        deterministic_risk_reasons.extend(
                            risk_result.receipt.output.get(
                                "reason_codes",
                                ("deterministic_risk_escalate",),
                            )
                        )
                    risk_receipts = [risk_result.receipt]
                    if trend_signals:
                        trend_result = self.tool_execution_coordinator.execute(
                            "risk.classify_signal",
                            {
                                "observation": trend_observation,
                                "trend_signals": trend_signals,
                            },
                            context=ProductToolExecutionContext(
                                caller="runtime",
                                fact_snapshot=request.fact_snapshot,
                                episode_id=request.episode_id,
                                authorization_scope=(
                                    request.fact_snapshot.binding.authorization_scope
                                ),
                            ),
                            runtime=runtime,
                            record_call=True,
                        )
                        tool_receipts.append(trend_result.receipt)
                        if (
                            trend_result.receipt.outcome
                            != InvocationOutcome.SUCCEEDED
                        ):
                            raise AcceptanceError(
                                "Longitudinal risk classification failed"
                            )
                        risk_receipts.append(trend_result.receipt)
                    if (
                        "coordination.read_policy"
                        in EPISODE_DEFINITIONS[
                            request.episode_type
                        ].required_tools
                    ):
                        policy_routed_care_required = (
                            self._care_required_by_policy(
                                request=request,
                                runtime=runtime,
                                evidence=evidence,
                                risk_receipts=tuple(risk_receipts),
                                tool_receipts=tool_receipts,
                            )
                        )

            care_planned = WorkProductKind.CARE_STRATEGY in (
                set(plan.required_work_products) | set(plan.conditional_work_products)
            )
            care_policy_routed = (
                "coordination.read_policy"
                in EPISODE_DEFINITIONS[request.episode_type].required_tools
            )
            should_run_care = (
                policy_routed_care_required
                if care_policy_routed
                else care_planned
            )
            if should_run_care:
                if evidence is None:
                    raise AcceptanceError("Care cannot run without accepted Evidence")
                envelope, accepted = self._invoke_and_accept(
                    request=request,
                    runtime=runtime,
                    agent=self.agent_roster.care_strategy,
                    tool_receipts=tool_receipts,
                    accepted_evidence=evidence,
                    accepted_care=None,
                    safety_target=None,
                )
                envelopes.append(envelope)
                care = accepted
                evidence = runtime.accepted_work_products.get(
                    WorkProductKind.EVIDENCE_PACKET,
                    evidence,
                )
                self._evaluate(runtime, WorkProductKind.CARE_STRATEGY, care)

            review_target = care or evidence
            trigger_reasons = (
                safety_trigger_reasons(
                    target=review_target,
                    episode_type=request.episode_type.value,
                    external_action=False,
                    doctor_material=False,
                )
                if review_target
                else []
            )
            if deterministic_risk_reasons:
                trigger_reasons.extend(
                    [
                        "deterministic_risk_escalate",
                        *deterministic_risk_reasons,
                    ]
                )
                trigger_reasons = sorted(set(trigger_reasons))
            safety_planned = WorkProductKind.SAFETY_DECISION in (
                set(plan.required_work_products) | set(plan.conditional_work_products)
            )
            prepublication_safety_planned = (
                safety_planned
                and not _uses_doctor_material_semantics(request)
            )
            if trigger_reasons or prepublication_safety_planned:
                if review_target is None:
                    raise AcceptanceError("Safety checkpoint has no review target")
                safety, safety_envelopes, review_target = self._safety_loop(
                    request=request,
                    runtime=runtime,
                    target=review_target,
                    evidence=evidence,
                    care=care,
                    tool_receipts=tool_receipts,
                    reasons=trigger_reasons or ["registry_required_safety"],
                )
                envelopes.extend(safety_envelopes)
                if review_target.agent_id == AgentId.EVIDENCE_REASONING:
                    evidence = review_target
                elif review_target.agent_id == AgentId.CARE_STRATEGY:
                    care = review_target

            if request.episode_type is EpisodeType.ROLE_MATERIAL:
                if evidence is None:
                    raise AcceptanceError(
                        "Role material requires accepted Evidence"
                    )
                artifact_result = self.tool_execution_coordinator.execute(
                    "artifact.render",
                    {
                        "episode_id": request.episode_id,
                        "accepted_evidence_ref": evidence.work_product_ref,
                        "accepted_evidence_hash": evidence.target_hash,
                        "evidence_packet": evidence.payload,
                        "audience_role": _effective_audience_role(request),
                    },
                    context=ProductToolExecutionContext(
                        caller="runtime",
                        fact_snapshot=request.fact_snapshot,
                        authorization_scope=(
                            request.fact_snapshot.binding.authorization_scope
                        ),
                        episode_id=request.episode_id,
                        plan_id=runtime.plan.plan_id if runtime.plan else None,
                        plan_revision=runtime.episode_state_revision,
                        plan_step_id="prepare-role-material-basis",
                    ),
                    runtime=runtime,
                    record_call=True,
                )
                tool_receipts.append(artifact_result.receipt)
                if (
                    artifact_result.receipt.outcome
                    != InvocationOutcome.SUCCEEDED
                ):
                    raise AcceptanceError(
                        "Role material Artifact rendering failed"
                    )

            envelope, accepted = self._invoke_and_accept(
                request=request,
                runtime=runtime,
                agent=self.agent_roster.sleepcare,
                tool_receipts=tool_receipts,
                accepted_evidence=evidence,
                accepted_care=care,
                safety_target=None,
            )
            envelopes.append(envelope)
            communication = accepted
            self._evaluate(runtime, WorkProductKind.COMMUNICATION, communication)

            communication_trigger_reasons = safety_trigger_reasons(
                target=communication,
                episode_type=request.episode_type.value,
            )
            publication_safety_required = (
                _uses_doctor_material_semantics(request)
                or bool(communication_trigger_reasons)
            )
            if publication_safety_required:
                safety, safety_envelopes, communication = self._safety_loop(
                    request=request,
                    runtime=runtime,
                    target=communication,
                    evidence=evidence,
                    care=care,
                    tool_receipts=tool_receipts,
                    reasons=[
                        *communication_trigger_reasons,
                        (
                            "doctor_material"
                            if _uses_doctor_material_semantics(request)
                            else "communication_restricted_content"
                        )
                    ],
                )
                envelopes.extend(safety_envelopes)

            draft = CommunicationDraft.model_validate(communication.payload)
            publication_postflight(
                draft,
                accepted_evidence=evidence,
                accepted_care=care,
                safety=safety,
                safety_required=bool(
                    trigger_reasons
                    or deterministic_risk_reasons
                    or prepublication_safety_planned
                    or publication_safety_required
                ),
                safety_target=(
                    communication
                    if publication_safety_required
                    else review_target
                ),
                reviewed_knowledge_refs={
                    ref
                    for receipt in tool_receipts
                    if receipt.tool_name == "knowledge.retrieve_reviewed"
                    and receipt.outcome == InvocationOutcome.SUCCEEDED
                    for ref in [receipt.tool_invocation_id, *receipt.source_refs]
                },
                reviewed_knowledge_payloads={
                    ref: receipt.output
                    for receipt in tool_receipts
                    if receipt.tool_name == "knowledge.retrieve_reviewed"
                    and receipt.outcome == InvocationOutcome.SUCCEEDED
                    for ref in [receipt.tool_invocation_id, *receipt.source_refs]
                },
                readiness_decisions=request.runtime_readiness_decisions,
            )
            delivered = self.publication_service.publish(
                draft,
                episode_id=request.episode_id,
                request=request,
                tool_receipts=tool_receipts,
            )
            waiting_confirmation = bool(pending_confirmations)
            if care:
                strategy = CareStrategy.model_validate(care.payload)
                action = strategy.primary_action
                if strategy.transition_confirmation_required:
                    confirmation_candidate_id = (
                        action.candidate_id
                        if strategy.disposition == "propose"
                        and action is not None
                        else strategy.strategy_id
                    )
                    waiting_confirmation = True
                    pending_confirmations.append(
                        PendingConfirmationTarget(
                            confirmation_id=(
                                "confirmation:care:"
                                f"{confirmation_candidate_id}"
                            ),
                            target_kind="care",
                            candidate_id=confirmation_candidate_id,
                            candidate_hash=(
                                action.candidate_hash
                                if strategy.disposition == "propose"
                                and action is not None
                                else care.target_hash
                            ),
                            actor_id=(
                                request.fact_snapshot.binding.actor_id
                            ),
                            subject_id=(
                                request.fact_snapshot.binding.subject_id
                            ),
                            action_scope=(
                                "activate_care"
                                if strategy.disposition == "propose"
                                else "transition_care"
                            ),
                            reason=(
                                "建立或实质修改主要照护行动前需要确认。"
                            ),
                            expires_at=datetime.now(timezone.utc)
                            + timedelta(minutes=20),
                        )
                    )
            if not delivered:
                # A known delivery failure is terminal for this publication
                # attempt.  None of the confirmation targets below have been
                # registered with the API yet, so do not expose actions for an
                # undelivered draft or let their presence invalidate PARTIAL.
                pending_confirmations.clear()
                receipt = runtime.finish(
                    status=EpisodeStatus.PARTIAL,
                    execution_mode=ExecutionMode.SAFE_DEGRADED,
                    failure_codes=[
                        "publication_delivery_failed",
                    ],
                    tool_receipt_ids=[
                        item.tool_invocation_id for item in tool_receipts
                    ],
                )
            elif waiting_confirmation:
                receipt = runtime.finish(
                    status=EpisodeStatus.WAITING_CONFIRMATION,
                    tool_receipt_ids=[
                        item.tool_invocation_id for item in tool_receipts
                    ],
                )
            else:
                receipt = runtime.finish(
                    status=EpisodeStatus.COMPLETE,
                    tool_receipt_ids=[
                        item.tool_invocation_id for item in tool_receipts
                    ],
                )
            result = ProductEpisodeRunResult(
                registry_hash=stable_hash(product_agent_manifest()),
                continuation_request_hash=product_episode_request_hash(request),
                receipt=receipt,
                publication=draft,
                publication_delivered=delivered,
                envelopes=envelopes,
                agent_invocations=list(runtime.invocation_records),
                tool_receipts=tool_receipts,
                accepted_work_products=list(runtime.accepted_work_products.values()),
                pending_confirmations=pending_confirmations,
            )
            return self.episode_result_finalizer.finalize(
                result,
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        except AgentInteractionRequired as exc:
            receipt = runtime.finish(
                status=exc.status,
                failure_codes=[exc.reason_code],
                tool_receipt_ids=[
                    item.tool_invocation_id for item in tool_receipts
                ],
            )
            return self.episode_result_finalizer.finalize(
                ProductEpisodeRunResult(
                    registry_hash=stable_hash(product_agent_manifest()),
                    continuation_request_hash=product_episode_request_hash(
                        request
                    ),
                    receipt=receipt,
                    envelopes=envelopes,
                    agent_invocations=list(runtime.invocation_records),
                    tool_receipts=tool_receipts,
                    accepted_work_products=list(
                        runtime.accepted_work_products.values()
                    ),
                    pending_user_input=self._pending_user_input(
                        request=request,
                        interaction=exc,
                    ),
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        except PublicationIndeterminateError as exc:
            receipt = runtime.finish(
                status=EpisodeStatus.BLOCKED,
                execution_mode=ExecutionMode.SAFE_DEGRADED,
                failure_codes=[f"publication_indeterminate:{exc.phase}"],
                tool_receipt_ids=[
                    item.tool_invocation_id for item in tool_receipts
                ],
            )
            return self.episode_result_finalizer.finalize(
                ProductEpisodeRunResult(
                    registry_hash=stable_hash(product_agent_manifest()),
                    receipt=receipt,
                    envelopes=envelopes,
                    agent_invocations=list(runtime.invocation_records),
                    tool_receipts=tool_receipts,
                    accepted_work_products=list(
                        runtime.accepted_work_products.values()
                    ),
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )
        except Exception as exc:
            LOGGER.exception("Product Agent path failed closed")
            if safety is None and (
                _uses_doctor_material_semantics(request)
                or isinstance(exc, AcceptanceError)
                and "Safety" in str(exc)
            ):
                receipt = runtime.finish(
                    status=EpisodeStatus.BLOCKED,
                    failure_codes=[f"fail_closed:{type(exc).__name__}"],
                    tool_receipt_ids=[
                        item.tool_invocation_id for item in tool_receipts
                    ],
                )
                return self.episode_result_finalizer.finalize(
                    ProductEpisodeRunResult(
                        registry_hash=stable_hash(product_agent_manifest()),
                        receipt=receipt,
                        envelopes=envelopes,
                        agent_invocations=list(runtime.invocation_records),
                        tool_receipts=tool_receipts,
                        accepted_work_products=list(
                            runtime.accepted_work_products.values()
                        ),
                    ),
                    subject_id=request.fact_snapshot.binding.subject_id,
                    request=request,
                )
            return self.episode_result_finalizer.finalize(
                self._degraded(
                    request,
                    runtime,
                    failure_code=f"agent_path_failed:{type(exc).__name__}",
                    tool_receipts=tool_receipts,
                    envelopes=envelopes,
                ),
                subject_id=request.fact_snapshot.binding.subject_id,
                request=request,
            )

    def _invoke_and_accept(
        self,
        *,
        request: ProductEpisodeRunRequest,
        runtime: ProductEpisodeRuntime,
        agent: RuntimeAgentPort,
        tool_receipts: list[ToolReceipt],
        accepted_evidence: AcceptedWorkProduct | None,
        accepted_care: AcceptedWorkProduct | None,
        safety_target: AcceptedWorkProduct | None,
        revision_reason: str | None = None,
        collaboration_request: CrossAgentRequest | None = None,
        request_round: int = 0,
        seen_request_hashes: frozenset[str] = frozenset(),
        tool_session_id: str | None = None,
        acceptance_repair_count: int = 0,
    ) -> tuple[AgentEnvelope, AcceptedWorkProduct]:
        agent_id = agent.agent_id
        kind = agent.boundary.work_product_kind
        turn = self.agent_invocation_coordinator.invoke_turn(
            request=request,
            runtime=runtime,
            agent=agent,
            tool_receipts=tool_receipts,
            accepted_evidence=accepted_evidence,
            safety_target=safety_target,
            revision_reason=revision_reason,
            collaboration_request=collaboration_request,
            tool_session_id=tool_session_id,
        )
        envelope = turn.envelope
        pending_requests = [
            *envelope.tool_requests,
            *envelope.collaboration_requests,
        ]
        if pending_requests:
            if request_round >= 2:
                raise AcceptanceError("Agent request/feedback round limit exhausted")
            fingerprints = {
                self.agent_invocation_coordinator.request_fingerprint(item)
                for item in pending_requests
            }
            if fingerprints.intersection(seen_request_hashes):
                raise AcceptanceError("duplicate Agent request without new information")
            for tool_request in envelope.tool_requests:
                result = self.agent_invocation_coordinator.execute_tool_feedback(
                    request=request,
                    runtime=runtime,
                    agent=agent,
                    turn=turn,
                    tool_request=tool_request,
                )
                tool_receipts.append(result.receipt)
            for collaboration in envelope.collaboration_requests:
                self.agent_invocation_coordinator.validate_collaboration_binding(
                    collaboration,
                    request=request,
                    runtime=runtime,
                )
                if collaboration.request_type == CrossAgentRequestType.USER_FACT:
                    raise AgentInteractionRequired(
                        EpisodeStatus.WAITING_USER,
                        "agent_requested_user_fact",
                        collaboration,
                    )
                if collaboration.request_type == CrossAgentRequestType.CONFIRMATION:
                    raise AcceptanceError(
                        "Agent confirmation request must be materialized as a "
                        "typed Memory, Care, Habit or external-action target"
                    )
                if collaboration.receiver == AgentId.EVIDENCE_REASONING:
                    _, accepted_evidence = self._invoke_and_accept(
                        request=request,
                        runtime=runtime,
                        agent=self.agent_roster.evidence_reasoning,
                        tool_receipts=tool_receipts,
                        accepted_evidence=None,
                        accepted_care=None,
                        safety_target=None,
                        revision_reason=collaboration.objective,
                        collaboration_request=collaboration,
                        request_round=0,
                        seen_request_hashes=frozenset(),
                    )
                elif collaboration.receiver == AgentId.CARE_STRATEGY:
                    if accepted_evidence is None:
                        accepted_evidence = runtime.accepted_work_products.get(
                            WorkProductKind.EVIDENCE_PACKET
                        )
                    if accepted_evidence is None:
                        raise AcceptanceError(
                            "Care collaboration requires accepted Evidence"
                        )
                    _, accepted_care = self._invoke_and_accept(
                        request=request,
                        runtime=runtime,
                        agent=self.agent_roster.care_strategy,
                        tool_receipts=tool_receipts,
                        accepted_evidence=accepted_evidence,
                        accepted_care=None,
                        safety_target=None,
                        revision_reason=collaboration.objective,
                        collaboration_request=collaboration,
                        request_round=0,
                        seen_request_hashes=frozenset(),
                    )
                elif collaboration.receiver == AgentId.SAFETY_REVIEW:
                    raise AcceptanceError(
                        "Safety collaboration must use an exact accepted target checkpoint"
                    )
                else:
                    raise AcceptanceError("unsupported collaboration receiver")
            return self._invoke_and_accept(
                request=request,
                runtime=runtime,
                agent=agent,
                tool_receipts=tool_receipts,
                accepted_evidence=accepted_evidence,
                accepted_care=accepted_care,
                safety_target=safety_target,
                revision_reason=revision_reason,
                collaboration_request=collaboration_request,
                request_round=request_round + 1,
                seen_request_hashes=frozenset(
                    {*seen_request_hashes, *fingerprints}
                ),
                tool_session_id=turn.tool_session_id,
            )
        if kind is WorkProductKind.EVIDENCE_PACKET:
            try:
                accepted = accept_evidence(
                    envelope,
                    snapshot=request.fact_snapshot,
                    tool_receipts=tool_receipts,
                    authorized_user_input_refs={
                        *(
                            {
                                (
                                    "user_report:"
                                    if request.fact_snapshot.binding.role
                                    == "elder"
                                    else "authorized_observer_report:"
                                )
                                + stable_hash(request.user_text)[:16]
                            }
                            if request.user_text
                            else set()
                        ),
                        *(
                            response.source_ref
                            for response in request.user_fact_responses
                        ),
                    },
                )
            except AcceptanceError as exc:
                if acceptance_repair_count >= 1:
                    raise
                return self._invoke_and_accept(
                    request=request,
                    runtime=runtime,
                    agent=agent,
                    tool_receipts=tool_receipts,
                    accepted_evidence=accepted_evidence,
                    accepted_care=accepted_care,
                    safety_target=safety_target,
                    revision_reason=(
                        "bounded_acceptance_repair: "
                        f"{exc}. Revise only the rejected Evidence packet. Copy "
                        "Context source_scope exactly and keep every claim's "
                        "date_start and date_end inside that scope; use the scope "
                        "boundaries exactly when uncertain. Do not cite or describe "
                        "dates outside the source scope. Use only exact authorized "
                        "source references visible in Context or ToolReceipts: for "
                        "a confirmed Habit fact use its fact_ref, and for governed "
                        "Memory use its retrieval_handle. Never use entity IDs as "
                        "evidence references."
                    ),
                    collaboration_request=collaboration_request,
                    request_round=request_round,
                    seen_request_hashes=seen_request_hashes,
                    tool_session_id=turn.tool_session_id,
                    acceptance_repair_count=acceptance_repair_count + 1,
                )
        elif kind is WorkProductKind.CARE_STRATEGY:
            if accepted_evidence is None:
                raise AcceptanceError("Care requires accepted Evidence")
            care_state = self.care_store.get(
                request.fact_snapshot.binding.subject_id
            )
            if care_state.version != request.fact_snapshot.care_context_version:
                raise AcceptanceError("Care Context changed after FactSnapshot")
            active_action_id = (
                str(care_state.active_primary_action.get("candidate_id"))
                if care_state.active_primary_action
                else None
            )
            accepted = accept_care(
                envelope,
                snapshot=request.fact_snapshot,
                accepted_evidence=accepted_evidence,
                catalog=self.care_catalog,
                active_primary_action_id=active_action_id,
            )
        elif kind is WorkProductKind.SAFETY_DECISION:
            if safety_target is None:
                raise AcceptanceError("Safety requires exact target")
            try:
                accepted = accept_safety(
                    envelope,
                    snapshot=request.fact_snapshot,
                    target=safety_target,
                )
            except AcceptanceError as exc:
                if acceptance_repair_count >= 1:
                    raise
                return self._invoke_and_accept(
                    request=request,
                    runtime=runtime,
                    agent=agent,
                    tool_receipts=tool_receipts,
                    accepted_evidence=accepted_evidence,
                    accepted_care=accepted_care,
                    safety_target=safety_target,
                    revision_reason=(
                        "bounded_acceptance_repair: "
                        f"{exc}. Revise only the rejected Safety decision. Copy "
                        "review_target_type, review_target_id, review_target_hash, "
                        "fact_snapshot_hash, reviewed_episode_state_revision, and "
                        "policy_version exactly from Context target metadata; keep "
                        "the model's independently reasoned verdict."
                    ),
                    collaboration_request=collaboration_request,
                    request_round=request_round,
                    seen_request_hashes=seen_request_hashes,
                    tool_session_id=turn.tool_session_id,
                    acceptance_repair_count=acceptance_repair_count + 1,
                )
        else:
            if kind is not WorkProductKind.COMMUNICATION:
                raise AcceptanceError("unsupported concrete Agent work product")
            try:
                accepted = accept_communication(
                    envelope,
                    snapshot=request.fact_snapshot,
                    accepted_evidence=accepted_evidence,
                    accepted_care=accepted_care,
                    reviewed_knowledge_refs={
                        ref
                        for receipt in tool_receipts
                        if receipt.tool_name == "knowledge.retrieve_reviewed"
                        and receipt.outcome == InvocationOutcome.SUCCEEDED
                        for ref in [receipt.tool_invocation_id, *receipt.source_refs]
                    },
                    reviewed_knowledge_payloads={
                        ref: receipt.output
                        for receipt in tool_receipts
                        if receipt.tool_name == "knowledge.retrieve_reviewed"
                        and receipt.outcome == InvocationOutcome.SUCCEEDED
                        for ref in [receipt.tool_invocation_id, *receipt.source_refs]
                    },
                    require_personal_grounding=request.personalized,
                    authenticated_user_text=request.user_text,
                    expected_audience_role=_effective_audience_role(request),
                )
            except AcceptanceError as exc:
                if acceptance_repair_count >= 1:
                    raise
                return self._invoke_and_accept(
                    request=request,
                    runtime=runtime,
                    agent=agent,
                    tool_receipts=tool_receipts,
                    accepted_evidence=accepted_evidence,
                    accepted_care=accepted_care,
                    safety_target=safety_target,
                    revision_reason=(
                        "bounded_acceptance_repair: "
                        f"{exc}. Revise only the rejected Communication. Choose "
                        "exactly one accepted Evidence claim, copy that claim's "
                        "statement verbatim as the entire Communication text and "
                        "as the sole semantic binding rendered_text, cite only "
                        "that claim_id, and emit no Care refs or extra prose. Do "
                        "not paraphrase, add, remove, or change numeric tokens."
                    ),
                    collaboration_request=collaboration_request,
                    request_round=request_round,
                    seen_request_hashes=seen_request_hashes,
                    tool_session_id=turn.tool_session_id,
                    acceptance_repair_count=acceptance_repair_count + 1,
                )
        runtime.accept(kind, accepted)
        return envelope, accepted

    def _safety_loop(
        self,
        *,
        request: ProductEpisodeRunRequest,
        runtime: ProductEpisodeRuntime,
        target: AcceptedWorkProduct,
        evidence: AcceptedWorkProduct | None,
        care: AcceptedWorkProduct | None,
        tool_receipts: list[ToolReceipt],
        reasons: list[str],
        allow_revision: bool = True,
        evaluate_result: bool = True,
        return_non_approve: bool = False,
    ) -> tuple[AcceptedWorkProduct, list[AgentEnvelope], AcceptedWorkProduct]:
        envelopes: list[AgentEnvelope] = []
        current = target
        for _ in range(3):
            envelope, safety = self._invoke_and_accept(
                request=request,
                runtime=runtime,
                agent=self.agent_roster.safety_review,
                tool_receipts=tool_receipts,
                accepted_evidence=evidence,
                accepted_care=care,
                safety_target=current,
                revision_reason=",".join(reasons),
            )
            envelopes.append(envelope)
            decision = SafetyDecision.model_validate(safety.payload)
            if evaluate_result:
                self._evaluate(runtime, WorkProductKind.SAFETY_DECISION, safety)
            if decision.verdict == SafetyVerdict.APPROVE:
                return safety, envelopes, current
            if return_non_approve:
                return safety, envelopes, current
            if decision.verdict == SafetyVerdict.BLOCK:
                raise AcceptanceError("Safety blocked target")
            if not allow_revision:
                raise AcceptanceError("Safety requires external target revision")
            runtime.record_safety_revision(current.target_hash)
            responsible = decision.responsible_agent
            if responsible not in {
                AgentId.EVIDENCE_REASONING,
                AgentId.CARE_STRATEGY,
                AgentId.SLEEP_CARE,
            }:
                raise AcceptanceError("Safety revision has invalid owner")
            revision_agent = self.agent_roster.by_id(responsible)
            revised_envelope, current = self._invoke_and_accept(
                request=request,
                runtime=runtime,
                agent=revision_agent,
                tool_receipts=tool_receipts,
                accepted_evidence=evidence,
                accepted_care=care,
                safety_target=None,
                revision_reason=",".join(decision.reason_codes),
            )
            envelopes.append(revised_envelope)
            if responsible == AgentId.EVIDENCE_REASONING:
                evidence = current
            elif responsible == AgentId.CARE_STRATEGY:
                care = current
        raise AcceptanceError("Safety revision limit exhausted")

    @staticmethod
    def _evaluate(
        runtime: ProductEpisodeRuntime,
        kind: WorkProductKind,
        product: AcceptedWorkProduct,
    ) -> None:
        decision = runtime.evaluate(latest_kind=kind, latest=product)
        if decision.decision == EvaluationDecision.REPLAN:
            runtime.replan(reason=decision.replan_reason or "evaluation")
        elif decision.decision == EvaluationDecision.BLOCK:
            raise AcceptanceError("SleepCare evaluation blocked path")

    def _care_required_by_policy(
        self,
        *,
        request: ProductEpisodeRunRequest,
        runtime: ProductEpisodeRuntime,
        evidence: AcceptedWorkProduct,
        risk_receipts: tuple[ToolReceipt, ...],
        tool_receipts: list[ToolReceipt],
    ) -> bool:
        result = self.tool_execution_coordinator.execute(
            "coordination.read_policy",
            {
                "accepted_evidence_ref": evidence.work_product_ref,
                "accepted_evidence_hash": evidence.target_hash,
                "risk_decisions": [
                    {
                        "risk_receipt_ref": risk_receipt.tool_invocation_id,
                        "risk_level": risk_receipt.output["risk_level"],
                        "quality_status": risk_receipt.output[
                            "quality_status"
                        ],
                        "urgent_required": bool(
                            risk_receipt.output.get("urgent_required")
                        ),
                        "source_refs": risk_receipt.source_refs,
                    }
                    for risk_receipt in risk_receipts
                ],
            },
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=request.fact_snapshot,
                episode_id=request.episode_id,
                authorization_scope=(
                    request.fact_snapshot.binding.authorization_scope
                ),
            ),
            runtime=runtime,
            record_call=True,
        )
        tool_receipts.append(result.receipt)
        if result.receipt.outcome != InvocationOutcome.SUCCEEDED:
            raise AcceptanceError("Care coordination policy failed")
        payload = dict(result.receipt.output)
        payload.pop("policy", None)
        decision = CareCoordinationPolicyResult.model_validate(payload).routing
        return bool(decision.candidate_intents) and not decision.urgent_preempt

    def _urgent_preflight(
        self, request: ProductEpisodeRunRequest
    ) -> ProductEpisodeRunResult | None:
        result = self.tool_execution_coordinator.execute(
            "risk.match_urgent_boundary",
            {
                "text_inputs": [
                    request.user_text,
                    *(
                        response.answer
                        for response in request.user_fact_responses
                    ),
                ]
            },
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=request.fact_snapshot,
                episode_id=request.episode_id,
                authorization_scope=request.fact_snapshot.binding.authorization_scope,
            ),
        )
        if result.receipt.outcome != InvocationOutcome.SUCCEEDED:
            return self._failed_required_preflight(request, result.receipt)
        urgent = bool(result.receipt.output.get("urgent"))
        if not urgent:
            return None
        draft = CommunicationDraft(
            draft_id=f"urgent:{request.episode_id}",
            audience_role=request.fact_snapshot.binding.role
            if request.fact_snapshot.binding.role != "system"
            else "elder",
            text="这可能需要立即线下医疗帮助。请联系当地急救服务或请身边的人协助。",
            context_notice="这是确定性急症边界提示，未等待模型判断。",
        )
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=EpisodeType.URGENT_BOUNDARY,
            receipt_revision=1,
            terminal=True,
            execution_mode=ExecutionMode.DETERMINISTIC_ONLY,
            status=EpisodeStatus.COMPLETE,
            goal_achieved=True,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=0,
            tool_receipt_ids=[result.receipt.tool_invocation_id],
            trace_ref=f"trace:{request.episode_id}:urgent",
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash(product_agent_manifest()),
            receipt=receipt,
            publication=draft,
            publication_delivered=True,
            tool_receipts=[result.receipt],
        )

    @staticmethod
    def _failed_required_preflight(
        request: ProductEpisodeRunRequest,
        receipt: ToolReceipt,
        *,
        trace_suffix: str = "safety-preflight-failed",
    ) -> ProductEpisodeRunResult:
        episode_receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=1,
            terminal=True,
            execution_mode=ExecutionMode.SAFE_DEGRADED,
            status=EpisodeStatus.BLOCKED,
            goal_achieved=False,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=0,
            tool_receipt_ids=[receipt.tool_invocation_id],
            failure_codes=[f"required_tool_failed:{receipt.tool_name}"],
            trace_ref=f"trace:{request.episode_id}:{trace_suffix}",
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash(product_agent_manifest()),
            receipt=episode_receipt,
            tool_receipts=[receipt],
        )

    def _data_quality_recovery(
        self, request: ProductEpisodeRunRequest
    ) -> ProductEpisodeRunResult:
        receipts = []
        for tool_name in ("radar.assess_data_quality", "radar.get_device_status"):
            result = self.tool_execution_coordinator.execute(
                tool_name,
                request.tool_inputs.get(tool_name, {}),
                context=ProductToolExecutionContext(
                    caller="runtime",
                    fact_snapshot=request.fact_snapshot,
                    episode_id=request.episode_id,
                ),
            )
            receipts.append(result.receipt)
        draft = CommunicationDraft(
            draft_id=f"quality:{request.episode_id}",
            audience_role=(
                _effective_audience_role(request)
            ),
            text="当前记录质量不足，暂时无法形成个性化判断。请检查设备佩戴与连接。",
            context_notice="这是已审定的数据不足降级提示。",
        )
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=EpisodeType.DATA_QUALITY_RECOVERY,
            receipt_revision=1,
            terminal=True,
            execution_mode=ExecutionMode.DETERMINISTIC_ONLY,
            status=EpisodeStatus.PARTIAL,
            goal_achieved=False,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=0,
            tool_receipt_ids=[item.tool_invocation_id for item in receipts],
            failure_codes=["insufficient_data_quality"],
            trace_ref=f"trace:{request.episode_id}:quality",
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash(product_agent_manifest()),
            receipt=receipt,
            publication=draft,
            publication_delivered=True,
            tool_receipts=receipts,
        )

    def _run_registered_tools(
        self,
        request: ProductEpisodeRunRequest,
        runtime: ProductEpisodeRuntime,
    ) -> list[ToolReceipt]:
        definition = EPISODE_DEFINITIONS[request.episode_type]
        receipts: list[ToolReceipt] = []
        for tool_name in sorted(definition.required_tools):
            if tool_name in {
                "risk.match_urgent_boundary",
                "risk.classify_signal",
                "coordination.read_policy",
                "artifact.render",
            }:
                continue
            result = self.tool_execution_coordinator.execute(
                tool_name,
                request.tool_inputs.get(tool_name, {}),
                context=ProductToolExecutionContext(
                    caller="runtime",
                    fact_snapshot=request.fact_snapshot,
                    episode_id=request.episode_id,
                ),
                runtime=runtime,
                record_call=True,
            )
            receipts.append(result.receipt)
        return receipts

    @staticmethod
    def _request_requirements(
        request: ProductEpisodeRunRequest,
    ) -> tuple[set[WorkProductKind], set[str]]:
        required: set[WorkProductKind] = set()
        checkpoints: set[str] = set()
        if request.personalized and request.episode_type == EpisodeType.GROUNDED_DIALOGUE:
            required.add(WorkProductKind.EVIDENCE_PACKET)
        if _uses_doctor_material_semantics(request):
            required.add(WorkProductKind.SAFETY_DECISION)
            checkpoint = _doctor_safety_checkpoint(request.episode_type)
            if checkpoint is None:
                raise AcceptanceError(
                    "doctor material has no registered Safety checkpoint"
                )
            checkpoints.add(checkpoint)
        return required, checkpoints

    def _degraded(
        self,
        request: ProductEpisodeRunRequest,
        runtime: ProductEpisodeRuntime,
        *,
        failure_code: str,
        tool_receipts: list[ToolReceipt],
        envelopes: list[AgentEnvelope],
    ) -> ProductEpisodeRunResult:
        publication_failure_code: str | None = None
        if _uses_doctor_material_semantics(request):
            status = EpisodeStatus.BLOCKED
            publication = None
            delivered = False
        else:
            status = EpisodeStatus.PARTIAL
            cold_start_boundaries = tuple(
                dict.fromkeys(
                    degraded_boundary_sentence(item)
                    for item in request.runtime_readiness_decisions
                    if item.response_mode
                    in {ResponseMode.DEGRADED, ResponseMode.BLOCKED}
                )
            )
            publication = CommunicationDraft(
                draft_id=f"degraded:{request.episode_id}",
                audience_role=_effective_audience_role(request),
                text=(
                    "\n".join(cold_start_boundaries)
                    if cold_start_boundaries
                    else "目前无法可靠完成个性化判断；已保留现有状态，请稍后重试。"
                ),
                context_notice="这是明确标记的系统降级结果。",
            )
            try:
                delivered = self.publication_service.publish(
                    publication,
                    episode_id=request.episode_id,
                    request=request,
                    tool_receipts=tool_receipts,
                )
            except PublicationIndeterminateError as exc:
                status = EpisodeStatus.BLOCKED
                publication = None
                delivered = False
                publication_failure_code = (
                    f"publication_indeterminate:{exc.phase}"
                )
            except PublicationError:
                status = EpisodeStatus.BLOCKED
                publication = None
                delivered = False
        receipt = runtime.finish(
            status=status,
            execution_mode=ExecutionMode.SAFE_DEGRADED,
            failure_codes=[
                failure_code,
                *(
                    [publication_failure_code]
                    if publication_failure_code is not None
                    else []
                ),
            ],
            tool_receipt_ids=[
                item.tool_invocation_id for item in tool_receipts
            ],
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash(product_agent_manifest()),
            receipt=receipt,
            publication=publication,
            publication_delivered=delivered,
            envelopes=envelopes,
            agent_invocations=list(runtime.invocation_records),
            tool_receipts=tool_receipts,
            accepted_work_products=list(runtime.accepted_work_products.values()),
        )

    @staticmethod
    def _pending_user_input(
        *,
        request: ProductEpisodeRunRequest,
        interaction: AgentInteractionRequired,
    ) -> PendingUserInputTarget | None:
        collaboration = interaction.request
        if (
            interaction.status != EpisodeStatus.WAITING_USER
            or collaboration is None
        ):
            return None
        role = request.fact_snapshot.binding.role
        if role == "system":
            return None
        return PendingUserInputTarget(
            request_id=collaboration.request_id,
            question_text=collaboration.objective,
            why_needed=(
                "该可观察事实是完成当前受证据约束判断所必需的信息。"
            ),
            decision_scope=(
                collaboration.target_type
                or request.episode_type.value
            ),
            target_role=role,
            source_agent=collaboration.sender,
            expires_at=(
                collaboration.expires_at
                or datetime.now(timezone.utc) + timedelta(minutes=20)
            ),
        )


class CareStrategyView:
    def __init__(self, work_product: AcceptedWorkProduct) -> None:
        self.payload = work_product.payload

    @property
    def requires_confirmation(self) -> bool:
        action = self.payload.get("primary_action")
        return bool(action and action.get("confirmation_required"))

    @property
    def primary_action(self) -> CareActionCandidate | None:
        action = self.payload.get("primary_action")
        return CareActionCandidate.model_validate(action) if action else None


__all__ = [
    "PRODUCT_EPISODE_RESULT_SCHEMA_VERSION",
    "PRODUCT_EPISODE_RUNNER_VERSION",
    "InMemoryProductEpisodeResultStore",
    "PendingConfirmationTarget",
    "PendingUserInputTarget",
    "ProductEpisodeRunRequest",
    "ProductEpisodeRunResult",
    "ProductEpisodeResultStore",
    "ProductEpisodeRunner",
    "ProductUserFactResponse",
    "PublicationPublisher",
]
