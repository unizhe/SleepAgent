# Care governance and HITL

## Authority chain

```text
accepted SharedNightAnalysis / CareStrategy
  → structured CareActionCandidate
  → deterministic versioned policy
  → immutable Proposal / AWAITING_APPROVAL
  → human decision
  → separate ApprovalGrant
  → terminal CarePlan
  → trusted-operator human-attested execution
```

The Agent can propose only an allowlisted structured candidate. It cannot approve, create a grant/plan, perform care, or mutate Habit/Memory. Report prose and model confidence are never executable authority. Urgent safety stays on the deterministic zero-model path.

The action taxonomy is closed to non-medical recommendations and manual follow-up tasks. Unknown actions, destination/channel fields, mismatched audiences, stale analyses, non-HARD sources, insufficient evidence, expired authority, or superseded revisions fail closed. A SOFT-created analysis is deterministically re-evaluated when its source becomes HARD before actionable Care authority is created.

## Human approval

Proposal approval/rejection/revocation is append-only and idempotent. The Product boundary revalidates service principal, actor/subject binding, role, scope, authority epochs, subject, source analysis, hard-finalization revision, policy, expiry, and expected version. Approval creates a separate `ApprovalGrant`; proposal state alone cannot execute.

## CarePlan and execution

One usable grant converges on one immutable CarePlan. State transitions are policy controlled:

```text
NOT_STARTED → IN_PROGRESS → COMPLETED
NOT_STARTED ──────────────→ CANCELLED
IN_PROGRESS ──────────────→ CANCELLED
```

Every accepted command appends an immutable event with `source_authority=human_attested`. `COMPLETED` means only that a human reported completing the bounded action. It is not device verification, clinical confirmation, effectiveness, or improved sleep.

The Terminal Care CLI is explicitly **TRUSTED_OPERATOR**. Actor, subject, role, and epoch arguments are privileged operator assertions under the configured service/database credential, not cryptographically authenticated family/elder login. Mutations require `--acknowledge-trusted-operator`.

```bash
python -m sleepagent.care_cli [authority arguments] list
python -m sleepagent.care_cli [authority arguments] start PLAN_ID \
  --acknowledge-trusted-operator --idempotency-key KEY
python -m sleepagent.care_cli [authority arguments] complete PLAN_ID \
  --acknowledge-trusted-operator --idempotency-key KEY
python -m sleepagent.care_cli [authority arguments] outcome PLAN_ID
```

The CLI calls the existing application/repository boundary and renders normal zh-CN output without hashes or raw health data. `--trace` adds bounded technical pins.

No email, SMS, WeChat, notification, alarm, provider delivery, or device-control effect is implemented.
