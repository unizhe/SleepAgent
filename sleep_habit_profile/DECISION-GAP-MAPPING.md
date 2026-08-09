# Sleep Habit Decision-Gap Mapping Freeze

Status: architecture-frozen  
Scope: why a sleep-habit question may be asked and what decision may change  
Authority: this document is normative for
`HabitQuestionSelectionRequest.decision_kind`,
`decision_gap_ref`, catalog `affects_decisions`, and Agent routing.

## 1. Core rule

A missing profile field is not, by itself, a decision gap.

A `decision_relevant_gap` exists only when its owner can state all of the
following before asking:

1. the current Evidence or Care decision being made;
2. at least two supported outcomes, explanations, action parameters, or
   no-action choices that remain plausible;
3. the registered gap code that separates those alternatives;
4. one or more eligible v1 concepts whose answer could change that decision;
5. the Agent that will receive and use the captured answer.

If the answer cannot change the current output, no question is selected.
Profile completeness, curiosity, future usefulness and a blank field are
prohibited reasons for a `decision_relevant_gap`.

## 2. Finite decision kinds

The architecture freezes the existing four decision kinds:

| `decision_kind` | Who may initiate | Purpose | Requires a registered gap code |
|---|---|---|---|
| `evidence` | EvidenceReasoningAgent | Reduce a bounded interpretation ambiguity or explain data quality | yes |
| `care` | CareStrategyAgent | Choose or parameterize one bounded action/follow-up/coordination result | yes |
| `profile_review` | elder via SleepCareAgent | Inspect, correct, replace or expire an existing profile value | no; explicit user purpose is the cause |
| `optional_intake` | SleepCareAgent under policy | Offer a small, skippable setup question when no urgent task competes | no; may not pretend to be required for an Evidence/Care decision |

`explicit_habit_question` is also user-caused and does not require a generated
gap. The response may still become Evidence and a profile candidate through the
normal route.

`stale_fact_needed` and `source_conflict` are collection triggers, not extra
decision kinds. Outside an explicit profile review, a stale fact must still bind
to the applicable registered `E-*`/`C-*` decision gap; a material source
conflict uses `E-SOURCE-CONFLICT`. Staleness or conflict that does not affect the
current target is shown as status when relevant and is not an excuse to ask.

No free-form decision kind is permitted in v1.

## 3. Evidence gap registry

EvidenceReasoningAgent is the semantic producer of every `E-*` gap. A
deterministic quality Tool may expose a missing fact, but Evidence must bind it
to an interpretation decision before a question is allowed.

