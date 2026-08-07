# Plan Review Log: SleepAgent Skills 与受控自进化基础
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **Package hash is self-referential.** The manifest contains `package_hash` while the whole package is hashed, and the signature location is undefined. **Fix:** define canonical file hashing that excludes self-referential fields and use a detached Registry attestation.
2. **“SkillLock” conflates eligibility with actual use.** An Episode cannot know every future optional choice as if it were already invoked. **Fix:** separate an Episode-level allowed snapshot from an Invocation-level actual bundle lock.
3. **Emergency revoke loses to immutable Episode snapshots.** A paused Episode could keep using a now-dangerous Skill if it trusts only its original snapshot. **Fix:** add a monotonic fail-closed revocation overlay checked before calls, resume and publication.
4. **Orchestrator selection has no enforceable wire contract.** Saying it chooses IDs does not stop it from returning versions or free-form instructions through the current plan schema. **Fix:** add a strict `selected_optional_skill_ids` field and deterministic mandatory-union/revalidation.
5. **Replay language overpromises after privacy deletion.** Hashes cannot reconstruct a Context that retention policy has deleted. **Fix:** distinguish recompilation while an authorized Context snapshot exists from hash-only forensic verification afterward.
6. **Signature enforcement could be silently disabled in development and later leak to production.** **Fix:** require explicit test and production trust roots and make validation fail closed.
7. **Cutover behavior is unspecified.** If the new Registry fails at startup, a hidden legacy fallback would violate the user’s “new only” decision. **Fix:** require staging-gated atomic cutover and safe-degraded/fail-closed behavior with no old fallback.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now defines canonical package hashing and detached attestations, splits Episode and Invocation locks, adds a revocation overlay, makes optional selection schema-enforced, limits replay claims to retained data, separates trust roots and makes production cutover fail closed without legacy fallback. No finding was rejected.

## Round 2 — Codex review

1. **The catalog does not cover range Evidence.** `trend_review` requires Evidence → Trend, but `interpret_night_evidence` is explicitly single-night. **Fix:** rename it to atomic `interpret_scoped_evidence`, bounded to one explicit SourceScope.
2. **Skill selection is still under-specified per Episode.** An implementer could make every Skill optional or choose unsafe substitutes. **Fix:** add a mandatory/conditional resolution matrix for every Agent/Episode path and keep urgent deterministic.
3. **Episode SkillLock freezes context-dependent eligibility too early.** Data quality and accepted work products change during an Episode. **Fix:** lock the exact version universe at Episode start but recompute applicability per Invocation from current accepted state.
4. **YAML is a code/resource attack surface.** File allowlisting alone does not prevent unsafe tags, alias bombs or Unicode path collisions. **Fix:** require a constrained parser, structural limits and rejection of tags/anchors/aliases/merge keys and normalized collisions.
5. **Hidden tests can leak through repeated submissions.** Merely hiding examples does not stop adaptive overfitting to detailed failure feedback. **Fix:** return coarse reasons, cap final-holdout attempts and rotate it.
6. **Episode-level experiment bucketing permits longitudinal crossover.** The same elder could receive champion and challenger in related follow-ups. **Fix:** bucket by a protected subject/household stable key plus experiment salt.
7. **Safety revocation does not address already published material.** Preserving history is correct, but a known misleading doctor/family artifact needs an explicit correction path. **Fix:** add auditable correction/retraction linked to the original Artifact and SkillLock.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now covers scoped Evidence without increasing the 18-Skill count, defines the baseline resolution matrix, corrects lock semantics, hardens YAML loading, protects holdouts, prevents longitudinal experiment crossover, and adds historical Artifact correction/retraction. No finding was rejected.

## Round 3 — Codex review

