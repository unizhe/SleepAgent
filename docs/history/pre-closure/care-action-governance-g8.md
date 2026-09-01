# Governed CareAction and HITL authority

This document is the authoritative G8 contract. G8 ends with an inert,
durable approval capability. It creates no delivery intent and performs no
email, SMS, notification, alarm, device-control, or other external effect.

## Authority flow

```text
accepted SharedNightAnalysis / CareStrategy
  -> structured CareActionCandidateV2
  -> deterministic care-action-governance.v1 policy
  -> immutable CareActionProposal / AWAITING_APPROVAL
  -> authenticated Product actor and authoritative actor-subject binding
  -> append-only approve, reject, expire, supersede, or revoke decision
  -> separate CareApprovalGrant
  -> inert future G9 input only
```

An Agent can create only a candidate. Policy decides eligibility. Only a
currently authorized human with the exact required role and
`product:sleep:care:confirm` scope can create a grant. Neither report prose nor
model confidence is authority.

## Closed action taxonomy

| Action type | Catalog identity | Audience and approver | TTL |
| --- | --- | --- | --- |
| `recommend_consistent_wake_time` | `consistent-wake-time@1` | elder | 36 hours |
| `recommend_morning_light` | `morning-light@1` | elder | 36 hours |
| `request_manual_follow_up` | `nighttime-gentle-support@1` | family | 12 hours |
| `request_morning_review_feedback` | `morning-review-feedback@1` | family | 48 hours |

Unknown identities and mismatched catalog/action pairs fail closed. The
taxonomy contains no medical intervention, diagnosis, dispatch, delivery
channel, or device command. Candidate parameters reject destination-like keys
including email, phone, recipient, channel, SMTP, and SMS. An audience is a
semantic role; concrete contact and channel resolution are deferred to G9.

## Candidate and policy

`CareActionCandidateV2` binds the candidate and subject IDs; exact analysis,
SharedNightAnalysis hash, hard-finalization revision, CareStrategy invocation,
agent version and work product; allowlisted action semantics; evidence claim
references; urgency; audience; bounded structured parameters; creation time;
and a deterministic candidate hash. Display explanation is human context and
is excluded from executable semantic identity.

Only the accepted structured `CareStrategy.primary_action` in the canonical
SharedNightAnalysis commit enters this pipeline. Elder/family/doctor report
text, `EvidenceClaim.statement`, legacy reports, and shadow output cannot enter
it. A missing, malformed, unsupported, non-activatable, or non-confirmable
candidate is rejected.

Policy `care-action-governance.v1` requires a supported taxonomy entry,
sufficient accepted evidence references, current analysis, current hard
finalization, normal/watch urgency, matching subject/audience, and a useful
action-specific TTL. Urgent safety stays on the existing deterministic
zero-model boundary and cannot become this slower Agent-driven approval path.
The policy version, hash, decision, and reason are durable.

## Proposal and lifecycle

The semantic proposal key hashes analysis revision, action, subject, and policy
version. An exact retry converges on the existing proposal; a changed analysis
or action creates a distinct proposal. Proposal semantics, source pins, and
hashes are immutable after insert. Only state, version, and update time may
change under the transition trigger and CAS fence.

Valid transitions are:

```text
PROPOSED -> AWAITING_APPROVAL
AWAITING_APPROVAL -> APPROVED | REJECTED | EXPIRED
APPROVED -> REVOKED | EXPIRED
```

Rejected, expired, and revoked proposals cannot be resurrected. A materially
new analysis revision expires any older pending or approved proposal for that
night and appends a system supersession decision. Approval revalidates that the
source analysis is the latest and the exact hard-finalization revision is still
current. Authority never carries silently across analysis revisions.

## Human authority and decisions

The Product surface authenticates the request body and resolves the actor from
the server-side actor-subject binding. Decision functions independently verify
the service principal, process role, purpose, namespace/generation/subject,
binding ID, actor identity, exact required role, current authorization epoch,
scope, and binding validity. Request-body role claims cannot grant authority;
administration alone is not care approval authority.

The authenticated endpoints list and inspect role-scoped proposals and approve,
reject, or revoke them. They expose the structured action, bounded explanation,
night/analysis references, evidence reference IDs, policy disposition, state,
version, and expiry without dumping raw health payloads. A bounded optional
human reason is append-only audit metadata and is excluded from semantic hashes.

Every decision records actor/binding, choice, idempotency key, reason code,
previous/result state, proposal version, policy identity, and timestamp. The
decision table is append-only. Same actor/proposal/choice/idempotency retry
returns the same result and grant. A concurrent or later different terminal
decision appends a conflict audit and cannot overwrite authority.

## ApprovalGrant

Approval creates exactly one separate `CareApprovalGrant`; setting proposal
state alone is never executable authority. The grant binds proposal semantic
and candidate hashes, subject, action type, semantic audience/scope, approver
actor/role/binding, authorization epoch, policy version/hash, issued time,
expiry, idempotency key, state, version, and grant hash.

`is_usable(now, ...)` requires an active unexpired grant and exact subject,
action, authorization scope, and proposal hash. Wrong-subject and wrong-action
use, expiry, and revocation fail closed. PostgreSQL keeps active, expired, and
revoked states distinct. G9 must revalidate all bindings before any future
consumption; no G9 consumer exists in G8.

## Persistence, security, and operations

Migration 020 adds the normalized proposal, append-only decision, and grant
tables. All use RLS and FORCE RLS. The non-owner API role has scoped reads and
execute on server-context-checking decision functions, but no direct table
write. Worker authority can insert proposals/decisions and transition proposals
only inside exact scoped units of work. Semantic mutation and decision
update/delete triggers fail closed.

The protected aggregate operational snapshot adds pending count, oldest
pending age, approved-unconsumed count, expired count, revoked-grant count, and
decision-conflict/error count. It exposes no subject data. Sanitized events
cover candidate validation/rejection, proposal create/deduplicate, approval,
rejection, expiry, conflict, grant issue, and revocation.

## Future boundary

G9 may consume only a currently usable ApprovalGrant through a separately
reviewed delivery policy. It must resolve a permitted contact/channel, create
delivery authority, execute and reconcile effects, and preserve the distinction
between recommended, approved, delivered, acknowledged, completed, and observed
outcome. Approval does not write a care outcome or Memory fact. None of those
G9/G10 responsibilities are implemented or started by G8.
