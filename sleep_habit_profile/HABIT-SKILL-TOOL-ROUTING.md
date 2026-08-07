# Sleep Habit Skill → Tool Routing Freeze

Status: architecture-frozen  
Scope: sleep-habit capability inside the existing 1+2+1 Agent architecture  
Authority: this document specializes `../skills_design/PLAN.md` and
`../agent_architecture/PLAN.md`; it does not add an Agent or a nineteenth Skill.

## 1. Frozen design decision

Sleep habit is a cross-cutting capability, not an independent Agent:

- SleepCareAgent owns the user-facing question, explanation, profile review and
  elder confirmation experience.
- EvidenceReasoningAgent owns interpretation of captured answers, observer
  reports, objective evidence and conflicts.
- CareStrategyAgent consumes accepted Evidence and chooses one bounded care
  action; it never interprets a raw answer or writes the profile.
- SafetyReviewAgent reviews only safety-sensitive claims/actions and may preempt
  the ordinary flow; it does not maintain a second habit profile.
- Questionnaire Tool selects and captures questions deterministically.
- Habit Profile Tool reads typed profile state and builds an atomic candidate
  change set.
- Commit Controller is the only writer. No model Skill has a state-write Tool.

The capability reuses these existing Skills:

- `ask_minimal_clarification`
- `select_memory_context`
- `propose_memory_change`
- `interpret_scoped_evidence`
- `synthesize_evidence_conflict`
- `propose_single_care_action`
- the existing safety and explanation Skills

`plan_episode` decides whether the episode contains a clarification step, but
does not itself invent a habit question.

## 2. Routing table