| Gap code | Decision before asking | Eligible concept IDs | Minimal SleepCare question | Return owner | Allowed decision change / no-answer behavior |
|---|---|---|---|---|---|
| `E-PHASE-CONTEXT` | Whether an apparent sleep/wake phase or regularity difference is compatible with the reported usual pattern, preference or constraint, or remains unexplained | `habit.schedule_constraint`, `habit.usual_bedtime_window`, `habit.usual_wake_time_window`, `habit.schedule_regularity`, `habit.preferred_bedtime_window`, `habit.preferred_wake_time_window` | Ask the single usual-pattern, preference or constraint item that separates the live alternatives; never ask the user to reproduce a radar-derived metric | Evidence | May change “difference from reported/preferred routine” to “compatible with usual pattern/preference/constraint,” narrow uncertainty, or retain “unexplained.” Unknown/skip retains the current objective description without personal interpretation |
| `E-DAYTIME-CONTEXT` | Whether recent subjective impact is present and whether a nap pattern is a relevant alternative context | `habit.sleep_satisfaction_recent`, `habit.daytime_impact_recent`, `habit.nap_pattern`, `habit.nap_time_window`, `habit.nap_duration_minutes` | Ask one recent-impact or nap question selected from the alternatives already named by Evidence | Evidence | May add a bounded subjective-impact statement, keep competing context, or leave it unknown; it cannot prove a medical cause |
| `E-BEHAVIOR-ALTERNATIVE` | Whether a repeated voluntary behavior is a plausible context for the observed pattern | `habit.pre_sleep_behavior`, `habit.stimulant_timing`, `habit.alcohol_timing`, `habit.large_fluid_timing`, `habit.nap_pattern`, `habit.nap_time_window`, `habit.nap_duration_minutes` | Ask only the one behavior tied to the current alternative explanation | Evidence | May retain or remove that behavior as a plausible context, or leave alternatives unresolved; it cannot establish causality |
| `E-ENVIRONMENT-ALTERNATIVE` | Whether a preferred environment/aid is relevant to a reported or observed difference | `habit.environment_preference`, `habit.temperature_preference`, `habit.nonclinical_sleep_aid` | Ask one bounded environment preference/aid question | Evidence | May add environmental context or keep the difference unexplained; no answer leaves the objective/self-report layers unchanged |
| `E-NIGHT-OBSERVATION` | Whether a night event matches reported usual toileting/out-of-bed frequency, timing, duration and assistance pattern; whether it has direct observation support; and whether current duration/vitals/quality/trend make it a meaningful deviation | `habit.night_toileting_pattern`, `habit.night_out_of_bed_frequency`, `habit.night_out_of_bed_time_window`, `habit.night_out_of_bed_duration_minutes`, `habit.night_activity_assistance_need`, `habit.observed_snoring`, `habit.observed_night_leaving`, `habit.observed_abnormal_night_behavior` | Ask at most one event-specific usual-pattern or direct-observation question that separates the live alternatives; do not ask a generic symptom inventory | Evidence | May distinguish personal normal variation, a deviation requiring observation, or a safety escalation. It cannot downgrade an absolute red flag; Safety signals branch immediately to Safety |
| `E-SOURCE-CONFLICT` | How to present a material conflict between elder, family, Profile, or ObjectiveBaseline sources | the exact conflicting one of `habit.schedule_constraint`, `habit.usual_bedtime_window`, `habit.usual_wake_time_window`, `habit.schedule_regularity`, `habit.preferred_bedtime_window`, `habit.preferred_wake_time_window`, `habit.nap_pattern`, `habit.nap_time_window`, `habit.nap_duration_minutes`, `habit.environment_preference`, `habit.temperature_preference`, `habit.nonclinical_sleep_aid`, `habit.observed_snoring`, `habit.observed_night_leaving`, `habit.observed_abnormal_night_behavior` | Ask the authorized actor one neutral correction/confirmation question about that exact concept and time scope | Evidence | May supersede a profile candidate after confirmation, preserve both time-scoped reports, or explicitly retain conflict; never silently choose the “more authoritative-looking” source |
| `E-DATA-QUALITY` | Whether poor/missing device evidence is plausibly explained by placement or sleep location for the target night | `habit.device_position_last_night`, `habit.sleep_location_last_night` | Ask one night-specific device-position or location question only after quality evidence shows it matters | Evidence | May explain why evidence is unavailable/degraded or leave quality unexplained. It cannot turn degraded radar output into a valid measurement |

## 4. Care gap registry

CareStrategyAgent is the semantic producer of every `C-*` gap. It first receives
accepted Evidence and the care catalog. It may request one missing habit value
only when the value separates supported action choices or parameters. Captured
answers return to Evidence first; Care never consumes raw questionnaire output.

