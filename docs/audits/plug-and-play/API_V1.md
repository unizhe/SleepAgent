# Sleep Domain API v1

The deployed boundary is the canonical `backend.main:app` BFF profile, which
mounts exactly `/api/v1/*` and `/product/sleep/*`. The historical
`create_sleep_api_app(...)` symbol is a test-only compatibility factory that
delegates to the same canonical app composition and owns no worker or global
runtime. The committed BFF OpenAPI snapshot is
`docs/contracts/openapi/backend-bff-v1.json`.

All requests require:

- `Authorization: Bearer <rotating service credential>` over HTTPS;
- `X-Sleep-Actor-Assertion: <compact asymmetric JWS>`;
- a JWS `kid`, issuer, audience, assertion id, actor id, subject id, role,
  scopes, issued/expiry times, nonce, HTTP method/path, and request-body
  SHA-256 plus authorization, privacy, and retrieval-policy epochs;
- an active authoritative actor/subject role binding rechecked by SleepAgent.

Commands also require `Idempotency-Key` and return `202` with an
`operation_id`. The durable Operation is read at
`GET /api/v1/operations/{operation_id}`.

## Queries

- `GET /api/v1/subjects/{subject_id}/lifecycle`
- `GET /api/v1/subjects/{subject_id}/risk`
- `GET /api/v1/subjects/{subject_id}/night-episodes`
- `GET /api/v1/subjects/{subject_id}/night-episodes/{night_episode_id}`
- `GET /api/v1/subjects/{subject_id}/night-episodes/{night_episode_id}/view`
- `GET /api/v1/subjects/{subject_id}/events`
- `GET /api/v1/operations/{operation_id}`

Queries read committed projections only. A missing projection returns a
versioned pending or data-insufficient error and never creates background
work.

## Domain events

`GET /api/v1/subjects/{subject_id}/events` requires
`sleep:events:read` and accepts `cursor` plus a bounded `limit`. The response
declares `at_least_once` delivery. Consumers persist the opaque
`next_cursor` and deduplicate by `event_id`.

The service projects the internal transactional outbox at read time. Public
events include a Schema version, event type/version, aggregate id/version,
per-aggregate sequence, subject and optional Episode/revision identifiers,
timestamps, and a role/scope-minimized payload. The global delivery offset is
used only inside the signed cursor. It is not returned and does not imply
global business ordering.

Cursor sessions survive restart and are bound to the consumer service,
actor, subject, role, exact effective scope projection, authorization epoch,
and event Schema generation. Authority is rechecked on every poll. Expiry,
Schema change, or authorization projection change returns
`CURSOR_RESYNC_REQUIRED`; the consumer reloads current snapshots and obtains
a new cursor. Revocation additionally returns a scoped
`ACCESS_REVOKED_TOMBSTONE` directing deletion of the local authorized cache.
The tombstone explicitly sets `remote_recall_possible: false`: data already
delivered to another system cannot be remotely recalled.

The provider-agnostic example is in
`reference_client/sleep_api_v1_client.py`. It uses only HTTPS and the public
JSON contracts for asymmetric actor signing, commands, Operation polling,
queries, durable cursor storage, duplicate handling, resync, and tombstone
handling. It does not import server packages or send conversation history.
Outbound webhook delivery is not part of v1.

## Commands

- `POST /api/v1/subjects/{subject_id}/monitoring/activate`
- `POST /api/v1/subjects/{subject_id}/monitoring/deactivate`
- `POST /api/v1/subjects/{subject_id}/feedback/elder`
- `POST /api/v1/subjects/{subject_id}/feedback/family`
- `POST /api/v1/subjects/{subject_id}/night-episodes/{night_episode_id}/reanalysis`

Family feedback accepts authenticated family or caregiver bindings. Elder
self-report and family/caregiver report provenance remain distinct.

## Compatibility

Every request, response, page, and error carries its own schema version.
Unknown request fields are rejected. Additive response fields are compatible
within v1; a breaking change requires a new API/schema version and an overlap
window. Responses carry `X-API-Version: v1` and `Deprecation: false`.
Pagination and event polling are bounded to 100 items. Page cursors and event
cursors are separate contracts; event cursors additionally bind the event
Schema generation and have an explicit expiry/resync lifecycle.
