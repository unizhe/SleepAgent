from __future__ import annotations

import pytest
from pydantic import ValidationError

from sleepagent.config import (
    ObservationSemanticsVersion,
    ReportPipelineMode,
    SleepBackendSettings,
)


pytestmark = pytest.mark.unit


def _environment() -> dict[str, str]:
    return {
        "SLEEPAGENT_BACKEND_PROFILE": "g1-characterization",
        "SLEEPAGENT_BACKEND_DEPLOYMENT_MODE": "test",
        "SLEEPAGENT_BACKEND_PROCESS_ROLE": "api",
        "SLEEPAGENT_BACKEND_DATA_MODE": "replay",
        "SLEEPAGENT_BACKEND_DATABASE_DSN": (
            "postgresql://api:secret@postgres/replay_db"
        ),
        "SLEEPAGENT_BACKEND_DATABASE_IDENTITY": "replay_db",
        "SLEEPAGENT_BACKEND_DATABASE_ROLE": "sleepagent_api_replay",
        "SLEEPAGENT_BACKEND_SERVICE_PRINCIPAL_ID": "sleepagent-api-test",
        "SLEEPAGENT_BACKEND_DATABASE_SCOPE": "replay",
        "SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES": "replay:g1",
        "SLEEPAGENT_BACKEND_SERVICE_CREDENTIAL_REF": "test:service",
        "SLEEPAGENT_BACKEND_SIGNING_KEY_REF": "test:signing",
        "SLEEPAGENT_BACKEND_ENCRYPTION_KEY_REF": "test:encryption",
    }


def test_remediation_switch_defaults_use_v2_and_shared_only() -> None:
    settings = SleepBackendSettings.from_environment(_environment())

    assert settings.observation_semantics_version is ObservationSemanticsVersion.V2
    assert settings.report_pipeline_mode is ReportPipelineMode.SHARED_ONLY
    assert settings.emit_legacy_report_compatibility is False
    assert settings.acquisition_scheduler_enabled is False
    assert settings.live_delivery_enabled is False


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("OBSERVATION_SEMANTICS_VERSION", "latest"),
        ("REPORT_PIPELINE_MODE", "shared"),
        ("EMIT_LEGACY_REPORT_COMPATIBILITY", "yes"),
        ("ACQUISITION_SCHEDULER_ENABLED", "1"),
        ("LIVE_DELIVERY_ENABLED", "TRUE"),
    ),
)
def test_remediation_switch_environment_values_fail_closed(
    name: str,
    value: str,
) -> None:
    environment = _environment()
    environment[f"SLEEPAGENT_BACKEND_{name}"] = value

    with pytest.raises((ValueError, ValidationError)):
        SleepBackendSettings.from_environment(environment)


def test_unprefixed_environment_values_are_not_silently_reinterpreted() -> None:
    environment = _environment()
    environment.update(
        {
            "OBSERVATION_SEMANTICS_VERSION": "v2",
            "REPORT_PIPELINE_MODE": "shared_only",
            "LIVE_DELIVERY_ENABLED": "true",
        }
    )

    settings = SleepBackendSettings.from_environment(environment)

    assert settings.observation_semantics_version is ObservationSemanticsVersion.V2
    assert settings.report_pipeline_mode is ReportPipelineMode.SHARED_ONLY
    assert settings.live_delivery_enabled is False


def test_explicit_v1_remains_the_observation_rollback_contract() -> None:
    environment = _environment()
    environment["SLEEPAGENT_BACKEND_OBSERVATION_SEMANTICS_VERSION"] = "v1"

    settings = SleepBackendSettings.from_environment(environment)

    assert settings.observation_semantics_version is ObservationSemanticsVersion.V1


def test_shared_compat_remains_an_explicit_report_rollback_contract() -> None:
    environment = _environment()
    environment.update(
        {
            "SLEEPAGENT_BACKEND_REPORT_PIPELINE_MODE": "shared_compat",
            "SLEEPAGENT_BACKEND_EMIT_LEGACY_REPORT_COMPATIBILITY": "true",
        }
    )

    settings = SleepBackendSettings.from_environment(environment)

    assert settings.report_pipeline_mode is ReportPipelineMode.SHARED_COMPAT
    assert settings.emit_legacy_report_compatibility is True


@pytest.mark.parametrize(
    ("mode", "emit"),
    (("shared_only", "true"), ("shared_compat", "false"), ("shadow", "false")),
)
def test_report_mode_and_compatibility_write_switch_fail_closed(
    mode: str,
    emit: str,
) -> None:
    environment = _environment()
    environment["SLEEPAGENT_BACKEND_REPORT_PIPELINE_MODE"] = mode
    environment["SLEEPAGENT_BACKEND_EMIT_LEGACY_REPORT_COMPATIBILITY"] = emit

    with pytest.raises((ValueError, ValidationError)):
        SleepBackendSettings.from_environment(environment)


