#!/usr/bin/env python3
# 本脚本为 verify_backend.sh 提供 PostgreSQL process-fault 的窄域 CLI probe。
# 入口是下方八个子命令；它只观测/钳制既定故障窗口，不负责通用 verifier 框架或产品运行。
"""Narrow PostgreSQL probes for the canonical backend process-fault proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


ADMIN_DSN_ENV = "SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN"
WORKER_DSN_ENV = "SLEEPAGENT_FAULT_PROBE_WORKER_DSN"
WORKER_PRINCIPAL_ENV = "SLEEPAGENT_FAULT_PROBE_WORKER_PRINCIPAL"
DELIVERY_DESTINATION = "replay_care_notification"
TERMINAL_PHASES = {"succeeded", "blocked", "reconciliation_required", "failed"}


class TerminalProbeError(RuntimeError):
    """The durable state has moved beyond the requested fault window."""


def delivery_lock_name(semantic_effect_key: str) -> str:
    if not semantic_effect_key:
        raise ValueError("semantic effect key is required")
    return "sleepagent:replay-delivery-effect:" + semantic_effect_key


def _dsn(name: str = ADMIN_DSN_ENV) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _wait_until(
    probe: Callable[[], Any | None], *, timeout_seconds: float, description: str
) -> Any:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value is not None:
                return value
            last_error = None
        except TerminalProbeError:
            raise
        # PostgreSQL restart 窗口内短暂断连是预期现象；只有超时后才把最后错误附到证据中。
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    suffix = "" if last_error is None else f": {type(last_error).__name__}"
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str),
        encoding="utf-8",
    )


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("probe state must be a JSON object")
    return value


def _product_candidate(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT jsonb_build_object('operation_id', operation_id, "
        "'night_episode_id', target_resource_id, 'scope', jsonb_build_object("
        "'namespace_id', namespace_id, 'data_mode', data_mode, "
        "'namespace_generation', namespace_generation, 'run_id', run_id, "
        "'arm_id', arm_id, 'subject_id', subject_id, 'authorization_epoch', "
        "workload_authorization_snapshot_json -> 'authorization_epoch', "
        "'privacy_epoch', workload_authorization_snapshot_json -> "
        "'privacy_epoch', 'retrieval_policy_epoch', "
        "workload_authorization_snapshot_json -> 'retrieval_policy_epoch')) "
        "FROM public.sleep_domain_operations WHERE operation_type = "
        "'product.shared_analysis.v1' AND queue_name = 'product_agent' "
        "AND status IN ('pending', 'retry', 'running') "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    return None if row is None else dict(row[0])


def _product_snapshot(connection: Any, operation_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT product_attempt_id, attempt_sequence, lease_generation, "
        "fencing_token, to_jsonb(attempt) - ARRAY['attempt_sequence', "
        "'lease_generation', 'fencing_token', 'attempt_state', "
        "'query_visible', 'committed_at'] FROM public.backend_product_attempts "
        "AS attempt WHERE operation_id = %s AND attempt_state = 'prepared'",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    invocation_rows = connection.execute(
        "SELECT invocation_id, invocation_key, invocation_kind, request_sha256, "
        "response_sha256, current_state FROM public.backend_invocations "
        "WHERE operation_id = %s ORDER BY invocation_id",
        (operation_id,),
    ).fetchall()
    return dict(
        product_attempt_id=str(row[0]),
        old_owner=dict(
            attempt_sequence=int(row[1]),
            lease_generation=int(row[2]),
            fencing_token=str(row[3]),
        ),
        immutable_artifact=row[4],
        invocations=[list(item) for item in invocation_rows],
    )


def hold_product_commit(
    *, ready_file: Path, prepared_file: Path, takeover_file: Path,
    release_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    with psycopg.connect(_dsn()) as lock_connection:
        # Take the publish lock before the report queue can create its current
        # shared-analysis child.  Report and shared analysis intentionally use
        # the same durable queue, so waiting for a pending child before taking
        # this lock would race the Worker.
        lock_connection.execute(
            "LOCK TABLE public.sleep_domain_analysis_revisions IN SHARE MODE"
        )
        candidate = _wait_until(
            lambda: _product_candidate(lock_connection),
            timeout_seconds=timeout_seconds,
            description="a current shared-analysis operation",
        )
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            changed = setup.execute(
                "UPDATE public.sleep_domain_operations SET max_attempts = 1 "
                "WHERE operation_id = %s "
                "AND status IN ('pending', 'retry', 'running') "
                "AND attempt_count <= 1 RETURNING operation_id",
                (candidate["operation_id"],),
            ).fetchone()
            if changed is None:
                raise TerminalProbeError(
                    "shared analysis passed the bounded reclaim window"
                )
        _write(ready_file, candidate)

        prepared = _wait_until(
            lambda: _product_snapshot(lock_connection, candidate["operation_id"]),
            timeout_seconds=timeout_seconds,
            description="a durable prepared Product artifact",
        )
        state = {**candidate, **prepared}
        owner = lock_connection.execute(
            "SELECT worker_instance FROM public.sleep_domain_operations "
            "WHERE operation_id = %s",
            (candidate["operation_id"],),
        ).fetchone()
        state["old_owner"]["worker_instance"] = str(owner[0])
        _write(prepared_file, state)

        # Worker 被 SIGKILL 后只能轮换 execution ownership；business attempt 和
        # prepared artifact 身份必须保持不变，否则 reclaim 会重复调用模型。
        def takeover() -> dict[str, Any] | None:
            row = lock_connection.execute(
                "SELECT attempt.attempt_sequence, attempt.lease_generation, "
                "attempt.fencing_token, operation.worker_instance, "
                "operation.status, operation.attempt_count, "
                "operation.max_attempts FROM public.backend_product_attempts "
                "AS attempt JOIN public.sleep_domain_operations AS operation "
                "ON operation.operation_id = attempt.operation_id "
                "WHERE attempt.product_attempt_id = %s",
                (state["product_attempt_id"],),
            ).fetchone()
            if row is None or int(row[1]) <= state["old_owner"]["lease_generation"]:
                return None
            if (
                int(row[0]) != state["old_owner"]["attempt_sequence"]
                or str(row[2]) == state["old_owner"]["fencing_token"]
                or str(row[4]) != "running"
                or (int(row[5]), int(row[6])) != (1, 1)
            ):
                raise TerminalProbeError("Product reclaim changed logical identity")
            return {
                "attempt_sequence": int(row[0]),
                "lease_generation": int(row[1]),
                "fencing_token": str(row[2]),
                "worker_instance": str(row[3]),
            }

        state["new_owner"] = _wait_until(
            takeover, timeout_seconds=timeout_seconds,
            description="prepared Product ownership takeover",
        )
        _write(takeover_file, state)
        _wait_until(
            lambda: True if release_file.exists() else None,
            timeout_seconds=timeout_seconds,
            description="Product commit-lock release",
        )
        lock_connection.rollback()


def assert_stale_product_fence(state_file: Path) -> None:
    import psycopg

    # SIGKILL 后旧进程可能恢复执行；显式证明旧 fence 失效，才能排除双重发布。
    state = _read(state_file)
    with psycopg.connect(_dsn(), autocommit=True) as admin:
        current = admin.execute(
            "SELECT lease_generation, fencing_token, worker_instance "
            "FROM public.sleep_domain_operations WHERE operation_id = %s "
            "AND status = 'running' AND lease_expires_at > clock_timestamp()",
            (state["operation_id"],),
        ).fetchone()
    if current is None or int(current[0]) <= int(
        state["old_owner"]["lease_generation"]
    ):
        raise AssertionError("no newer live Product owner is available")
    current_owner = {
        "lease_generation": int(current[0]),
        "fencing_token": str(current[1]),
        "worker_instance": str(current[2]),
    }
    scope = state["scope"]
    with psycopg.connect(_dsn(WORKER_DSN_ENV), autocommit=True) as connection:
        settings = {
            "sleepagent.process_role": "worker",
            "sleepagent.service_principal_id": os.environ.get(
                WORKER_PRINCIPAL_ENV, "sleepagent-worker-test"
            ).strip(),
            "sleepagent.purpose": "worker",
            **{f"sleepagent.{key}": value for key, value in scope.items()},
        }
        for key, value in settings.items():
            connection.execute("SELECT set_config(%s, %s, false)", (key, "" if value is None else str(value)))

        def allows(owner: Mapping[str, Any]) -> bool:
            connection.execute(
                "SELECT set_config('sleepagent.worker_instance', %s, false)",
                (owner["worker_instance"],),
            )
            return bool(connection.execute(
                "SELECT public.sleepagent_operation_fence_allows(%s, %s, %s)",
                (state["operation_id"], owner["lease_generation"], owner["fencing_token"]),
            ).fetchone()[0])

        stale_allows = allows(state["old_owner"])
        current_allows = allows(current_owner)
        if stale_allows or not current_allows:
            print(json.dumps({
                "current_fence": current_allows,
                "stale_fence": stale_allows,
            }, sort_keys=True))
            raise AssertionError("stale/current Product fence proof failed")
    print(json.dumps({"current_fence": True, "stale_fence": False}, sort_keys=True))


def assert_stale_product_fence_and_release(
    *, state_file: Path, release_file: Path, timeout_seconds: float
) -> None:
    _wait_until(
        lambda: True if state_file.exists() and state_file.stat().st_size else None,
        timeout_seconds=timeout_seconds,
        description="prepared Product ownership takeover",
    )
    assert_stale_product_fence(state_file)
    _write(release_file, {"release": True})
    print(json.dumps({"publish_lock_released": True}, sort_keys=True))


def assert_product_recovery(
    *, state_file: Path, root_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    state = _read(state_file)
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        def terminal() -> tuple[Any, ...] | None:
            row = connection.execute(
                "SELECT status, attempt_count, max_attempts, lease_generation, "
                "operation_json -> 'result' FROM public.sleep_domain_operations "
                "WHERE operation_id = %s", (state["operation_id"],),
            ).fetchone()
            return row if row is not None and str(row[0]) == "succeeded" else None

        operation = _wait_until(
            terminal, timeout_seconds=timeout_seconds,
            description="terminal Product publish",
        )
        if (int(operation[1]), int(operation[2])) != (1, 1):
            raise AssertionError("crash reclaim consumed a business attempt")
        final = connection.execute(
            "SELECT attempt_sequence, lease_generation, fencing_token, "
            "attempt_state, query_visible, to_jsonb(attempt) - ARRAY["
            "'attempt_sequence','lease_generation','fencing_token','attempt_state',"
            "'query_visible','committed_at'] FROM public.backend_product_attempts "
            "AS attempt WHERE product_attempt_id = %s",
            (state["product_attempt_id"],),
        ).fetchone()
        if final is None or (
            int(final[0]) != 1
            or int(final[1]) <= state["old_owner"]["lease_generation"]
            or str(final[2]) == state["old_owner"]["fencing_token"]
            or (str(final[3]), bool(final[4])) != ("committed", True)
            or final[5] != state["immutable_artifact"]
        ):
            raise AssertionError("prepared Product identity/ownership drifted")
        invocations = [list(item) for item in connection.execute(
            "SELECT invocation_id, invocation_key, invocation_kind, request_sha256, "
            "response_sha256, current_state FROM public.backend_invocations "
            "WHERE operation_id = %s ORDER BY invocation_id",
            (state["operation_id"],),
        ).fetchall()]
        if (
            invocations != state["invocations"]
            or len(invocations) != 1
            or invocations[0][5] != "response_received"
        ):
            raise AssertionError("Product recovery reinvoked the model or drifted identity")
        result = dict(operation[4])
        artifact = state["immutable_artifact"]["attempt_json"]
        if (
            result["product_attempt_id"] != state["product_attempt_id"]
            or result["analysis_revision_id"]
            != artifact["analysis"]["analysis_revision_id"]
            or result["night_episode_revision_id"]
            != artifact["night_episode_revision_id"]
        ):
            raise AssertionError("terminal Product result drifted from the artifact")
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM public.backend_product_attempts WHERE "
            "operation_id = %s), (SELECT count(*) FROM "
            "public.sleep_domain_analysis_revisions WHERE analysis_revision_id = %s), "
            "(SELECT count(*) FROM public.sleep_domain_analysis_role_views WHERE "
            "analysis_revision_id = %s), (SELECT count(*) FROM "
            "public.sleep_domain_domain_outbox WHERE operation_id = %s AND "
            "aggregate_type = 'AnalysisRevision')",
            (state["operation_id"], result["analysis_revision_id"],
             result["analysis_revision_id"], state["operation_id"]),
        ).fetchone()
        if tuple(int(value) for value in counts) != (1, 1, 3, 1):
            raise AssertionError("Product terminal publish was not exactly once")
        root_id = root_file.read_text(encoding="utf-8").strip()
        linked = connection.execute(
            "SELECT shared.operation_id FROM public.sleep_domain_operations AS root "
            "JOIN public.sleep_domain_operations AS report ON report.operation_id = "
            "root.operation_json #>> '{result,product_operation_id}' "
            "JOIN public.sleep_domain_operations AS shared ON shared.operation_id = "
            "report.operation_json #>> '{report_result,shared_operation_id}' "
            "WHERE root.operation_id = %s "
            "AND report.operation_type = 'product.report.run.v1' "
            "AND shared.operation_type = 'product.shared_analysis.v1'",
            (root_id,),
        ).fetchone()
        if linked != (state["operation_id"],):
            raise AssertionError("recovered Product operation is not the root result")
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    print(json.dumps({"attempt_count": 1, "lease_generation": int(final[1]), "terminal_result_sha256": digest}, sort_keys=True))


def _pending_delivery(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT delivery_intent_id, semantic_effect_key FROM "
        "public.backend_delivery_intents WHERE destination = %s "
        "AND status IN ('pending', 'retry') ORDER BY created_at LIMIT 1",
        (DELIVERY_DESTINATION,),
    ).fetchone()
    return None if row is None else {
        "delivery_intent_id": str(row[0]), "semantic_effect_key": str(row[1])
    }


def hold_delivery_effect_lock(
    *, ready_file: Path, release_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    with psycopg.connect(_dsn(), autocommit=True) as connection:
        delivery = _wait_until(
            lambda: _pending_delivery(connection), timeout_seconds=timeout_seconds,
            description="a pending deterministic delivery",
        )
        name = delivery_lock_name(delivery["semantic_effect_key"])
        # advisory lock 只冻结确定性 sink 的 effect commit，让 Worker 恰好停在
        # send_started/dispatching，从而构造外部投递结果未知的窗口。
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (name,))
        _write(ready_file, {**delivery, "lock_name": name})
        _wait_until(
            lambda: True if release_file.exists() else None,
            timeout_seconds=timeout_seconds, description="delivery lock release",
        )
        if connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (name,)
        ).fetchone() != (True,):
            raise RuntimeError("delivery advisory lock was not released")


def wait_delivery_send_started(
    *, state_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    state = _read(state_file)
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        # 必须在 SIGKILL 前观察到 durable send_started 且 effect 尚未落库；更早或
        # 更晚终止都不能证明 ambiguous delivery 的 reconciliation 语义。
        def probe() -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT invocation.invocation_id, invocation.current_state, "
                "intent.status, (SELECT count(*) FROM "
                "public.backend_replay_delivery_effects_v2 AS effect WHERE "
                "effect.semantic_effect_key = intent.semantic_effect_key) FROM "
                "public.backend_delivery_intents AS intent JOIN "
                "public.sleep_domain_domain_outbox AS source ON source.event_id = "
                "intent.source_event_id JOIN public.backend_invocations AS invocation "
                "ON invocation.operation_id = source.operation_id AND "
                "invocation.invocation_kind = 'external_sink' WHERE "
                "intent.delivery_intent_id = %s",
                (state["delivery_intent_id"],),
            ).fetchone()
            if row is None:
                return None
            if str(row[1]) == "send_started" and str(row[2]) == "dispatching":
                if int(row[3]) != 0:
                    raise TerminalProbeError("delivery effect committed before SIGKILL")
                return {"invocation_id": str(row[0])}
            if str(row[2]) in {"delivered", "dead_letter", "outcome_unknown"}:
                raise TerminalProbeError("delivery passed the ambiguous window")
            return None

        state.update(_wait_until(
            probe, timeout_seconds=timeout_seconds,
            description="committed delivery send_started state",
        ))
        _write(state_file, state)
    print(json.dumps(state, sort_keys=True))


def assert_delivery_recovery(*, state_file: Path, timeout_seconds: float) -> None:
    import psycopg

    state = _read(state_file)
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        _wait_until(
            lambda: True if connection.execute(
                "SELECT status FROM public.backend_delivery_intents WHERE "
                "delivery_intent_id = %s", (state["delivery_intent_id"],),
            ).fetchone() == ("delivered",) else None,
            timeout_seconds=timeout_seconds, description="reconciled delivery",
        )
        # 恢复后先 reconciliation 再允许重试发送，并要求最终只有一个 effect；
        # 这是防止 ambiguous delivery 被盲目重发的核心证明。
        states = [str(row[0]) for row in connection.execute(
            "SELECT to_state FROM public.backend_invocation_journal WHERE "
            "invocation_id = %s ORDER BY sequence", (state["invocation_id"],),
        ).fetchall()]
        if (
            states.count("send_started") != 2
            or states.count("response_received") != 1
            or "reconciled" not in states
            or states.index("reconciled") > states.index("send_started", 2)
        ):
            raise AssertionError("delivery was resent without reconciliation")
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM public.backend_replay_delivery_effects_v2 "
            "WHERE semantic_effect_key = %s), (SELECT count(*) FROM "
            "public.sleep_domain_operations WHERE operation_type = "
            "'delivery_reconciliation' AND target_resource_id = %s AND "
            "status = 'succeeded')",
            (state["semantic_effect_key"], state["delivery_intent_id"]),
        ).fetchone()
        if tuple(int(value) for value in counts) != (1, 1):
            raise AssertionError("delivery reconciliation/effect was not exactly once")
    print(json.dumps({"effect_count": 1, "journal_states": states}, sort_keys=True))


def _root_snapshot(connection: Any, root_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT journey_id, namespace_id, namespace_generation, run_id, arm_id, "
        "subject_id, semantic_key, scenario_sha256, manifest_sha256, "
        "generator_version, adapter_version, model_version, policy_sha256, "
        "schema_manifest_sha256, phase FROM public.backend_demo_journeys "
        "WHERE root_operation_id = %s", (root_id,),
    ).fetchone()
    if row is None:
        return None
    journey_id = str(row[0])
    facts = {
        "checkpoints": connection.execute(
            "SELECT checkpoint_id, semantic_checkpoint_key, checkpoint_sha256 "
            "FROM public.backend_demo_journey_checkpoints WHERE journey_id = %s "
            "ORDER BY checkpoint_id", (journey_id,),
        ).fetchall(),
        "batches": connection.execute(
            "SELECT batch_id, batch_sequence, canonical_payload_sha256, "
            "observation_count FROM public.backend_replay_ingress_batches WHERE "
            "journey_id = %s AND status = 'committed' ORDER BY batch_sequence",
            (journey_id,),
        ).fetchall(),
    }
    return {
        "root_operation_id": root_id,
        "journey_id": journey_id,
        "identity": list(row[1:14]),
        "phase": str(row[14]),
        "durable_facts": {key: [list(item) for item in value] for key, value in facts.items()},
    }


def wait_root_active(
    *, root_file: Path, state_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    # PostgreSQL restart 必须发生在 root 已产生 durable progress、但尚未终态时，
    # 否则无法证明已提交 checkpoint/batch 能跨数据库进程重启保留。
    root_id = root_file.read_text(encoding="utf-8").strip()
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        def probe() -> dict[str, Any] | None:
            state = _root_snapshot(connection, root_id)
            if state is None or state["phase"] in {"accepted", "staging_input"}:
                return None
            if state["phase"] in TERMINAL_PHASES:
                raise TerminalProbeError("root became terminal before PostgreSQL restart")
            return state

        state = _wait_until(
            probe, timeout_seconds=timeout_seconds,
            description="a progressed active replay root",
        )
        _write(state_file, state)
    print(json.dumps({"root_operation_id": root_id, "phase": state["phase"]}, sort_keys=True))


def assert_root_recovery(
    *, root_file: Path, state_file: Path, timeout_seconds: float
) -> None:
    import psycopg

    # 重启后沿用同一 root/identity/pins，并包含重启前全部 durable facts；禁止用
    # 新建 journey 掩盖恢复失败。
    before = _read(state_file)
    root_id = root_file.read_text(encoding="utf-8").strip()
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        def terminal() -> dict[str, Any] | None:
            state = _root_snapshot(connection, root_id)
            if state is None or state["phase"] not in TERMINAL_PHASES:
                return None
            if state["phase"] != "succeeded":
                raise TerminalProbeError(f"root recovered as {state['phase']}")
            return state

        after = _wait_until(
            terminal, timeout_seconds=timeout_seconds,
            description="the original root terminal state",
        )
        if after["journey_id"] != before["journey_id"] or after["identity"] != before["identity"]:
            raise AssertionError("PostgreSQL restart changed root identity/pins")
        for kind, old_rows in before["durable_facts"].items():
            if not all(row in after["durable_facts"][kind] for row in old_rows):
                raise AssertionError(f"PostgreSQL restart lost committed {kind}")
        count = connection.execute(
            "SELECT count(*) FROM public.backend_demo_journeys WHERE "
            "namespace_id = %s AND namespace_generation = %s AND semantic_key = %s",
            (before["identity"][0], before["identity"][1], before["identity"][5]),
        ).fetchone()
        if count != (1,):
            raise AssertionError("PostgreSQL restart created a second replay root")
    print(json.dumps({"root_operation_id": root_id, "phase": "succeeded", "same_root": True}, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend-process-fault-probe")
    actions = parser.add_subparsers(dest="action", required=True)
    specifications = {
        "hold-product-commit": ("ready", "prepared", "takeover", "release"),
        "hold-delivery-effect-lock": ("ready", "release"),
        "wait-delivery-send-started": ("state",),
        "assert-delivery-recovery": ("state",),
        "assert-stale-product-fence": ("state",),
        "assert-stale-product-fence-and-release": ("state", "release"),
        "assert-product-recovery": ("state", "root"),
        "wait-root-active": ("root", "state"),
        "assert-root-recovery": ("root", "state"),
    }
    for name, file_names in specifications.items():
        item = actions.add_parser(name)
        for file_name in file_names:
            item.add_argument(f"--{file_name}-file", type=Path, required=True)
        if name != "assert-stale-product-fence":
            item.add_argument("--timeout-seconds", type=float, default=240)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    values = vars(args).copy()
    action = values.pop("action")
    handlers = {
        "hold-product-commit": hold_product_commit,
        "assert-stale-product-fence": assert_stale_product_fence,
        "assert-stale-product-fence-and-release": (
            assert_stale_product_fence_and_release
        ),
        "assert-product-recovery": assert_product_recovery,
        "hold-delivery-effect-lock": hold_delivery_effect_lock,
        "wait-delivery-send-started": wait_delivery_send_started,
        "assert-delivery-recovery": assert_delivery_recovery,
        "wait-root-active": wait_root_active,
        "assert-root-recovery": assert_root_recovery,
    }
    handlers[action](**values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