| Gap code | Decision before asking | Eligible concept IDs | Minimal SleepCare question | Return path | Allowed decision change / no-answer behavior |
|---|---|---|---|---|---|
| `C-GOAL` | Which one of several supported low-risk outcomes should be prioritized | `habit.primary_goal`, `habit.maintain_preference`, `habit.sleep_satisfaction_recent`, `habit.daytime_impact_recent` | Ask one priority or preserve-this-habit question | SleepCare → Evidence → Care | May change the selected outcome focus or produce no action. Unknown/skip uses no-action or the least-assumptive generic explanation |
| `C-TIMING` | Which supported timing parameter fits the elder's usual routine, preferred routine and constraints | `habit.schedule_constraint`, `habit.usual_bedtime_window`, `habit.usual_wake_time_window`, `habit.schedule_regularity`, `habit.preferred_bedtime_window`, `habit.preferred_wake_time_window`, `habit.nap_pattern`, `habit.nap_time_window`, `habit.action_constraint` | Ask only the usual timing, preferred timing or constraint needed to choose between current parameter options | SleepCare → Evidence → Care | May select/adjust timing, remove an infeasible option, or yield no action; it may not prescribe against an unknown constraint |
| `C-BURDEN` | Whether to offer no change, one small step, or a simple follow-up | `habit.acceptable_care_burden`, `habit.primary_goal`, `habit.schedule_constraint`, `habit.action_constraint` | Ask one burden or feasibility question | SleepCare → Evidence → Care | May reduce burden, defer, or choose no action; a non-answer never defaults to a high-burden intervention |
| `C-ENVIRONMENT-FIT` | Which reviewed environmental adjustment, if any, is compatible with preferences/home constraints | `habit.environment_preference`, `habit.temperature_preference`, `habit.nonclinical_sleep_aid`, `habit.action_constraint` | Ask one preference/constraint that separates the candidate adjustments | SleepCare → Evidence → Care | May parameterize an environment action, reject it as incompatible, or choose no action |
| `C-ACTION-FIT` | Which one of the already-supported bounded actions best preserves preferences and avoids a known behavior conflict | `habit.maintain_preference`, `habit.nap_pattern`, `habit.nap_duration_minutes`, `habit.pre_sleep_behavior`, `habit.stimulant_timing`, `habit.alcohol_timing`, `habit.large_fluid_timing`, `habit.nonclinical_sleep_aid`, `habit.acceptable_care_burden` | Ask one action-specific fit question; never run a lifestyle inventory | SleepCare → Evidence → Care | May choose one candidate, modify it, or choose no action. The answer does not prove that the habit caused the sleep pattern |
| `C-COORDINATION` | Whether/when an authorized family coordination candidate is appropriate | `habit.schedule_constraint`, `habit.action_constraint`, `habit.observed_night_leaving`, `habit.observed_abnormal_night_behavior` | Ask one authorization/constraint or observation-context question required by the candidate | SleepCare → Evidence → Care, then normal confirmation/policy gate | May draft, constrain, defer or reject coordination. It never directly sends/shares or expands family permissions |
| `C-FOLLOWUP` | Whether a previous action remains acceptable and what bounded follow-up criterion/time is feasible | `habit.primary_goal`, `habit.maintain_preference`, `habit.acceptable_care_burden`, `habit.action_constraint`, `habit.sleep_satisfaction_recent`, `habit.daytime_impact_recent` | Ask one outcome/burden/constraint question tied to the active care action | SleepCare → Evidence → Care | May continue, simplify, stop, reschedule or choose no further action; it cannot infer adherence from silence |
| `C-DELIVERY` | Whether an accepted Care action should be delivered immediately or in the morning, by voice/light/silent mode, at what burden and volume, and whether family coordination is appropriate | `habit.delivery_timing_preference`, `habit.delivery_modality_preference`, `habit.interruption_burden`, `habit.family_notification_preference`, `habit.quiet_hours`, `habit.voice_volume_preference`, `habit.night_activity_assistance_need` | Ask only the single missing delivery preference that changes the current catalog-approved delivery; otherwise use the conservative default | SleepCare → Evidence → Care, then catalog/device/coordination policy gates | May reduce interruption, defer to morning, choose voice/light/silent, constrain volume, or draft family coordination. It cannot override quiet hours or notify family without policy, and cannot suppress a Safety escalation |

