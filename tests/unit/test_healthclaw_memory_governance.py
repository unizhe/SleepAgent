from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from sleepagent.persistence import RadarPersistenceStore
from sleepagent.product_runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    InvocationOutcome,
    MemoryChangeCandidate,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.product_runtime.governance import (
    AcceptedWorkProduct,
    InMemoryMemoryContextStore,
    MemoryContextState,
    MemoryItem,
)
from sleepagent.product_runtime.longitudinal_memory import (
    DeploymentControlAttestation,
    DeterministicInductionWorker,
    GovernedMemoryItemV2,
    InductionJobState,
    InductionReceiptStatus,
    InMemoryLongitudinalResultStore,
    LongitudinalMemoryService,
    MemoryItemStatus,
    MemoryPurpose,
    MemoryQueryIntent,
    PendingProfileCandidate,
    ProfileCandidateProjection,
    ProvenanceType,
    SensitivityClass,
)
from sleepagent.product_runtime.product_persistence import (
    PersistentProductEpisodeResultStore,
)
from sleepagent.product_runtime.registry import (
    TOOL_DEFINITIONS,
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.product_runtime.runner import ProductEpisodeRunResult
from sleepagent.product_runtime.tooling import (
    CoreProductToolService,
    ProductToolError,
    ProductToolExecutionContext,
    ProductToolExecutor,
)


NOW = datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)


def _scope() -> SourceScope:
    return SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="UTC",
        date_start=NOW.date(),
        date_end=NOW.date(),
        valid_night_count=1,
    )


def _snapshot(*, role: str = "elder", subject_id: str = "elder-1") -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id=f"snapshot:{subject_id}:{role}",
        binding=AuthenticatedBinding(
            actor_id=f"actor:{role}",
            subject_id=subject_id,
            role=role,
            authorization_scope=("personal_memory:read",),
        ),
        source_scope=_scope(),
        canonical_data_version="canonical-v1",
        created_at=NOW,
    )


def _result(
    *,
    episode_id: str,
    subject_id: str = "elder-1",
    receipt_revision: int = 1,
    episode_type: EpisodeType = EpisodeType.MORNING_REVIEW,
    status: EpisodeStatus = EpisodeStatus.COMPLETE,
    accepted: bool = True,
    raw_sentinel: str | None = None,
) -> ProductEpisodeRunResult:
    products = []
    if accepted:
        products.append(
            AcceptedWorkProduct(
                work_product_ref=f"evidence:{episode_id}",
                agent_id=AgentId.EVIDENCE_REASONING,
                target_id=f"evidence:{episode_id}",
                target_hash="c" * 64,
                fact_snapshot_hash=_snapshot(
                    subject_id=subject_id
                ).fact_snapshot_hash,
                episode_state_revision=receipt_revision,
                payload={
                    "claims": [
                        {
                            "semantic": "observed",
                            "source_kind": "canonical_observation",
                        }
                    ],
                    "reasoning": raw_sentinel,
                    "tool_output": raw_sentinel,
                    "conversation": raw_sentinel,
                },
                accepted_at=NOW,
            )
        )
    return ProductEpisodeRunResult(
        registry_hash="b" * 64,
        receipt=EpisodeReceipt(
            episode_id=episode_id,
            episode_type=episode_type,
            receipt_revision=receipt_revision,
            terminal=True,
            execution_mode=ExecutionMode.INTELLIGENT,
            status=status,
            goal_achieved=status == EpisodeStatus.COMPLETE,
            fact_snapshot_id=_snapshot(
                subject_id=subject_id
            ).fact_snapshot_id,
            fact_snapshot_hash=_snapshot(
                subject_id=subject_id
            ).fact_snapshot_hash,
            source_scope=_scope(),
            final_episode_state_revision=receipt_revision,
            trace_ref=f"trace:{episode_id}",
        ),
        accepted_work_products=products,
    )


def _release_attestation(attestation_id: str) -> DeploymentControlAttestation:
    return DeploymentControlAttestation(
        attestation_id=attestation_id,
        manifest_encryption_verified=True,
        backup_crypto_expiry_verified=True,
        worker_least_privilege_verified=True,
        publication_journal_verified=True,
        writer_fencing_verified=True,
        orphan_terminal_count=0,
        benchmark_gate_passed=True,
        attested_at=NOW,
    )


def _context(
    *,
    caller: AgentId,
    role: str = "elder",
    subject_id: str = "elder-1",
    purpose: MemoryPurpose | None = None,
    invocation_id: str = "invocation:1",
) -> ProductToolExecutionContext:
    return ProductToolExecutionContext(
        caller=caller,
        fact_snapshot=_snapshot(role=role, subject_id=subject_id),
        authorization_scope=("personal_memory:read",),
        episode_id="episode:query",
        plan_id="plan:1",
        plan_revision=1,
        plan_step_id="step:memory",
        invocation_id=invocation_id,
        user_intent_ref="user-intent:1" if purpose else None,
        user_intent_hash="d" * 64 if purpose else None,
        user_intent_purpose=purpose,
        max_memory_items=4,
        memory_token_budget=800,
    )


