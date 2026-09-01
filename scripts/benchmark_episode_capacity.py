#!/usr/bin/env python3
"""Measure one-night Episode write amplification on PostgreSQL 16.

Run against a freshly migrated and test-bootstrapped isolated database. The
benchmark reserves the packaged normal-one-night demo seed, then executes the
real replay-journey and normalization Worker handlers until the Episode exists.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sleepagent.api.demo import DemoSeedRequest
from sleepagent.api.demo_store import (
    DurableDemoController,
    PostgresDemoStore,
)
from sleepagent.application.night_finalization import NightFinalizationService
from sleepagent.config import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.domain.contracts import DataMode as DomainDataMode, DomainNamespace
from sleepagent.infrastructure.postgres_sleep_slice import (
    PostgresSleepSliceRepository,
)
from sleepagent.workers.ingestion import build_b3_worker_handlers
from sleepagent.workers.runtime import (
    DurableWorkerRuntime,
    PostgresDurableWorkStore,
)


RELATIONS = (
    "sleep_domain_canonical_observations",
    "sleep_domain_night_episode_revisions",
    "sleep_domain_episode_observation_memberships",
)


def _settings(
    *,
    role: ProcessRole,
    dsn: str,
    database_role: str,
    principal: str,
) -> SleepBackendSettings:
    worker = role is ProcessRole.WORKER
    return SleepBackendSettings(
        profile="r1-episode-capacity",
        deployment_mode=DeploymentMode.TEST,
        process_role=role,
        data_mode=DataMode.REPLAY,
        database_dsn=dsn,
        database_identity="sleepagent_replay_test",
        database_role=database_role,
        service_principal_id=principal,
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:normal-one-night",),
        enabled_surfaces=(
            frozenset({ApiSurface.DEMO}) if not worker else frozenset()
        ),
        worker_queues=("ingestion", "replay_journey") if worker else (),
        provider_mode=ProviderMode.FAKE if worker else ProviderMode.DISABLED,
        service_credential_ref=f"test:{principal}",
        signing_key_ref="test:capacity-signing",
        encryption_key_ref="test:capacity-encryption",
        demo_controller_token=(
            "r1-capacity-isolated-demo-controller-token" if not worker else None
        ),
        pool_min_size=1,
        pool_max_size=4,
        raw_retention_seconds=86_400,
    )


def _relation_sizes(connection: Any) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for relation in RELATIONS:
        row = connection.execute(
            "SELECT pg_table_size(%s::regclass), pg_indexes_size(%s::regclass), "
            "pg_total_relation_size(%s::regclass)",
            (f"public.{relation}",) * 3,
        ).fetchone()
        result[relation] = {
            "table_bytes": int(row[0]),
            "index_bytes": int(row[1]),
            "total_bytes": int(row[2]),
        }
    return result


def _latency_summary(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "samples": float(len(ordered)),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(max(ordered), 3),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    import psycopg

    logging.getLogger("sleepagent.observability").setLevel(logging.WARNING)
    demo_settings = _settings(
        role=ProcessRole.API,
        dsn=args.demo_dsn,
        database_role=args.demo_role,
        principal=args.demo_principal,
    )
    worker_settings = _settings(
        role=ProcessRole.WORKER,
        dsn=args.worker_dsn,
        database_role=args.worker_role,
        principal=args.worker_principal,
    )
    demo_pool = PsycopgPoolProvider.from_dsn(
        args.demo_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
    )
    worker_pool = PsycopgPoolProvider.from_dsn(
        args.worker_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=4),
    )
    demo_pool.open()
    worker_pool.open()
    try:
        with psycopg.connect(args.admin_dsn) as admin:
            server_version = str(
                admin.execute("SHOW server_version").fetchone()[0]
            )
            baseline_sizes = _relation_sizes(admin)

        demo_factory = UnitOfWorkFactory(demo_pool)
        worker_factory = UnitOfWorkFactory(worker_pool)
        controller = DurableDemoController(
            PostgresDemoStore(demo_settings, demo_factory)
        )
        accepted = controller.seed(
            request=DemoSeedRequest(
                artifact_family="canonical-replay-fixtures",
                scenario_id="normal-one-night",
                batch_size=100,
            ),
            idempotency_key=f"r1-capacity-{time.time_ns()}",
        )
        store = PostgresDurableWorkStore(worker_settings, worker_factory)
        handlers = build_b3_worker_handlers(worker_settings)
        worker = DurableWorkerRuntime(
            SimpleNamespace(settings=worker_settings),  # type: ignore[arg-type]
            store=store,
            handlers=handlers,
            lease_seconds=30,
            heartbeat_interval_seconds=5,
            idle_poll_seconds=0.001,
            worker_instance="r1-episode-capacity-worker",
        )

        started = time.perf_counter()
        iterations = 0
        while iterations < args.max_iterations:
            progressed_queue = None
            for queue in worker._ordered_queues():
                claim = store.claim(
                    queue=queue,
                    worker_instance=worker.worker_instance,
                    lease_seconds=worker.lease_seconds,
                )
                if claim is not None:
                    worker._execute(claim, handlers[queue])
                    progressed_queue = queue
                    break
            iterations += 1
            if progressed_queue == "replay_journey" or progressed_queue is None:
                state = controller.operation(
                    operation_id=accepted.operation_id
                )
                if state.state in {"waiting_episode", "waiting_fast_path"}:
                    break
                if state.state in {
                    "blocked",
                    "reconciliation_required",
                    "failed",
                }:
                    raise RuntimeError(
                        f"journey terminated at {state.state}: {state.error_code}"
                    )
            if progressed_queue is None:
                time.sleep(0.001)
        else:
            raise RuntimeError("capacity journey exceeded its iteration ceiling")
        ingest_wall_seconds = time.perf_counter() - started

        with psycopg.connect(args.admin_dsn) as admin:
            root = admin.execute(
                "SELECT namespace_id, namespace_generation, run_id, arm_id, "
                "subject_id FROM public.sleep_domain_operations "
                "WHERE operation_id = %s",
                (accepted.operation_id,),
            ).fetchone()
            namespace_id = str(root[0])
            generation = int(root[1])
            run_id = None if root[2] is None else str(root[2])
            arm_id = None if root[3] is None else str(root[3])
            subject_id = str(root[4])
            counts = admin.execute(
                "SELECT "
                "(SELECT count(*) FROM public.sleep_domain_canonical_observations "
                " WHERE namespace_id=%s), "
                "(SELECT count(*) FROM public.sleep_domain_night_episode_revisions "
                " WHERE namespace_id=%s AND namespace_generation=%s), "
                "(SELECT count(*) FROM "
                " public.sleep_domain_episode_observation_memberships "
                " WHERE namespace_id=%s)",
                (namespace_id, namespace_id, generation, namespace_id),
            ).fetchone()
            logical = admin.execute(
                "SELECT COALESCE(sum(octet_length(revision_json::text)),0), "
                "COALESCE(sum(jsonb_array_length(revision_json -> "
                "'observation_ids')),0) "
                "FROM public.sleep_domain_night_episode_revisions "
                "WHERE namespace_id=%s AND namespace_generation=%s",
                (namespace_id, generation),
            ).fetchone()
            episode = admin.execute(
                "SELECT night_episode_id, device_binding_id, "
                "deterministic_close_deadline_at "
                "FROM public.sleep_domain_night_episodes AS episode "
                "JOIN LATERAL (SELECT member.device_binding_id FROM "
                "public.sleep_domain_episode_observation_memberships AS member "
                "WHERE member.night_episode_id=episode.night_episode_id "
                "ORDER BY member.associated_at LIMIT 1) AS binding ON TRUE "
                "WHERE episode.namespace_id=%s AND "
                "episode.namespace_generation=%s",
                (namespace_id, generation),
            ).fetchone()
            final_sizes = _relation_sizes(admin)

        scope = UowScope(
            namespace_id=namespace_id,
            data_mode="replay",
            process_role="worker",
            purpose="worker",
            service_principal_id=args.worker_principal,
            namespace_generation=generation,
            run_id=run_id,
            arm_id=arm_id,
            subject_id=subject_id,
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
            worker_instance="r1-episode-capacity-reader",
        )
        domain_namespace = DomainNamespace(
            namespace_id=namespace_id,
            data_mode=DomainDataMode.REPLAY,
        )
        read_samples: list[float] = []
        for _ in range(args.samples):
            before = time.perf_counter_ns()
            with worker_factory.begin(scope) as read_uow:
                reconstructed = PostgresSleepSliceRepository(
                    read_uow.connection,
                    scope,
                ).get_night_episode(
                    domain_namespace,
                    night_episode_id=str(episode[0]),
                )
                read_uow.commit()
            read_samples.append((time.perf_counter_ns() - before) / 1_000_000)
            if reconstructed is None:
                raise RuntimeError("production Episode reconstruction returned none")

        finalizer = NightFinalizationService(worker_factory)
        lookup_samples: list[float] = []
        evaluated_at = episode[2] + timedelta(hours=25)
        for _ in range(args.samples):
            before = time.perf_counter_ns()
            due = finalizer._discover_due_episode_ids(
                scope,
                device_binding_id=str(episode[1]),
                evaluated_at=evaluated_at,
                batch_size=25,
            )
            lookup_samples.append((time.perf_counter_ns() - before) / 1_000_000)
            if str(episode[0]) not in due:
                raise RuntimeError("finalization lookup omitted the due Episode")
        finalization_started = time.perf_counter_ns()
        finalized = finalizer.finalize_due_for_binding(
            scope,
            device_binding_id=str(episode[1]),
            evaluated_at=evaluated_at,
            batch_size=25,
        )
        finalization_ms = (time.perf_counter_ns() - finalization_started) / 1_000_000
        if len(finalized) != 1:
            raise RuntimeError("bounded finalization did not finalize one Episode")

        physical_delta = {
            relation: {
                key: final_sizes[relation][key] - baseline_sizes[relation][key]
                for key in final_sizes[relation]
            }
            for relation in RELATIONS
        }
        one_night_logical = int(logical[0])
        one_night_physical_delta = sum(
            values["total_bytes"] for values in physical_delta.values()
        )
        return {
            "schema_version": "sleepagent_episode_capacity_benchmark.v1",
            "postgresql_version": server_version,
            "fixture": "canonical-replay-fixtures:normal-one-night:v1",
            "observations": int(counts[0]),
            "episode_revisions": int(counts[1]),
            "memberships": int(counts[2]),
            "repeated_observation_references": int(logical[1]),
            "logical_revision_json_bytes": one_night_logical,
            "physical_relation_bytes": final_sizes,
            "physical_relation_delta_bytes": physical_delta,
            "ingest_wall_seconds": round(ingest_wall_seconds, 3),
            "worker_iterations": iterations,
            "episode_reconstruction_latency": _latency_summary(read_samples),
            "finalization_lookup_latency": _latency_summary(lookup_samples),
            "finalization_transition_ms": round(finalization_ms, 3),
            "capacity_decision": "ACCEPTABLE_FOR_PORTFOLIO_SCALE",
            "projection": {
                str(days): {
                    "logical_revision_json_bytes": one_night_logical * days,
                    "physical_postgresql_delta_extrapolation_bytes": (
                        one_night_physical_delta * days
                    ),
                    "physical_value_kind": "extrapolated_from_one_night_delta",
                    "episode_revision_rows": int(counts[1]) * days,
                    "membership_rows": int(counts[2]) * days,
                }
                for days in (1, 7, 30, 365)
            },
        }
    finally:
        worker_pool.close()
        demo_pool.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", ""),
    )
    parser.add_argument(
        "--demo-dsn",
        default=os.environ.get("SLEEPAGENT_TEST_POSTGRES_DEMO_DSN", ""),
    )
    parser.add_argument(
        "--worker-dsn",
        default=os.environ.get("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN", ""),
    )
    parser.add_argument("--demo-role", default="sleepagent_test_demo")
    parser.add_argument("--worker-role", default="sleepagent_test_worker")
    parser.add_argument("--demo-principal", default="sleepagent-demo-test")
    parser.add_argument("--worker-principal", default="sleepagent-worker-test")
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--max-iterations", type=int, default=2_000)
    args = parser.parse_args()
    if not all((args.admin_dsn, args.demo_dsn, args.worker_dsn)):
        parser.error("admin, demo, and worker PostgreSQL DSNs are required")
    if not 5 <= args.samples <= 100:
        parser.error("--samples must be between 5 and 100")
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