| Route | Owner / trigger | Existing Skill | Tool calls | Typed input | Typed output | Returned to | Permission boundary |
|---|---|---|---|---|---|---|---|
| H-R1 decide whether to ask | SleepCare; explicit habit request, profile review, optional light intake, or an accepted decision gap from Evidence/Care | `plan_episode` then `ask_minimal_clarification` | `policy.read`, then `questionnaire.select_profile` | episode/plan binding, trigger, `decision_kind`, registered `decision_gap_ref`, candidate concept IDs, concept states, alternative explanations, remaining budget | zero-or-one `HabitQuestionSelectionReceipt` with selection ID, question-bank/version refs and causal decision-gap ref | SleepCare | A question is allowed only by `DECISION-GAP-MAPPING.md` or an explicit user/profile-review trigger; unknown fields alone are not a trigger |
| H-R2 present the question | SleepCare; H-R1 selected one question | `ask_minimal_clarification` | no additional stateful Tool; render the selected Tool output | selection receipt plus role/tone policy | one plain-language question and the allowed answer/skip controls | elder or authorized family actor | SleepCare may rephrase without changing concept, answer semantics, option set, refusal behavior or provenance |
| H-R3 capture an answer | SleepCare interaction boundary; answer, unknown, skip, refusal or do-not-ask response | `ask_minimal_clarification` governs the interaction; deterministic runtime performs capture | `questionnaire.capture_profile` | exact selection receipt, actor binding, answer payload, optional suppression confirmation | `HabitQuestionCapture`, typed captured answers, refusal/suppression state, safety signal refs | captured evidence to Evidence; suppression state to questionnaire policy; safety signal to deterministic safety gate | Capture is append-only evidence, not a profile write. Family answers retain direct-observation / relayed / inferred semantics |
| H-R4 interpret scoped habit evidence | Evidence; H-R3 or a normal evidence episode needs subjective context | `interpret_scoped_evidence` | `profile.read`, `memory.read`, radar/quality/knowledge/evidence-ledger reads as required | accepted source scope, minimal relevant concept IDs, captured answer/observer evidence, objective evidence refs | `EvidencePacket`: fact/self-report/inference/unknown layers, confidence, conflicts, alternative explanations and optional registered gap request | SleepCare for explanation; Care for a care decision | Evidence reads only concepts required by the current scope. It cannot commit or silently convert a report into a fact |
| H-R5 resolve subjective/objective or source conflict | Evidence; actual conflict affects answerability or action selection | `synthesize_evidence_conflict` | `profile.read`, `evidence.read_ledger`, `memory.read`, `knowledge.retrieve_reviewed` as needed | conflicting source refs, concept IDs, baseline refs and the current decision target | bounded conflict synthesis, remaining uncertainty, optional registered gap request | SleepCare or Care according to the target decision | Conflict is retained; the Skill must not overwrite the older source merely to make the profile internally tidy |
| H-R6 read profile for elder review | SleepCare; explicit profile review or a pending candidate explanation | `select_memory_context` | `profile.read` | purpose, requested concept IDs, role, include-stale-for-review flag | minimal typed profile slice with status, source/evidence refs, confidence and staleness | SleepCare | SleepCare may read the elder-facing slice only. Care receives accepted Evidence rather than unrestricted raw Profile |
| H-R7 propose profile change | SleepCare; an elder-origin answer or elder review supplies a profile-eligible candidate | `propose_memory_change` | `profile.read`, `memory.compare`, `profile.build_change_set` | eligible captured-answer refs, current concept versions, requested create/replace/expire operation, reason | atomic `HabitProfileChangeSet` plus elder-readable change summary; still uncommitted | elder for explicit confirmation | Only elder-origin answers are directly profile-candidate eligible. Family input first becomes Evidence and requires an elder-facing confirmation flow before promotion |
| H-R8 confirm and commit | deterministic interaction/runtime boundary; elder explicitly confirms the exact H-R7 change set | no model Skill performs the write | `confirmation.validate`, then Commit Controller `state.commit_habit_profile` | change-set ID/hash/version, confirmation binding and scope, source/evidence refs | commit receipt and new profile version, or a typed rejection/conflict result | runtime, then minimal new-version context to SleepCare | The Commit Controller is the only writer. Confirmation is bound to the exact atomic change set; changed/stale sets require re-confirmation |
| H-R9 use habit information in care | Care; accepted Evidence establishes a care-relevant constraint/preference/goal | `propose_single_care_action` or `assess_followup_outcome` | `care.read_state`, `care.read_catalog`, `care.read_constraints`, coordination reads | accepted `EvidencePacket`, active care state and catalog constraints | one bounded action, parameters, burden, follow-up criterion, or explicit no-action | SleepCare; safety review if required | Care does not read a raw questionnaire capture and does not update Profile. Missing optional habit data cannot force an action |
| H-R10 explain the result | SleepCare; accepted Evidence/Care output available | `explain_for_elder` or `answer_grounded_question` | reviewed knowledge/artifact reads only when needed | accepted work products, relevant profile slice only for explicit review, provenance refs | grounded explanation stating what was known, reported, inferred, unknown and changed | elder/family according to authorization | Explanation cannot upgrade confidence or claim that a question answered an unrelated decision |
| H-R11 safety preemption | deterministic gate and Safety; capture/evidence/action contains an urgent or protected signal | `review_claim_and_boundary` / `review_action_and_publication` | risk/policy tools and accepted claim/action refs | signal/action plus provenance and publication target | `SafetyDecision`: continue, constrain, redirect or urgent-boundary response | SleepCare/runtime | Safety may stop or constrain the episode, but does not write Habit Profile or treat refusal as a clinical fact |
| H-R12 online event resolution | deterministic runtime; a typed sensor/algorithm event enters an Episode | `interpret_scoped_evidence` | `reasoning.resolve_event_context`, then automatic `profile.read` and `baseline.read` | event type, current signals and provenance refs | registered Evidence gap plus exact concept/baseline/quality/trend/clinical requirements | EvidenceReasoningAgent | API caller cannot hand-select or omit concepts for the event mapping |
| H-R13 Care delivery | CareStrategyAgent; accepted Evidence supports an action | `propose_single_care_action` | `care.read_catalog`, `device.read_delivery_policy`, `coordination.read_policy` | accepted claims, profile-derived preference claims, policies | one catalog action plus typed `CareDeliveryDecision` and optional CoordinationCandidate | SleepCare/runtime acceptance | no direct device actuation or family notification; conservative default when preferences are unavailable |
| H-R14 multifactor risk | deterministic runtime before publication and before any urgent-sensitive model path | `review_claim_and_boundary` / `review_action_and_publication` | `risk.classify_signal` | all six typed factors and source refs | `normal | watch | escalate`, Safety/urgent flags and reason codes | SafetyReviewAgent or urgent boundary | an absolute red flag fixes personalization to explanation-only; `escalate` cannot bypass Safety |