def _governed_memory(
    *,
    subject_id: str = "elder-1",
    memory_id: str = "memory:wake-time",
    typed_value: str = "07:00",
) -> GovernedMemoryItemV2:
    return GovernedMemoryItemV2(
        memory_id=memory_id,
        subject_id=subject_id,
        memory_type="routine",
        concept_id="sleep.preferred_wake_time",
        value_schema_id="bounded_string.v1",
        typed_value=typed_value,
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        source_ref="user_report:wake-time",
        source_scope_kind=SourceScopeKind.CURRENT_NIGHT,
        version=1,
        recorded_at=NOW - timedelta(days=1),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=30),
        sensitivity_class=SensitivityClass.PERSONAL,
        allowed_roles=(AgentId.EVIDENCE_REASONING, AgentId.SLEEP_CARE),
        allowed_purposes=(
            MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
            MemoryPurpose.EXPLICIT_MEMORY_REVIEW,
            MemoryPurpose.EXPLICIT_MEMORY_CHANGE,
            MemoryPurpose.EXPLICIT_MEMORY_FORGET,
        ),
        status=MemoryItemStatus.ACTIVE,
        confirmation_ref="confirmation:wake-time",
        retention_policy_version="sleepagent-retention.v1",
    )


def test_memory_query_rejects_missing_selector_wildcard_and_runtime_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryQueryIntent.model_validate(
            {
                "purpose": "personal_evidence_context",
                "memory_types": ["governed_memory"],
                "selector_kind": "concept_ids",
                "requested_concept_ids": [],
                "requested_time_scope": "current_night",
            }
        )
    with pytest.raises(ValidationError):
        MemoryQueryIntent.model_validate(
            {
                "purpose": "personal_evidence_context",
                "memory_types": ["governed_memory"],
                "selector_kind": "concept_ids",
                "requested_concept_ids": ["all"],
                "requested_time_scope": "current_night",
            }
        )
    with pytest.raises(ValidationError):
        MemoryQueryIntent.model_validate(
            {
                "purpose": "personal_evidence_context",
                "memory_types": ["governed_memory"],
                "selector_kind": "concept_ids",
                "requested_concept_ids": ["sleep.preferred_wake_time"],
                "requested_time_scope": "current_night",
                "subject_id": "attacker-chosen",
            }
        )
    invalid_selectors = (
        {
            "purpose": "personal_evidence_context",
            "memory_types": [],
            "selector_kind": "concept_ids",
            "requested_concept_ids": ["sleep.last_night"],
            "requested_time_scope": "current_night",
        },
        {
            "purpose": "personal_evidence_context",
            "memory_types": ["all"],
            "selector_kind": "concept_ids",
            "requested_concept_ids": ["sleep.last_night"],
            "requested_time_scope": "current_night",
        },
        {
            "purpose": "explicit_memory_review",
            "memory_types": ["governed_memory"],
            "selector_kind": "item_handles",
            "item_handles": ["*"],
            "requested_time_scope": "current_night",
        },
        {
            "purpose": "explicit_memory_review",
            "memory_types": ["governed_memory"],
            "selector_kind": "item_handles",
            "item_handles": ["all"],
            "requested_time_scope": "current_night",
        },
        {
            "purpose": "explicit_memory_review",
            "memory_types": ["governed_memory"],
            "selector_kind": "inventory_page",
            "inventory_cursor": "*",
            "requested_time_scope": "current_night",
        },
        {
            "purpose": "explicit_memory_review",
            "memory_types": ["governed_memory"],
            "selector_kind": "inventory_page",
            "inventory_cursor": "all",
            "requested_time_scope": "current_night",
        },
    )
    for arguments in invalid_selectors:
        with pytest.raises(ValidationError):
            MemoryQueryIntent.model_validate(arguments)


def test_memory_read_injects_subject_and_never_crosses_张三_李四() -> None:
    class CountingMemoryStore(InMemoryMemoryContextStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_subjects: list[str] = []

        def get(self, subject_id: str):
            self.read_subjects.append(subject_id)
            return super().get(subject_id)

    memory_store = CountingMemoryStore()
    memory_store._items["person:张三"] = MemoryContextState(
        subject_id="person:张三",
        version=1,
        items=[
            _governed_memory(
                subject_id="person:张三",
                memory_id="memory:张三:起床",
                typed_value="07:00",
            )
        ],
    )
    memory_store._items["person:李四"] = MemoryContextState(
        subject_id="person:李四",
        version=1,
        items=[
            _governed_memory(
                subject_id="person:李四",
                memory_id="memory:李四:起床",
                typed_value="09:00",
            )
        ],
    )
    service = LongitudinalMemoryService(
        memory_store=memory_store,
        repository=InMemoryLongitudinalResultStore(),
    )
    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["governed_memory"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.preferred_wake_time"],
        "requested_time_scope": "current_night",
    }
    zhang_context = _context(
        caller=AgentId.EVIDENCE_REASONING,
        subject_id="person:张三",
        invocation_id="invocation:张三:读取",
    )
    result = service.read(arguments, zhang_context, now=NOW)
    assert [item["display_value"] for item in result["items"]] == ["07:00"]
    assert memory_store.read_subjects == ["person:张三"]

    memory_store.read_subjects.clear()
    with pytest.raises(ValidationError):
        service.read(
            {**arguments, "subject_id": "person:李四"},
            zhang_context,
            now=NOW,
        )
    assert memory_store.read_subjects == []

    executor = ProductToolExecutor(
        handlers={"memory.read": service.read},
        core_service=CoreProductToolService(),
    )
    denied = executor.execute(
        "memory.read",
        {**arguments, "subject_id": "person:李四"},
        context=zhang_context,
    )
    assert denied.receipt.outcome == InvocationOutcome.FAILED
    assert denied.receipt.error_code == "ValidationError"
    assert memory_store.read_subjects == []


def test_production_build_contains_one_memory_read_registry_definition() -> None:
    from setuptools import find_packages

    packages = find_packages(include=["sleepagent*"])
    package_roots = {
        Path(package.replace(".", "/"))
        for package in packages
    }
    definition_sites: list[Path] = []
    for package_root in package_roots:
        for source in package_root.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else None
                )
                if function_name not in {"ToolDefinition", "_tool"}:
                    continue
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and first.value == "memory.read"
                ):
                    definition_sites.append(source)
    assert definition_sites == [
        Path("sleepagent/product_runtime/registry.py")
    ]
    assert not Path("sleepagent/radar_agent/tools").exists()