## 5. Gap request contract

The architecture-level request is:

```text
HabitDecisionGapRequest
  gap_ref                  opaque unique reference
  gap_code                 one registered E-* or C-* code
  decision_kind            evidence | care
  producer_agent_id        EvidenceReasoningAgent | CareStrategyAgent
  decision_target_ref      EvidencePacket/Care candidate/plan-step reference
  decision_target_version
  live_alternatives[]      at least two, including no-action when applicable
  eligible_concept_ids[]   non-empty subset allowed by the registry row
  why_answer_changes_decision
  return_agent_id
  expires_at
  source_refs[]
```

The Questionnaire selection request carries `gap_ref`, not an unbounded
natural-language instruction. The service resolves the registered `gap_code`,
intersects its eligible concepts with:

- catalog respondent/role rules;
- trigger rules;
- current/stale/conflict state;
- prior answer/refusal/suppression state;
- the remaining episode budget.

Given the same versioned catalog and request state, candidate filtering and
selection are deterministic. A model may explain the chosen question, but may
not introduce a concept outside the intersection.

The current code skeleton already carries `decision_kind` and
`decision_gap_ref`. Engineering must add or validate the finite `gap_code`,
producer/return binding, decision target/version and live-alternative
counterfactual. Free-text `affects_decisions` metadata must be replaced by, or
validated against, this registry.

Online event context is a separate deterministic producer. For
`event_type=night_out_of_bed`,
`reasoning.resolve_event_context` creates `E-NIGHT-OBSERVATION` and the exact
concept/baseline/signal/quality/trend/clinical requirements from
`ONLINE-REASONING-FREEZE.md`; the API caller must not construct this concept
list.

## 6. Minimal-question and budget policy

- A normal episode asks at most one habit question at a time and no more than
  the existing three-question episode budget.
- Each question must cite one active trigger and, for DRG, one non-expired gap
  request.
- If several concepts are eligible, choose the lowest-burden concept that
  separates the most live alternatives; ties use catalog order.
- `unknown`, skip and prefer-not-to-answer consume the current question but do
  not create a fact.
- An elder's explicit do-not-ask withdrawal creates subject/concept
  `profile_question` suppression and ends selection for that scope until the
  elder changes it.
- An answer may reveal a new question, but it may not automatically start a
  completeness chain. The owner must produce a new decision target and gap.
- Safety preemption stops the remaining ordinary question budget.
- Profile review displays current values and sources before offering correction;
  it does not quiz the elder about all empty concepts.
- Optional intake is visibly optional and stops when the user turns to another
  task.

## 7. Result return and decision closure

Every captured answer first becomes provenance-bearing Evidence:

```text
selection receipt
  → captured answer / refusal / suppression
  → Evidence interpretation
  → accepted EvidencePacket
  → original Evidence or Care decision target
  → explanation, bounded action/parameter, or explicit no-action
```

The decision owner closes the gap by recording:

- `resolved`: the accepted answer changed or selected the result;
- `retained_uncertainty`: the answer was unknown, conflicting, insufficient or
  declined;
- `expired`: the target/version is no longer current;
- `preempted`: Safety or a higher-priority episode ended the flow.

Closing a decision gap is separate from updating Habit Profile. If the captured
claim is profile-eligible, the candidate still follows the elder confirmation
and Commit Controller route in `HABIT-SKILL-TOOL-ROUTING.md`.

## 8. ObjectiveBaseline boundary

ObjectiveBaseline values are never questionnaire candidates and never close a
gap by being copied into Habit Profile. Evidence compares a windowed,
versioned baseline with subjective habit/observation sources and preserves:

- source type and actor;
- time window and timezone/sleep-day definition;
- quality/coverage;
- uncertainty and conflict.

When subjective and objective sources differ, the allowed outputs are a
time-scoped difference, alternative explanations, a registered gap, or
“unknown.” Automatic correction of either source is prohibited.
