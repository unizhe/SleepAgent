# 本模块统一解析进程配置与密钥引用，并以强类型设置拒绝不完整部署。
"""Typed, fail-closed configuration for the modular backend.

Only this module translates deployment environment variables into backend
configuration.  Domain and transport modules receive a frozen settings object
instead of reading process environment directly.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from hashlib import sha256
from typing import Mapping, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from sleepagent.persistence.migrations import LATEST_SCHEMA_VERSION


SETTINGS_PREFIX = "SLEEPAGENT_BACKEND_"


class DeploymentMode(str, Enum):
    TEST = "test"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class DataMode(str, Enum):
    LIVE = "live"
    REPLAY = "replay"


class ProcessRole(str, Enum):
    API = "api"
    WORKER = "worker"
    MIGRATION = "migration"


class ApiSurface(str, Enum):
    PUBLIC_V1 = "public_v1"
    PRODUCT = "product"
    INTERNAL = "internal"
    DEMO = "demo"
    PERCEPTOR_PUSH = "perceptor_push"


class ProviderMode(str, Enum):
    DISABLED = "disabled"
    FAKE = "fake"
    LIVE = "live"


class ModelMode(str, Enum):
    DISABLED = "disabled"
    DETERMINISTIC = "deterministic"
    LIVE = "live"


class ObservationSemanticsVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"


class ReportPipelineMode(str, Enum):
    LEGACY = "legacy"
    SHARED_COMPAT = "shared_compat"
    SHADOW = "shadow"
    SHARED_ONLY = "shared_only"


class SleepBackendSettings(BaseModel):
    """One deployment-scoped backend configuration.

    ``database_scope`` is an explicit credential/database attestation.  It is
    intentionally separate from ``namespace_prefixes`` so live and replay
    deployments cannot claim isolation merely by changing a row attribute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str = Field(min_length=1)
    deployment_mode: DeploymentMode
    process_role: ProcessRole
    data_mode: DataMode
    database_dsn: SecretStr
    database_identity: str = Field(min_length=1)
    database_role: str = Field(min_length=1)
    service_principal_id: str = Field(min_length=1)
    database_scope: DataMode
    namespace_prefixes: tuple[str, ...]
    enabled_surfaces: frozenset[ApiSurface] = frozenset()
    worker_queues: tuple[str, ...] = ()
    # Connector/data-provider authority and model implementation are separate
    # deployment dimensions. Replay may therefore use a fake data provider
    # with an explicitly opted-in live Product model.
    provider_mode: ProviderMode = ProviderMode.DISABLED
    model_mode: ModelMode = ModelMode.DISABLED
    # Observation V2 is the authoritative default after the bounded G2C
    # cutover proof.  Explicit V1 remains available as the rollback contract;
    # later remediation switches remain default-preserving until their goals.
    observation_semantics_version: ObservationSemanticsVersion = (
        ObservationSemanticsVersion.V2
    )
    report_pipeline_mode: ReportPipelineMode = ReportPipelineMode.SHARED_ONLY
    emit_legacy_report_compatibility: bool = Field(default=False, strict=True)
    acquisition_scheduler_enabled: bool = Field(default=False, strict=True)
    live_delivery_enabled: bool = Field(default=False, strict=True)
    service_credential_ref: str = Field(default="unconfigured", min_length=1)
    signing_key_ref: str = Field(min_length=1)
    encryption_key_ref: str = Field(min_length=1)
    perceptor_client_secret_ref: str | None = None
    perceptor_provider_account_id: str | None = Field(default=None, min_length=1)
    perceptor_namespace_id: str | None = Field(default=None, min_length=1)
    perceptor_namespace_generation: int = Field(default=1, ge=1)
    perceptor_authorization_epoch: int = Field(default=1, ge=0)
    perceptor_freshness_seconds: int = Field(default=300, ge=30, le=3_600)
    internal_auth_token: SecretStr | None = None
    demo_controller_token: SecretStr | None = None
    supported_schema_min: int = Field(
        default=LATEST_SCHEMA_VERSION,
        ge=1,
    )
    supported_schema_max: int = Field(
        default=LATEST_SCHEMA_VERSION,
        ge=1,
    )
    pool_min_size: int = Field(default=1, ge=0, le=50)
    pool_max_size: int = Field(default=8, ge=1, le=100)
    pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    lock_timeout_ms: int = Field(default=2_000, ge=1, le=60_000)
    statement_timeout_ms: int = Field(default=15_000, ge=1, le=300_000)
    idle_transaction_timeout_ms: int = Field(
        default=10_000,
        ge=1,
        le=300_000,
    )
    raw_retention_seconds: int = Field(
        default=2,
        ge=1,
        le=31_536_000,
    )
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_compressed_body_bytes: int = Field(
        default=1_048_576,
        ge=1_024,
        le=16_777_216,
    )
    max_decompressed_body_bytes: int = Field(
        default=2_097_152,
        ge=1_024,
        le=33_554_432,
    )
    max_json_depth: int = Field(default=32, ge=2, le=128)
    max_json_members: int = Field(default=20_000, ge=10, le=1_000_000)
    actor_assertion_issuer: str = Field(default="sleepagent-bff-v1", min_length=1)
    actor_assertion_audience: str = Field(default="sleepagent-backend", min_length=1)
    actor_assertion_key_id: str = Field(default="primary", min_length=1)

    @model_validator(mode="after")
    def validate_deployment_contract(self) -> Self:
        shared_only = self.report_pipeline_mode is ReportPipelineMode.SHARED_ONLY
        if shared_only == self.emit_legacy_report_compatibility:
            raise ValueError(
                "shared_only must disable legacy report compatibility; "
                "rollback/shadow modes must enable it explicitly"
            )
        parsed = urlsplit(self.database_dsn.get_secret_value())
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("backend authority must use a PostgreSQL DSN")
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("database DSN must identify a host and database")
        if self.database_scope != self.data_mode:
            raise ValueError("database_scope must equal deployment data_mode")
        expected_prefix = f"{self.data_mode.value}:"
        if not self.namespace_prefixes or any(
            not value.startswith(expected_prefix)
            for value in self.namespace_prefixes
        ):
            raise ValueError(
                f"all namespace prefixes must start with {expected_prefix!r}"
            )
        if len(set(self.namespace_prefixes)) != len(self.namespace_prefixes):
            raise ValueError("namespace prefixes must be unique")
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")
        if (
            self.supported_schema_min != LATEST_SCHEMA_VERSION
            or self.supported_schema_max != LATEST_SCHEMA_VERSION
        ):
            raise ValueError(
                "supported schema range must exactly match the release target"
            )
        if self.process_role == ProcessRole.API:
            if self.worker_queues:
                raise ValueError("API process cannot own worker queues")
            if self.model_mode != ModelMode.DISABLED:
                raise ValueError("API process cannot load a model")
            if self.provider_mode != ProviderMode.DISABLED:
                raise ValueError("API process cannot load a provider")
            if ApiSurface.DEMO in self.enabled_surfaces:
                if self.enabled_surfaces != frozenset({ApiSurface.DEMO}):
                    raise ValueError(
                        "demo API profile may expose only the demo surface"
                    )
            elif self.enabled_surfaces.intersection(
                {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
            ):
                allowed_bff = {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
                if ApiSurface.PERCEPTOR_PUSH in self.enabled_surfaces:
                    allowed_bff.add(ApiSurface.PERCEPTOR_PUSH)
                if self.enabled_surfaces != frozenset(allowed_bff):
                    raise ValueError(
                        "BFF API profile may expose only public_v1, product, "
                        "and perceptor_push"
                    )
            elif self.enabled_surfaces not in {
                frozenset({ApiSurface.INTERNAL}),
                frozenset({ApiSurface.PERCEPTOR_PUSH}),
            }:
                raise ValueError(
                    "API profile must be demo-only, BFF-only, Perceptor-only, "
                    "or internal-only"
                )
        elif self.process_role == ProcessRole.MIGRATION:
            if self.worker_queues or self.enabled_surfaces:
                raise ValueError(
                    "migration process cannot expose API surfaces or queues"
                )
            if self.model_mode != ModelMode.DISABLED:
                raise ValueError("migration process cannot load a model")
            if self.provider_mode != ProviderMode.DISABLED:
                raise ValueError("migration process cannot load a provider")
        else:
            if self.enabled_surfaces:
                raise ValueError("worker process cannot expose API surfaces")
            if not self.worker_queues:
                raise ValueError("worker process requires at least one queue")
        if ApiSurface.DEMO in self.enabled_surfaces:
            if self.deployment_mode == DeploymentMode.PRODUCTION:
                raise ValueError("production cannot expose the demo surface")
            if self.data_mode != DataMode.REPLAY:
                raise ValueError("demo surface requires replay data mode")
            demo_token = (
                None
                if self.demo_controller_token is None
                else self.demo_controller_token.get_secret_value()
            )
            if demo_token is None or len(demo_token.encode("utf-8")) < 32:
                raise ValueError(
                    "demo surface requires a strong demo-controller token"
                )
        if ApiSurface.INTERNAL in self.enabled_surfaces:
            internal_token = (
                None
                if self.internal_auth_token is None
                else self.internal_auth_token.get_secret_value()
            )
            minimum = 32 if self.deployment_mode == DeploymentMode.PRODUCTION else 16
            if internal_token is None or len(internal_token.encode("utf-8")) < minimum:
                raise ValueError(
                    "internal surface requires a dedicated strong token"
                )
        if ApiSurface.PERCEPTOR_PUSH in self.enabled_surfaces:
            if self.data_mode != DataMode.LIVE:
                raise ValueError("perceptor_push surface requires live data mode")
            required_perceptor = {
                "perceptor_client_secret_ref": self.perceptor_client_secret_ref,
                "perceptor_provider_account_id": self.perceptor_provider_account_id,
                "perceptor_namespace_id": self.perceptor_namespace_id,
            }
            missing_perceptor = sorted(
                name for name, value in required_perceptor.items() if not value
            )
            if missing_perceptor:
                raise ValueError(
                    "perceptor_push surface requires: "
                    + ", ".join(missing_perceptor)
                )
            assert self.perceptor_namespace_id is not None
            if self.perceptor_namespace_id not in self.namespace_prefixes:
                raise ValueError(
                    "perceptor namespace must be one configured namespace prefix"
                )
        if self.deployment_mode == DeploymentMode.PRODUCTION:
            if self.data_mode != DataMode.LIVE:
                raise ValueError("production requires live data mode")
            if self.provider_mode == ProviderMode.FAKE:
                raise ValueError("production cannot use a fake provider")
            if self.model_mode == ModelMode.DETERMINISTIC:
                raise ValueError("production cannot use a deterministic model")
            if _looks_like_default_ref(self.signing_key_ref):
                raise ValueError("production signing key reference is unsafe")
            if _looks_like_default_ref(self.encryption_key_ref):
                raise ValueError("production encryption key reference is unsafe")
            if _looks_like_default_ref(self.service_credential_ref):
                raise ValueError("production service credential reference is unsafe")
        return self

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SleepBackendSettings":
        env = os.environ if environment is None else environment

        def required(name: str) -> str:
            value = env.get(f"{SETTINGS_PREFIX}{name}", "").strip()
            if not value:
                raise ValueError(f"{SETTINGS_PREFIX}{name} is required")
            return value

        profile = required("PROFILE")
        role = ProcessRole(required("PROCESS_ROLE"))
        mode = DeploymentMode(required("DEPLOYMENT_MODE"))
        data_mode = DataMode(required("DATA_MODE"))
        default_surfaces = (
            "public_v1,product" if role == ProcessRole.API else ""
        )
        surfaces = frozenset(
            ApiSurface(value)
            for value in _csv(
                env.get(f"{SETTINGS_PREFIX}ENABLED_SURFACES", default_surfaces)
            )
        )
        queues = tuple(
            _csv(env.get(f"{SETTINGS_PREFIX}WORKER_QUEUES", ""))
        )
        internal_token = env.get(f"{SETTINGS_PREFIX}INTERNAL_AUTH_TOKEN")
        demo_token = env.get(f"{SETTINGS_PREFIX}DEMO_CONTROLLER_TOKEN")
        return cls(
            profile=profile,
            deployment_mode=mode,
            process_role=role,
            data_mode=data_mode,
            database_dsn=SecretStr(required("DATABASE_DSN")),
            database_identity=required("DATABASE_IDENTITY"),
            database_role=required("DATABASE_ROLE"),
            service_principal_id=required("SERVICE_PRINCIPAL_ID"),
            database_scope=DataMode(required("DATABASE_SCOPE")),
            namespace_prefixes=tuple(
                _csv(required("NAMESPACE_PREFIXES"))
            ),
            enabled_surfaces=surfaces,
            worker_queues=queues,
            provider_mode=ProviderMode(
                env.get(
                    f"{SETTINGS_PREFIX}PROVIDER_MODE",
                    "disabled",
                ).strip()
            ),
            model_mode=ModelMode(
                env.get(
                    f"{SETTINGS_PREFIX}MODEL_MODE",
                    "disabled",
                ).strip()
            ),
            observation_semantics_version=ObservationSemanticsVersion(
                env.get(
                    f"{SETTINGS_PREFIX}OBSERVATION_SEMANTICS_VERSION",
                    "v2",
                ).strip()
            ),
            report_pipeline_mode=ReportPipelineMode(
                env.get(
                    f"{SETTINGS_PREFIX}REPORT_PIPELINE_MODE",
                    "shared_only",
                ).strip()
            ),
            emit_legacy_report_compatibility=_boolean(
                env, "EMIT_LEGACY_REPORT_COMPATIBILITY", False
            ),
            acquisition_scheduler_enabled=_boolean(
                env, "ACQUISITION_SCHEDULER_ENABLED", False
            ),
            live_delivery_enabled=_boolean(
                env, "LIVE_DELIVERY_ENABLED", False
            ),
            service_credential_ref=required("SERVICE_CREDENTIAL_REF"),
            signing_key_ref=required("SIGNING_KEY_REF"),
            encryption_key_ref=required("ENCRYPTION_KEY_REF"),
            perceptor_client_secret_ref=(
                env.get(f"{SETTINGS_PREFIX}PERCEPTOR_CLIENT_SECRET_REF") or None
            ),
            perceptor_provider_account_id=(
                env.get(f"{SETTINGS_PREFIX}PERCEPTOR_PROVIDER_ACCOUNT_ID") or None
            ),
            perceptor_namespace_id=(
                env.get(f"{SETTINGS_PREFIX}PERCEPTOR_NAMESPACE_ID") or None
            ),
            perceptor_namespace_generation=_integer(
                env, "PERCEPTOR_NAMESPACE_GENERATION", 1
            ),
            perceptor_authorization_epoch=_integer(
                env, "PERCEPTOR_AUTHORIZATION_EPOCH", 1
            ),
            perceptor_freshness_seconds=_integer(
                env, "PERCEPTOR_FRESHNESS_SECONDS", 300
            ),
            internal_auth_token=(
                None if internal_token is None else SecretStr(internal_token)
            ),
            demo_controller_token=(
                None if demo_token is None else SecretStr(demo_token)
            ),
            supported_schema_min=_integer(
                env,
                "SUPPORTED_SCHEMA_MIN",
                LATEST_SCHEMA_VERSION,
            ),
            supported_schema_max=_integer(
                env,
                "SUPPORTED_SCHEMA_MAX",
                LATEST_SCHEMA_VERSION,
            ),
            pool_min_size=_integer(env, "POOL_MIN_SIZE", 1),
            pool_max_size=_integer(env, "POOL_MAX_SIZE", 8),
            pool_timeout_seconds=_floating(env, "POOL_TIMEOUT_SECONDS", 5.0),
            lock_timeout_ms=_integer(env, "LOCK_TIMEOUT_MS", 2_000),
            statement_timeout_ms=_integer(
                env,
                "STATEMENT_TIMEOUT_MS",
                15_000,
            ),
            idle_transaction_timeout_ms=_integer(
                env,
                "IDLE_TRANSACTION_TIMEOUT_MS",
                10_000,
            ),
            raw_retention_seconds=_integer(
                env,
                "RAW_RETENTION_SECONDS",
                2,
            ),
            request_timeout_seconds=_floating(
                env,
                "REQUEST_TIMEOUT_SECONDS",
                15.0,
            ),
            max_compressed_body_bytes=_integer(
                env,
                "MAX_COMPRESSED_BODY_BYTES",
                1_048_576,
            ),
            max_decompressed_body_bytes=_integer(
                env,
                "MAX_DECOMPRESSED_BODY_BYTES",
                2_097_152,
            ),
            max_json_depth=_integer(env, "MAX_JSON_DEPTH", 32),
            max_json_members=_integer(env, "MAX_JSON_MEMBERS", 20_000),
            actor_assertion_issuer=env.get(
                f"{SETTINGS_PREFIX}ACTOR_ASSERTION_ISSUER",
                "sleepagent-bff-v1",
            ).strip(),
            actor_assertion_audience=env.get(
                f"{SETTINGS_PREFIX}ACTOR_ASSERTION_AUDIENCE",
                "sleepagent-backend",
            ).strip(),
            actor_assertion_key_id=env.get(
                f"{SETTINGS_PREFIX}ACTOR_ASSERTION_KEY_ID",
                "primary",
            ).strip(),
        )

    def public_fingerprint(self) -> str:
        """Hash non-secret configuration for manifests and audit receipts."""

        parsed = urlsplit(self.database_dsn.get_secret_value())
        safe_database = {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "database": parsed.path.strip("/"),
        }
        payload = self.model_dump(
            mode="json",
            exclude={
                "database_dsn",
                "internal_auth_token",
                "demo_controller_token",
            },
        )
        payload["database"] = safe_database
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    return int(env.get(f"{SETTINGS_PREFIX}{name}", str(default)))


def _floating(env: Mapping[str, str], name: str, default: float) -> float:
    return float(env.get(f"{SETTINGS_PREFIX}{name}", str(default)))


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(
        f"{SETTINGS_PREFIX}{name}",
        "true" if default else "false",
    ).strip()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(
        f"{SETTINGS_PREFIX}{name} must be exactly 'true' or 'false'"
    )


def _looks_like_default_ref(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {
        "default",
        "development",
        "dev",
        "local",
        "test",
        "changeme",
    } or lowered.startswith(("dev:", "test:", "local:"))


__all__ = [
    "ApiSurface",
    "DataMode",
    "DeploymentMode",
    "ModelMode",
    "ObservationSemanticsVersion",
    "ProcessRole",
    "ProviderMode",
    "ReportPipelineMode",
    "SETTINGS_PREFIX",
    "SleepBackendSettings",
]


# 密钥引用与解析和进程配置共享同一安全边界。
"""Fail-closed key-reference adapter for backend identity and encryption.

Production accepts environment- or file-backed references.  Deterministic
material is available only to explicit test/development profiles and is useful
for reproducible replay fixtures; the reference, never the secret, appears in
dependency manifests.
"""

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

class BackendKeyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActorKeyMaterial:
    key_id: str
    public_key_pem: bytes
    private_key: Ed25519PrivateKey | None = None

    @property
    def public_key_sha256(self) -> str:
        return hashlib.sha256(self.public_key_pem).hexdigest()


class BackendKeyProvider:
    def __init__(
        self,
        deployment_mode: DeploymentMode,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.deployment_mode = deployment_mode
        self.environment = os.environ if environment is None else environment

    def secret(self, reference: str, *, purpose: str, minimum_bytes: int) -> bytes:
        value = self._resolve(reference, purpose=purpose)
        if len(value) < minimum_bytes:
            raise BackendKeyError(f"{purpose} key material is too short")
        return value

    def actor_key(self, reference: str, *, key_id: str) -> ActorKeyMaterial:
        if reference.startswith("test:"):
            self._require_nonproduction_test_reference(reference)
            private_key = Ed25519PrivateKey.from_private_bytes(
                hashlib.sha256(
                    f"sleepagent:actor-assertion:{reference}".encode("utf-8")
                ).digest()
            )
            public_pem = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return ActorKeyMaterial(
                key_id=key_id,
                public_key_pem=public_pem,
                private_key=private_key,
            )
        public_pem = self._resolve(reference, purpose="actor verification")
        try:
            key = serialization.load_pem_public_key(public_pem)
        except (TypeError, ValueError) as exc:
            raise BackendKeyError("actor verification key is not valid PEM") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise BackendKeyError("actor verification key must be Ed25519")
        canonical = key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return ActorKeyMaterial(key_id=key_id, public_key_pem=canonical)

    def actor_verification_key(
        self,
        reference: str,
        *,
        key_id: str,
    ) -> ActorKeyMaterial:
        """Load public-only actor verification material for an API process."""

        if reference.startswith("test:"):
            raise BackendKeyError(
                "actor verification reference must not derive private key material"
            )
        material = self.actor_key(reference, key_id=key_id)
        if material.private_key is not None:
            raise BackendKeyError(
                "actor verification reference unexpectedly contained a private key"
            )
        return material

    def encryption_key(self, reference: str) -> bytes:
        raw = self._resolve(reference, purpose="encryption")
        if reference.startswith("test:"):
            return hashlib.sha256(raw).digest()
        try:
            decoded = base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise BackendKeyError("encryption key must be canonical base64") from exc
        if len(decoded) != 32:
            raise BackendKeyError("encryption key must decode to 32 bytes")
        if base64.b64encode(decoded) != raw.strip():
            raise BackendKeyError("encryption key must use canonical base64")
        return decoded

    def _resolve(self, reference: str, *, purpose: str) -> bytes:
        if reference.startswith("test:"):
            self._require_nonproduction_test_reference(reference)
            return hashlib.sha256(
                f"sleepagent:{purpose}:{reference}".encode("utf-8")
            ).digest()
        if reference.startswith("env:"):
            name = reference.removeprefix("env:")
            if not name or name not in self.environment:
                raise BackendKeyError(f"{purpose} environment reference is unavailable")
            value = self.environment[name].encode("utf-8")
            if not value:
                raise BackendKeyError(f"{purpose} environment secret is empty")
            return value
        if reference.startswith("file:"):
            raw_path = reference.removeprefix("file:")
            path = Path(raw_path)
            if not path.is_absolute() or path.is_symlink():
                raise BackendKeyError(f"{purpose} file reference must be absolute and not a symlink")
            try:
                value = path.read_bytes()
            except OSError as exc:
                raise BackendKeyError(f"{purpose} file reference is unavailable") from exc
            if not value:
                raise BackendKeyError(f"{purpose} key file is empty")
            return value
        raise BackendKeyError(
            f"{purpose} reference must use env:, file:, or explicit test: scheme"
        )

    def _require_nonproduction_test_reference(self, reference: str) -> None:
        if self.deployment_mode == DeploymentMode.PRODUCTION:
            raise BackendKeyError(
                f"deterministic key reference {reference!r} is forbidden in production"
            )


__all__ = [
    "ActorKeyMaterial",
    "BackendKeyError",
    "BackendKeyProvider",
]
