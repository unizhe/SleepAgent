# SleepAgent v1 sleep-habit concept coverage

_Architecture freeze artifact — authority: `sleep_habit_profile/PLAN.md`_

## Purpose and authority

This matrix freezes the architecture scope for the first engineering version of
SleepAgent's sleep-habit capability. It is the authoritative coverage list for
engineering catalog work; it does not claim that the current Python seed
catalog, persistence, UI, medical review, or release evidence is complete.

The current `DEFAULT_HABIT_CONCEPTS` contains 22 reviewed skeleton definitions,
including the online night-activity and Care-delivery concepts. Engineering
must add the remaining 16 v1 entries below rather than broaden an existing
concept incompatibly.
If an existing concept's question meaning, answer range, respondent rule,
persistence eligibility, or safety route must change, the versioning rules in
`PLAN.md` apply.

Status meanings:

- `v1`: required for the first complete engineering implementation.
- `v1.1`: explicitly deferred; absence does not block v1.
- `unsupported`: outside Habit capability and must not be inferred or stored as
  a Habit Profile fact.

Context ownership meanings:

- `Habit Profile`: a confirmed, structured, field-expiring personal habit,
  preference, goal, or non-clinical constraint.
- `Recent Context`: an Episode-scoped or deliberately short-lived report. A
  `profile_eligible` recent report may be stored only after elder confirmation,
  but it never becomes a timeless stable habit.
- `ObjectiveBaseline`: a deterministic, window-bound Trend/Baseline Artifact,
  not a Questionnaire answer or Habit Profile fact.
- `Safety`: a separately governed clinical/safety event or context, never a
  Habit Profile fact.

Trigger abbreviations:

- `OLI`: `optional_light_intake`
- `EHQ`: `explicit_habit_question`
- `DRG`: `decision_relevant_gap`
- `SFN`: `stale_fact_needed`
- `SC`: `source_conflict`
- `EPR`: `explicit_profile_review`

Decision gap codes are defined normatively in
`DECISION-GAP-MAPPING.md`.

## Frozen v1 Questionnaire and Profile concepts

