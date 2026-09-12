#!/usr/bin/env python3
"""Independent-process G8 authority probe against the real PostgreSQL boundary."""

from __future__ import annotations

import argparse
import json
import os
from types import SimpleNamespace

from sleepagent.api.postgres import PostgresAuthorityStore, PostgresProductBackend
from sleepagent.api.product import ProductRequestContext
from sleepagent.api.product_contracts import ProductRole
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--expected-version", required=True, type=int)
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    dsn = os.environ["SLEEPAGENT_TEST_POSTGRES_API_DSN"]
    principal_id = os.environ["SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"]
    provider = PsycopgPoolProvider.from_dsn(
        dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
        application_name="sleepagent-g8-independent-hitl-probe",
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
            actor_id=args.actor_id,
            subject_id=args.subject_id,
            role=ProductRole.ELDER,
            purpose="sleep_care",
        )
        context = ProductRequestContext(
            service_principal_id=principal_id,
            actor_id=args.actor_id,
            binding_id=authority.binding_id,
            subject_id=args.subject_id,
            role=authority.role,
            effective_scopes=authority.effective_scopes,
            namespace_id=authority.namespace_id,
            namespace_generation=authority.namespace_generation,
            data_mode=authority.data_mode,
            run_id=authority.run_id,
            arm_id=authority.arm_id,
            purpose="sleep_care",
            authorization_epoch=authority.authorization_epoch,
            privacy_epoch=authority.privacy_epoch,
            retrieval_epoch=authority.retrieval_policy_epoch,
            policy_sha256="process-authority-resolved",
        )
        context.require_scope("product:sleep:care:confirm")
        result = PostgresProductBackend(
            factory,
            cursor_key=b"g" * 32,
        ).decide_care_proposal(
            context,
            proposal_id=args.proposal_id,
            expected_version=args.expected_version,
            choice="approve",
            idempotency_key=args.idempotency_key,
            reason_code="independent_human_confirmed",
            reason=None,
        )
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