def test_unbound_product_tool_executor_has_no_memory_read_fallback() -> None:
    executor = ProductToolExecutor(core_service=CoreProductToolService())
    assert "memory.read" not in executor.handlers
    explicitly_bound = ProductToolExecutor(
        handlers={"memory.read": lambda arguments, context: arguments},
        core_service=CoreProductToolService(),
    )
    assert "memory.read" in explicitly_bound.handlers
    with pytest.raises(ProductToolError, match="no handler"):
        executor.execute(
            "memory.read",
            {
                "purpose": "personal_evidence_context",
                "memory_types": ["governed_memory"],
                "selector_kind": "concept_ids",
                "requested_concept_ids": ["sleep.last_night"],
                "requested_time_scope": "current_night",
            },
            context=_context(
                caller=AgentId.EVIDENCE_REASONING,
                subject_id="person:张三",
            ),
        )


def test_legacy_memory_is_review_only_and_v2_is_minimally_retrieved() -> None:
    memory_store = InMemoryMemoryContextStore()
    memory_store._items["elder-1"] = MemoryContextState(
        subject_id="elder-1",
        version=2,
        items=[
            MemoryItem(
                memory_id="legacy:1",
                subject_id="elder-1",
                value="legacy free text",
                source_ref="legacy:1",
                version=1,
            ),
            _governed_memory(),
        ],
    )
    repository = InMemoryLongitudinalResultStore()
    service = LongitudinalMemoryService(
        memory_store=memory_store,
        repository=repository,
    )
    evidence = service.read(
        {
            "purpose": "personal_evidence_context",
            "memory_types": ["governed_memory"],
            "selector_kind": "concept_ids",
            "requested_concept_ids": ["sleep.preferred_wake_time"],
            "requested_time_scope": "current_night",
        },
        _context(caller=AgentId.EVIDENCE_REASONING),
        now=NOW,
    )
    assert len(evidence["items"]) == 1
    assert evidence["items"][0]["display_value"] == "07:00"
    assert "memory:wake-time" not in json.dumps(evidence)
    assert evidence["items"][0]["allowed_current_use"] == "confirmed_memory"

    review = service.read(
        {
            "purpose": "explicit_memory_review",
            "memory_types": ["governed_memory"],
            "selector_kind": "inventory_page",
            "inventory_cursor": "FIRST",
            "requested_time_scope": "current_night",
        },
        _context(
            caller=AgentId.SLEEP_CARE,
            purpose=MemoryPurpose.EXPLICIT_MEMORY_REVIEW,
        ),
        now=NOW,
    )
    assert {item["status"] for item in review["items"]} == {
        "active",
        "legacy_unclassified",
    }
    unchanged = memory_store.get("elder-1")
    assert unchanged.version == 2
    assert isinstance(unchanged.items[0], MemoryItem)
    assert unchanged.items[0].classification_status == "legacy_unclassified"
    with pytest.raises(ValueError, match="top-level user intent"):
        service.read(
            {
                "purpose": "explicit_memory_review",
                "memory_types": ["governed_memory"],
                "selector_kind": "inventory_page",
                "inventory_cursor": "FIRST",
                "requested_time_scope": "current_night",
            },
            _context(caller=AgentId.SLEEP_CARE),
            now=NOW,
        )


def test_multiple_memory_reads_share_one_agent_episode_budget() -> None:
    memory_store = InMemoryMemoryContextStore()
    memory_store._items["elder-1"] = MemoryContextState(
        subject_id="elder-1",
        version=1,
        items=[_governed_memory()],
    )
    repository = InMemoryLongitudinalResultStore()
    service = LongitudinalMemoryService(
        memory_store=memory_store,
        repository=repository,
    )
    context = _context(caller=AgentId.EVIDENCE_REASONING).model_copy(
        update={"memory_token_budget": 250}
    )
    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["governed_memory"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.preferred_wake_time"],
        "requested_time_scope": "current_night",
    }
    first = service.read(arguments, context, now=NOW)
    with pytest.raises(ValueError, match="token budget"):
        service.read(arguments, context, now=NOW)
    assert first["items"]
    assert first["token_count"] <= 250
    assert repository.resolve_handle(
        first["items"][0]["retrieval_handle"],
        now=NOW,
    ) is not None


