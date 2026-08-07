# Perceptor push integration

`POST /integrations/perceptor/webhook` is the supported Perceptor push boundary.
It reads and bounds the original HTTP headers and body before JSON
materialization, selects one explicit compatibility profile, verifies the
configured HMAC algorithm, timestamp window and nonce, then writes through the
shared `RadarPersistenceStore`.

## Durable acceptance

An accepted request atomically commits:

- an immutable encrypted `RawIngressRecord`;
- one normalization work item;
- one processing-outbox intent.

Only after that transaction commits does the route return the configured
vendor-compatible success envelope. Exact duplicate deliveries return the same
success semantic. A validly signed malformed request or
same-message-id/different-body collision is durably quarantined and
acknowledged; invalid authentication fails closed without writing raw data.
Responses and structured logs do not echo raw payloads, signatures, device
identifiers or full provider message IDs.

A separately enabled leased worker uses the retained Adapter resolution lock to
normalize raw bytes into device-scoped `AdapterObservationCandidate` records.
Only the effective-time `DeviceBinding` promotion service can create canonical
elder-scoped observations. Missing/untrusted source time, an absent binding, an
interval gap or ambiguity stays quarantined. Worker leases can be reclaimed
after process failure.

## Configuration and capability status

All `SLEEPAGENT_PERCEPTOR_PUSH_*` settings are explicit in
[`../.env.example`](../.env.example). There is no unsigned production mode.
The signing representation, algorithm and secret suffix are profile fields;
unsupported combinations are rejected. Perceptor capability declarations
remain `PENDING` until real callback signing and retry behavior is confirmed.

Production requires the configured shared
`SLEEPAGENT_RADAR_AGENT_DATABASE_URL` plus the raw encryption/retention
settings. Standalone SQLite is accepted only for explicit local/test
environments. A production push runtime also requires a `live:*` namespace;
replay/fake namespaces fail closed.

## Legacy local repository

`sleepagent.integrations.perceptor.webhook.PerceptorWebhookRepository` remains
only as a deprecated local compatibility/diagnostic implementation. The
FastAPI route does not call it, it is not a production authority, and there is
no dual write to `perceptor_webhook_events.sqlite3`. If a `/receive` alias is
introduced for vendor migration, it must invoke the same unified service rather
than a separate repository.

The old `server.py` is now a standard-library, loopback-only observation probe.
It does not persist payloads; its old `/receive` path returns `410`. The probe
and `Radar_monitor.py` require
`SLEEPAGENT_LEGACY_PERCEPTOR_DIAGNOSTIC=true` in a non-production deployment.

## One-time legacy import and authority cutover

Migration `020_legacy_authority_cutover` is additive. The importer opens the
old SQLite file read-only, preserves legacy raw IDs and normalized-content
hashes, then writes raw bytes only through the unified encrypted Raw Inbox.
Unless a reviewed manifest proves data mode, signature, trustworthy event
time, binding, Adapter lock and compatibility profile, the record is terminally
quarantined under an isolated `replay:legacy-quarantine:*` namespace.

`CompatibilityMigrationController` enforces the persisted sequence:

1. `expanded`;
2. succeeded import (an audited zero-row import is valid) and current-revision
   backfill;
3. privacy-minimized shadow comparison for every current revision;
4. canonical compatibility projection plus configuration-gated `cutover`;
5. rollback probe and optional `rolled_back` selection.

Canonical commits never dual-write `radar_night_summaries`. The table is
updated only by this migration/finalization flow and remains a compatibility
read model of the current committed revision. Cutover events are append-only;
an atomic versioned state row serializes concurrent phase changes.

## Real-device acceptance

`RealPerceptorAcceptanceReport` records separate transport, push-auth, pull
normalization, binding, NightEpisode, fast-path, Agent slow-path and API/client
verdicts. Automated evidence can produce `PENDING` or `FAILED`; `VERIFIED`
requires immutable real evidence, immutable test results and a named human
reviewer. Adapter `CapabilityVerificationReceipt` records remain separate and
are not used to mislabel platform stages as Adapter capabilities.