1. **The catalog omits formal elder material.** ReportAgent is required for saved role material, but only family and doctor Skills exist. **Fix:** replace family-only Skill with `draft_user_material` and separately qualify elder/family variants, preserving the 18-Skill baseline.
2. **A Skill can expand context by declaring fields “required.”** That would invert least privilege. **Fix:** make Skill context declarations a consumption ceiling below authenticated AgentProfile/Episode minimization and test this invariant.
3. **Optional selection is not causally bound to a target call.** A flat ID list could select another Agent's Skill or become stale before invocation. **Fix:** bind selection to plan step, Agent and purpose, then recompute/revalidate at call time.
4. **Candidates can rewrite their own tests.** The mutable-surface prose does not technically prevent edits to public `evals/` or risk metadata. **Fix:** enforce a parent-version field-level diff allowlist; automated PATCH cannot change evals, Scorecards, permissions, dependencies or risk.
5. **Baseline “pre-approved” state has no bootstrapping governance.** Foundation could self-attest all 18 Skills without named review. **Fix:** require risk-appropriate named approvals and bind them with test reports in the release attestation.
6. **Prompt examples and evaluation examples are conflated.** A compiler could inject all test cases or exceed context budgets. **Fix:** distinguish prompt example refs from `evals/`, explicitly select/hash them and cap their count/tokens.
7. **The experiment stable key remains linkable.** A raw stable subject key in telemetry would leak identity across systems. **Fix:** use a bucket-only HMAC service with per-experiment salt and never expose its input/output key to Skills or evaluators.
8. **ModelAdapter is an ungoverned instruction layer.** A provider adapter could silently add behavior outside Skill versioning. **Fix:** restrict it to protocol/field mapping and test that it cannot inject domain instructions.

VERDICT: REVISE

### Codex response

Accepted all eight findings. The plan now covers elder materials through separately qualified variants, makes context requirements non-expansive, causally binds optional selections, prevents candidates from editing their tests, governs baseline approval, separates Prompt examples from evals, hardens experiment bucketing, and constrains ModelAdapter semantics. No finding was rejected.

## Round 4 — Codex review

1. **Role variants are not independently versioned.** If elder/family live in one Package, changing one silently changes the other's hash and experiment. **Fix:** make `(skill_id, variant_id, version)` the immutable deployable unit with independent qualification and locking.
2. **The Episode lock permits only one ModelProfile.** A primary model failure cannot safely use the separately certified fallback promised by the plan. **Fix:** lock an ordered set of individually qualified primary/fallback profiles and record the actual profile per attempt.
3. **Package verification has a TOCTOU gap.** Compiler could reopen a path after validation and consume replaced bytes. **Fix:** compile only from the verified immutable in-memory object, never reopen package files.
4. **GlobalPolicy and AgentProfile provenance is weaker than Skill provenance.** Hidden hardcoded domain strings would recreate a second behavior source. **Fix:** make Policy/Profile immutable attested artifacts and include their hashes in locks/receipts.
5. **A2A-created calls can bypass skill-selection causality.** They may not exist in the original plan step mapping. **Fix:** require a validated new plan step/revision before any delegated/A2A invocation and bind selection to it.
6. **Bundle telemetry cannot by itself attribute blame to one Skill.** Automatic mutation from a multi-Skill failure would be confounded. **Fix:** label outcomes bundle-level until controlled single-variable replay/ablation establishes Skill causality.
7. **Single rare reports can poison candidate generation.** A malicious or anomalous case should alert safety without rewriting behavior. **Fix:** require a preregistered minimum cohort for automated clustering while allowing single severe events to trigger human investigation/revocation only.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now independently versions variants, supports only prequalified fallback models, removes package TOCTOU, versions Policy/Profile as first-class artifacts, binds A2A calls to revised plan steps, prevents false single-Skill attribution and adds cohort protection against one-case poisoning. No finding was rejected.

## Round 5 — Codex review

1. The 18 logical Skills now cover every registered non-urgent Agent path; elder/family role variants are independently versioned without inventing another logical Skill.
2. Authority remains closed under composition: Context, Tool, A2A, Schema, ModelProfile and publication permissions can only be narrowed, never expanded by a Package or optional selection.
3. Episode/Invocation locks, revocation overlay, fallback qualification, immutable verified bytes and Artifact correction form a coherent version/recovery chain.
4. Future evolution is separated from Foundation and has causal attribution, privacy, holdout, approval, gray-release and rollback gates; no production self-modification path is accidentally introduced in phase 1.
5. Remaining items—concrete Prompt prose, production signing infrastructure, named approvers and real-data statistical thresholds—are explicitly release inputs or later-phase work, not unresolved architecture contradictions. They may not be fabricated or bypassed during implementation.

VERDICT: APPROVED