def test_governed_memory_revisions_append_and_invalidate_old_handle() -> None:
    memory_store = InMemoryMemoryContextStore(control_clock=lambda: NOW)
    snapshot = _snapshot()
    create = MemoryChangeCandidate(
        candidate_id="memory:wake",
        operation="create",
        subject_id="elder-1",
        memory_type="routine",
        concept_id="sleep.preferred_wake_time",
        value_schema_id="bounded_string.v1",
        typed_value="07:00",
        provenance_type="elder_confirmed",
        source_ref="user_report:create",
        sensitivity_class="personal",
        allowed_roles=(AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING),
        allowed_purposes=(
            "personal_evidence_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ),
        confirmation_required=True,
    )
    memory_store.apply(
        create,
        expected_version=0,
        confirmed=True,
        fact_snapshot=snapshot,
        confirmation_ref="confirmation:create",
    )
    repository = InMemoryLongitudinalResultStore()
    service = LongitudinalMemoryService(
        memory_store=memory_store,
        repository=repository,
    )
    context = _context(caller=AgentId.EVIDENCE_REASONING)
    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["governed_memory"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.preferred_wake_time"],
        "requested_time_scope": "current_night",
    }
    first = service.read(arguments, context, now=NOW)
    assert len(first["items"]) == 1
    old_output = {"items": first["items"]}
    replace = create.model_copy(
        update={
            "operation": "replace",
            "typed_value": "07:30",
            "source_ref": "user_report:replace",
            "candidate_hash": None,
        }
    )
    replace = MemoryChangeCandidate.model_validate(
        replace.model_dump(mode="json", exclude={"candidate_hash"})
    )
    updated = memory_store.apply(
        replace,
        expected_version=1,
        confirmed=True,
        fact_snapshot=snapshot,
        confirmation_ref="confirmation:replace",
    )
    assert [item.version for item in updated.items] == [1, 2]
    with pytest.raises(ValueError, match="no longer current"):
        service.validate_model_input(old_output, context, now=NOW)


def test_conflict_groups_are_connected_atomic_and_fail_closed() -> None:
    def conflicting(
        memory_id: str,
        value: str,
        conflict_refs: tuple[str, ...],
    ) -> GovernedMemoryItemV2:
        payload = _governed_memory().model_dump(
            mode="json",
            exclude={"value_hash"},
        )
        payload.update(
            {
                "memory_id": memory_id,
                "typed_value": value,
                "conflict_refs": conflict_refs,
            }
        )
        return GovernedMemoryItemV2.model_validate(payload)

    first = conflicting(
        "memory:wake-a",
        "07:00",
        ("memory:wake-b",),
    )
    second = conflicting("memory:wake-b", "08:00", ())
    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["governed_memory"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.preferred_wake_time"],
        "requested_time_scope": "current_night",
    }

    incomplete_store = InMemoryMemoryContextStore()
    incomplete_store._items["elder-1"] = MemoryContextState(
        subject_id="elder-1",
        version=1,
        items=[first],
    )
    incomplete = LongitudinalMemoryService(
        memory_store=incomplete_store,
        repository=InMemoryLongitudinalResultStore(),
    ).read(
        arguments,
        _context(caller=AgentId.EVIDENCE_REASONING),
        now=NOW,
    )
    assert incomplete["items"] == []
    assert "conflict_group_incomplete" in incomplete["reason_codes"]

    complete_store = InMemoryMemoryContextStore()
    complete_store._items["elder-1"] = MemoryContextState(
        subject_id="elder-1",
        version=2,
        items=[first, second],
    )
    service = LongitudinalMemoryService(
        memory_store=complete_store,
        repository=InMemoryLongitudinalResultStore(),
    )
    too_small = service.read(
        arguments,
        _context(caller=AgentId.EVIDENCE_REASONING).model_copy(
            update={"max_memory_items": 1}
        ),
        now=NOW,
    )
    assert too_small["items"] == []
    assert "conflict_group_omitted_budget" in too_small["reason_codes"]

    complete = service.read(
        arguments,
        _context(
            caller=AgentId.EVIDENCE_REASONING,
            invocation_id="invocation:conflict-complete",
        ),
        now=NOW,
    )
    assert len(complete["items"]) == 2
    assert len(
        {item["conflict_group"] for item in complete["items"]}
    ) == 1


def test_terminal_bundle_worker_is_deterministic_and_manifest_is_sanitized() -> None:
    sentinel = "DO_NOT_COPY_RAW_PRIVATE_TEXT"
    store = InMemoryLongitudinalResultStore()
    bundle = store.append_terminal_bundle(
        _result(episode_id="episode:complete", raw_sentinel=sentinel),
        subject_id="elder-1",
        now=NOW,
    )
    manifest_json = bundle.manifest.model_dump_json()
    assert sentinel not in manifest_json
    assert "conversation" not in manifest_json
    worker = DeterministicInductionWorker(store)
    assert not hasattr(worker.repository, "history")
    assert not hasattr(worker.repository, "latest")
    receipt = worker.process_next(now=NOW)
    assert receipt is not None
    assert receipt.status == InductionReceiptStatus.SUCCEEDED
    assert receipt.episode_digest_ref
    assert len(store.list_active_digests("elder-1", as_of=NOW)) == 1
    assert DeterministicInductionWorker(store).process_next(now=NOW) is None

    urgent = store.append_terminal_bundle(
        _result(
            episode_id="episode:urgent",
            episode_type=EpisodeType.URGENT_BOUNDARY,
            accepted=False,
        ),
        subject_id="elder-1",
        now=NOW,
    )
    excluded = DeterministicInductionWorker(store).process_next(now=NOW)
    assert excluded is not None
    assert excluded.status == InductionReceiptStatus.EXCLUDED
    assert excluded.episode_digest_ref is None
    assert store.job(urgent.job.job_id).state == InductionJobState.SUCCEEDED


def test_explicitly_declined_profile_candidate_is_not_resurfaced_offline() -> None:
    candidate = MemoryChangeCandidate(
        candidate_id="candidate:declined-wake",
        operation="create",
        subject_id="elder-1",
        memory_type="routine",
        concept_id="sleep.preferred_wake_time",
        value_schema_id="bounded_string.v1",
        typed_value="07:00",
        provenance_type="elder_confirmed",
        source_ref="user_report:declined-wake",
        sensitivity_class="personal",
        allowed_roles=(AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING),
        allowed_purposes=(
            "personal_evidence_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ),
        confirmation_required=True,
    )
    product = AcceptedWorkProduct(
        work_product_ref="sleep-care:declined",
        agent_id=AgentId.SLEEP_CARE,
        target_id="sleep-care:declined",
        target_hash="d" * 64,
        fact_snapshot_hash=_snapshot().fact_snapshot_hash,
        episode_state_revision=1,
        payload={
            "memory_change_candidates": [
                candidate.model_dump(mode="json")
            ]
        },
        accepted_at=NOW,
    )
    result = _result(episode_id="episode:declined", accepted=False).model_copy(
        update={
            "accepted_work_products": [product],
            "declined_confirmation_ids": [candidate.candidate_id],
        }
    )
    store = InMemoryLongitudinalResultStore()
    bundle = store.append_terminal_bundle(
        result,
        subject_id="elder-1",
        now=NOW,
    )
    assert bundle.manifest.declined_candidate_refs == (
        candidate.candidate_id,
    )
    receipt = DeterministicInductionWorker(store).process_next(now=NOW)
    assert receipt is not None
    assert receipt.status == InductionReceiptStatus.SUCCEEDED
    assert store.list_pending_candidates("elder-1", as_of=NOW) == []


def test_manifest_expiry_dead_letters_and_cannot_fallback_to_raw_audit() -> None:
    store = InMemoryLongitudinalResultStore()
    bundle = store.append_terminal_bundle(
        _result(episode_id="episode:expired"),
        subject_id="elder-1",
        now=NOW,
    )
    late = bundle.manifest.expires_at + timedelta(seconds=1)
    receipt = DeterministicInductionWorker(store).process_next(now=late)
    assert receipt is not None
    assert receipt.status == InductionReceiptStatus.DEAD_LETTER
    assert store.job(bundle.job.job_id).state == InductionJobState.DEAD_LETTER
    store.purge_expired_manifests(now=late)
    with pytest.raises(ValueError, match="source_manifest_expired"):
        store.replay_dead_letter(
            job_id=bundle.job.job_id,
            parent_receipt_ref=receipt.receipt_id,
            now=late,
        )


def test_digest_read_requires_attestation_and_old_handle_dies_on_kill_switch() -> None:
    store = InMemoryLongitudinalResultStore()
    store.append_terminal_bundle(
        _result(episode_id="episode:digest"),
        subject_id="elder-1",
        now=NOW,
    )
    DeterministicInductionWorker(store).process_all(now=NOW)
    service = LongitudinalMemoryService(
        memory_store=InMemoryMemoryContextStore(),
        repository=store,
    )
    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["episode_digest"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.last_night"],
        "requested_time_scope": "current_night",
    }
    disabled = service.read(
        arguments,
        _context(caller=AgentId.EVIDENCE_REASONING),
        now=NOW,
    )
    assert disabled["items"] == []
    store.enable_digest_read(
        DeploymentControlAttestation(
            attestation_id="attestation:1",
            manifest_encryption_verified=True,
            backup_crypto_expiry_verified=True,
            worker_least_privilege_verified=True,
            publication_journal_verified=True,
            writer_fencing_verified=True,
            orphan_terminal_count=0,
            benchmark_gate_passed=True,
            attested_at=NOW,
        )
    )
    context = _context(caller=AgentId.EVIDENCE_REASONING)
    enabled = service.read(arguments, context, now=NOW)
    handle = enabled["items"][0]["retrieval_handle"]
    assert enabled["items"][0]["allowed_current_use"] == "episodic_hint_only"
    with pytest.raises(
        ValueError,
        match="canonical_source_resolver_unavailable",
    ):
        service.resolve_source(
            {"retrieval_handle": handle},
            context,
            now=NOW,
        )
    store.kill_switch(reason_code="privacy_incident", now=NOW)
    with pytest.raises(ValueError, match="unknown_or_expired"):
        service.resolve_source(
            {"retrieval_handle": handle},
            context,
            now=NOW,
        )


def test_single_registry_keeps_resolver_evidence_only() -> None:
    assert "memory.read" in TOOL_DEFINITIONS
    assert "memory.resolve_source" in TOOL_DEFINITIONS
    assert "memory.resolve_source" in TOOL_INVOCATION_ALLOWLIST[
        AgentId.EVIDENCE_REASONING
    ]
    assert "memory.resolve_source" not in TOOL_INVOCATION_ALLOWLIST[
        AgentId.SLEEP_CARE
    ]
    assert "memory.read" not in TOOL_INVOCATION_ALLOWLIST[AgentId.CARE_STRATEGY]
    assert "memory.read" not in TOOL_INVOCATION_ALLOWLIST[AgentId.SAFETY_REVIEW]


def test_induced_profile_candidate_is_nonoperative_until_exact_elder_confirmation() -> None:
    store = InMemoryLongitudinalResultStore()
    projection = ProfileCandidateProjection(
        candidate_id="candidate:wake",
        operation="create",
        concept_id="sleep.preferred_wake_time",
        memory_type="routine",
        value_schema_id="bounded_string.v1",
        value_schema_version="1",
        typed_value="07:00",
        source_ref="user_report:wake",
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        sensitivity_class=SensitivityClass.PERSONAL,
    )
    pending = PendingProfileCandidate(
        candidate_id=projection.candidate_id,
        candidate_hash=str(projection.candidate_hash),
        subject_id="elder-1",
        source_result_hash="f" * 64,
        projection=projection,
        lineage_refs=("manifest:1",),
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    store._pending_candidates[
        (pending.subject_id, pending.candidate_hash)
    ] = pending
    service = LongitudinalMemoryService(
        memory_store=InMemoryMemoryContextStore(),
        repository=store,
    )
    review = service.review_pending_candidates(
        {},
        _context(
            caller=AgentId.SLEEP_CARE,
            purpose=MemoryPurpose.EXPLICIT_MEMORY_REVIEW,
        ),
        now=NOW,
    )
    assert review["notice"] == "尚未保存"
    handle = review["candidates"][0]["candidate_handle"]
    prepared = service.prepare_pending_candidate(
        {"candidate_handle": handle},
        _context(
            caller=AgentId.SLEEP_CARE,
            purpose=MemoryPurpose.EXPLICIT_MEMORY_CHANGE,
        ),
        now=NOW,
    )
    candidate = prepared["memory_change_candidate"]
    assert candidate["confirmation_required"] is True
    assert prepared["status"] == "pending_exact_confirmation"
    assert service.memory_store.get("elder-1").version == 0
    store.apply_privacy_action(
        subject_id="elder-1",
        action="withdraw",
        causal_ref="privacy:1",
        now=NOW,
    )
    with pytest.raises(ValueError, match="expired"):
        service.prepare_pending_candidate(
            {"candidate_handle": handle},
            _context(
                caller=AgentId.SLEEP_CARE,
                purpose=MemoryPurpose.EXPLICIT_MEMORY_CHANGE,
            ),
            now=NOW,
        )


def test_persistent_bundle_encrypts_manifest_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "longitudinal.sqlite3"
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    store = PersistentProductEpisodeResultStore(
        persistence,
        manifest_key="test-only-key",
    )
    bundle = store.append_terminal_bundle(
        _result(episode_id="episode:persistent"),
        subject_id="elder-1",
        now=NOW,
    )
    row = persistence.connection.execute(
        "SELECT ciphertext FROM product_induction_manifests"
    ).fetchone()
    assert row is not None
    assert "elder-1" not in str(row[0])
    persistence.connection.close()

    restarted_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    restarted = PersistentProductEpisodeResultStore(
        restarted_persistence,
        manifest_key="test-only-key",
    )
    assert restarted.latest("episode:persistent") == _result(
        episode_id="episode:persistent"
    )
    assert restarted.job(bundle.job.job_id).state == InductionJobState.PENDING
    assert (
        restarted.get_manifest(bundle.manifest.manifest_id).manifest_hash
        == bundle.manifest.manifest_hash
    )
    processed = DeterministicInductionWorker(restarted).process_next(now=NOW)
    assert processed is not None
    assert processed.status == InductionReceiptStatus.SUCCEEDED
    restarted_persistence.connection.close()

    verified_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    verified = PersistentProductEpisodeResultStore(
        verified_persistence,
        manifest_key="test-only-key",
    )
    assert verified.job(bundle.job.job_id).state == InductionJobState.SUCCEEDED
    assert len(verified.list_active_digests("elder-1", as_of=NOW)) == 1
    assert DeterministicInductionWorker(verified).process_next(now=NOW) is None


def test_persistent_terminal_bundle_rolls_back_speculative_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(
            tmp_path / "atomic.sqlite3",
            check_same_thread=False,
        )
    )
    store = PersistentProductEpisodeResultStore(
        persistence,
        manifest_key="test-only-key",
    )
    durable_append = persistence.append_product_terminal_bundle

    def fail_append(**_kwargs: object) -> bool:
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(
        persistence,
        "append_product_terminal_bundle",
        fail_append,
    )
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        store.append_terminal_bundle(
            _result(episode_id="episode:atomic"),
            subject_id="elder-1",
            now=NOW,
        )
    assert store.history("episode:atomic") == []
    assert persistence.count_product_terminal_orphans() == 0

    monkeypatch.setattr(
        persistence,
        "append_product_terminal_bundle",
        durable_append,
    )
    bundle = store.append_terminal_bundle(
        _result(episode_id="episode:atomic"),
        subject_id="elder-1",
        now=NOW,
    )
    assert store.job(bundle.job.job_id).state == InductionJobState.PENDING


def test_persistent_job_lease_and_publication_reservation_are_cross_process_cas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cas.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="test-only-key",
    )
    bundle = first.append_terminal_bundle(
        _result(episode_id="episode:cas"),
        subject_id="elder-1",
        now=NOW,
    )
    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="test-only-key",
    )
    first_lease = first.lease_next_job(worker_id="worker:1", now=NOW)
    assert first_lease is not None
    assert second.lease_next_job(worker_id="worker:2", now=NOW) is None
    recovered = second.lease_next_job(
        worker_id="worker:2",
        now=NOW + timedelta(seconds=31),
    )
    assert recovered is not None
    assert recovered.job_id == bundle.job.job_id
    assert recovered.attempt_count == 2
    assert recovered.lease_owner == "worker:2"
    dead_letter = second.fail_induction(
        job=recovered,
        error_code="synthetic_failure",
        now=NOW + timedelta(seconds=31),
        max_attempts=1,
    )
    assert dead_letter is not None
    replayed = second.replay_dead_letter(
        job_id=recovered.job_id,
        parent_receipt_ref=dead_letter.receipt_id,
        now=NOW + timedelta(seconds=32),
    )
    assert replayed.processing_generation == 2
    replay_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    replay_store = PersistentProductEpisodeResultStore(
        replay_persistence,
        manifest_key="test-only-key",
    )
    durable_replay = replay_store.job(recovered.job_id)
    assert durable_replay.state == InductionJobState.PENDING
    assert durable_replay.processing_generation == 2

    first_entry, first_created = first.reserve_publication(
        command_hash="b" * 64,
        episode_id="episode:cas",
        draft_hash="a" * 64,
        now=NOW,
    )
    second_entry, second_created = second.reserve_publication(
        command_hash="b" * 64,
        episode_id="episode:cas",
        draft_hash="a" * 64,
        now=NOW,
    )
    assert first_created is True
    assert second_created is False
    assert second_entry.intent_id == first_entry.intent_id
    completed = first.complete_publication(
        intent_id=first_entry.intent_id,
        delivered=True,
        now=NOW,
    )
    replay = second.complete_publication(
        intent_id=first_entry.intent_id,
        delivered=True,
        now=NOW,
    )
    assert completed == replay


