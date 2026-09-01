# Care Outcome and Personalization Governance

Status: authoritative G10 contract
Policy: `care-outcome-evaluation.v1`
Schema: migration `022_care_outcome_feedback.sql`

## Authority chain

G10 begins only with a G9 `CareExecutionEvent` whose resulting state is
`completed`, whose authority is `human_attested`, and whose plan/grant/source
authority was valid when the event was committed. Approval, plan creation,
START, cancellation, expiry, invalidation, and supersession do not create a
normal outcome evaluation.

Human completion is not device verification. It defines the start of an
observation window. Baseline and follow-up facts retain their own
NightFinalization, episode-revision, device/vendor, metric, and Observation V2
authority.

Every outcome has `causal_claim=false`. The permitted interpretation is a
subject-scoped observation after completion. It is never a treatment effect,
medical efficacy statement, or assertion that the action caused a change.

## Deterministic action policies

| Action | Mode | Baseline / follow-up | Metric | Window | Personalization |
|---|---|---:|---|---:|---|
| `recommend_consistent_wake_time` | wake-time consistency | 2 / 2 minimum, 7 / 7 maximum | local observed wake minute; variability delta; 10-minute stable threshold | 14 days | governed episodic Memory candidate may be proposed |
| `recommend_morning_light` | indirect sleep context | 1 / 1 | sleep-window duration only as indirect context | 7 days | forbidden |
| `request_manual_follow_up` | execution only | 0 / 0 | none | immediate | forbidden |
| `request_morning_review_feedback` | execution only | 0 / 0 | none | immediate | forbidden |

Radar does not observe light exposure. Morning-light evaluation therefore
cannot claim behavior compliance or directional physiological improvement;
`NOT_COMPARABLE` is the normal final interpretation when indirect context is
available.

No LLM chooses a metric, threshold, baseline, follow-up, quality label, or
outcome. CareStrategy payloads cannot alter the evaluation policy.

## Baseline and follow-up selection

Selection is ordered and bounded by authoritative observation end time.
Baseline nights end no later than human completion. Follow-up nights end after
completion and no later than the action policy's observation-window end.

Only the current `HARD_FINALIZED`, non-provisional finalization revision for a
night is eligible. SOFT_FINALIZED evidence does not produce final outcome
authority. A selected night binds its finalization revision ID and material
hash plus the source episode revision ID. A superseded finalization revision
is never silently reused.

Evidence without the policy metric is deterministically ineligible. Evidence
carrying the same metric under a different unit, aggregation/window semantic,
coverage semantic, or source authority is `NOT_COMPARABLE`; it is not silently
converted or dropped.

## Observation Semantics V2 compatibility

Comparison requires exact equality of:

- `metric_id`;
- canonical unit;
- window semantics;
- coverage semantics;
- source authority.

The fact must be trusted `observation_semantics.v2` and non-ambiguous.
`movement_index` is never compared with `movement_event_count`.
`legacy_ambiguous_movement` is untrusted, and `average_movement` is rejected by
the G10 contract.

## Lifecycle and revisions

The durable registration projection uses:

```text
WAITING_FOR_FOLLOWUP
→ READY_FOR_EVALUATION
→ EVALUATED | INSUFFICIENT_DATA | NOT_COMPARABLE
```

No follow-up means `WAITING_FOR_FOLLOWUP`, not `STABLE` or no improvement.
Insufficient coverage or an expired window with too few eligible nights yields
`INSUFFICIENT_DATA`.

CareOutcome rows are immutable. Their semantic identity binds execution,
policy, exact baseline set, exact follow-up set, comparison facts, category,
and `causal_claim=false`. The same inputs converge on one outcome. A materially
revised current finalization produces a new CareOutcome revision whose
`supersedes_care_outcome_id` points to the retained prior result. Durable
operations are bounded, lease-fenced, restart-safe, and unique by completion
or triggering finalization revision.

