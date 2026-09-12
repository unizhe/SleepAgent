# Nightly operator check

The daily operator entry point is read-only and uses the existing real-night
report identity and RLS session contract. Its date is the canonical
`wake_date` in `Asia/Shanghai`; it is not an inferred sleep duration or a raw
collection date.

```bash
./scripts/nightly.sh             # previous Asia/Shanghai calendar date
./scripts/nightly.sh 2026-09-09  # stored historical wake date
./scripts/nightly.sh list        # newest 30 stored NightEpisode dates
```

The dated command runs `b_stabilization_readonly_acceptance.sh`, keeps its full
output, and then reads the episode, report, observation-membership,
finalization, and Product state in a separate `BEGIN READ ONLY` transaction.
It does not enqueue work, contact Perceptor, invoke a model, or update a work
receipt. A missing episode prints `NIGHT_NOT_FOUND=YYYY-MM-DD` and exits 3;
recovery is intentionally separate.

Each run replaces the deterministic operator snapshot files below. PostgreSQL
remains canonical; raw vendor payloads are not exported.

```text
/var/lib/sleepagent/nightly-runs/YYYY-MM-DD/
  acceptance.log
  summary.json
  summary.txt
```

Set `SLEEPAGENT_RUNTIME_ROOT` when the runtime state is stored somewhere other
than `/var/lib/sleepagent`.

The supported governed recovery commands remain `history-backfill` (an
explicit interval split into deterministic chunks of at most one hour) and
`sleep-report-recover` (an explicit report date) in `sleepagent.device_cli`.
They contact Perceptor and write durable intake/work state, so `nightly.sh`
never starts them. The repository does not assert how long Perceptor retains
data; recoverability therefore depends on vendor availability at request time.

Raw encrypted ingress has a configured `retention_until` and may become
cryptographically unavailable after governed retention. Canonical
observations, NightEpisode aggregates and immutable revisions, observation and
source-report links, source report metadata, finalization revisions,
operations, receipts, and audit records remain PostgreSQL records under their
respective lifecycle policies. The nightly archive is only a compact operator
index over those canonical records.