def test_cross_process_revocation_and_kill_switch_cannot_be_resurrected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "张三-李四-cross-process.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="张三李四-test-only-key",
    )
    for subject_id, episode_id in (
        ("person:张三", "episode:张三:晨间回顾"),
        ("person:李四", "episode:李四:晨间回顾"),
    ):
        first.append_terminal_bundle(
            _result(episode_id=episode_id, subject_id=subject_id),
            subject_id=subject_id,
            now=NOW,
        )
    DeterministicInductionWorker(first).process_all(now=NOW)

    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="张三李四-test-only-key",
    )
    first.enable_digest_read(_release_attestation("attestation:张三李四"))
    assert second.digest_read_enabled() is True

    arguments = {
        "purpose": "personal_evidence_context",
        "memory_types": ["episode_digest"],
        "selector_kind": "concept_ids",
        "requested_concept_ids": ["sleep.last_night"],
        "requested_time_scope": "current_night",
    }
    service = LongitudinalMemoryService(
        memory_store=InMemoryMemoryContextStore(),
        repository=second,
    )
    zhang_context = _context(
        caller=AgentId.EVIDENCE_REASONING,
        subject_id="person:张三",
        invocation_id="invocation:张三",
    )
    zhang_read = service.read(arguments, zhang_context, now=NOW)
    zhang_handle = zhang_read["items"][0]["retrieval_handle"]
    zhang_digest_id = second.list_active_digests(
        "person:张三",
        as_of=NOW,
    )[0].digest_id

    first.apply_privacy_action(
        subject_id="person:张三",
        action="withdraw",
        causal_ref="privacy:张三:撤回同意",
        now=NOW + timedelta(seconds=1),
    )
    assert second.current_epochs("person:张三").privacy_epoch == 1
    assert second.list_active_digests("person:张三", as_of=NOW) == []
    assert len(second.list_active_digests("person:李四", as_of=NOW)) == 1
    zhang_events = [
        json.loads(raw)
        for raw in second_persistence.list_product_episode_digest_event_json()
        if json.loads(raw)["digest_id"] == zhang_digest_id
    ]
    assert zhang_events[-1]["privacy_epoch"] == 1
    with pytest.raises(ValueError, match="retrieval_handle_binding_changed"):
        service.resolve_source(
            {"retrieval_handle": zhang_handle},
            zhang_context,
            now=NOW + timedelta(seconds=1),
        )

    li_context = _context(
        caller=AgentId.EVIDENCE_REASONING,
        subject_id="person:李四",
        invocation_id="invocation:李四",
    )
    li_read = service.read(arguments, li_context, now=NOW)
    li_handle = li_read["items"][0]["retrieval_handle"]
    kill_epoch = first.kill_switch(
        reason_code="privacy_incident:张三",
        now=NOW + timedelta(seconds=2),
    )
    assert kill_epoch == 1
    assert second.digest_read_enabled() is False
    with pytest.raises(ValueError, match="retrieval_handle_binding_changed"):
        service.resolve_source(
            {"retrieval_handle": li_handle},
            li_context,
            now=NOW + timedelta(seconds=2),
        )

    # A stale instance may still flush its non-authoritative JSON mirror.  It
    # cannot re-enable reads or lower the database-owned policy epoch.
    second._save_state()
    assert first.digest_read_enabled() is False
    assert (
        first.current_epochs("person:李四").retrieval_policy_epoch
        == kill_epoch
    )


