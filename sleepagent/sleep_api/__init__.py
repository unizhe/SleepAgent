"""Independent, versioned sleep-domain service boundary."""

from sleepagent.sleep_api.auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    AuthenticatedActorContext,
    AuthoritativeRoleBinding,
    AuthorizationEpochRoleCache,
    HttpsBearerServicePrincipalVerifier,
    InMemoryAuthoritativeRoleBindingResolver,
    RotatingServiceCredential,
    SleepApiAuthenticator,
)
from sleepagent.sleep_api.app import create_sleep_api_app
from sleepagent.sleep_api.contracts import *  # noqa: F403
from sleepagent.sleep_api.persistence import (
    FeedbackRecord,
    OperationCommand,
    SleepApiPersistence,
)
from sleepagent.sleep_api.router import create_sleep_api_router, install_sleep_api
from sleepagent.sleep_api.runtime import (
    build_sleep_api_runtime_from_env,
    get_sleep_api_runtime,
    start_sleep_api_worker,
    stop_sleep_api_worker,
)
from sleepagent.sleep_api.service import (
    AuthorizationEpochRoleViewCache,
    OpaquePageCursorCodec,
    SleepApiOperationWorker,
    SleepApiRuntime,
)

__all__ = [
    "ActorAssertionVerifier",
    "ActorVerificationKey",
    "AuthenticatedActorContext",
    "AuthoritativeRoleBinding",
    "AuthorizationEpochRoleCache",
    "AuthorizationEpochRoleViewCache",
    "FeedbackRecord",
    "HttpsBearerServicePrincipalVerifier",
    "InMemoryAuthoritativeRoleBindingResolver",
    "OpaquePageCursorCodec",
    "OperationCommand",
    "RotatingServiceCredential",
    "SleepApiAuthenticator",
    "SleepApiOperationWorker",
    "SleepApiPersistence",
    "SleepApiRuntime",
    "build_sleep_api_runtime_from_env",
    "create_sleep_api_router",
    "create_sleep_api_app",
    "get_sleep_api_runtime",
    "install_sleep_api",
    "start_sleep_api_worker",
    "stop_sleep_api_worker",
]
