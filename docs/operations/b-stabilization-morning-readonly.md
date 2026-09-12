# B stabilization morning read-only acceptance

This entry point is intentionally read-only. It reads the scoped business
database through the existing RLS report identity and inspects redacted,
rotated server logs. It does not enqueue backfill, run Product Agent, call a
model, close an episode, or mutate business state.

From the repository root, run:

```bash
./scripts/b_stabilization_readonly_acceptance.sh
```

The default target is `TARGET_WAKE_DATE=2026-09-07` in `Asia/Shanghai`. The
default report is written with mode `0600` to:

```text
/var/lib/sleepagent/logs/b-stabilization/morning-2026-09-07.txt
```

To evaluate another date or choose a different local evidence file, pass the
date and output path explicitly:

```bash
./scripts/b_stabilization_readonly_acceptance.sh 2026-09-07 /tmp/morning-readonly.txt
```

Set `SLEEPAGENT_RUNTIME_ROOT` when the runtime state is stored somewhere other
than `/var/lib/sleepagent`.

Interpret Push receipt continuity separately from final observation coverage.
History success cannot erase a recorded Push gap, and an accepted vendor/raw
response is not a completed repair until its normalization has converged.
`INSUFFICIENT_EVIDENCE` is the correct transport attribution when server-side
logs cannot distinguish vendor silence from an upstream path failure.

Operational rollback is bounded and does not downgrade schema 029: stop only
the B stabilization tmux workers/scheduler if they are unsafe, leave API,
cpolar, PostgreSQL, durable work, attempts, and audit rows intact, and use the
capacity CAS rollback SQL recorded under `.b_stabilization_evidence/`. Never
reset work state or revive terminal dead letters as part of rollback.