def test_concurrent_revocation_wins_over_inflight_induction_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "张三-concurrent-revocation.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="张三-test-only-key",
    )
    bundle = first.append_terminal_bundle(
        _result(
            episode_id="episode:张三:并发撤权",
            subject_id="person:张三",
        ),
        subject_id="person:张三",
        now=NOW,
    )
    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="张三-test-only-key",
    )
    worker = DeterministicInductionWorker(first, worker_id="worker:张三")
    original_derive = worker._derive

    def revoke_after_derive(*args: object, **kwargs: object):
        derived = original_derive(*args, **kwargs)
        second.apply_privacy_action(
            subject_id="person:张三",
            action="withdraw",
            causal_ref="privacy:张三:并发撤权",
            now=NOW + timedelta(milliseconds=1),
        )
        return derived

    monkeypatch.setattr(worker, "_derive", revoke_after_derive)
    assert worker.process_next(now=NOW) is None
    assert first.current_epochs("person:张三").privacy_epoch == 1
    assert first.list_active_digests("person:张三", as_of=NOW) == []
    assert first.semantic_receipt(bundle.job.job_id) is None
    assert first.job(bundle.job.job_id).state == InductionJobState.RETRYABLE_FAILED


def test_cross_process_kill_switch_fences_inflight_induction_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "李四-kill-switch-race.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="李四-kill-switch-key",
    )
    bundle = first.append_terminal_bundle(
        _result(
            episode_id="episode:李四:kill-switch-race",
            subject_id="person:李四",
        ),
        subject_id="person:李四",
        now=NOW,
    )
    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="李四-kill-switch-key",
    )
    worker = DeterministicInductionWorker(first, worker_id="worker:李四")
    original_derive = worker._derive

    def kill_after_derive(*args: object, **kwargs: object):
        derived = original_derive(*args, **kwargs)
        second.kill_switch(
            reason_code="incident:李四:立即停用",
            now=NOW + timedelta(milliseconds=1),
        )
        return derived

    monkeypatch.setattr(worker, "_derive", kill_after_derive)
    assert worker.process_next(now=NOW) is None
    assert first.current_epochs(
        "person:李四"
    ).retrieval_policy_epoch == 1
    assert first.digest_read_enabled() is False
    assert first.list_active_digests("person:李四", as_of=NOW) == []
    assert first.semantic_receipt(bundle.job.job_id) is None