| Concept ID | Meaning | Context owner | Respondent | Answer contract | Persistent eligibility | Allowed triggers | Decision gaps affected | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `habit.primary_goal` | The one sleep-related experience the elder most wants to improve now | Habit Profile | `elder_only` | `choice`; unknown/variable/prefer-not allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, EPR | C-GOAL, C-BURDEN, C-FOLLOWUP | v1, existing seed |
| `habit.maintain_preference` | A current sleep habit or routine the elder explicitly wants to preserve | Habit Profile | `elder_only` | bounded `short_text`; `user_data` only | `profile_eligible`, 180-day class | OLI, EHQ, DRG, SFN, EPR | C-GOAL, C-ACTION-FIT, C-FOLLOWUP | v1, add |
| `habit.schedule_constraint` | Required wake/evening timing caused by work, care, family, or fixed activity | Habit Profile | `elder_only` | `choice`; variable/not-applicable allowed | `profile_eligible`, 180-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING, C-BURDEN, C-COORDINATION | v1, existing seed |
| `habit.usual_bedtime_window` | The elder/observer-reported usual local-time bedtime window, distinct from both preferred timing and radar-derived phase | Habit Profile | `elder_or_observer`; observer requires a stated observation window/opportunity | `choice` over reviewed broad local-time windows; variable/unknown allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.usual_wake_time_window` | The elder/observer-reported usual local-time wake window, distinct from both preferred timing and radar-derived phase | Habit Profile | `elder_or_observer`; observer requires a stated observation window/opportunity | `choice` over reviewed broad local-time windows; variable/unknown allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.schedule_regularity` | The reported degree/pattern of day-to-day sleep schedule regularity, without computing a score | Habit Profile | `elder_or_observer`; observer requires a stated observation window/opportunity | `choice`; regular/varies-by-day/variable/unknown allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.preferred_bedtime_window` | The local-time bedtime window the elder prefers to maintain, not radar-observed bedtime | Habit Profile | `elder_only` | `choice` over reviewed local-time windows; variable allowed | `profile_eligible`, 180-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.preferred_wake_time_window` | The local-time wake window the elder prefers to maintain, not radar-observed wake time | Habit Profile | `elder_only` | `choice` over reviewed local-time windows; variable allowed | `profile_eligible`, 180-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-PHASE-CONTEXT, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.nap_pattern` | Whether and how often the elder usually naps | Habit Profile | `elder_or_observer` | `choice`; variable allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, SC, EPR | E-DAYTIME-CONTEXT, E-BEHAVIOR-ALTERNATIVE, E-SOURCE-CONFLICT, C-TIMING, C-ACTION-FIT | v1, existing seed |
| `habit.nap_time_window` | The usual approximate local-time window for naps | Habit Profile | `elder_or_observer` | `choice` over reviewed broad windows; variable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-DAYTIME-CONTEXT, E-BEHAVIOR-ALTERNATIVE, E-SOURCE-CONFLICT, C-TIMING | v1, add |
| `habit.nap_duration_minutes` | Approximate usual duration of one nap | Habit Profile | `elder_or_observer` | `bounded_number`, minutes; variable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-DAYTIME-CONTEXT, E-BEHAVIOR-ALTERNATIVE, E-SOURCE-CONFLICT, C-ACTION-FIT | v1, existing seed |
| `habit.pre_sleep_behavior` | One repeated pre-sleep behavior relevant to the active decision, not a complete ritual inventory | Habit Profile | `elder_only` | `choice`; variable/not-applicable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, EPR | E-BEHAVIOR-ALTERNATIVE, C-ACTION-FIT | v1, existing seed |
| `habit.stimulant_timing` | Relative timing pattern of coffee or strong tea | Habit Profile | `elder_only` | `choice`; variable/not-applicable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, EPR | E-BEHAVIOR-ALTERNATIVE, C-ACTION-FIT | v1, existing seed |
| `habit.alcohol_timing` | Relative timing pattern of alcohol, without quantity scoring or moral labels | Habit Profile | `elder_only` | `choice`; variable/not-applicable/prefer-not allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, EPR | E-BEHAVIOR-ALTERNATIVE, C-ACTION-FIT | v1, add |
| `habit.large_fluid_timing` | Relative timing pattern of large fluid intake near the sleep period | Habit Profile | `elder_only` | `choice`; variable/not-applicable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, EPR | E-BEHAVIOR-ALTERNATIVE, C-ACTION-FIT | v1, add |
| `habit.environment_preference` | Preferred light and sound environment for sleep | Habit Profile | `elder_only` | `choice`; no-good/bad labeling | `profile_eligible`, 180-day class | EHQ, DRG, SFN, SC, EPR | E-ENVIRONMENT-ALTERNATIVE, E-SOURCE-CONFLICT, C-ENVIRONMENT-FIT | v1, existing seed |
| `habit.temperature_preference` | Broad temperature/airflow preference that may constrain a low-risk action | Habit Profile | `elder_only` | `choice`; variable/no-preference allowed | `profile_eligible`, 180-day class | EHQ, DRG, SFN, SC, EPR | E-ENVIRONMENT-ALTERNATIVE, E-SOURCE-CONFLICT, C-ENVIRONMENT-FIT | v1, add |
| `habit.nonclinical_sleep_aid` | A necessary non-clinical comfort aid such as an eye mask, earplugs, fan, or pillow arrangement | Habit Profile | `elder_only` | bounded `short_text`; not-applicable allowed; medical devices route out | `profile_eligible`, 180-day class | EHQ, DRG, SFN, SC, EPR | E-ENVIRONMENT-ALTERNATIVE, E-SOURCE-CONFLICT, C-ENVIRONMENT-FIT, C-ACTION-FIT | v1, add |
| `habit.acceptable_care_burden` | The amount of change or follow-up burden the elder is currently willing to accept | Habit Profile | `elder_only` | `choice` from none/one-small-step/simple-repeatable-step; variable allowed | `profile_eligible`, 90-day class | OLI, EHQ, DRG, SFN, EPR | C-BURDEN, C-ACTION-FIT, C-FOLLOWUP | v1, add |
| `habit.delivery_timing_preference` | Whether non-urgent feedback is preferred immediately, in the morning, conditionally, or not at all | Habit Profile | `elder_only` | `choice`; variable/no-reminder allowed | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY | v1, implemented |
| `habit.delivery_modality_preference` | Preference for voice, light, or silent recording for non-urgent delivery | Habit Profile | `elder_only` | `choice`; no-fixed-preference allowed | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY | v1, implemented |
| `habit.interruption_burden` | Tolerable interruption level for a non-urgent night interaction | Habit Profile | `elder_only` | `choice`: none/low/medium/high | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY, C-BURDEN | v1, implemented |
| `habit.family_notification_preference` | Non-urgent preference for no, morning, immediate, or conditional family notification | Habit Profile | `elder_only` | `choice`; Safety policy may override only through the reviewed path | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY, C-COORDINATION | v1, implemented |
| `habit.quiet_hours` | Elder-declared time window in which non-urgent active delivery should remain silent | Habit Profile | `elder_only` | bounded `short_text` pending reviewed time-window options | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY, C-TIMING | v1, implemented |
| `habit.voice_volume_preference` | Preferred voice-delivery volume within device policy limits | Habit Profile | `elder_only` | `bounded_number`, percent 0–100 | `profile_eligible`, 180-day class | EHQ, DRG, SFN, EPR | C-DELIVERY | v1, implemented |
| `habit.action_constraint` | A non-clinical time, family, home, mobility, or environment constraint on a proposed action | Habit Profile | `elder_only` | bounded `short_text`; not-applicable allowed; clinical content routes out | `profile_eligible`, 90-day class | EHQ, DRG, SFN, EPR | C-TIMING, C-BURDEN, C-ENVIRONMENT-FIT, C-COORDINATION, C-FOLLOWUP | v1, add |

## Frozen v1 Recent Context and observation concepts

| Concept ID | Meaning | Context owner | Respondent | Answer contract | Persistent eligibility | Allowed triggers | Decision gaps affected | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `habit.sleep_satisfaction_recent` | Elder's own recent sleep satisfaction over the reviewed time window | Recent Context | `elder_only` | `scale`; unknown/prefer-not allowed | `profile_eligible` only as confirmed short-lived context, 14-day class | OLI, EHQ, DRG, SFN, EPR | E-DAYTIME-CONTEXT, C-GOAL, C-FOLLOWUP | v1, existing seed |
| `habit.daytime_impact_recent` | Elder's own recent daytime impact or sleepiness, without a standardized scale | Recent Context | `elder_only` | `scale`; unknown/prefer-not allowed; urgent severity routes to Safety | `profile_eligible` only as confirmed short-lived context, 14-day class | OLI, EHQ, DRG, SFN, EPR | E-DAYTIME-CONTEXT, C-GOAL, C-FOLLOWUP | v1, add |
| `habit.observed_snoring` | Recent directly observed snoring/gasping behavior and observation opportunity | Recent Context, conditionally Safety | `elder_or_observer`; family requires direct opportunity | `choice` plus observation window/opportunity/confidence | non-urgent report may be `profile_eligible`, 30-day class; safety hit is never a Profile candidate | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, E-SOURCE-CONFLICT | v1, existing seed |
| `habit.night_toileting_pattern` | Elder/observer-reported usual night toileting pattern, distinct from a single event | Habit Profile | `elder_or_observer`; observer requires direct opportunity | `choice`; variable/unknown allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, C-DELIVERY | v1, implemented |
| `habit.night_out_of_bed_frequency` | Reported usual number of night out-of-bed events | Habit Profile | `elder_or_observer`; observer requires direct opportunity | `bounded_number`, count/night | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION | v1, implemented |
| `habit.night_out_of_bed_time_window` | Reported usual approximate local-time window for night out-of-bed activity | Habit Profile | `elder_or_observer`; observer requires direct opportunity | bounded `short_text`; variable/unknown allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, C-DELIVERY | v1, implemented |
| `habit.night_out_of_bed_duration_minutes` | Reported usual duration of one night out-of-bed event | Habit Profile | `elder_or_observer`; observer requires direct opportunity | `bounded_number`, minutes | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION | v1, implemented |
| `habit.night_activity_assistance_need` | Whether usual night activity is self-managed or requires occasional/usual assistance | Habit Profile | `elder_or_observer`; observer requires direct opportunity | `choice`; variable allowed | `profile_eligible`, 90-day class | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, C-DELIVERY, C-COORDINATION | v1, implemented |
| `habit.observed_night_leaving` | Recent directly observed night leaving/wandering behavior and observation opportunity | Recent Context, conditionally Safety | `elder_or_observer`; family requires direct opportunity | `choice` plus observation window/opportunity/confidence | non-urgent report may be `profile_eligible`, 30-day class; injury/fall hit routes to Safety | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, E-SOURCE-CONFLICT, C-COORDINATION | v1, implemented |
| `habit.observed_abnormal_night_behavior` | Recent directly observed talking, shouting, unusual movement, confusion, or other abnormal night behavior | Recent Context, conditionally Safety | `elder_or_observer`; family requires direct opportunity | `choice` over reviewed observable categories plus opportunity; no diagnosis labels | non-urgent report may be `profile_eligible`, 30-day class; safety hit is never a Profile candidate | EHQ, DRG, SFN, SC, EPR | E-NIGHT-OBSERVATION, E-SOURCE-CONFLICT, C-COORDINATION | v1, add |
| `habit.device_position_last_night` | Whether the radar position/obstruction changed for the relevant night | Recent Context / data quality | `elder_or_observer` | `choice`; unknown allowed | `episode_only` | DRG | E-DATA-QUALITY | v1, existing seed |
| `habit.sleep_location_last_night` | Whether the elder slept elsewhere or spent a substantial period outside the monitored sleep location | Recent Context / data quality | `elder_or_observer` | `choice`; unknown allowed | `episode_only` | DRG | E-DATA-QUALITY | v1, add |

## Frozen v1 temporal semantics

Subjective schedule concepts and ObjectiveBaseline use the same temporal
comparison contract:

- `timezone_name` is the IANA timezone effective for the reported/observed
  window; UTC alone is not an elder-facing schedule meaning.
- `sleep_day_rule` is `wake_date` in v1: an overnight main sleep period belongs
  to the local date on which the elder wakes.
- A reviewed broad time-window option resolves to local start/end times plus an
  explicit `crosses_midnight` flag; labels alone are not the stored value.
- A change of timezone or an unknown timezone prevents automatic comparison or
  merge. It yields unknown/conflict context; recurring travel patterns remain
  v1.1.
- ObjectiveBaseline must expose the same timezone and sleep-day rule before
  Evidence compares it with a subjective usual/preferred window.

This is the architecture rule; exact reviewed option boundaries remain catalog
configuration.

## Frozen v1 ObjectiveBaseline metrics

These IDs are metric/artifact IDs, not `HabitConceptDefinition` IDs. They are
never selected by Questionnaire Tool and never become Profile candidates.

| Metric ID | Meaning | Context owner | Producer / answer type | Persistence | Trigger/use | Decisions affected | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline.bed_phase` | Distribution/typical local sleep-entry or bed phase over a validated window | ObjectiveBaseline | deterministic Trend/Baseline Tool Artifact | automatic window-bound Artifact with coverage, quality, data and algorithm version | current-night/trend/profile explanation | E-PHASE-CONTEXT | v1 |
| `baseline.wake_phase` | Distribution/typical local wake/leave phase over a validated window | ObjectiveBaseline | deterministic Trend/Baseline Tool Artifact | same as above | current-night/trend/profile explanation | E-PHASE-CONTEXT | v1 |
| `baseline.sleep_regularity` | Validated regularity/variability metric over a defined sleep-day window | ObjectiveBaseline | deterministic Trend/Baseline Tool Artifact | same as above | trend and phase interpretation | E-PHASE-CONTEXT | v1 |
| `baseline.night_out_of_bed` | Windowed distribution of radar-observed night out-of-bed events: events/valid night, usual count range, local-time windows, median/P90 duration, nights with events and recent change | ObjectiveBaseline | deterministic Trend/Baseline Tool Artifact; typed `NightOutOfBedBaselineValue` | same as above | night behavior interpretation, relative-deviation and conflict handling | E-NIGHT-OBSERVATION | v1, implemented skeleton |

