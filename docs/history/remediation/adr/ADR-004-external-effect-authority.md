# ADR-004: External effect authority

- Status: Accepted for remediation
- Date: 2026-08-30
- Scope: Future M15-M17 delivery/outcome migration; no G1 effects

## Decision

Every real-world effect follows this authority chain:

```text
LLM / CareStrategy
    -> candidate or proposal only
    -> deterministic policy and exact-target HITL authority
    -> durable intent
    -> effect adapter
```

## Invariants

- An LLM cannot select or resolve a recipient, create delivery authority, send
  a message, or attest completion.
- Policy and approval bind the exact target, content, adapter class, and
  authorization epoch before durable intent.
- Effect adapters execute only a valid durable intent and remain idempotent
  across retries and crashes.
- `shadow`, `legacy`, `compatibility`, and `replay` paths never acquire
  authority to trigger real external delivery.
- Replay remains synthetic and non-release.
- Provider acceptance, delivery, recipient acknowledgement, care-action
  completion, and later outcome evaluation are distinct durable states.
- Outcome evaluation describes observed association only and cannot claim
  unverified medical causation; any personalization update remains governed.

## G1 boundary

`live_delivery_enabled` defaults to false and is not consumed by production
dispatch in G1. No email, SMS, provider call, or real effect is introduced.