def test_higher_excluded_revision_supersedes_existing_digest_cross_process(
    tmp_path: Path,
) -> None:
    database = tmp_path / "李四-revision-supersede.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="李四-test-only-key",
    )
    episode_id = "episode:李四:terminal-revision"
    first.append_terminal_bundle(
        _result(
            episode_id=episode_id,
            subject_id="person:李四",
            receipt_revision=1,
        ),
        subject_id="person:李四",
        now=NOW,
    )
    revision_one = DeterministicInductionWorker(first).process_next(now=NOW)
    assert revision_one is not None
    assert revision_one.episode_digest_ref is not None

    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="李四-test-only-key",
    )
    first.append_terminal_bundle(
        _result(
            episode_id=episode_id,
            subject_id="person:李四",
            receipt_revision=2,
            accepted=False,
        ),
        subject_id="person:李四",
        now=NOW + timedelta(seconds=1),
    )
    revision_two = DeterministicInductionWorker(first).process_next(
        now=NOW + timedelta(seconds=1)
    )
    assert revision_two is not None
    assert revision_two.status == InductionReceiptStatus.EXCLUDED
    assert revision_two.episode_digest_ref is None
    assert second.list_active_digests("person:李四", as_of=NOW) == []
    assert second.digest_status(
        revision_one.episode_digest_ref
    ).value == "superseded"