Every ObjectiveBaseline Artifact must expose `unavailable | provisional |
established`, the actual observation window, timezone/sleep-day rule, valid
night count, coverage, quality, data version, algorithm ID/version, and source
refs. Threshold values remain Trend/Baseline Tool configuration, not Habit
catalog content.

## Frozen Safety boundary entries

These are routing categories, not Profile concepts.

| Boundary ID | Meaning | Context owner | Source | Habit persistence | Route | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `safety.habit_response_urgent_signal` | Breathing difficulty, chest pain, loss of consciousness, injury, uncontrollable sleepiness, or another reviewed urgent response signal | Safety | any current answer, preserving actor/source | never | stop remaining Habit questions and enter deterministic urgent/safety path | v1 |
| `safety.habit_clinical_context` | Disease, allergy, medication, dose, medical device, contraindication, or diagnostic/standardized-scale material encountered during Habit collection | Safety / Clinical Care Context | current answer or unstructured candidate | never | route outside Habit Profile to the authorized clinical/safety workflow | v1 |

## Explicit v1.1 deferrals

| Candidate ID | Deferred meaning | Reason for deferral | Status |
| --- | --- | --- | --- |
| `habit.travel_timezone_pattern` | Recurrent travel/timezone-related schedule pattern | Requires additional sleep-day/timezone semantics beyond ordinary home use | v1.1 |
| `habit.household_sleep_disturbance` | Recurrent disturbance from another household member or shared room | Useful but not required to establish the v1 architecture | v1.1 |
| `habit.detailed_substance_pattern` | Quantity/frequency detail beyond relative caffeine/alcohol timing | Higher sensitivity and can drift toward clinical screening | v1.1 |
| `habit.multi_environment_configuration` | Detailed multi-select bedroom configuration | v1 broad preferences and bounded aids are sufficient | v1.1 |