## 3. Skill manifest freeze

The current Python catalog is an implementation skeleton. Before engineering
acceptance, its Skill allowlists must represent the following architecture
routes:

| Existing Skill | Required habit-capability Tool allowance | Reason |
|---|---|---|
| `ask_minimal_clarification` | `questionnaire.select_profile`, `questionnaire.capture_profile` | Select and capture only the Tool-governed minimal question |
| `select_memory_context` | `profile.read` in addition to generic Memory reads | Read a minimal typed Habit Profile slice for an explicit review |
| `propose_memory_change` | `profile.read`, `profile.build_change_set` in addition to compare reads | Build, but never commit, a typed profile delta |
| `interpret_scoped_evidence` | retain `profile.read`; add `reasoning.resolve_event_context` and `baseline.read` | Interpret captured reports and online events together with the exact minimum habit/baseline context |
| `synthesize_evidence_conflict` | add `profile.read` and `baseline.read` | Compare typed subjective and objective sources without flattening the conflict |
| `interpret_longitudinal_pattern` | add `profile.read` and `baseline.read` | Use window-bound trends and relevant subjective patterns without rewriting either source |
| `propose_single_care_action` | add `device.read_delivery_policy`; retain Care catalog and coordination policy | Bind timing/modality/burden/volume/quiet-hours/family notification to reviewed policy |

The runtime may execute selection/capture deterministically at the plan-step
boundary, as the current skeleton does. That does not change the semantic
owner: `ask_minimal_clarification` is the only Skill allowed to request or
present the habit clarification, and the Tool remains the source of question
identity and capture semantics.

Habit Profile H-R8 does not add `confirmation.validate` or
`state.commit_habit_profile` to `propose_memory_change` (or any other
profile-mutation Skill). It belongs to the deterministic confirmation/commit
boundary. The existing Safety publication Skill may read
`confirmation.validate` for its separate review purpose; that does not give it
profile-write authority. `state.commit_habit_profile` remains Commit
Controller-only.

## 4. Memory versus Habit Profile routing

| Information | Canonical destination | Rule |
|---|---|---|
| A value defined by a frozen `HabitConceptDefinition` | Habit Profile | It must use the profile candidate → confirmation → commit path; do not duplicate it as free-form Memory |
| General conversational preference that is useful later but is not a habit concept | Memory | Use the normal Memory candidate and confirmation policy |
| Device/algorithm-derived sleep metric | ObjectiveBaseline Artifact | Never store it as a subjective habit or generic Memory |
| A one-night event or recent symptom/experience | Evidence/Recent Context | It can inform a decision but does not automatically become a stable profile claim |
| Urgent/protected safety signal | Safety evidence and policy-controlled record | A storage refusal does not erase safety handling, and safety handling does not imply Habit Profile consent |

If a statement contains both a stable habit and a recent event, Evidence splits
the claims and preserves the same source reference. Only the stable,
profile-eligible claim enters H-R7.

## 5. Family-provided information

Family input follows:

1. H-R3 records actor identity and observation semantics.
2. H-R4 interprets it as observer evidence, not elder self-report.
3. It may immediately support a bounded evidence explanation or care
   constraint when provenance and confidence permit.
4. It cannot directly become a confirmed Habit Profile value.
5. If long-term promotion is useful, SleepCare presents the proposed value,
   source and uncertainty to the elder; only the elder-confirmed H-R7/H-R8
   change is committed.

This removes duplicate collection and prevents family authority from silently
overwriting the elder's stated preference.

## 6. No giant Habit Skill

The architecture rejects a single Skill that performs question selection,
interpretation, profile mutation and care planning. Those are different trust
and permission boundaries. A new Skill is justified only if a future decision
cannot be expressed by an existing owner Skill without crossing one of those
boundaries; adding more concepts alone is not justification.