def test_new_terminal_revision_invalidates_inflight_older_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "张三-revision-cas.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    first = PersistentProductEpisodeResultStore(
        first_persistence,
        manifest_key="张三-revision-key",
    )
    episode_id = "episode:张三:revision-race"
    revision_one_bundle = first.append_terminal_bundle(
        _result(
            episode_id=episode_id,
            subject_id="person:张三",
            receipt_revision=1,
        ),
        subject_id="person:张三",
        now=NOW,
    )
    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(database, check_same_thread=False)
    )
    second = PersistentProductEpisodeResultStore(
        second_persistence,
        manifest_key="张三-revision-key",
    )
    worker = DeterministicInductionWorker(first, worker_id="worker:revision-1")
    original_derive = worker._derive

    def append_revision_two_after_derive(*args: object, **kwargs: object):
        derived = original_derive(*args, **kwargs)
        second.append_terminal_bundle(
            _result(
                episode_id=episode_id,
                subject_id="person:张三",
                receipt_revision=2,
                accepted=False,
            ),
            subject_id="person:张三",
            now=NOW + timedelta(milliseconds=1),
        )
        return derived

    monkeypatch.setattr(worker, "_derive", append_revision_two_after_derive)
    assert worker.process_next(now=NOW) is None
    assert first.semantic_receipt(revision_one_bundle.job.job_id) is None
    assert first.list_active_digests("person:张三", as_of=NOW) == []

    revision_two = DeterministicInductionWorker(
        second,
        worker_id="worker:revision-2",
    ).process_next(now=NOW + timedelta(seconds=1))
    assert revision_two is not None
    assert revision_two.status == InductionReceiptStatus.EXCLUDED
    assert second.list_active_digests("person:张三", as_of=NOW) == []


def test_benchmark_full_history_code_is_not_in_production_package() -> None:
    from setuptools import find_packages

    packages = find_packages(include=["sleepagent*"])
    assert not any("benchmark" in package for package in packages)
    assert not any(
        "full_history" in tool_name for tool_name in TOOL_DEFINITIONS
    )
