from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

import pytest

from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.runtime import TaskService
from sleepagent.radar_agent.schemas import RadarDevice, RadarDeviceStatus
from sleepagent.radar_agent.tools import (
    LLMToolRequest,
    OrchestratorToolExecutor,
    ToolAuthorizationError,
    ToolConfirmation,
    ToolExecutionContext,
    ToolPermission,
    WhitelistedMCPAdapter,
    build_default_registry,
    build_internal_handlers,
    in_memory_state_writer,
    task_service_audit_sink,
    task_service_confirmation_verifier,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _context(
    *tools: str,
    role: str = "family",
    permissions: set[ToolPermission] | None = None,
    confirmation: ToolConfirmation | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id="task-001",
        trace_id="trace-001",
        actor_id="actor-001",
        actor_role=role,
        permissions=permissions or set(),
        allowed_tools=set(tools),
        confirmation=confirmation,
    )


def test_whitelist_declares_schema_permissions_confirmation_state_and_audit() -> None:
    registry = build_default_registry()
    metadata = {item.name: item.metadata() for item in registry.definitions()}

    assert {
        "radar.read",
        "quality.assess",
        "night_summary.generate",
        "trend.calculate",
        "alert_rules.evaluate",
        "report.generate",
    } <= metadata.keys()
    assert "memory.read" not in metadata
    assert metadata["radar.read"].input_schema["type"] == "object"
    assert metadata["radar.read"].output_schema["type"] == "object"
    assert metadata["radar.read"].required_permissions == [ToolPermission.RADAR_READ]
    assert metadata["radar.read"].state_effect == "read"
    assert metadata["alert_rules.evaluate"].state_effect == "none"
    assert metadata["family.notify"].confirmation == "required"
    assert metadata["family.notify"].state_effect == "write"
    assert metadata["family.notify"].audit.redact_input_fields == ["content"]
    optional = [item for item in metadata.values() if item.optional]
    assert {item.name for item in optional} == {
        "context.weather.read",
        "context.room_temperature.read",
        "context.calendar.read",
        "context.medication_diet.read",
    }
    for item in optional:
        assert set(item.input_schema["properties"]["mode"]["enum"]) == {"mock", "manual"}


def test_llm_cannot_execute_unlisted_or_out_of_scope_tool_and_denials_are_audited() -> None:
    events = []
    executor = OrchestratorToolExecutor(build_default_registry(), audit_sink=events.append)

    with pytest.raises(ToolAuthorizationError) as unlisted:
        executor.execute(
            LLMToolRequest(
                request_id="req-unknown",
                task_id="task-001",
                tool_name="shell.exec",
                arguments={"command": "id"},
            ),
            _context("shell.exec"),
        )
    assert unlisted.value.code == "tool_not_whitelisted"
    assert events[-1].input_summary == {"argument_keys": ["command"]}

    with pytest.raises(ToolAuthorizationError) as scope:
        executor.execute(
            LLMToolRequest(
                request_id="req-scope",
                task_id="task-001",
                tool_name="radar.read",
                arguments={"radar_device_id": "radar-001"},
            ),
            _context(permissions={ToolPermission.RADAR_READ}),
        )
    assert scope.value.code == "tool_not_allowed_in_context"
    assert [event.decision for event in events] == [
        "requested", "denied", "requested", "denied"
    ]


def test_legacy_memory_read_is_not_registered_or_built() -> None:
    called = []
    handlers = build_internal_handlers(
        provider=ReplayRadarProvider(scenario="normal_night")
    )
    assert "memory.read" not in handlers
    assert "memory_reader" not in inspect.signature(
        build_internal_handlers
    ).parameters
    registry = build_default_registry(
        {"memory.read": lambda value, context: called.append(value)}
    )
    assert registry.definition("memory.read") is None
    assert registry.handler("memory.read") is None
    executor = OrchestratorToolExecutor(registry)
    request = LLMToolRequest(
        request_id="req-memory",
        task_id="task-001",
        tool_name="memory.read",
        arguments={"subject_id": "elder-001"},
    )

    with pytest.raises(ToolAuthorizationError) as denied:
        executor.execute(
            request,
            _context("memory.read", role="elder"),
        )
    assert denied.value.code == "tool_not_whitelisted"
    assert called == []


def test_high_risk_state_write_needs_matching_approved_confirmation() -> None:
    calls = []

    def notify(value, context):
        calls.append((value, context))
        return {"action_ref": "notification-001", "status": "sent"}

    events = []
    registry = build_default_registry({"family.notify": notify})
    executor = OrchestratorToolExecutor(
        registry,
        audit_sink=events.append,
        confirmation_verifier=lambda confirmation: confirmation.confirmation_id == "confirm-001",
    )
    request = LLMToolRequest(
        request_id="req-notify",
        task_id="task-001",
        tool_name="family.notify",
        arguments={
            "subject_id": "elder-001",
            "content": "Sensitive family message",
            "evidence_refs": ["claim:001"],
        },
    )
    base = {ToolPermission.EXTERNAL_ACTION}

    with pytest.raises(ToolAuthorizationError) as missing:
        executor.execute(request, _context("family.notify", permissions=base))
    assert missing.value.code == "confirmation_required"
    wrong = ToolConfirmation(
        confirmation_id="confirm-001",
        task_id="task-001",
        action_type="alert.send",
        confirmed_by="family-user",
    )
    with pytest.raises(ToolAuthorizationError) as mismatch:
        executor.execute(
            request,
            _context("family.notify", permissions=base, confirmation=wrong),
        )
    assert mismatch.value.code == "confirmation_action_mismatch"
    unverified = wrong.model_copy(
        update={"action_type": "family.notify", "confirmation_id": "forged"}
    )
    with pytest.raises(ToolAuthorizationError) as forged:
        executor.execute(
            request,
            _context("family.notify", permissions=base, confirmation=unverified),
        )
    assert forged.value.code == "confirmation_not_verified"
    approved = wrong.model_copy(update={"action_type": "family.notify"})
    result = executor.execute(
        request,
        _context("family.notify", permissions=base, confirmation=approved),
    )

    assert result.output["status"] == "sent"
    assert result.state_effect == "write"
    assert len(calls) == 1
    assert events[-1].decision == "completed"
    assert events[-1].input_summary["content"] == "[REDACTED]"


def test_mcp_lists_only_authorized_tools_and_delegates_execution_to_orchestrator() -> None:
    registry = build_default_registry(
        {"context.weather.read": lambda value, context: {
            "source": "weather",
            "mode": value.mode,
            "values": value.values,
            "live_connector_used": False,
        }}
    )
    adapter = WhitelistedMCPAdapter(OrchestratorToolExecutor(registry))
    context = _context(
        "context.weather.read",
        "memory.read",
        permissions={ToolPermission.EXTERNAL_CONTEXT_READ},
    )

    assert [tool.name for tool in adapter.list_tools(context)] == ["context.weather.read"]
    result = adapter.call_tool(
        task_id="task-001",
        tool_name="context.weather.read",
        arguments={"mode": "manual", "values": {"condition": "rain"}},
        context=context,
    )
    assert result.output == {
        "source": "weather",
        "mode": "manual",
        "values": {"condition": "rain"},
        "live_connector_used": False,
    }


def test_internal_radar_read_and_quality_tools_use_existing_canonical_services() -> None:
    provider = ReplayRadarProvider(scenario="normal_night")
    registry = build_default_registry(build_internal_handlers(provider=provider))
    adapter = WhitelistedMCPAdapter(OrchestratorToolExecutor(registry))
    context = _context(
        "radar.read",
        "quality.assess",
        permissions={ToolPermission.RADAR_READ, ToolPermission.ANALYSIS_RUN},
    )
    device_id = provider.list_devices()[0].radar_device_id

    radar = adapter.call_tool(
        task_id="task-001",
        tool_name="radar.read",
        arguments={"radar_device_id": device_id},
        context=context,
    )
    quality = adapter.call_tool(
        task_id="task-001",
        tool_name="quality.assess",
        arguments=radar.output,
        context=context,
    )

    assert radar.output["snapshots"]
    assert quality.output["night_summary"]["radar_device_id"] == device_id
    assert quality.output["night_summary"]["data_quality_status"] == "good"


def test_tool_audit_events_persist_through_task_service() -> None:
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(RadarSubject(subject_id="elder-001", display_name="Elder"))
    store.save_role_binding(
        RadarUserRoleBinding(
            role_binding_id="family-001",
            user_id="user-001",
            subject_id="elder-001",
            role="family",
            display_name="Family",
        )
    )
    store.save_device(
        RadarDevice(
            radar_device_id="radar-001",
            display_name="Radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-001",
        )
    )
    service = TaskService(store, require_authorization=False)
    task = service.create_task(
        subject_id="elder-001",
        radar_device_id="radar-001",
        role="family",
        role_binding_ids=["family-001"],
    )
    registry = build_default_registry({"family.notify": in_memory_state_writer("sent")})
    from sleepagent.radar_agent.schemas import HumanConfirmationRequest
    from sleepagent.radar_agent.runtime import RadarTaskStatus

    service.transition_task(task.task_id, RadarTaskStatus.RUNNING)
    service.request_confirmation(
        task.task_id,
        HumanConfirmationRequest(
            confirmation_id="confirm-001",
            task_id=task.task_id,
            action_type="family.notify",
            requested_role="family",
            reason="Family notification requires approval.",
        ),
    )
    service.resolve_confirmation(
        task.task_id,
        "confirm-001",
        approved=True,
        actor_id="user-001",
        actor_role="family",
    )
    executor = OrchestratorToolExecutor(
        registry,
        audit_sink=task_service_audit_sink(service),
        confirmation_verifier=task_service_confirmation_verifier(service),
    )
    confirmation = ToolConfirmation(
        confirmation_id="confirm-001",
        task_id=task.task_id,
        action_type="family.notify",
        confirmed_by="user-001",
    )
    executor.execute(
        LLMToolRequest(
            request_id="req-001",
            task_id=task.task_id,
            tool_name="family.notify",
            arguments={"subject_id": "elder-001", "content": "Call family"},
        ),
        ToolExecutionContext(
            task_id=task.task_id,
            trace_id=task.trace_id,
            actor_id="orchestrator",
            actor_role="family",
            permissions={ToolPermission.EXTERNAL_ACTION},
            allowed_tools={"family.notify"},
            confirmation=confirmation,
        ),
    )

    tool_logs = [log for log in store.list_audit_logs(task.task_id) if log.action.startswith("tool_")]
    assert [log.action for log in tool_logs] == ["tool_requested", "tool_completed"]
    assert tool_logs[-1].payload["input_summary"]["content"] == "[REDACTED]"
