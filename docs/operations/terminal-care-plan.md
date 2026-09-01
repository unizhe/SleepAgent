# Terminal Care Plan operations

G9 exposes an internal, terminal-first execution loop for actions already
authorized by G8. It does not send a notification or perform the care action.
An authorized human performs the real-world action and records an attestation.

## Authority and creation

The only creation path is an active, unexpired G8 `ApprovalGrant`. The grant
insert and immutable `CarePlanEntry` creation share one PostgreSQL transaction.
`approval_grant_id` is unique and the plan identifier and semantic hash are
deterministic, so approval retries converge on one plan. Migration 021 also
performs a bounded, idempotent backfill of active grants that were present at
deployment. Unapproved or rejected proposals cannot produce a plan.

The immutable plan snapshots the grant/proposal hashes, subject, closed action
type, executor role, source hard-finalization and shared-analysis references,
CareStrategy version, evidence references, structured parameters, policy and
renderer versions, and validity window. Its validity never extends past the
grant or proposal expiry.

## Human execution contract

Execution state is separate from proposal/grant state:

```text
NOT_STARTED -> IN_PROGRESS -> COMPLETED
NOT_STARTED ----------------> CANCELLED
IN_PROGRESS ----------------> CANCELLED
```

`recommend_consistent_wake_time` is a bounded-period action and requires
`START` before `COMPLETE`. `recommend_morning_light` is one-time and permits
direct completion. The two follow-up actions are explicit follow-up tasks and
also permit direct completion. The policy is closed and versioned; the CLI
does not decide transition semantics.

Every successful command appends one immutable event with
`source_authority=human_attested`, actor principal/binding/role, subject,
authority epoch, occurrence/record times, previous/resulting state and version,
command fingerprint, and an optional sanitized note of at most 500 characters.
`COMPLETED` means only that an authorized human reported completion. It does
not mean device measurement, clinical confirmation, effectiveness, or improved
sleep.

Exact retries use the same idempotency key and converge on the original event.
Key reuse for different semantics and stale/concurrent CAS commands fail
closed. PostgreSQL serializes the state row; it does not use last-write-wins.

## Authority loss and history

Every state-changing command revalidates the authenticated API principal,
actor-subject binding, executor role, execution scope, authority epoch,
namespace generation, subject, grant, proposal, and validity window. A revoked
grant makes a nonterminal plan `INVALIDATED`; an expired window makes it
`EXPIRED`; material source supersession makes it `SUPERSEDED`. No later start,
complete, or cancel event is accepted. Events that were validly recorded before
authority loss remain immutable. A plan completed before later revocation
keeps its historical `COMPLETED` attestation while the grant is separately
reported as no longer active.

## Terminal commands

Use the API database capability profile and current authority epochs:

```text
python -m sleepagent.care_cli \
  --actor-id ACTOR --subject-id SUBJECT --role elder \
  --authorization-epoch N --privacy-epoch N \
  --retrieval-policy-epoch N list

python -m sleepagent.care_cli [authority arguments] show PLAN_ID
python -m sleepagent.care_cli [authority arguments] start PLAN_ID \
  --idempotency-key KEY [--note NOTE]
python -m sleepagent.care_cli [authority arguments] complete PLAN_ID \
  --idempotency-key KEY [--note NOTE]
python -m sleepagent.care_cli [authority arguments] cancel PLAN_ID \
  --idempotency-key KEY [--note NOTE]
python -m sleepagent.care_cli [authority arguments] history PLAN_ID
```

`list --state` accepts `active`, `not_started`, `in_progress`, `completed`,
`cancelled`, `expired`, or `invalidated`. Normal output is deterministic zh-CN
and omits hashes and authority internals. `--json` provides stable structured
output; `--trace` adds bounded identifiers and hashes for authorized debugging.
The CLI resolves role and binding server-side and calls the application service;
it never writes SQL directly.

## Recovery and operations

Plans, current projection, events, and command idempotency live in PostgreSQL.
After process loss, rerun `list` or `show` and retry an uncertain command with
the same idempotency key. Do not edit the state table or delete an event.

The internal aggregate snapshot includes `care_execution` with active,
not-started, in-progress, completed, cancelled, expired/invalidated counts,
oldest executable plan age, and conflict/error count. It contains no subject
identifier or human note. Structured logs use sanitized event names and reason
codes; they exclude note text and health context.

All three G9 tables use RLS and FORCE RLS. The API role has subject-scoped read
access and execute-function authority only; it has no direct mutation grant.
No PUBLIC function execution is granted.

## Deliberate boundary

G9 creates no email, SMS, WeChat, notification, alarm, device-control, provider,
or delivery record. Plan creation and execution do not mutate governed Habit or
Memory. No outcome comparison, effectiveness classification, CareOutcome, or
personalization receipt exists. A future G10 may evaluate a completed plan
against a later hard-finalized night; that work has not started.