def test_direct_boolean_switch_values_are_strictly_typed() -> None:
    settings = SleepBackendSettings.from_environment(_environment())

    with pytest.raises(ValidationError):
        SleepBackendSettings.model_validate(
            {
                **settings.model_dump(),
                "live_delivery_enabled": "false",
            }
        )


def test_scheduler_opt_in_requires_worker_and_scheduled_queue() -> None:
    environment = _environment()
    environment.update(
        {
            "SLEEPAGENT_BACKEND_PROCESS_ROLE": "worker",
            "SLEEPAGENT_BACKEND_ENABLED_SURFACES": "",
            "SLEEPAGENT_BACKEND_WORKER_QUEUES": "night.finalization_scan",
            "SLEEPAGENT_BACKEND_PROVIDER_MODE": "disabled",
            "SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED": "true",
        }
    )

    settings = SleepBackendSettings.from_environment(environment)

    assert settings.acquisition_scheduler_enabled is True
    assert settings.worker_queues == ("night.finalization_scan",)


def test_perceptor_loopback_origin_is_explicit_and_https_only() -> None:
    values = _environment()
    values.update(
        {
            "SLEEPAGENT_BACKEND_DATA_MODE": "live",
            "SLEEPAGENT_BACKEND_DATABASE_SCOPE": "live",
            "SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES": "live:g7",
            "SLEEPAGENT_BACKEND_PROCESS_ROLE": "worker",
            "SLEEPAGENT_BACKEND_ENABLED_SURFACES": "",
            "SLEEPAGENT_BACKEND_WORKER_QUEUES": "perceptor.history_overlap_pull",
            "SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED": "true",
            "SLEEPAGENT_BACKEND_PROVIDER_MODE": "live",
            "SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_ID_REF": "env:CLIENT_ID",
            "SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_SECRET_REF": "env:CLIENT_SECRET",
            "SLEEPAGENT_BACKEND_PERCEPTOR_PROVIDER_ACCOUNT_ID": "account",
            "SLEEPAGENT_BACKEND_PERCEPTOR_NAMESPACE_ID": "live:g7",
            "SLEEPAGENT_BACKEND_PERCEPTOR_BASE_URL": "https://127.0.0.1:18443",
        }
    )

    settings = SleepBackendSettings.from_environment(values)

    assert settings.perceptor_base_url == "https://127.0.0.1:18443"
    with pytest.raises(ValueError, match="HTTPS origin"):
        SleepBackendSettings.from_environment(
            {**values, "SLEEPAGENT_BACKEND_PERCEPTOR_BASE_URL": "http://127.0.0.1:18443"}
        )


def test_scheduled_queue_without_feature_gate_fails_closed() -> None:
    environment = _environment()
    environment.update(
        {
            "SLEEPAGENT_BACKEND_PROCESS_ROLE": "worker",
            "SLEEPAGENT_BACKEND_ENABLED_SURFACES": "",
            "SLEEPAGENT_BACKEND_WORKER_QUEUES": "night.finalization_scan",
        }
    )

    with pytest.raises((ValueError, ValidationError), match="opt-in"):
        SleepBackendSettings.from_environment(environment)


def test_scheduled_perceptor_pull_requires_dedicated_client_id_reference() -> None:
    environment = _environment()
    environment.update(
        {
            "SLEEPAGENT_BACKEND_PROCESS_ROLE": "worker",
            "SLEEPAGENT_BACKEND_DATA_MODE": "live",
            "SLEEPAGENT_BACKEND_DATABASE_SCOPE": "live",
            "SLEEPAGENT_BACKEND_NAMESPACE_PREFIXES": "live:g7",
            "SLEEPAGENT_BACKEND_ENABLED_SURFACES": "",
            "SLEEPAGENT_BACKEND_WORKER_QUEUES": "perceptor.history_overlap_pull",
            "SLEEPAGENT_BACKEND_PROVIDER_MODE": "live",
            "SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED": "true",
            "SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_SECRET_REF": "test:vendor-secret",
            "SLEEPAGENT_BACKEND_PERCEPTOR_PROVIDER_ACCOUNT_ID": "vendor-account",
            "SLEEPAGENT_BACKEND_PERCEPTOR_NAMESPACE_ID": "live:g7",
        }
    )
    with pytest.raises((ValueError, ValidationError), match="live provider"):
        SleepBackendSettings.from_environment(environment)

    environment["SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_ID_REF"] = "test:vendor-id"
    settings = SleepBackendSettings.from_environment(environment)

    assert settings.perceptor_client_id_ref == "test:vendor-id"