## Explicitly unsupported in Habit capability

| Category | Required destination | Status |
| --- | --- | --- |
| PSQI, ISI, ESS, or fragments presented as a custom Habit questionnaire | Independent reviewed assessment workflow, if ever introduced | unsupported |
| Disease history, allergies, complete medication list, doses, medical contraindications, or medical sleep devices | Clinical/Care Safety Context | unsupported |
| Demographic profile or population labels | Authorized identity/profile domain outside Habit | unsupported |
| Composite sleep-habit score, good/bad type, ranking, or profile-completeness score | No destination; prohibited output | unsupported |
| Radar-derived bedtime, wake time, regularity, or out-of-bed result rewritten as a confirmed self-reported habit | ObjectiveBaseline only | unsupported |
| Complete mandatory sleep diary | Independent prospective diary tool after separate review | unsupported |

## Engineering catalog delta

The following 16 v1 Questionnaire concept IDs remain additions relative to the
current 22-definition catalog:

1. `habit.maintain_preference`
2. `habit.usual_bedtime_window`
3. `habit.usual_wake_time_window`
4. `habit.schedule_regularity`
5. `habit.preferred_bedtime_window`
6. `habit.preferred_wake_time_window`
7. `habit.nap_time_window`
8. `habit.alcohol_timing`
9. `habit.large_fluid_timing`
10. `habit.temperature_preference`
11. `habit.nonclinical_sleep_aid`
12. `habit.acceptable_care_burden`
13. `habit.action_constraint`
14. `habit.daytime_impact_recent`
15. `habit.observed_abnormal_night_behavior`
16. `habit.sleep_location_last_night`

Engineering must register these as reviewed definitions before enabling their
selection. Exact localized wording, reviewed options, TTL values inside the
declared validity class, and named domain/medical approval are implementation
and governance work; they may not change the frozen semantic owner,
respondent boundary, persistence class, decision-gap mapping, or safety route
without architecture/version review.