When the action-specific minimum number of current HARD_FINALIZED follow-up
nights becomes available, the finalization hook advances a waiting
registration to `READY_FOR_EVALUATION` before the Worker claim. This makes the
ready state and aggregate operational signal real rather than inferred after
evaluation has already completed.

## Personalization boundary

Each CareOutcome gets a distinct immutable `PersonalizationEffectReceipt`.
The receipt answers only whether the episode may inform later personalization.
It is not itself Habit or Memory authority.

For a comparable consistent-wake-time episode, `IMPROVED`, `STABLE`, and
`WORSENED` may all propose a subject- and episode-scoped governed Memory
candidate. The candidate is compatible with the existing
`MemoryChangeCandidate` contract, binds the exact canonical candidate hash,
uses `accepted_evidence` provenance, and requires the existing exact elder
confirmation path. The existing governed Memory change/confirmation functions
accept this exact candidate and expose only the confirmed revision to future
analysis. One episode never becomes a universal claim about recommendation
effectiveness.

Late authoritative evidence creates a new receipt whose
`supersedes_receipt_id` binds the prior receipt. Operational candidate counts
join only the current CareOutcome projection, so a historical superseded
proposal is not counted as current personalization authority.

G10 does not propose a Habit from one execution. The reviewed Habit catalog
has no authority to infer a stable wake-time habit from this evidence, and all
Habit changes independently require exact elder confirmation.

Hard boundaries:

```text
CareOutcome
→ PersonalizationEffectReceipt
→ confirmation-required candidate
→ existing Habit/Memory governance
→ accepted/rejected immutable revision
```

There is no direct insert from G10 into
`backend_habit_profile_revisions_v2` or
`backend_governed_memory_revisions_v2`. Once an authorized elder accepts a
compatible Memory candidate through existing governance, the existing
longitudinal Memory query path makes that governed state available to future
SleepCare, EvidenceReasoning, and CareStrategy analysis.

## Triggering, recovery, and operations

The G9 completion transition registers the evaluation and enqueues an immediate
`care.outcome.evaluate.v1` operation plus one idempotent delayed window-expiry
operation for observational policies. A future HARD_FINALIZED revision enqueues
a bounded follow-up operation; there is no polling framework. Migration 022
idempotently backfills already-completed G9 plans into this same path. Existing
durable claim, lease, fencing, retry, checkpoint, and finalization machinery
owns the operation boundary.

Sanitized events are:

- `care_outcome_waiting`;
- `care_outcome_ready`;
- `care_outcome_evaluated`;
- `care_outcome_insufficient_data`;
- `care_outcome_not_comparable`;
- `care_outcome_superseded`;
- `personalization_effect_receipt_created`;
- `personalization_candidate_proposed`.

The protected aggregate includes pending/ready/evaluated counts, oldest wait
age, insufficient/not-comparable/superseded counts, and personalization
candidate proposal/accept/reject counts. It carries no subject or note label.

All three G10 tables use subject/generation RLS and FORCE RLS, revoke PUBLIC
access, and preserve append-only outcome/receipt evidence. A Worker may create
evaluation evidence but cannot bypass Habit/Memory confirmation authority.

## Terminal product contract

`python -m sleepagent.care_cli outcome <plan-id>` and `outcomes` use the same
API authority boundary as existing care commands. Normal zh-CN output shows
the care plan, execution status/time, observation window, before/after facts,
result, evidence quality, bounded explanation, and non-causality disclaimer.
`--trace` adds only pinned revision IDs, policy identity, outcome hash, and
receipt identity.

No follow-up renders `等待后续睡眠数据`. Incomplete evidence renders
`数据不足，暂无法评估`. Internal exceptions, raw health payloads, subject IDs,
and database details are not rendered.

## External-effect isolation

G10 creates no email, SMS, notification, alarm, provider delivery, or device
control. External effects remain zero.
