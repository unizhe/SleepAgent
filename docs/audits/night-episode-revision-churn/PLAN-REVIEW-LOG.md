# NightEpisode revision churn build log

## Act 3 — Build

### Round 1 — Codex build

Confirmed that `EpisodeLifecycleProjector` intentionally creates one revision
per attached observation and that `normal-one-night` therefore ends at revision
494. The persistence adapter previously passed every cumulative observation ID
to the membership insert and aggregated the entire membership table for every
revision. On PostgreSQL 16 this produced 122,265 membership ID write/check
inputs for 494 durable memberships and took 276.247 seconds from journey
creation to Product-ready success.

Implemented a parent-relative membership delta on `EpisodeRevisionMutation`.
Revision numbers, parent links, cumulative revision JSON, current pointers and
all 494 revision rows remain unchanged. The repository now inserts only the
new membership IDs and validates those exact rows against their canonical
observation identity, subject, Episode, binding, event time and receipt time.
An empty delta still writes the revision and skips only membership SQL.

Post-change PostgreSQL 16 proof completed in 105.562 seconds. It retained 494
revision rows, final revision 494, 494 unique memberships, an exact final
revision/membership set match, one initial fast-path/Product semantic chain and
three ready role projections. Baseline and optimized semantic membership
digests, excluding server-generated IDs, were identical:
`c4e9a2ec6bb483eee3bb35592da99ec4`.

### Fix pass 1 — preserve empty-delta revisions

Diff review found the first placement of the empty-delta return preceded the
revision insert. It was moved after the immutable revision insert, and a
focused regression proves a deadline revision with no new observation still
persists while issuing no membership write/check.

### Codex verification

- Targeted lifecycle/replay/fast-path/persistence: 43 passed.
- Explicit late/correction/idempotent-reclaim/restart/fence set: 9 passed.
- Real PostgreSQL first-slice semantic proof: 1 passed.
- Fresh disposable PostgreSQL repository/UoW/Product integration: 8 passed.
- Full suite: 1206 passed, 14 skipped.
- No public contract, DTO, schema, migration, scenario, Agent/LLM path, Demo
  timeout or Terminal Demo display changed.

The cumulative observation list inside every immutable revision JSON remains
by design, so revision snapshot serialization and one transaction per revision
remain residual costs. The repeated physical membership rewrite and full-set
membership scan are removed.
