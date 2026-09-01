#!/usr/bin/env python3
"""Independent-process terminal CarePlan probe for G9 PostgreSQL acceptance."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

from sleepagent.api.postgres import PostgresAuthorityStore
from sleepagent.api.product_contracts import ProductRole
from sleepagent.application.care_execution import (
    CareExecutionPrincipal,
    CarePlanApplicationService,
)
from sleepagent.care_cli import build_parser, execute_command
from sleepagent.domain.care_actions import CareAudience
from sleepagent.infrastructure.postgres_care_execution import (
    PostgresCarePlanRepository,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)


def main() -> int:
    arguments = build_parser().parse_args()
    dsn = os.environ["SLEEPAGENT_TEST_POSTGRES_API_DSN"]
    principal_id = os.environ["SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"]
    provider = PsycopgPoolProvider.from_dsn(
        dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
        application_name="sleepagent-g9-independent-terminal-probe",
    )
    provider.open()
    try:
        factory = UnitOfWorkFactory(provider)
        authority = PostgresAuthorityStore(
            SimpleNamespace(
                data_mode=SimpleNamespace(value="live"),
                service_principal_id=principal_id,
            ),
            factory,
        ).resolve(
            actor_id=arguments.actor_id,
            subject_id=arguments.subject_id,
            role=ProductRole(arguments.role),
            purpose="sleep_care",
        )
        asserted_epochs = (
            arguments.authorization_epoch,
            arguments.privacy_epoch,
            arguments.retrieval_policy_epoch,
        )
        resolved_epochs = (
            authority.authorization_epoch,
            authority.privacy_epoch,
            authority.retrieval_policy_epoch,
        )
        if asserted_epochs != resolved_epochs:
            raise RuntimeError("terminal probe authority epochs are stale")
        principal = CareExecutionPrincipal(
            service_principal_id=principal_id,
            actor_id=arguments.actor_id,
            actor_role=CareAudience(authority.role.value),
            actor_binding_id=authority.binding_id,
            subject_id=arguments.subject_id,
            effective_scopes=authority.effective_scopes,
            authorization_epoch=authority.authorization_epoch,
            privacy_epoch=authority.privacy_epoch,
            retrieval_policy_epoch=authority.retrieval_policy_epoch,
            namespace_id=authority.namespace_id,
            namespace_generation=authority.namespace_generation,
            data_mode=authority.data_mode,
            run_id=authority.run_id,
            arm_id=authority.arm_id,
        )
        payload, rendered = execute_command(
            arguments,
            CarePlanApplicationService(PostgresCarePlanRepository(factory)),
            principal,
        )
        if os.environ.get("SLEEPAGENT_G9_PROBE_DROP_RESPONSE") == "1":
            # Acceptance-only uncertain-response simulation: the application
            # command has committed, but the client receives no payload.
            os._exit(75)
        if arguments.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(rendered)
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
