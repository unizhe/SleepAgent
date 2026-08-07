"""Environment-owned runtime for the unified Perceptor push pipeline."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    connect_postgres_store,
)
from sleepagent.sleep_domain import (
    AdministrativeAccessPolicy,
    AdapterDeploymentStatus,
    CandidatePromotionService,
    ControlledAdapterRegistry,
    DataMode,
    DomainNamespace,
    ProviderAccountRecord,
    RawPayloadEncryptionPolicy,
    SleepDomainRepository,
    perceptor_v1_registration,
)

from .push_ingestion import (
    PerceptorNormalizationWorker,
    PerceptorPushCompatibilityProfile,
    PerceptorPushConfigurationError,
    PerceptorPushHttpLimits,
    PerceptorPushIngestionService,
    PerceptorPushRateLimiter,
    SignatureMode,
)


PERCEPTOR_PUSH_ENABLED_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_ENABLED"
PERCEPTOR_PUSH_NAMESPACE_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_NAMESPACE"
PERCEPTOR_PUSH_ACCOUNT_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_PROVIDER_ACCOUNT_ID"
PERCEPTOR_PUSH_PROFILE_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_PROFILE_ID"
PERCEPTOR_PUSH_SECRET_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_SIGNING_SECRET"
PERCEPTOR_PUSH_ALGORITHM_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_ALGORITHM"
PERCEPTOR_PUSH_MODE_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_SIGNATURE_MODE"
PERCEPTOR_PUSH_ENVIRONMENT_ENV = "SLEEPAGENT_PERCEPTOR_PUSH_ENVIRONMENT"
PERCEPTOR_PUSH_CONFIG_FINGERPRINT_ENV = (
    "SLEEPAGENT_PERCEPTOR_PUSH_CONFIGURATION_FINGERPRINT"
)
PERCEPTOR_PUSH_ADAPTER_SHA256_ENV = (
    "SLEEPAGENT_PERCEPTOR_PUSH_ADAPTER_ARTIFACT_SHA256"
)
PERCEPTOR_PUSH_TIMESTAMP_WINDOW_ENV = (
    "SLEEPAGENT_PERCEPTOR_PUSH_TIMESTAMP_WINDOW_SECONDS"
)
PERCEPTOR_PUSH_FUTURE_SKEW_ENV = (
    "SLEEPAGENT_PERCEPTOR_PUSH_FUTURE_SKEW_SECONDS"
)
PERCEPTOR_PUSH_WORKER_ENABLED_ENV = (
    "SLEEPAGENT_PERCEPTOR_PUSH_WORKER_ENABLED"
)
RADAR_AGENT_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"
RADAR_AGENT_SQLITE_PATH_ENV = "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH"
DEFAULT_RADAR_AGENT_SQLITE_PATH = "/tmp/sleepagent_radar_agent.sqlite3"
LOGGER = logging.getLogger(__name__)


@dataclass
class PerceptorPushRuntime:
    store: RadarPersistenceStore
    repository: SleepDomainRepository
    registry: ControlledAdapterRegistry
    ingestion: PerceptorPushIngestionService
    worker: PerceptorNormalizationWorker
    owns_store: bool = True

    def close(self) -> None:
        self.registry.close()
        if self.owns_store:
            self.store.connection.close()


class PerceptorPushWorkerLoop:
    """Process-local lease consumer; leases make restart/reclaim durable."""

    def __init__(self, runtime: PerceptorPushRuntime) -> None:
        self.runtime = runtime
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="perceptor-normalization-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.runtime.worker.run_once(
                    now=datetime.now(timezone.utc)
                )
            except Exception:
                LOGGER.exception(
                    "Perceptor normalization attempt failed; "
                    "the durable lease will be reclaimable"
                )
                self._stop.wait(0.25)
                continue
            if not processed:
                self._stop.wait(0.25)


def build_perceptor_push_runtime_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    store: RadarPersistenceStore | None = None,
) -> PerceptorPushRuntime:
    values = os.environ if environ is None else environ
    if not _bool(values.get(PERCEPTOR_PUSH_ENABLED_ENV)):
        raise PerceptorPushConfigurationError(
            f"{PERCEPTOR_PUSH_ENABLED_ENV}=true is required"
        )
    required_names = (
        PERCEPTOR_PUSH_NAMESPACE_ENV,
        PERCEPTOR_PUSH_ACCOUNT_ENV,
        PERCEPTOR_PUSH_PROFILE_ENV,
        PERCEPTOR_PUSH_SECRET_ENV,
        PERCEPTOR_PUSH_ALGORITHM_ENV,
        PERCEPTOR_PUSH_MODE_ENV,
        PERCEPTOR_PUSH_ENVIRONMENT_ENV,
        PERCEPTOR_PUSH_CONFIG_FINGERPRINT_ENV,
        PERCEPTOR_PUSH_ADAPTER_SHA256_ENV,
    )
    missing = [name for name in required_names if not values.get(name)]
    if missing:
        raise PerceptorPushConfigurationError(
            "Perceptor push requires explicit configuration: "
            + ", ".join(missing)
        )

    namespace_text = str(values[PERCEPTOR_PUSH_NAMESPACE_ENV])
    try:
        mode = DataMode(namespace_text.split(":", 1)[0])
        signature_mode = SignatureMode(str(values[PERCEPTOR_PUSH_MODE_ENV]))
    except (ValueError, IndexError) as exc:
        raise PerceptorPushConfigurationError(
            "invalid Perceptor namespace or signature mode"
        ) from exc
    namespace = DomainNamespace(namespace_id=namespace_text, data_mode=mode)
    fingerprint = _sha256_setting(
        values, PERCEPTOR_PUSH_CONFIG_FINGERPRINT_ENV
    )
    adapter_sha256 = _sha256_setting(
        values, PERCEPTOR_PUSH_ADAPTER_SHA256_ENV
    )
    account_id = str(values[PERCEPTOR_PUSH_ACCOUNT_ENV])
    environment = str(values[PERCEPTOR_PUSH_ENVIRONMENT_ENV])
    if environment.strip().lower() == "production" and mode != DataMode.LIVE:
        raise PerceptorPushConfigurationError(
            "production Perceptor push requires an explicit live namespace"
        )
    profile = PerceptorPushCompatibilityProfile(
        profile_id=str(values[PERCEPTOR_PUSH_PROFILE_ENV]),
        provider_account_id=account_id,
        signing_secret=str(values[PERCEPTOR_PUSH_SECRET_ENV]),
        signature_algorithm=str(
            values[PERCEPTOR_PUSH_ALGORITHM_ENV]
        ).upper(),
        signature_mode=signature_mode,
        signing_path=str(values.get("PERCEPTOR_SIGNING_PATH", "/")),
        append_ampersand_to_secret=_bool(
            values.get("PERCEPTOR_SIGN_SECRET_APPEND_AMPERSAND")
        ),
        timestamp_window=timedelta(
            seconds=_positive_int(
                values,
                PERCEPTOR_PUSH_TIMESTAMP_WINDOW_ENV,
                default=300,
            )
        ),
        future_clock_skew=timedelta(
            seconds=_nonnegative_int(
                values,
                PERCEPTOR_PUSH_FUTURE_SKEW_ENV,
                default=30,
            )
        ),
        environment=environment,
    )
    owned_store = store or _store_from_environment(values)
    try:
        repository = SleepDomainRepository(
            owned_store,
            raw_payload_policy=RawPayloadEncryptionPolicy.from_environment(
            production=environment.strip().lower() == "production",
                environ=values,
            ),
        )
        registration = perceptor_v1_registration(
            configuration_fingerprint=fingerprint,
            adapter_artifact_sha256=adapter_sha256,
            provider_account_ids=(account_id,),
            environments=(environment,),
        )
        existing_account = repository.get_provider_account(
            namespace,
            provider_account_id=account_id,
        )
        if existing_account is None:
            repository.save_provider_account(
                ProviderAccountRecord(
                    namespace=namespace,
                    provider_account_id=account_id,
                    provider_id="perceptor",
                    configuration_fingerprint=fingerprint,
                    status="enabled",
                    metadata={
                        "compatibility_profile_id": profile.profile_id,
                        "environment": environment,
                        "capability_status": "pending",
                    },
                    created_at=datetime.now(timezone.utc),
                )
            )
        elif (
            existing_account.provider_id != "perceptor"
            or existing_account.configuration_fingerprint != fingerprint
            or existing_account.status != "enabled"
            or existing_account.metadata.get("compatibility_profile_id")
            != profile.profile_id
            or existing_account.metadata.get("environment") != environment
            or existing_account.metadata.get("capability_status") != "pending"
        ):
            raise PerceptorPushConfigurationError(
                "configured Perceptor provider account does not match "
                "the immutable unified-persistence record"
            )
        registry = ControlledAdapterRegistry(
            namespace=namespace,
            repository=repository,
            allowlist=(registration,),
            authorized_human_reviewers=frozenset(),
        )
        if (
            registry.deployment_status(
                adapter_id="perceptor-v1",
                adapter_version="1.0.0",
            )
            == AdapterDeploymentStatus.REGISTERED
        ):
            registry.enable(
                adapter_id="perceptor-v1",
                adapter_version="1.0.0",
                deployment_event_id=(
                    f"enable:perceptor-v1:{profile.profile_id}"
                ),
                actor_id="deployment-configuration",
                reason="explicit Perceptor push runtime configuration",
                changed_at=datetime.now(timezone.utc),
            )
        ingestion = PerceptorPushIngestionService(
            namespace=namespace,
            repository=repository,
            registry=registry,
            profiles={profile.profile_id: profile},
            http_limits=PerceptorPushHttpLimits(
                max_body_bytes=_positive_int(
                    values,
                    "SLEEPAGENT_PERCEPTOR_PUSH_MAX_BODY_BYTES",
                    default=1_048_576,
                ),
                max_header_count=_positive_int(
                    values,
                    "SLEEPAGENT_PERCEPTOR_PUSH_MAX_HEADER_COUNT",
                    default=64,
                ),
                max_header_bytes=_positive_int(
                    values,
                    "SLEEPAGENT_PERCEPTOR_PUSH_MAX_HEADER_BYTES",
                    default=16_384,
                ),
            ),
            rate_limiter=PerceptorPushRateLimiter(
                requests=_positive_int(
                    values,
                    "SLEEPAGENT_PERCEPTOR_PUSH_RATE_REQUESTS",
                    default=120,
                ),
                window=timedelta(
                    seconds=_positive_int(
                        values,
                        "SLEEPAGENT_PERCEPTOR_PUSH_RATE_WINDOW_SECONDS",
                        default=60,
                    )
                ),
            ),
        )
        promotion = CandidatePromotionService(
            repository,
            access_policy=AdministrativeAccessPolicy(
                {},
                authorization_ids={},
            ),
        )
        worker = PerceptorNormalizationWorker(
            namespace=namespace,
            repository=repository,
            registry=registry,
            promotion_service=promotion,
            worker_id=(
                "perceptor-push:"
                + hashlib.sha256(
                    namespace.namespace_id.encode("utf-8")
                ).hexdigest()[:16]
            ),
        )
        return PerceptorPushRuntime(
            store=owned_store,
            repository=repository,
            registry=registry,
            ingestion=ingestion,
            worker=worker,
            owns_store=store is None,
        )
    except Exception:
        if store is None:
            owned_store.connection.close()
        raise


_RUNTIME_LOCK = threading.Lock()
_RUNTIME: PerceptorPushRuntime | None = None
_WORKER_LOOP: PerceptorPushWorkerLoop | None = None


def get_perceptor_push_runtime() -> PerceptorPushRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = build_perceptor_push_runtime_from_env()
        return _RUNTIME


def start_perceptor_push_worker() -> None:
    global _RUNTIME, _WORKER_LOOP
    if not _bool(os.getenv(PERCEPTOR_PUSH_WORKER_ENABLED_ENV)):
        return
    with _RUNTIME_LOCK:
        if _WORKER_LOOP is None:
            runtime = (
                _RUNTIME
                if _RUNTIME is not None
                else build_perceptor_push_runtime_from_env()
            )
            _RUNTIME = runtime
            _WORKER_LOOP = PerceptorPushWorkerLoop(runtime)
        _WORKER_LOOP.start()


def stop_perceptor_push_worker() -> None:
    global _RUNTIME, _WORKER_LOOP
    with _RUNTIME_LOCK:
        if _WORKER_LOOP is not None:
            _WORKER_LOOP.stop()
            _WORKER_LOOP = None
        if _RUNTIME is not None:
            _RUNTIME.close()
            _RUNTIME = None


def _store_from_environment(
    values: Mapping[str, str],
) -> RadarPersistenceStore:
    database_url = values.get(RADAR_AGENT_DATABASE_URL_ENV)
    if database_url:
        if (
            str(values.get(PERCEPTOR_PUSH_ENVIRONMENT_ENV, "")).strip().lower()
            == "production"
        ):
            lowered = database_url.strip().lower()
            if (
                lowered.startswith("sqlite")
                or ":memory:" in lowered
                or "/tmp/" in lowered
            ):
                raise PerceptorPushConfigurationError(
                    "production Perceptor push requires shared PostgreSQL "
                    "storage, not SQLite or /tmp"
                )
        return connect_postgres_store(database_url)
    if (
        str(values.get(PERCEPTOR_PUSH_ENVIRONMENT_ENV, "")).strip().lower()
        == "production"
    ):
        raise PerceptorPushConfigurationError(
            "production Perceptor push requires "
            f"{RADAR_AGENT_DATABASE_URL_ENV}; standalone SQLite is local/test only"
        )
    path = Path(
        values.get(
            RADAR_AGENT_SQLITE_PATH_ENV,
            DEFAULT_RADAR_AGENT_SQLITE_PATH,
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=30,
    )
    connection.execute("PRAGMA journal_mode = WAL")
    return RadarPersistenceStore.connect_sqlite(connection)


def _sha256_setting(values: Mapping[str, str], name: str) -> str:
    value = str(values[name])
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PerceptorPushConfigurationError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return value


def _positive_int(
    values: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as exc:
        raise PerceptorPushConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise PerceptorPushConfigurationError(f"{name} must be positive")
    return value


def _nonnegative_int(
    values: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as exc:
        raise PerceptorPushConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise PerceptorPushConfigurationError(f"{name} cannot be negative")
    return value


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "PERCEPTOR_PUSH_ENABLED_ENV",
    "PERCEPTOR_PUSH_WORKER_ENABLED_ENV",
    "PerceptorPushRuntime",
    "build_perceptor_push_runtime_from_env",
    "get_perceptor_push_runtime",
    "start_perceptor_push_worker",
    "stop_perceptor_push_worker",
]
