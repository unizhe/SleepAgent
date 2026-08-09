# Plan Review Log: SleepAgent Agent 架构与真实行为

> 本文件是非权威历史审计与构建记录，保留的旧 roster、兼容路径和阶段性结论不再构成架构许可。当前唯一权威是 [`PLAN.md`](PLAN.md) 中冻结的四角色 roster。
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **Fact freshness is underspecified.** Agent outputs are not pinned to one immutable evidence snapshot, so a long Episode could mix dates or accept stale work. **Fix:** bind every call and commit to a hashed FactSnapshot; new evidence requires a new snapshot and legal replan.
2. **Cross-Episode writes can race.** “One active action” and shared memory are stated as intentions, but concurrent morning/chat/follow-up Episodes could overwrite each other. **Fix:** version Care Context, serialize per-subject mutations, and reject stale commits.
3. **Confirmation can be replayed against changed candidates.** Confirmation lacks candidate version, actor, scope and expiry. **Fix:** bind confirmation to candidate hash, actor, subject, scope and expiry; invalidate on change.
4. **Acceptance authority is too model-heavy.** The plan says Orchestrator validates results but does not define deterministic checks for unsupported claims, stale refs or expression drift. **Fix:** add explicit acceptance gates for Evidence, Care, Safety, Communication and Memory.
5. **Safety trigger is overbroad.** Requiring SafetyReview for any Care Proposal that “involves health impact” would make nearly every low-risk sleep action invoke Safety, contradicting conditional review. **Fix:** trigger on actions outside a reviewed low-risk catalog, contraindications, low confidence, sensitive/external scope or policy conflict.
6. **Episode minimum paths are ambiguous.** Morning/trend output requires a user-readable draft, but the table does not say when Dialogue is required versus deterministic fallback. **Fix:** list mandatory and conditional paths per Episode and reserve templates for explicit degraded behavior.
7. **Risk timing is unclear.** Urgent preflight and post-Evidence risk classification are conflated. **Fix:** run urgent matching before models; run evidence-dependent risk classification only after Evidence acceptance.
8. **Budgets omit planner/evaluator and repair calls.** Agent limits alone allow Orchestrator evaluations, schema repairs and retries to exceed the intended ceiling. **Fix:** add total model-call caps and bounded repair/retry semantics; forbid blind retry of unknown external effects.
9. **A2A edge list omits Dialogue safety review.** Dialogue is described as requesting Safety but the allowed-edge list does not permit it. **Fix:** add Dialogue → Safety with typed reasons.
10. **Provider request IDs are not universally available.** Making them unconditional can fail compliant providers. **Fix:** require internal invocation/input/parent IDs always and provider request IDs when supported.

VERDICT: REVISE

### Codex response

Accepted all ten findings. The plan now defines immutable FactSnapshots, Care Context versioning, confirmation binding, deterministic acceptance gates, narrower Safety triggers, explicit Episode paths, two-phase safety ordering, total model budgets, retry/unknown-outcome rules, the missing A2A edge and provider-ID fallback. No finding was rejected.

## Round 2 — Codex review

1. **The snapshot invariant blocks valid downstream work.** Requiring every call to read one FactSnapshot that also contains the Ledger version makes Care/Dialogue unable to consume Evidence accepted during the same Episode. **Fix:** freeze only external/entry facts in FactSnapshot and version accepted in-Episode work separately with `episode_state_revision`.
2. **Shared Ledger commits can still race.** Round 1 serializes action/memory/external writes but omits concurrent Evidence Ledger mutations. **Fix:** version every shared-state commit; allow append-only Evidence rebase only after snapshot/ref/conflict revalidation.
3. **Temporal grounding is not a first-class contract.** “Fresh Ledger” is undefined, so dialogue can answer a 30-day or historical question from the wrong night. **Fix:** add explicit SourceScope with exact range, timezone, as-of and valid-night count; announce scope changes.
4. **Pause/resume semantics are absent.** `waiting_user` and `waiting_confirmation` can resume against changed facts or consume unbounded wall time. **Fix:** checkpoint and pause active budget, revalidate auth/urgent/snapshot/context/candidate on resume, expire or supersede stale work.
5. **Tool results lack a trust and provenance contract.** Agent outputs are strict, but provider/RAG/tool returns can still inject instructions or hide unknown side effects. **Fix:** require ToolReceipt, trust labels, scope non-expansion, instruction/data separation and confirmation tokens for side effects.
6. **Morning communication is internally contradictory.** The mandatory table permits a fixed morning draft while the authority section limits templates to degraded mode. **Fix:** require Dialogue for intelligent morning output; reserve registered fixed text for urgent/data-quality and labeled degradation.
7. **General-knowledge dialogue would over-call Evidence.** Not every sleep-knowledge question needs personal Ledger generation. **Fix:** allow Dialogue + Knowledge for explicitly non-personal answers while requiring fresh Evidence for personalized claims.
8. **Document precedence is unclear.** Multiple root plans and AGENTS.md still claim different current rosters. **Fix:** declare this plan authoritative for target Agent behavior, preserve product plans above it and mark old Agent plans as migration references.
9. **A2A dedupe still uses task language.** The new architecture is Episode-centric. **Fix:** key dedupe by Episode and causal request fields.
10. **Acceptance tests omit the new hard invariants.** No scenario proves scope switching, stale resume, concurrent commit, confirmation replay or prompt/tool injection resistance. **Fix:** add explicit behavioral scenarios and criteria.

VERDICT: REVISE

### Codex response

Accepted all findings. The plan now separates immutable source facts from revisioned in-Episode work, versions every shared commit, introduces SourceScope and resume rules, adds ToolReceipt/trust boundaries, resolves communication/general-knowledge routing, declares plan authority, keys A2A to Episode and adds concurrency/resume/injection acceptance scenarios. No finding was rejected.

## Round 3 — Codex review

1. **The final Orchestrator composition can bypass valid upstream products.** Even a compliant Communication Draft can be altered when the unique publisher summarizes it. **Fix:** add a deterministic publication postflight over the exact final text/Artifact and prohibit free model rewriting after it passes.
2. **Safety approval is not bound to what was actually reviewed.** A decision could be reused after a Care parameter, draft, fact or policy changes. **Fix:** bind every SafetyDecision to target type/id/hash, FactSnapshot, Episode revision, policy version and expiry; require separate target-scoped reviews.
3. **The model can self-label a novel action as low risk.** “Versioned low-risk catalog” is only a trigger description, not an activation gate. **Fix:** require registered `care_action_id/version` and bounded parameters for every activatable action; free-text suggestions remain non-activatable until reviewed and registered.
4. **Memory can silently become a personal fact source.** Relevant old memory is currently allowed into Evidence context without a hard revalidation rule. **Fix:** preserve source/confirmation/expiry metadata, treat Memory as context candidate, require current Evidence acceptance for facts and version user corrections.
5. **Role and authorization provenance is underspecified.** A role switch or prompt claim could be interpreted as authority. **Fix:** derive actor/subject/role/scope only from authenticated bindings and minimize identifiers per Agent ContextPacket.
6. **Execution truth has no final contract.** The system can return a result without proving whether it was intelligent, degraded or deterministic-only. **Fix:** define a mandatory EpisodeReceipt and three explicit execution modes shared by UI, state and audit.
7. **Safety ordering is too coarse.** Approval of Evidence could be mistaken for approval of later Care, Communication or side effects. **Fix:** make each review target-specific and validate the exact current target again at publication.
8. **Acceptance scenarios do not prove release quality under stochastic calls.** Structural assertions alone can pass with brittle or pre-baked output. **Fix:** add real-model repeated runs, fault injection, per-Agent quality dimensions and zero-tolerance hard release gates.
9. **Context minimization is named but not testable.** Strict schemas do not prevent unnecessary real identifiers from reaching models. **Fix:** add field-level minimization and identity-escalation adversarial scenarios.

VERDICT: REVISE

### Codex response

Accepted all nine findings. The plan now binds Safety and all reusable work to exact targets, gates actions through a registered catalog, separates Memory from current Evidence, authenticates authorization independently of prompts, adds deterministic publication postflight, mandatory execution receipts/modes, privacy scenarios and real-call release gates. No finding was rejected.

## Round 4 — Codex review

1. **The promised invocation whitelist is not actually specified.** A later implementation could let any first-class Agent call any subagent/tool and still claim compliance. **Fix:** add an explicit caller → Agent/tool/denial matrix and make unlisted calls deny-by-default.
2. **A dynamic plan can omit mandatory work.** The model can reduce cost by deleting an Episode checkpoint or completion condition. **Fix:** define a strict EpisodePlan and have runtime validate it against registered minimum paths, Safety checkpoints, exits and budgets before execution.
3. **The lifecycle publishes before it knows side-effect results.** A confirmation flow could tell the user an action succeeded and only then attempt the commit. **Fix:** split pre-commit validation, deterministic commit/execution and post-receipt publication; represent unknown outcomes and idempotent recovery.
4. **Episode completion is semantically ambiguous.** `complete` can mean “candidate shown,” “action activated,” “observation period ended” or merely “a reply exists.” **Fix:** define runtime-owned statuses and success conditions for every Episode; separate Episode completion from Care Action lifecycle.
5. **Waiting Episodes conflict with a single final Receipt.** Resume would either mutate history or create unaudited continuation. **Fix:** version checkpoint receipts for `waiting_*` and terminal receipts for terminal states; never overwrite prior revisions.
6. **Subagent outputs can bypass their accountable parent.** The unique Orchestrator publisher could directly use a Trend/Alert/Report draft. **Fix:** require every subagent result to return through its first-class owner/requester and pass the owner-specific acceptance gate.
7. **Side-effect tools are not assigned to a non-model authority.** Saying “Agents do not execute” is insufficient without an explicit executor. **Fix:** reserve all state mutation and external effects for a deterministic commit controller after acceptance, authorization and confirmation.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now includes a deny-by-default invocation matrix, runtime-validated EpisodePlan, per-Episode completion contract, checkpoint/terminal Receipt revisions, parent acceptance for subagents and a receipt-first deterministic commit/publication order. No finding was rejected.

## Round 5 — Codex review

The final consistency pass checked the locked user decisions, all nine Agent identities, tool reclassifications, eight Episode paths, invocation matrix, authority order, target-bound Safety decisions, completion/receipt semantics, autonomy and confirmation, failure behavior, budgets and A–T release scenarios.

No blocking or major unresolved Agent-architecture issue remains. In particular:

1. The `1+4+4` roster preserves Trend, AlertCare, Report and Memory without turning deterministic data/risk/RAG functions back into Agents.
2. Every intelligent path requires a real Orchestrator planning call plus only the necessary independent specialist calls; deterministic and degraded paths are explicitly distinguishable.
3. Runtime-owned contracts prevent planners, publishers, child Agents, stale Safety approvals, Memory, role text or confirmation replay from expanding authority.
4. Episode completion, Care Action state, state commit, side-effect outcome and user publication are separately defined and testable.
5. Remaining items under Risks / open questions are explicitly out-of-scope domain validation or empirical tuning, not undefined Agent responsibility or collaboration behavior.

VERDICT: APPROVE

### Codex response

No further plan revision is required. The consistency pass only clarified that Orchestrator has the highest authority among model Agents—not above deterministic policy—and removed wording that implied every Agent must emit candidate actions.

---

## Act 3 — Build

### Round 1 — Codex build: product Agent contracts and deterministic governance

Date: 2026-07-16  
SPEC_FILE=`agent_architecture/PLAN.md` (the product-Agent specification subordinate to `product_information_architecture/PLAN.md`)  
MAX_FIX_ROUNDS=2  
LOG_FILE=`agent_architecture/PLAN-REVIEW-LOG.md`

- Added the parallel `sleepagent.radar_agent.product_agent` namespace so the
  target product architecture does not silently reuse or rename the legacy
  SHHS, fixed-nine-Agent, or dynamic `1+4` contracts.
- Implemented the exact `1+4+4` roster, eight Episode and five SourceScope
  types, immutable FactSnapshot, revision-bound ContextPacket, strict
  per-Agent payloads and Envelope, ToolReceipt, EpisodePlan, EpisodeReceipt,
  Care Action and execution-mode contracts.
- Added deny-by-default Agent, child-Agent, A2A and tool invocation matrices.
  Model Agents cannot receive state-changing or external side-effect tools;
  only the deterministic commit controller can execute them after confirmation.
- Added runtime validation preventing a generated EpisodePlan from deleting a
  registered minimum path, Safety checkpoint, exit condition or budget bound.
- Added deterministic Evidence, Care, Safety and Communication acceptance
  gates; a reviewed versioned low-risk Care Action catalog; exact target/hash/
  policy/revision Safety binding; immutable candidate hashing; confirmation
  replay protection; per-subject CAS Care Context commits; idempotent external
  ToolReceipts that preserve unknown outcomes without blind retry; and final
  publication postflight for claim, action, number and Safety-target drift.
- Added `ProductAgentInvoker`, which performs one separately recorded structured
  model call per registered judgment Agent, selects the Agent-specific output
  Schema, records Context/Prompt/provider/input/causal metadata, and rejects
  unauthorized delegation, tool or A2A requests before an Envelope can proceed.

### Codex verification

- `PYTHONDONTWRITEBYTECODE=1 python -m pytest
  tests/test_product_agent_architecture.py
  tests/test_product_agent_governance.py
  tests/test_product_agent_invocation.py -q` — passed, 25 tests.
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q` — passed, 629 tests and
  5 skipped in 12.31 seconds; existing dynamic and legacy paths remain intact.
- `git diff --check` — passed.
- `python -m ruff check ...` could not run because Ruff is not installed in the
  current environment; Python compilation/import and pytest collection passed.

This is a completed first implementation slice, not completion of the full
Agent plan. Product Care Orchestrator planning/evaluation, Episode lifecycle and
resume, parent-owned child result integration, real tool adapters/persistence,
all A–T behavior scenarios, and gated real-provider repeated-run acceptance
remain. No frontend/backend route was changed, no legacy path was removed, and
no commit, push or remote mutation was made.

Fix rounds used: 1 (normalized CandidateAction hashing before signing so default
fields cannot create inconsistent candidate hashes).

### Round 2 — Codex build: runtime-owned Care Episode lifecycle

Date: 2026-07-16

- Added a runtime-owned `ProductEpisodeRuntime` with a real independent
  Orchestrator planning call, one bounded plan-repair attempt, deterministic
  EpisodePlan validation, a separate Orchestrator evaluation after every Agent
  result/failure, new-evidence/conflict/failure/policy-only replanning, two-
  replan and total Agent/model/tool budgets, and runtime-owned completion checks.
- Added actual Agent execution bookkeeping around `ProductAgentInvoker`:
  failed calls still consume budget and produce a safe failure record; a second
  Agent cannot run before the required Orchestrator evaluation; a model cannot
  self-report completion while required work is missing.
- Enforced child ownership. Trend/Alert/Report results remain pending until
  their registered primary parent consumes them in a new causal invocation.
  Shared Memory is logically requested by a primary Agent, physically routed
  through Orchestrator, and can only be consumed by that original requester.
- Added causal A2A target calls, Episode-scoped deduplication and two-round
  dispute limits. A logged A2A event alone does not satisfy collaboration.
- Added versioned waiting Receipts, authentication/FactSnapshot/Care Context
  resume checks, JSON-serializable `EpisodeRuntimeSnapshot`, append-only
  checkpoint storage, and restore without overwriting older checkpoint or
  Episode Receipt revisions.
- Tightened execution truth: intelligent checkpoints require all registered
  work products, partial results require verified useful publication, unknown
  side-effect outcomes cannot be reported complete, urgent flow remains
  deterministic-only with no fabricated Agent calls, and doctor materials now
  require a real SafetyReview work product as well as the targeted checkpoint.
- Added `ProductToolExecutor` and adapters that reuse the old deterministic
  DataQuality, Trend metrics, RiskSignal/urgent and reviewed RAG capabilities
  as strict tools. Read calls retry at most once, return cropped ToolReceipts,
  and enter model Context only under `tool_output_untrusted`; model Agents still
  cannot execute Care/Memory writes or external effects.

### Codex verification

- Product Agent focused suite — 42 passed:
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest
  tests/test_product_agent_architecture.py
  tests/test_product_agent_governance.py
  tests/test_product_agent_invocation.py
  tests/test_product_agent_episode_runtime.py
  tests/test_product_agent_tooling.py -q`.
- Full Python regression — `646 passed, 5 skipped in 12.41s`.
- `git diff --check` and explicit `python -m py_compile` for all six product
  Agent modules passed.
- No legacy runtime was removed, no frontend/API route was changed, and no
  commit, push or remote mutation was made.

Fix rounds used: 2.

Remaining before the Agent plan can be claimed complete: dedicated acceptance
gates for Trend/Alert/Report/Memory payload semantics; active soft-deadline and
schema-error repair/degradation paths; an end-to-end automatic Episode runner
that binds real tool receipts, Agent gates, deterministic commit and publication
without test-side choreography; the complete A–T scenario matrix including
concurrency, stale Safety, confirmation replay and cross-role assertions; and
the separately gated repeated real-provider release run. The goal therefore
remains active.

### Round 3 — Codex build: specialist gates and end-to-end Episode runner

Date: 2026-07-16

- Added deterministic acceptance gates for every remaining specialist payload:
  Trend must stay inside SourceScope/coverage and cannot emit risk or Care;
  AlertCare requires accepted parent Care/Evidence and confirmation-bound,
  deduplicated coordination candidates; Report roles share one authorized claim
  set; Memory keeps source/subject/confirmation/expiry metadata and exposes only
  confirmed, current refs as personalization candidates—not current Evidence.
- Added runtime active-clock enforcement using the registered 30/90-second soft
  deadlines. Waiting checkpoints pause the active clock, resume restarts it,
  deadline always wins over another model call, and expiry produces a truthful
  partial/blocked Receipt without a cosmetic evaluation call.
- Added exactly one budgeted retry for schema-validation or transient model
  failures. Both the failed attempt and repair have separate invocation/context/
  parent records and consume Agent/model budgets; a second failure proceeds to
  Orchestrator evaluation and degradation.
- Added `ProductEpisodeRunner`, which performs urgent preflight before every
  model, executes deterministic preflight/deferred/Agent-requested tools,
  creates the validated Orchestrator plan, invokes only selected Agents, applies
  the Agent-specific acceptance gate, evaluates after every result/failure,
  handles bounded replan, routes child and A2A calls causally, runs publication
  postflight and lets `ProductEpisodeRuntime` issue the final Receipt.
- Added deterministic-only data-quality recovery and urgent paths, an
  intelligent normal-morning path, an honest required-Agent failure path, and
  a full 30-day Evidence → Trend child → Evidence parent → Dialogue path.
  The runner no longer requires tests or callers to manually call gates and
  mutate runtime revisions between steps.
- Fixed two integration findings during the bounded passes: a failed required
  Evidence call now stops dependent Dialogue scheduling; and an accepted child
  result is visible only to its accountable parent, never automatically exposed
  as a raw child draft to downstream Dialogue/Report.

### Codex verification

- Product Agent focused suite — 54 passed in 0.98s across architecture,
  governance, invocation, Episode runtime, tooling and runner tests.
- Full Python regression — `658 passed, 5 skipped in 12.86s`.
- Explicit compilation of all seven product Agent modules and
  `git diff --check` passed.
- Current end-to-end scenario evidence covers A (normal morning), B (data
  quality), C (30-day trend), H (urgent preemption) and structural/hard-boundary
  portions of I–T. The complete D/E/F/G sequences and per-Agent J fault matrix
  are not yet fully proven, so no full A–T or release claim is made.

Fix rounds used: 2.

Remaining: complete the conflict/dialogue/action/follow-up/doctor-material
runner paths, systematic failure and authorization matrices, A–T manifest and
zero-tolerance verifier, then run the separately gated real-provider scenarios
three times. No commit, push, frontend/API mutation or legacy removal was made.

### Round 4 — Codex build: conflict, Care commit and role-material closure

Date: 2026-07-16

- Completed the self-report/device-conflict path with real causal A2A calls:
  Evidence preserves observed, user-reported and unknown semantics, requests an
  independent Safety critique, Safety returns an accepted `revise` decision and
  a causal revision request, revised Evidence is independently invoked, and a
  second exact-target Safety review approves it before Dialogue asks one bounded
  question. Both Safety decisions remain auditable as unused for the final
  communication because neither is incorrectly promoted to a blanket approval.
- Completed Care candidate execution semantics. A candidate is postflight-
  verified and checkpointed as `waiting_confirmation` without a side effect;
  resume explicitly revalidates the current authenticated binding and immutable
  FactSnapshot, resolves only an accepted and displayed candidate, validates the
  candidate/version/hash/actor/subject/scope/Care Context token, commits through
  the deterministic CAS controller, and appends confirmation/action IDs to a new
  terminal Receipt revision without rewriting the waiting Receipt.
- Completed the follow-up path through Evidence → routed Memory → revised
  Evidence → Care → Dialogue. Only confirmed, current, same-subject Memory is
  integrated by the requesting primary Agent; the resulting communication uses
  observational rather than causal language and performs no unconfirmed state
  change.
- Completed the doctor-material path through Evidence → Dialogue → Report →
  Dialogue → Safety → publication. Report remains a parent-owned child result,
  `artifact.render` runs after Report, Safety approves the exact accepted Report
  work-product hash and current Episode revision, and the selected authorized
  role artifact—not the generic Dialogue text—is postflight-verified and
  published. Draft generation performs no external share.
- Added accepted-claim numeric-token binding for publication postflight, so
  report dates/numbers can be retained only when their exact tokens derive from
  selected accepted claims; new or drifted numbers remain blocked.
- Tightened Receipt and tool truth: accepted but unused Safety decisions are now
  listed separately; failed deterministic ToolReceipts propagate to Episode
  failures; deferred tool failure yields a useful `partial/safe_degraded`
  response rather than false completion; urgent-boundary tool failure blocks
  before every model call; missing-handler exceptions fail closed instead of
  escaping the runner.

### Codex verification

- Product Agent focused suite — 60 passed in 0.81s across architecture,
  governance, invocation, Episode runtime, tooling and runner tests.
- Full Python regression — `664 passed, 5 skipped in 12.46s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. The unavailable optional `ruff` executable was not treated as proof;
  compilation and the repository test suite are the applicable checks here.
- New end-to-end scenario evidence covers D (self-report conflict), E (Care
  candidate plus confirmation commit), F (Care follow-up), G (doctor material),
  the two-round causal portion of I, tool-failure and execution-mode portions of
  J/Q, identity/confirmation portions of K/M/N, stale-target publication
  portions of P, and child-ownership portions of S.

Proof fix rounds used: 0. Post-proof self-review added failure propagation and
explicit current binding/snapshot inputs; the focused and full suites were then
rerun from clean process invocations and remained green.

Remaining before the full Agent plan can be claimed release-complete: the
systematic per-Agent Orchestrator/Evidence/Trend/Care/Safety/Dialogue/Report/
Memory/Alert fault matrix, three-role and cross-subject authorization matrix,
concurrent Care/Memory/external-action reconciliation, A–T manifest and
zero-tolerance aggregate verifier, persistent production adapters/API wiring,
and the separately gated real-provider repeated-run evaluation. No commit,
push, frontend/API mutation or legacy removal was made.

### Round 5 — Codex build: fail-closed release gates and fault/auth hardening

Date: 2026-07-16

- Added registered conservative degradation for non-role-material Episodes.
  Orchestrator planning failure now records both bounded failed planning calls
  and uses a deterministic-only partial playbook; Evidence/Trend/Care/Memory/
  Dialogue failures retain only safe accepted facts or a scope-only fallback.
  Doctor Report/Safety failures remain blocked rather than publishing an
  unreviewed artifact.
- Added failed Orchestrator invocation records and bounded planning retry
  evidence instead of losing failed provider calls when entering the playbook.
- Changed Artifact authorization so requested roles must be derivable from the
  authenticated role or explicit authorization scope. User claims remain
  `user_text_untrusted`; primary/shared Agents receive them only where needed,
  while Trend/Report/Alert child Contexts do not receive raw user text or direct
  actor/subject identifiers.
- Replaced the external-action boolean confirmation flag with a hashed
  `ExternalActionCandidate` and exact candidate/version/hash/actor/subject/
  action/snapshot/Care Context confirmation validation. Idempotency-key
  collisions, mutated payloads and confirmation replay under a new key are
  rejected before execution; unknown outcomes remain cached and are not blindly
  repeated.
- Added `acceptance.py`, an A–T scenario enum, structured observation manifest,
  hard-violation taxonomy and fail-closed release verifier. It requires all
  scenarios, five Evidence semantics, three authenticated roles, four data
  conditions, deterministic replay/fault/real-provider evidence, every Agent
  fault target, three repetitions for model-judgment scenarios, domain review
  and zero hard violations. The checked-in manifest intentionally starts empty
  and release-ineligible; tests cannot silently turn partial coverage into a
  release claim.
- Added a per-Agent fault matrix for Dialogue, Trend, Care, Memory, Report,
  AlertCare and Safety, plus existing Orchestrator/Evidence fault paths, and
  added role/minimum-disclosure assertions.

### Codex verification

- Compilation of all product Agent modules and the three changed test modules
  passed.
- Focused proof after two bounded fix passes: `38 passed, 2 failed` across
  runner, governance and acceptance-verifier tests.
- Fix pass 1 renamed a pytest parameter that collided with the reserved
  `request` fixture. Fix pass 2 corrected Context objective assembly to read
  `runtime.plan.objective`; the previous nonexistent `runtime.objective`
  attribute had caused every intelligent path to fall into the deterministic
  playbook before its first Agent call.
- The two remaining failures are isolated to one time-dependent test fixture:
  RoleMaterial and conflict models construct SafetyDecision expiry as the fixed
  `NOW + 1 hour`, which is already earlier than the actual test process time on
  2026-07-16. Both traces reach the expected Safety call, then correctly reject
  that expired decision at the acceptance gate. No production gate should be
  weakened; the fixture must use a still-valid deterministic acceptance time or
  a sufficiently future expiry in the next bounded build continuation.
- Full-suite proof was not run because the focused proof is not green. No
  commit, push, API/frontend mutation or legacy removal was made.

Fix rounds used: 2. Per the bounded build rule, no third code fix was attempted
in this round; the active goal remains incomplete.

### Round 6 — Codex build: commit recovery, concurrent side effects and Memory state

Date: 2026-07-16

- Repaired the two intentionally fail-closed Safety tests by making their test
  decisions deterministically future-valid. The production expiry gate remains
  unchanged and continues to reject stale SafetyDecision objects.
- Added append-only `InMemoryProductEpisodeResultStore` and explicit
  publication-delivery truth to `ProductEpisodeRunResult`. A confirmed Care
  action now records the real state-changing ToolReceipt before delivery. If the
  publication channel fails, revision 2 is a truthful `partial/safe_degraded`
  Receipt with `publication_delivered=false`; recovery revalidates identity and
  snapshot, reuses the same publication idempotency key, does not call the
  commit controller, and appends a `complete` revision 3 after delivery.
- Added a serialized deterministic commit boundary for Care, Memory and
  external actions. External actions reserve the current Care Context version
  before execution, convert executor exceptions to cached `unknown` Receipts,
  reject cross-subject tokens, mutated payloads, stale concurrent confirmations
  and replay under a different key, and allow only an exact same-key Receipt
  lookup without executing again.
- Upgraded `MemoryWriteCandidate` to a version/hash-bound confirmation
  candidate and added `InMemoryMemoryContextStore`. Confirmed create/replace/
  expire commits check both Care Context and FactSnapshot Memory versions,
  append an auditable ToolReceipt, retain replaced/expired history and advance
  both versions. A stale concurrent Memory candidate has zero effect.
- Added behavior assertions for publication recovery revision order, unchanged
  Care state across republish, concurrent Care candidates, external-action
  version reservation/cross-subject rejection, and confirmed Memory correction
  history.

### Codex verification

- Product Agent focused suite — `80 passed in 0.89s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `684 passed, 5 skipped in 12.73s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Relevant call-site searches found no remaining use of the removed
  boolean external-confirmation API.
- Diff/spec review confirmed state-changing Receipts precede delivery, recovery
  is append-only and does not re-run the action, and concurrent writes are
  serialized within the reference controller.

Fix rounds used: 0.

The release manifest intentionally remains empty and fail-closed. The full
Agent plan therefore remains active: actual A–T observation records, three-run
real-provider evidence, three-role/domain-reviewed expectations, grounded-
dialogue waiting/supersede coverage, and remaining release-quality audits are
not yet proven. No commit, push, frontend/API mutation or legacy removal was
made.

### Round 7 — Codex build: grounded dialogue resume and urgent interruption

Date: 2026-07-16

- Added an auditable waiting-Episode closure contract without inventing a sixth
  completion status. An authenticated goal change appends a terminal `blocked`
  Receipt with `changed_goal:<replacement_episode_id>`; an urgent interruption
  uses `interrupted_by_urgent:<replacement_episode_id>`. The original
  `waiting_*` checkpoint remains immutable in the append-only result store.
- Added `grounded_dialogue` `waiting_user` continuation. The runner restores
  runtime-owned Agent invocations, accepted work products, hashes, evidence,
  candidates, Safety state and prior ToolReceipts, preserves the existing
  Episode budgets/revision counter, invokes a new causally recorded Dialogue
  call, and appends a new waiting or terminal Receipt.
- Made resume fail closed unless a new user answer is present and the Episode
  identity/type, authenticated binding, FactSnapshot hash and Care Context
  version remain valid. Changed identity or stale facts raise
  `ResumeRequiresReplan` and perform no new model call.
- Re-ran the urgent-boundary tool before any resumed model call. A positive
  result terminally interrupts the old Episode, creates a distinct registered
  `urgent_boundary` Episode and publishes only the fixed safety prompt. The
  replacement path is deterministic-only even if its second deterministic
  check fails or disagrees, so it cannot fall through to an ordinary model.
- Added end-to-end assertions for a question checkpoint followed by a same-
  Episode answer, append-only revisions `1 -> 2`, goal-change supersede,
  urgent interruption before a second Dialogue call, and changed-identity /
  stale-snapshot rejection.

### Codex verification

- Product Agent focused suite — `84 passed in 0.92s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `688 passed, 5 skipped in 12.82s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed immutable prior checkpoints,
  distinct causal Dialogue invocation IDs, no model call after urgent resume
  detection, and exact identity/snapshot/Care Context checks.
- Fix pass 1 supplied the registered common FactSnapshot/policy ToolReceipts in
  the grounded-dialogue fixture so terminal completion truth matched the
  Episode registry. Fix pass 2 replaced the generic urgent `run()` fallback
  with a dedicated deterministic-only replacement path.

Fix rounds used: 2.

The release manifest remains intentionally empty and fail-closed. Actual A–T
observations, three independent real-provider runs, three-role/domain-reviewed
expected results and the remaining release-quality audits are still required
before the Agent plan can be called release-complete. No commit, push,
frontend/API mutation or legacy removal was made.

### Round 8 — Codex build: conditional role-material Safety paths

Date: 2026-07-16

- Corrected the `role_material` registry so Evidence, Dialogue and Report are
  the base required path while `doctor_material_or_external_share` is an
  explicit conditional Safety checkpoint. Selecting Safety without its
  registered checkpoint, inventing another checkpoint or expanding the
  request-specific required set is rejected by the deterministic plan gate.
- Added request-specific plan requirements to the Episode planner. A doctor
  artifact deterministically adds SafetyReview and the exact checkpoint to the
  planning context and validator; the Orchestrator receives one bounded repair
  attempt if it omits either. Replan preserves the prior required work products
  and Safety checkpoints instead of planning them away.
- Changed role-material publication postflight to require an exact,
  target-hash-bound Safety approval only when Safety was selected. Doctor
  material remains blocked without it. Elder and family drafts can complete
  through the minimum Evidence -> Dialogue -> Report -> Dialogue graph without
  a fabricated Safety invocation.
- Generalized the role-material behavior model fixture to all three roles and
  added assertions for elder/family minimum graphs, doctor plan repair, exact
  doctor Safety binding and zero SafetyDecision refs on non-doctor drafts.

### Codex verification

- Product Agent focused suite — `87 passed in 0.98s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `691 passed, 5 skipped in 12.86s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed that request-required doctor Safety
  survives plan repair/replan, non-doctor material does not over-call Safety,
  and publication still uses the accepted Report artifact rather than the
  Dialogue draft.
- One self-review fix pass added the invariant that any role-material plan which
  selects SafetyReview must also select its registered conditional checkpoint.

Fix rounds used: 1.

This round does not execute or imply external sharing: artifact generation is
still a draft-only result, while external share intent, confirmation and
side-effect execution remain a separate future path. The release manifest also
remains empty and fail-closed pending actual A–T observations, three-run real
provider evidence and domain-reviewed three-role expectations. No commit,
push, frontend/API mutation or legacy removal was made.

### Round 9 — Codex build: confirmed external artifact sharing

Date: 2026-07-16

- Added an authenticated `ExternalShareIntent` for `role_material`. The request
  is accepted only when the binding contains both `external.share` and the
  exact `share_target:<target_ref>` scope; page role or user text cannot create
  that authority.
- After Report integration, the runner creates one deterministic
  `ExternalActionCandidate` whose hash transitively binds the Artifact id and
  role, accepted Report work-product hash, recipient ref/role/display label and
  one-time share scope. The candidate and exact payload are persisted in the
  append-only run result and displayed in a `waiting_confirmation` Receipt
  which explicitly says no share has occurred.
- External sharing receives its own independent SafetyReview call over the
  deterministic action candidate. Doctor material plus sharing produces two
  distinct Safety decisions—one for the Artifact and one for the external
  action—so Artifact approval cannot implicitly authorize a share.
- Added `confirm_external_share`: it revalidates identity, FactSnapshot, Care
  Context, candidate/version/hash, recipient fields, current Safety policy and
  expiry before calling the serialized commit controller. The real external
  ToolReceipt is recorded before publication; success, failure and unknown are
  worded and receipted differently.
- Exact same-key completion lookup is idempotent and does not call the executor
  again. Unknown results are cached and remain `partial/safe_degraded`; they are
  never blindly retried. A post-action publication failure retains the action
  Receipt and recovers publication at revision 3 without resharing.

### Codex verification

- External-share focused runner scenarios — `6 passed` covering authorization,
  waiting/no side effect, exact confirmation, distinct doctor/action Safety,
  success replay, unknown no-retry, target mutation and publication recovery.
- Product Agent focused suite — `93 passed in 1.01s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `697 passed, 5 skipped in 12.52s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual review confirmed side-effect-before-publication ordering,
  append-only revisions, exact Report/recipient hash binding and zero model
  access to an execution capability.
- Fix pass 1 moved exact confirmation/binding/Snapshot/Safety validation ahead
  of append-only latest-result lookup. Fix pass 2 enforced equality across the
  candidate target, authenticated intent and payload recipient fields before
  the executor can run.

Fix rounds used: 2.

The external channel remains an injected deterministic executor; concrete
notification providers and deployment adapters are outside this Agent plan.
The release manifest remains empty and fail-closed pending actual A–T
observations, three-run real-provider evidence and domain-reviewed three-role
expectations. No commit, push, frontend/API mutation or legacy removal was
made.

### Round 10 — Codex build: data-quality Evidence unknown path

Date: 2026-07-16

- Restored `data_quality_recovery` as a real intelligent Episode path instead
  of always short-circuiting to a deterministic template. Its registry now
  requires EvidenceAnalysis, grants a normal model budget and the runner's
  minimum graph invokes only EvidenceAnalysis after the registered quality and
  device tools.
- Added an Episode-specific Evidence acceptance boundary: every claim must be
  `unknown`, coverage and source refs are mandatory, confidence remains subject
  to the global unknown gate, and at least one explicit missing reason is
  required. A health conclusion in an uninterpretable-data Episode is rejected
  before it can enter accepted state or publication.
- Added deterministic composition from the accepted unknown claim and missing
  reason, followed by exactly one shared registered device/record recovery
  step. No Dialogue, Care Proposal or candidate action is fabricated.
- Preserved honest fallback behavior. Orchestrator planning failure publishes a
  `partial/deterministic_only` recovery; rejected or failed Agent work publishes
  `partial/safe_degraded`. Both state the unexplainable boundary and the same
  single recovery step without a health conclusion.
- Added behavior tests for the complete intelligent graph, Orchestrator outage
  and invalid observed-fact Evidence, including execution-mode, accepted-Agent,
  publication and zero-Care-candidate assertions.

### Codex verification

- Product Agent focused suite — `95 passed in 1.02s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `699 passed, 5 skipped in 12.76s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed that the intelligent plan cannot
  omit EvidenceAnalysis, deterministic fallback remains a separate registered
  mode, accepted data-quality output cannot contain a personal health
  conclusion, and publication cannot create a Care Action.
- One self-review fix pass moved EvidenceAnalysis from a request-only runner
  requirement into the authoritative Episode registry; the explicit
  deterministic start path continues to clear Agent requirements by design.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

---

# Plan Review Log: SleepAgent 四角色 Agent 架构收口

Date: 2026-07-24

Act 1 (grill) complete — plan locked with the user. This redesign supersedes
the former `1+4+4` target roster while preserving the prior review/build
history above as migration evidence. `MAX_ROUNDS=5`.

## Round 1 — Codex review

1. **Evidence 的“事实形成权”可能与 canonical 数据冲突。** 原文容易让模型 Agent 看起来是原始事实来源。**Fix:** 将其限定为 Evidence claim 的唯一形成者，观测值只能来自 canonical 数据和 ToolReceipt。
2. **SleepCare 可以通过计划遗漏 Safety。** 条件触发若只存在于 Agent 说明中，就不是强制闸门。**Fix:** 在规划后、target 形成后和发布前运行确定性 Safety 触发检查，命中后强制 checkpoint 或阻断。
3. **Safety 的“两轮”与删减退路含糊。** 旧批准可能被复用于新 hash，删掉争议句也可能绕过复审。**Fix:** 明确首次审查后最多两次修订提交，每次新 hash 使旧决定失效，删减 target 重新过触发与发布检查。
4. **明确记忆指令可被引用文本或工具内容伪造。** “忘记”还可能被误解为擦除审计。**Fix:** 仅接受已认证用户当前最外层直接意图；引用、RAG、Tool 和 Agent 消息无授权能力；长期记忆遗忘与业务/审计保留分离。
5. **中心路由可能人为增加 SleepCare 模型调用。** 如果每次转发都要求主模型参与，会放大延迟且没有新增判断价值。**Fix:** 允许 runtime 按结构化请求直接转发，SleepCare 保持逻辑责任但不必每次 Invocation。
6. **四类成果缺少显式确定性验收门。** 仅有 Schema 和职责说明不足以阻止无来源 claim、目录外行动或最终表达漂移。**Fix:** 新增 Evidence、Care、Safety、Communication 四类 gate。
7. **`approved` Skill 不等于可承载生产流量。** 生命周期中尚有 shadow/canary/champion，任意 approved 解析会绕过发布控制。**Fix:** 生产版本还必须是当前 champion 或该主体被分配的 canary。
8. **主 Agent 禁止个人事实可能妨碍正常对话确认。** 忠实复述用户刚说的话不应强制产生一次 Evidence 调用。**Fix:** 允许标记为 `user_reported` 的直接确认，但用于解释、风险或行动前必须进入 Evidence。
9. **Agent 闭环定义可能被误读为每次调用必须循环。** Safety 首次 approve 或原子评估不一定需要工具回合。**Fix:** 明确闭环是角色生命周期能力，不是每个 Invocation 的机械步骤。

VERDICT: REVISE

### Codex response

接受全部九项。计划已补充 canonical 事实权威、runtime 强制 Safety 触发、精确复审轮次、记忆授权信任边界、无额外模型的中心转发、四类验收 Gate、Skill 发布资格、用户自述确认例外和角色级闭环定义。没有拒绝项。

## Round 2 — Codex review

1. **SleepCare 的文字职责侵入 runtime 状态权。** “创建/恢复 Episode”可能让模型决定恢复、完成和必经路径。**Fix:** runtime 独占创建、恢复、预算、完成和 Receipt；SleepCare 只提出受注册表约束的计划与评估建议。
2. **Care 读取用户反馈与 Evidence 独占个人 claim 存在冲突。** 行动反馈是业务事件，但不能被 Care 改写成客观改善或疗效。**Fix:** Care 可用带来源的确认反馈决定下一步；客观变化、比较和因果必须先进入 Evidence。
3. **Safety 退回最终文案时没有合法责任人。** 用户只锁定 Evidence/Care 修订专业内容，但最终措辞漂移属于 SleepCare。**Fix:** 事实归 Evidence、行动归 Care、纯表达归 SleepCare；SleepCare 不得借改写措辞改变专业语义。
4. **外部内容缺少统一 trust boundary。** Knowledge、Memory、设备文本或 Tool 输出可携带提示注入并改变路由。**Fix:** 增加 trust label、指令/数据隔离和 scope 不扩张规则。
5. **旧成果的失效传播未写清。** 上游 SourceScope、Policy 或 target 变化后，旧验收与 Safety 批准可能继续使用。**Fix:** 所有依赖工作成果绑定输入 hash，任一依赖变化即重新验收/重审或终止。
6. **一般知识路径可能滑入个人行动。** 主 Agent 可以从通用健康教育逐渐变成个性化建议而不调用专业 Agent。**Fix:** 个人结论必经 Evidence，具体行动选择/调整/跟进必经 Care。
7. **Safety 在权威顺序中位于 Evidence 之后，阻断语义不明确。** 它不能改事实，但必须能阻止某个 claim 被使用。**Fix:** 明确 Safety 可 veto 使用并要求 Evidence 重做，但不能成为 canonical 事实来源。
8. **硬安全审计的“完整输入输出”可能违反最小化和保留策略。** **Fix:** 调查证据进入合法、加密、限权安全审计区；常规 SkillOutcome 只保留结构化字段、refs 与 hash。

VERDICT: REVISE

### Codex response

接受全部八项并完成修订。计划现在明确 runtime/Agent 权限、反馈与 Evidence 的边界、按 target owner 修订、注入防护、失效传播、一般知识升级条件、Safety veto 语义和审计数据最小化。没有拒绝项。

## Round 3 — Codex review

1. **“最多六个”可被子 Agent、临时 Agent 或后台 Agent 绕过。** **Fix:** 上限统计所有独立模型判断身份；Invocation、Tool、Service 和离线控制面不计。
2. **唯一发布者与 deterministic urgent/failure 模板冲突。** **Fix:** SleepCare 是正常智能路径唯一发布者；runtime 模板是明确标记的非 Agent 例外，且不能生成个性化结论。
3. **Tool 权限只有原则，没有可迁移 Registry 的矩阵。** **Fix:** 增加四 Agent deny-by-default 能力表，明确只读能力和禁止项，继续使用现有 Registry。
4. **Artifact render 容易被误报为已保存/已导出。** **Fix:** render 只产生待提交内容，外部状态必须以 Commit Controller Receipt 为准。
5. **Safety 审查顺序和 target scope 不完整。** Evidence、Care、Communication 和 external action 不能共享一个批准。**Fix:** 增加四类分阶段 checkpoint 和执行前重新验证。
6. **Evidence 失败时“展示已有记录”可能绕过当前范围与权限。** **Fix:** 仅允许与当前 SourceScope、权限、有效期匹配的已验收记录。
7. **Care 失败时无条件保持行动可能违反新出现的硬禁忌。** **Fix:** 模型不生成新方案，但确定性急症、权限、确认或禁忌规则可以阻止/暂停执行。

VERDICT: REVISE

### Codex response

接受全部七项。计划已增加统一计数口径、确定性发布例外、能力矩阵、render/commit 区分、target-specific Safety 顺序、已有 Evidence 使用条件和 Care 失败时的硬策略暂停。没有拒绝项。

## Round 4 — Codex review

1. **个人基线的状态归属不清。** 把派生基线放进自由文本长期记忆会失去数据版本和时间范围。**Fix:** 基线/覆盖/趋势参考归版本化分析状态或 Artifact，Evidence 经 ToolReceipt 验证。
2. **“不可修改”业务记录缺少纠错机制。** 真正错误不能永久污染后续判断。**Fix:** 采用 append-only correction/supersede，不原地重写历史。
3. **Care↔Evidence 和主 Agent replan 没有具体循环上限。** 中心路由仍可能无限消耗调用。**Fix:** 结构化去重，补证/复核最多两个有新信息的往返，模型 replan 最多两次。
4. **AgentEnvelope 只隐含在现有实现复用中。** 后续重构可能退回自由文本交接。**Fix:** 明确通用 Envelope、typed payload、状态和未知字段拒绝。
5. **目录外建议没有激活语义。** Care 可能把自由文本建议通过用户确认变成行动。**Fix:** 目录外内容只能讨论，领域审阅和 catalog 登记前不可激活。
6. **确认绑定字段未在新计划中重述。** Schema 迁移可能丢失旧架构已有的重放保护。**Fix:** 绑定 candidate id/version/hash、actor、subject、scope、expiry，任何相关变化使确认失效。
7. **急症 preflight 与 Evidence 后风险分类顺序未明确。** **Fix:** 增加两阶段确定性风险顺序，Agent 不能降低硬风险。

VERDICT: REVISE

### Codex response

接受全部七项并完成修订。计划补齐分析状态归属、追加纠错、循环预算、严格 Envelope、目录外行动、确认重放保护和风险执行顺序。没有拒绝项。

## Round 5 — Codex review

最终一致性检查覆盖了 Agent 定义与计数、四个角色的闭环和禁区、旧六个角色的合并/降级、deny-by-default Tool 边界、三类状态、中心路由、最小运行路径、Safety target 与两轮复审、验收 Gate、风险顺序、职责不可接管、自进化隔离和原地 Schema 迁移。

未发现新的阻断或重大架构缺口：

1. 当前名单严格只有四个 Agent；旧角色只在迁移表和实施步骤中出现，不会继续作为运行身份。
2. Evidence claim、Care action、Safety veto 和 SleepCare publication 分别具有独立责任、输入、输出、权限和失败后果。
3. Trend、协同、报告与 Memory 已完整落入 Tool/Service/Skill，且没有通过“后台/临时/子 Agent”重新计数。
4. runtime、Policy 和 Commit Controller 保持高于模型 Agent 的状态与执行权；Safety 是强制但范围受限的语义闸门。
5. Skill 演进只能通过隔离的离线控制面，不能改变四角色拓扑或生产权限。
6. 现有 namespace、runtime、治理和测试被明确要求原地改造；没有长期双轨或新 `v2` 目录。
7. 剩余 open questions 均是模型/预算测量、领域 catalog 审批、数据保留和实现拆分，不再影响 Agent 身份或协作定义。

VERDICT: APPROVED

### Codex response

计划无需继续修订。对抗性审查在第 5 轮收敛；本审查由当前 Codex 会话切换 critic 角色完成，不是第二模型评审。

### Round 11 — Codex build: Report failure conservative role template

Date: 2026-07-16

- Replaced the blanket `role_material` failure block with the frozen failure
  matrix behavior: when Report generation fails, an elder or family read-only
  request may publish a clearly labeled conservative template derived only
  from already accepted Evidence.
- The template admits only the existing conservative degraded semantics,
  carries the accepted claim ids through publication postflight, states that
  formal material generation is unavailable and never claims Agent review or
  successful sharing.
- Doctor material remains blocked because its Artifact requires an exact
  SafetyReview. Any request carrying an external-share intent also remains
  blocked; Report failure cannot create an external action candidate, consume
  confirmation or execute a side effect.
- Added elder/family Report fault-injection scenarios and an authorized family
  external-share fault scenario. The tests assert `partial/safe_degraded` only
  for read-only non-doctor output, accepted Evidence provenance, two bounded
  failed Report calls, zero Care/share candidates and fail-closed sharing.

### Codex verification

- Targeted Report/failure matrix tests — `10 passed, 32 deselected in 0.83s`.
- Product Agent focused suite — `98 passed in 0.99s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `702 passed, 5 skipped in 12.56s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed that the degraded template has an
  accepted Evidence source, no Report/Dialogue product is fabricated, doctor
  Safety is not bypassed and external sharing cannot continue without a real
  accepted Artifact.

Fix rounds used: 0.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 12 — Codex build: deterministic risk-triggered Safety replan

Date: 2026-07-16

- Added registered morning Safety checkpoints for deterministic risk signals
  and Evidence conflict. A normal risk result preserves the minimum
  Evidence-to-Dialogue graph; `watch` or `escalate` records a deterministic
  `new_evidence` trigger and performs one bounded Orchestrator replan which
  cannot omit SafetyReview or the exact `risk_signal_review` checkpoint.
- Bound `risk.classify_signal` input to the accepted Evidence work-product ref
  and claim ids. This makes the post-Evidence ordering causal rather than
  merely sequential, while invalid risk enumerations add a runtime failure and
  cannot produce a false complete result.
- Added SafetyReview after the risk-aware Communication draft. Publication now
  requires an approved SafetyDecision bound to that exact Dialogue work
  product and hash; an Evidence approval cannot approve the later
  Communication. Safety failure, revise/block, wrong target or replan failure
  blocks high-risk ordinary publication.
- Extended replan contracts with deterministically required Agents/checkpoints,
  a causal parent invocation id and honest recording/budget accounting for two
  failed replan attempts. Runtime-selected conditions are unioned with prior
  requirements so a replan cannot plan away existing work.
- Added behavior tests for normal, `watch`, `escalate`, Safety outage, wrong
  Safety target, replan outage and invalid risk output, plus registry tests that
  one of multiple registered conditional Safety checkpoints is sufficient but
  omitting all of them is rejected.

### Codex verification

- Risk/architecture/runtime focused suite — `75 passed in 1.19s`.
- Product Agent focused suite — `105 passed in 1.00s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `709 passed, 5 skipped in 12.74s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed normal-night minimality,
  post-Evidence risk causality, bounded replan accounting, exact Communication
  Safety binding and fail-closed high-risk behavior.
- Fix pass 1 changed conditional Safety validation from requiring every
  registered condition to requiring at least one registered checkpoint while
  preserving exact request-required checkpoint validation. Fix pass 2 bound
  the deferred risk request to the accepted Evidence ref and claim set.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 13 — Codex build: post-confirm Care continuity candidates

Date: 2026-07-16

- Extended the Care confirmation path beyond the deterministic action commit.
  A successful commit now advances to a distinct immutable FactSnapshot whose
  action/Care versions and provenance include the exact commit Receipt, then
  records a bounded `new_evidence` replan before any further Agent judgment.
- Added a real accountable collaboration path for the confirmed action:
  CarePlanning delegates reminder/coordination judgment to AlertCare and
  confirmed-action recording judgment to Memory through the Orchestrator, then
  consumes both child products in a revised CarePlanning work product.
- Added first-class `memory_write_candidates` and `coordination_candidates` to
  the run result. Only accepted child products integrated by their accountable
  Care parent are exposed, and post-confirm completion considers only products
  bound to the new FactSnapshot. Both candidate types remain confirmation-bound;
  this path never calls `state.commit_memory` or `external.notify`.
- Replaced the stale pre-confirm publication after activation with verified
  post-commit truth: the selected Care action is active, generated continuity
  items are still only candidates, and no Memory write or reminder send has
  occurred. Publication recovery preserves these candidates and does not
  recommit the action; incomplete continuity remains partial after recovery.
- Tightened one-round dependency convergence. A parent revision that requests
  dependencies again is not accepted as if its original dependencies had been
  resolved. Added successful continuity assertions and a Memory fault-injection
  scenario proving the already committed Care action remains active while the
  Episode fails closed as `partial` with no unintegrated child candidate leak.

### Codex verification

- Targeted Care confirmation, continuity failure and publication recovery
  tests — `3 passed, 46 deselected in 0.66s`.
- Product Agent focused suite — `106 passed in 1.08s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `710 passed, 5 skipped in 12.70s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed original-snapshot confirmation,
  post-commit snapshot/replan causality, Care ownership of both child results,
  confirmation-only candidate semantics, honest partial failure and
  idempotent publication recovery.
- Fix pass 1 restricted continuity extraction to the post-commit FactSnapshot,
  prevented recurrent unresolved delegation from being accepted, and bound any
  numeric token in the confirmed action title to its accepted candidate source.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 14 — Codex build: causal request Context and Agent-owned Trend tools

Date: 2026-07-16

- Added an explicit `validated_agent_request` trust class and now place the
  exact validated DelegationRequest or A2ARequest in the target Agent's
  ContextPacket. Child Agents no longer have to infer their objective, evidence
  set or expected product from a generic Episode objective.
- Bound delegation authorization to the exact source invocation in runtime and
  checkpoint state. Episode-local duplicate delegation ids are rejected rather
  than overwriting an earlier causal grant; child revisions are allowed only
  after the same delegated child requested tools and the matching ToolReceipt
  objects are present in the new Context.
- Replaced blanket Context sharing with role-specific work-product visibility.
  Each target sees only the latest relevant accepted primary products plus exact
  child results it must integrate; shared Memory visibility is derived from its
  logical requesting primary. ToolReceipt visibility is likewise filtered by
  the target Agent tool allowlist, with the confirmed Care commit exposed only
  to the post-confirm Care/Alert/Memory continuity branch.
- Strengthened Agent `target_hash` so it covers the complete semantic
  ContextPacket (excluding only per-call ids), including authorization scope,
  causal request, trusted/untrusted inputs, accepted work and visible tool
  results. Tool Context now exposes receipt versions, caller, snapshot hash,
  input hash and observation time, so an input change cannot retain the same
  target merely because a tool summary happens to be unchanged.
- Moved deterministic trend-metric execution into the real TrendAgent tool
  request loop. Trend now makes one independent request call, receives one
  Agent-owned `trend.calculate_metrics` Receipt, and creates a causally linked
  revised Trend invocation before Evidence can integrate it. The old deferred
  runtime path no longer performs a duplicate trend calculation.
- Added structural assertions for exact Trend/Care/Memory/Alert delegation
  requests, Evidence-to-Safety A2A context, child Context minimization, one
  Agent-owned trend receipt, causal revision parentage and target-hash change
  after the tool result arrives.

### Codex verification

- Product Agent focused suite — `107 passed in 1.28s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `711 passed, 5 skipped in 13.10s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual diff/spec review confirmed exact source-invocation delegation
  binding, no child revision before a matching tool result, role-minimized
  Contexts, Agent-owned Trend metrics and semantic Context target hashing.
- Fix pass 1 preserved the existing fail-closed child-authorization error
  contract after introducing causal child revisions. Fix pass 2 added exact
  source invocation to delegation grants, rejected duplicate delegation ids and
  required matching ToolReceipt evidence before a child revision.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 15 — Codex build: receipt-grounded general Knowledge dialogue

Date: 2026-07-16

- Completed the frozen `grounded_dialogue` pure-general-knowledge path without
  adding personal Evidence, Care, Safety, Memory or Report work. Dialogue now
  owns its `knowledge.retrieve_reviewed` ToolRequest, consumes the resulting
  ToolReceipt in a causally linked revision, and can complete from the minimal
  Dialogue-only Agent path.
- Added a SourceScope-specific Communication gate for `general_knowledge`.
  The published answer must visibly carry an `一般知识` label, its scope notice
  must state `不基于本人数据`, and every cited reference must come from a
  successful reviewed-Knowledge receipt whose caller is Dialogue. A complete
  or useful partial answer cannot treat request-provided resolvable refs as a
  substitute for an actual Knowledge invocation.
- Added honest Knowledge-failure handling. After the deterministic two-attempt
  tool bound is exhausted, Dialogue may publish only a citation-free partial
  response that explicitly says it cannot answer reliably; the failed receipt,
  attempt count and runtime failure remain visible. A model-generated general
  claim without any Knowledge invocation is rejected and replaced by the
  registered conservative degraded publication.
- Added success, tool-failure and ungrounded-claim behavioral tests. The success
  trace asserts exactly two Dialogue invocations and one Dialogue-owned
  Knowledge receipt, while both negative paths assert that the reviewed claim
  is never published.

### Codex verification

- Targeted general-Knowledge success/failure/bypass tests — `3 passed, 50
  deselected in 0.99s`.
- Product Agent focused suite — `110 passed in 1.10s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `714 passed, 5 skipped in 13.00s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual spec review confirmed the visible non-personal label,
  Dialogue-owned reviewed retrieval, receipt-only citation set, minimal Agent
  graph and honest failed-tool partial behavior.
- Fix pass 1 prevented a model from bypassing grounding merely by marking an
  unsupported knowledge claim `partial`: non-clarification fallback now needs
  an actual failed Knowledge receipt, zero citations and an explicit inability
  to answer reliably.

Fix rounds used: 1.

No implementation deviation was required. Care transition confirmation and
withdrawal were deliberately not invented because the frozen payload does not
yet define a transition candidate/hash binding contract. The release manifest
remains empty and fail-closed pending actual A–T observations, three-run
real-provider evidence and domain-reviewed expectations. No commit, push,
frontend/API mutation or legacy removal was made.

### Round 16 — Codex build: personalized Dialogue-to-Evidence A2A closure

Date: 2026-07-16

- Completed the personalized `grounded_dialogue` evidence-recovery path. A
  Dialogue Agent that lacks current personal Evidence can issue an allowlisted
  A2A evidence request; the runtime invokes Evidence with the exact validated
  request, then makes a new causally linked Dialogue call whose Context contains
  the accepted Evidence work product.
- Bound the returned path structurally: the call graph is Dialogue → Evidence
  → Dialogue revision, the revision target hash changes with the new accepted
  input, and the final Communication must cite a non-empty subset of the exact
  claims returned by that A2A target rather than any unrelated earlier claim.
- Treated the pre-evidence Dialogue draft as an unresolved dependency instead
  of an accepted user result. It remains available in raw audit output but
  cannot become the final publication. If Evidence fails both bounded model
  attempts, no Evidence or Dialogue product is accepted and the registered
  safe-degraded publication states that personalized explanation is
  unavailable.
- Added success, Evidence failure and citation-bypass behavior tests. The
  negative cases deliberately inject an unsupported personal draft and a
  causal conclusion that ignores the returned Evidence; neither text reaches
  the user publication.

### Codex verification

- Targeted personalized A2A success/failure/citation tests — `3 passed, 53
  deselected in 0.91s`.
- Product Agent focused suite — `113 passed in 1.09s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `717 passed, 5 skipped in 13.09s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual spec review confirmed exact A2A request Context, accepted
  Evidence hand-back, causal parentage, changed target hash, claim-set binding,
  and no publication of unaccepted personalized drafts.
- Fix pass 1 made a Dialogue draft with a pending Evidence request ineligible
  for acceptance so a failed dependency cannot leak unsupported personal text.
  Fix pass 2 required the revised Dialogue to cite the exact returned Evidence
  claim set and added a causal-claim bypass test.

Fix rounds used: 2.

No implementation deviation was required. Care transition confirmation and
withdrawal still require a frozen transition candidate/hash binding contract;
none was invented here. The release manifest remains empty and fail-closed
pending actual A–T observations, three-run real-provider evidence and
domain-reviewed expectations. No commit, push, frontend/API mutation or legacy
removal was made.

### Round 17 — Codex build: reviewed questionnaire-bound Dialogue

Date: 2026-07-16

- Exposed the existing deterministic `QuestionnaireService` as the registered
  read-only `questionnaire.select` product tool. Its receipt now carries the
  selected question, question source/version, policy/version and a stable
  versioned source ref rather than allowing Dialogue to author a screening
  question from free-form model output.
- Bound every Dialogue questionnaire request to the authenticated episode
  context. The runtime overwrites model-provided subject and role with the
  current FactSnapshot subject and authenticated binding role, and clamps the
  turn to exactly one question before hashing and executing the effective tool
  request.
- Added a `needs_input` Communication acceptance gate. The published
  `question_id` and text must exactly match one schema-valid candidate from a
  successful Dialogue-owned questionnaire receipt for the current subject and
  role, and the candidate's complete versioned source ref must be present in
  that receipt.
- Completed the bounded question/resume trace: Dialogue requests the reviewed
  question, receives a causally linked tool-result revision, checkpoints as
  `waiting_user`, then resumes the same Episode and appends a second Receipt
  after the answer. The test proves the model cannot smuggle a different
  subject, role or three-question request through the tool boundary.
- Added negative behavior tests for a model-invented question and a two-attempt
  questionnaire timeout. Invented ids/text are rejected before publication;
  tool failure yields an honest partial response and never presents an
  unreviewed fallback question.

### Codex verification

- Targeted questionnaire adapter and Dialogue waiting/failure tests — `7
  passed` across the selected tooling and runner cases.
- Product Agent focused suite — `116 passed in 1.15s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `720 passed, 5 skipped in 13.11s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual spec review confirmed authenticated subject/role binding,
  single-question clamping, schema and version-ref validation, exact prompt
  preservation, append-only resume and honest failed-tool behavior.
- Fix pass 1 added strict `QuestionnaireSelection` validation plus full
  candidate source-ref binding so a nominally successful but malformed or
  unversioned tool payload cannot authorize a user-facing question.

Fix rounds used: 1.

No implementation deviation was required. Care transition confirmation and
withdrawal still require a frozen transition candidate/hash binding contract;
none was invented here. The release manifest remains empty and fail-closed
pending actual A–T observations, three-run real-provider evidence and
domain-reviewed expectations. No commit, push, frontend/API mutation or legacy
removal was made.

### Round 18 — Codex build: runtime-owned Dialogue wait decision

Date: 2026-07-16

- Bound `waiting_user` to the exact accepted Dialogue `needs_input` work
  product. The Orchestrator evaluation must reference the same reviewed
  `question_id`; a different id cannot create a checkpoint for a question that
  was never accepted or shown.
- Made the relationship bidirectional. An accepted `needs_input` Dialogue
  cannot be treated as `complete`, `partial`, `blocked`, replan or continue,
  while a `waiting_user` evaluation cannot be issued after an Agent failure,
  unaccepted dependency draft or non-Dialogue work product.
- Enforced the invariant inside `ProductEpisodeRuntime`, which owns Episode
  completion and waiting status, rather than only in the high-level runner.
  Invalid Orchestrator evaluations consume their recorded invocation but leave
  no waiting status or unresolved question behind; the runner then emits the
  registered safe-degraded terminal result.
- Added behavioral tests for an Orchestrator-selected question-id mismatch,
  premature completion of an unanswered Dialogue question and waiting before
  the questionnaire dependency has produced an accepted Communication. The
  existing valid reviewed-question checkpoint and append-only resume path
  remains unchanged.

### Codex verification

- Focused Episode waiting and Dialogue decision tests — `8 passed` across the
  selected runtime and runner cases.
- Product Agent focused suite — `119 passed in 1.13s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `723 passed, 5 skipped in 13.11s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual state-machine review confirmed exact question-id binding,
  no checkpoint before accepted Communication, no model-declared completion
  for a `needs_input` product and conservative publication after invalid
  evaluation.
- Fix pass 1 moved the invariant from runner-local validation into
  `ProductEpisodeRuntime` and updated the runtime waiting fixtures so they use
  real `needs_input` Dialogue products rather than unrelated completed Agent
  output.

Fix rounds used: 1.

No implementation deviation was required. Care transition confirmation and
withdrawal still require a frozen transition candidate/hash binding contract;
none was invented here. The release manifest remains empty and fail-closed
pending actual A–T observations, three-run real-provider evidence and
domain-reviewed expectations. No commit, push, frontend/API mutation or legacy
removal was made.

### Round 19 — Codex build: exact Care confirmation target and display

Date: 2026-07-16

- Bound an Orchestrator `waiting_confirmation` decision to an accepted complete
  Dialogue display and the exact recommended, activatable, confirmation-bound
  candidate from the latest accepted CarePlanning work product. Waiting before
  Communication, selecting an unknown candidate or declaring an unconfirmed
  action complete now fails at the Episode state machine.
- Restricted the checkpoint Receipt and publication metadata to the single
  confirmation target. Other accepted alternatives remain discussion content
  and cannot be confirmed merely because they existed in the Care payload.
- Added a deterministic confirmation summary derived only from the accepted
  candidate: current subject scope, title, 3–7 day duration, catalog parameters,
  objective metric, subjective question reference, stop conditions, cautions,
  external-effect boundary and withdrawal path. No state change, Memory write,
  reminder or contact occurs before confirmation.
- Extended publication value binding to accepted Care candidates so numeric
  duration and parameter values must pass the same postflight source check as
  accepted Evidence values.
- Added success and adversarial behavior tests covering a hidden alternative,
  candidate-id mismatch, premature completion and confirmation wait before the
  Dialogue display. Confirming the hidden alternative leaves Care Context at
  version zero.

### Codex verification

- Focused Care confirmation, continuity and adversarial decision tests — `5
  passed, 59 deselected in 0.70s`.
- Product Agent focused suite — `122 passed in 1.27s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `726 passed, 5 skipped in 12.77s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual state/publication review confirmed one displayed target, exact
  candidate metadata, no pre-confirmation side effect and candidate-backed
  numeric values.
- Fix pass 1 restricted both decision resolution and later confirmation lookup
  to the latest accepted CarePlanning product so a revised or withdrawn older
  candidate cannot re-enter the confirmation path.

Fix rounds used: 1.

No implementation deviation was required. Care transition confirmation beyond
initial activation still requires a frozen transition candidate/hash binding
contract; none was invented here. The release manifest remains empty and
fail-closed pending actual A–T observations, three-run real-provider evidence
and domain-reviewed expectations. No commit, push, frontend/API mutation or
legacy removal was made.

### Round 20 — Codex build: versioned Care state-transition confirmation

Date: 2026-07-16

- Replaced the unsafe bare Care transition enum with a strict
  `CareTransitionCandidate` carrying its own ID/version/hash, the exact current
  action ID/hash, source Care Context version, from/to status, reason and a
  mandatory confirmation boundary. Only active/paused actions can produce the
  registered pause/resume/natural-completion/end transitions.
- Added the authenticated current Care state to the CarePlanning Context and
  made the Care acceptance gate compare the transition candidate against the
  exact action reference, status, FactSnapshot Care Context version and the
  runtime active-action invariant. A missing, stale or cross-action state is
  rejected before publication.
- Extended the Episode state machine and publication path so an Orchestrator
  may wait only for the exact accepted transition shown by a complete Dialogue
  product. The deterministic confirmation display states current/target status,
  reason, single-action scope, no external effects and the requirement for a
  fresh candidate before any later state change.
- Added `transition_care_action` to the serialized commit controller and
  `confirm_care_transition` to the runner. Exact actor/subject/action scope,
  candidate version/hash, FactSnapshot hash and Care Context version are
  checked before a CAS commit; the verified Receipt is then adopted as a new
  FactSnapshot before final publication. Same-key retries return the original
  Receipt and stale/new-key confirmation replay has no side effect.
- Added governance and end-to-end Runner tests for malformed/bare transitions,
  stale state binding, idempotent confirmed pause, full pre-confirmation display
  and the final active-to-paused Care Context revision.

### Codex verification

- Focused governance and Runner tests — `84 passed in 1.27s` before the final
  fail-closed active-state check.
- Product Agent focused suite — `126 passed in 1.15s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `730 passed, 5 skipped in 13.41s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual contract/state/publication review confirmed that transition
  IDs cannot collide with action candidates, action and transition proposals
  cannot be mixed, terminal state changes preserve the audited action reference,
  and no state mutation occurs before exact confirmation.
- Fix pass 1 made the accepted current Care state a first-class Agent input and
  bound it through acceptance, waiting, display and commit. The final self-review
  also rejected transitions when the runtime does not report one active or
  paused primary action.

Fix rounds used: 1.

No implementation deviation was required. Atomic adjustment/replacement of an
active action still needs the separately versioned replacement-candidate path;
this round covers pause, resume, natural completion and explicit end only. The
release manifest remains empty and fail-closed pending actual A–T observations,
three-run real-provider evidence and domain-reviewed expectations. No commit,
push, frontend/API mutation or legacy removal was made.

### Round 21 — Codex build: atomic Care adjustment replacement

Date: 2026-07-16

- Extended `CandidateAction` with a complete replacement binding to the current
  candidate ID/version/hash and source Care Context version. An adjustment must
  keep the same candidate ID, remain catalog-backed and confirmation-required,
  and use exactly the next candidate version; partial bindings and version gaps
  are rejected by the strict contract.
- Extended `CareContextState` with the active candidate version and catalog
  action ID/version. The Care acceptance gate now proves that an adjustment
  replaces the exact active/paused candidate and the same reviewed catalog
  action under the current FactSnapshot, rather than allowing a second action
  or a different catalog entry disguised under the old candidate ID.
- Added serialized `replace_care_action` CAS handling. It validates the new
  candidate confirmation under the dedicated `replace_care_action` scope,
  atomically advances the candidate version/hash while preserving active/paused
  status, emits a state Receipt, and returns the original Receipt for a valid
  same-key retry. Old activation confirmations and stale Care Context versions
  have no side effect.
- Routed adjustment confirmation through the existing Care action checkpoint,
  with deterministic display of the replacement scope, reviewed parameters,
  duration, measures, stop/caution boundary, external-effect boundary and fresh
  confirmation requirement. After commit the runner adopts the new Care state
  into the FactSnapshot before generating bounded Memory/Alert continuity
  candidates and publishing “adjusted” rather than “started”.
- Added governance and end-to-end behavior tests for exact-current acceptance,
  candidate-version advancement, catalog-action substitution rejection, old
  confirmation rejection, atomic state replacement, complete pre-confirmation
  display and post-commit continuity.

### Codex verification

- Focused governance and Runner tests — `87 passed in 1.04s` after both bounded
  fix passes.
- Product Agent focused suite — `129 passed in 1.21s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `733 passed, 5 skipped in 13.21s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual contract/state/publication review confirmed one candidate ID,
  strict next-version replacement, stable catalog identity, no pre-confirmation
  mutation, serialized CAS, post-commit FactSnapshot adoption and honest final
  wording.
- Fix pass 1 kept the test adjustment inside the reviewed catalog instead of
  weakening catalog enforcement. Fix pass 2 closed version-gap and catalog-swap
  loopholes discovered during self-review.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 22 — Codex build: confirmation-scoped Care follow-up

Date: 2026-07-16

- Added a strict `CareFollowupDisposition` contract for in-scope continuation,
  adjustment, pause, natural completion and explicit end. Continuation must
  contain no state-change candidate and must state a structured no-change
  reason; every other disposition must carry the exact matching replacement or
  transition candidate.
- Extended `CareContextState` with the confirmed action start, planned end and
  next follow-up times. Activation and adjustment establish a new bounded
  3–7-day scope, while pause/completion/end preserve its audited dates. The
  state contract rejects incomplete action/time references and follow-up times
  outside the confirmed interval.
- Made `care_followup` requests require the versioned active/paused Care state,
  and passed the Episode type into the Care acceptance gate. A model can publish
  “continue” only while the current action is active and the FactSnapshot time
  is strictly inside the original interval; at or after the planned end it must
  produce a newly confirmed replacement/state transition.
- Prevented a paused action from resuming after its confirmed end in both the
  acceptance gate and serialized commit controller. Expired resume now requires
  a new versioned adjustment rather than silently extending the old scope.
- Added behavior tests for missing current state, valid in-scope non-causal
  follow-up, continuation exactly at/after scope end, expired resume rejection,
  and a follow-up pause that remains side-effect free until exact transition
  confirmation and then advances Care Context/FactSnapshot once.

### Codex verification

- Focused governance and Runner tests — `91 passed in 1.34s`.
- Product Agent focused suite — `133 passed in 1.16s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `737 passed, 5 skipped in 13.37s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual review confirmed exact current-action binding, structured
  follow-up outcomes, no state mutation before confirmation, strict time-scope
  exhaustion, bounded resume and Receipt-backed post-commit publication.
- Fix pass 1 closed the boundary where an action could continue exactly at its
  planned end or resume after expiry; both now require a fresh adjustment scope.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 23 — Codex build: action-period follow-up evidence

Date: 2026-07-18

- Added a strict `CareActionObservation` contract to follow-up Evidence and
  Trend payloads. It binds the exact active candidate ID/version/hash, Care
  Context version, confirmed action start/end, timezone-aware observation
  interval, `during_action` relation and an invariant false causal attribution.
- Made the Evidence and Trend acceptance gates require that binding in every
  `care_followup`. They reject missing/stale candidate windows, observations
  outside SourceScope, and timestamps before activation or after the earlier of
  the FactSnapshot `as_of` time and confirmed planned end. Other Episode types
  cannot attach the follow-up-only observation contract.
- Exposed a minimized immutable current-action context to Evidence, Trend,
  Memory and Care during follow-up while keeping it out of Dialogue. The
  context omits subject identity and audit timestamps, so each specialist can
  bind its work without receiving unnecessary identifiers.
- Exercised the real Evidence → Memory/Trend → revised Evidence ownership path
  in the follow-up behavior model. Trend remains a child result and only enters
  shared state after Evidence consumes it; insufficient nights produce an
  explicit no-direction conclusion instead of a fabricated improvement.
- Added affirmative action-causality rejection to both Communication acceptance
  and final publication postflight. Negated uncertainty such as “cannot
  determine that the action caused it” remains publishable, while a clean
  accepted draft cannot be replaced by a causal Orchestrator/publication
  rewrite.
- Added governance and end-to-end tests for exact observation binding, stale
  Evidence/Trend rejection, scoped context visibility, Trend parent ownership,
  insufficient-change wording and causal-claim refusal.

### Codex verification

- Focused governance and Runner tests — `94 passed in 1.41s` before final
  timestamp hardening.
- Product Agent focused suite — `136 passed in 1.63s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and A–T release-gate
  tests.
- Full Python regression — `740 passed, 5 skipped in 12.96s`.
- Explicit compilation of all product Agent modules and `git diff --check`
  passed. Manual contract/context/acceptance/publication review confirmed exact
  current-action version binding, timezone-aware confirmed-scope enforcement,
  minimized disclosure, child-to-parent Trend integration and two independent
  non-causal publication gates.
- Fix pass 1 replaced a brittle checkpoint-call counter after adding the real
  Trend child invocation and removed unnecessary subject/audit fields from the
  shared action context. Fix pass 2 replaced date-granularity observation
  bounds with exact timezone-aware timestamps to close the planned-end-day
  ambiguity.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 24 — Codex build: auditable release-evidence gates

Date: 2026-07-18

- Versioned the checked-in acceptance manifest to
  `sleepagent-product-agent-acceptance.v2` and added strict per-call evidence
  for Orchestrator and specialist Agent invocations: invocation identity, role,
  provider/model, optional provider request ID and validation status.
- Bound every observed Agent call set to the exact `EpisodeReceipt` invocation
  IDs and unique participating roster. A `real_provider` observation that
  claims model judgment must now carry both genuine Orchestrator and Agent call
  records and cannot use a `deterministic_only` Receipt.
- Required three distinct real-provider repetition indices for every A–T
  scenario that contains model judgment; deterministic replay and fault runs
  can no longer substitute for the three independent live-provider runs. The
  pure urgent-preemption scenario remains correctly exempt from model calls.
- Scoped the complete fault-target matrix to scenario J. Every fault
  observation must contain a recorded failed invocation for its declared
  target, so unrelated fault labels in another scenario cannot satisfy the
  Orchestrator/Evidence/Trend/Care/Safety/Dialogue/Report/Memory/Alert matrix.
- Added explicit `accepted | rejected | failed` status to versioned
  Orchestrator invocation records and bumped the Episode checkpoint runtime to
  v2. Invalid plans are recorded as rejected, provider/schema failures as
  failed, and accepted planning/evaluation calls remain distinguishable.
- Made runtime-to-observation conversion verify authenticated role, Episode,
  FactSnapshot, SourceScope, final state revision and Receipt invocation set;
  its call-graph hash now includes Orchestrator nodes instead of only
  specialists.
- Separated “domain reviewed” from “expectation met”. A reviewed-but-rejected
  run is listed in `failed_expectation_run_ids` and blocks release just as an
  unreviewed run or hard invariant violation does.

### Codex verification

- Product Agent focused suite — `141 passed in 1.26s` across architecture,
  governance, invocation, Episode runtime, tooling, runner and release-gate
  tests.
- Full Python regression — `745 passed, 5 skipped in 12.83s`.
- Explicit compilation of every product Agent module and `git diff --check`
  passed. The checked-in v2 manifest parses successfully and remains
  fail-closed with all 20 scenarios and all nine J fault targets absent.
- Manual contract review confirmed exact Receipt/call-set equality, unique call
  IDs, target-specific failure proof, real-provider-only repetition counting,
  rejected-plan recording, runtime/role/snapshot consistency and independent
  domain approval state.
- Fix pass 1 added target-specific fault proof and Orchestrator validation
  status after self-review found that fault labels could still be fabricated.
  Fix pass 2 added `expectation_met` after review found that a domain-reviewed
  but explicitly rejected result could otherwise contribute to release.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 25 — Codex build: semantic Evidence provenance

Date: 2026-07-18

- Replaced the Evidence gate's former string-membership check with a typed
  provenance index. Every claim source is now classified as canonical
  observation, exact user report, reviewed knowledge, Trend analysis,
  confirmed memory, accepted ledger evidence or data-quality evidence before
  it can support an Evidence semantic.
- Added a semantic compatibility matrix: observed facts accept only canonical
  or Trend sources, user-reported claims accept exact user input or confirmed
  memory, grounded knowledge accepts only reviewed knowledge, and inferences
  accept bounded evidence classes while excluding policy/risk/data-quality
  sources. Unknown claims retain broad source visibility but remain subject to
  their existing low-confidence gate.
- Made every `EvidenceClaim`, including grounded knowledge and unknowns,
  require at least one source reference. Added a hashed, index-bound
  `user_report_source_ref` so model claims can cite the exact request input
  without copying raw text into an identifier.
- Added `evidence_source_kind` to successful read-only `ToolReceipt` records
  and classified the canonical radar, data-quality, Trend and reviewed
  knowledge tools. Policy, risk, questionnaire and ledger-read receipts remain
  deliberately unclassified; failed or state-changing receipts cannot declare
  an evidence source kind.
- Rebuilt the runtime source index from the immutable FactSnapshot, exact user
  inputs, successful classified tool receipts, accepted claims, accepted
  Trend products and current confirmed memory. Caller-provided
  `additional_resolvable_refs` remain available to non-Evidence compatibility
  paths but can no longer authorize an Evidence claim.
- Migrated care, transition, conflict and alert behavior fixtures from the old
  canonical-source shortcut to exact user-report references. Added governance
  positive/negative compatibility tests and end-to-end adversarial tests that
  prove neither a `policy.read` invocation nor an arbitrary caller-supplied
  resolvable ref can be published as a personal observed fact.
- Bumped the behavior/tool identifiers to `sleepagent-product-runner.v2` and
  `sleepagent-product-tools.v2`. The Episode checkpoint payload did not change,
  so its existing v2 checkpoint version remains correct and older untyped
  pending evidence fails closed on resume.

### Codex verification

- Product Agent focused suite — `149 passed in 1.33s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release-gate
  tests.
- Full Python regression — `753 passed, 5 skipped in 13.10s`.
- Explicit compilation of every product Agent module and `git diff --check`
  passed. `ruff` was not installed in the workspace; manual static review
  covered every `EvidenceClaim` construction and every `accept_evidence` call.
- Manual contract review confirmed fail-closed unclassified refs, semantic
  source matching, exact hashed user-input binding, successful-read-only
  receipt constraints, checkpoint index restoration and non-publication after
  source-confusion rejection.
- Fix pass 1 migrated user-reported behavior fixtures and their request inputs
  after the stricter gate exposed canonical-source misuse. Fix pass 2 updated
  the remaining architecture error assertion and hardened receipt validation
  and runtime version signaling during self-review.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 26 — Codex build: data-quality-bound Evidence conclusions

Date: 2026-07-18

- Promoted the canonical quality gate's existing
  `health_conclusion_allowed` result from an untyped nested summary into
  versioned `ProductToolResult` and `ToolReceipt` fields. The strict receipt
  validator permits the field only on a successful read-only
  `radar.assess_data_quality` receipt classified as data-quality evidence.
- Updated the existing `DataQualityGate` adapter to propagate its real
  `RadarNightSummary.health_conclusion_allowed` decision. The product tool
  executor now rejects a quality handler that omits the typed permission, so a
  legacy or malformed success cannot silently bypass the gate.
- Added a deterministic runtime quality state with false-dominant aggregation
  across receipts. For Episode definitions that require
  `radar.assess_data_quality`, Evidence observed facts and inferences now
  require an explicit successful `true`; an explicit `false`, a failed tool or
  an untyped receipt all fail closed. User-reported facts remain preservable,
  and the existing data-quality recovery contract continues to accept only
  explicit unknown claims.
- Exposed the typed permission in the untrusted tool context so the Evidence
  model sees the same boundary enforced by governance. The accepted work
  target hash therefore includes the exact quality receipt, while the model
  still cannot change the permission.
- Added governance tests proving unusable radar data blocks observed health
  conclusions without erasing exact user reports, tool tests for both strict
  receipt propagation and the real offline-device adapter, and end-to-end
  Runner tests for explicit unusable, omitted and failed quality decisions.
  Every case degrades without accepting or publishing the fabricated “stable
  night” conclusion.
- Bumped the product identifiers to `sleepagent-product-runner.v3` and
  `sleepagent-product-tools.v3`, and the result schema to
  `ProductToolResult.v2`. Episode checkpoint shape remains v2; restored older
  required-quality work is intentionally fail-closed unless it contains an
  explicit typed permission.

### Codex verification

- Product Agent focused suite — `155 passed in 1.28s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release-gate
  tests.
- Full Python regression — `759 passed, 5 skipped in 12.97s`.
- Explicit compilation of every product Agent module and `git diff --check`
  passed. Manual contract review covered the real quality adapter, receipt
  validation, preplanning order, false-dominant aggregation, required-tool
  registry lookup, context exposure and degraded non-publication.
- Fix pass 1 added the typed receipt-to-acceptance chain and migrated normal
  test quality receipts. Fix pass 2 closed missing/failed-quality fail-open
  behavior, required explicit typed tool output and added the real adapter
  proof.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 27 — Codex build: typed deterministic risk boundary

Date: 2026-07-18

- Added the versioned `ProductRiskLevel` contract with normal, watch,
  escalate, uncertain and urgent-boundary states. Successful
  `risk.classify_signal` and `risk.match_urgent_boundary` results now expose
  typed `risk_level` and `urgent_boundary` fields on both ProductToolResult and
  ToolReceipt; other tools, failed receipts and state-changing receipts cannot
  claim those decisions.
- Made both risk tools fail closed when a handler omits its typed decision.
  Runner preflight and post-Evidence classification consume only the typed
  receipt fields rather than trusting arbitrary `output_summary` strings.
  Untyped and invalid results therefore produce auditable failed receipts
  instead of implicit normal classifications.
- Adapted the existing deterministic RiskSignal capability to the product
  vocabulary: legacy `info` maps to product `normal`, while watch, escalate,
  uncertain and urgent retain their conservative meaning. `uncertain` now
  follows the same required SafetyReview branch as watch/escalate, and an
  urgent result that somehow appears after a non-urgent preflight blocks the
  ordinary publication path.
- Connected the real canonical quality result to deferred risk classification.
  Runner supplies the Episode ID, accepted Evidence identity and the exact
  `night_summary` from the successful quality receipt, closing the prior
  mismatch where the adapter received neither its required Episode ID nor the
  radar summary it classifies.
- Fixed the reused RiskSignal implementation's no-source path: benign urgent
  preflight and quality-unknown decisions now omit Evidence claims rather than
  constructing invalid reviewed claims without refs. This preserves strict
  Evidence provenance while allowing a normal non-urgent preflight to succeed.
- Added strict missing-field tests, real urgent true/false adapter tests,
  legacy-info mapping proof, uncertain Safety routing, escaped-urgent blocking
  and an end-to-end normal morning using the actual RiskSignal adapter. The
  real adapter produces `normal`, completes intelligently and does not invoke
  unnecessary SafetyReview.
- Bumped the product identifiers to `sleepagent-product-runner.v4` and
  `sleepagent-product-tools.v4`, and the result schema to
  `ProductToolResult.v3`. No Episode checkpoint shape changed.

### Codex verification

- Focused product plus legacy RiskSignal tests — `100 passed in 1.49s` before
  the final real-adapter Runner proof.
- Product Agent focused suite — `162 passed in 1.46s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release-gate
  tests.
- Full Python regression — `766 passed, 5 skipped in 13.29s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Manual review
  confirmed typed-field authority, strict tool/result matching, normal and
  uncertain mappings, canonical summary injection, preflight ordering and
  fail-closed post-preflight urgent behavior.
- Fix pass 1 migrated typed risk/urgent contracts and existing behavior tests.
  Fix pass 2 found and repaired the real benign-preflight no-ref failure,
  supplied canonical quality input to classification and added the actual
  adapter end-to-end proof.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 28 — Codex build: runtime-bound acceptance evidence

Date: 2026-07-18

- Upgraded the checked-in release manifest and strict schema to
  `sleepagent-product-agent-acceptance.v3`. Evidence-semantic coverage is no
  longer supplied as a free list of labels: every counted semantic now carries
  an `AcceptanceEvidenceBinding` with its claim ID, accepted work-product ref
  and exact work-product hash.
- Added the non-sensitive `AcceptedWorkProduct` metadata set to each acceptance
  observation and require it to match the final EpisodeReceipt exactly. Every
  product must bind the final FactSnapshot and a state revision no newer than
  the Receipt; semantic bindings must point to a recorded EvidenceAnalysis
  product with the same hash and a corresponding recorded Agent call.
- Made `observation_from_runtime` derive semantic bindings only from accepted
  Evidence Envelopes in the final EpisodeRuntimeSnapshot. It now rejects a
  missing or duplicate raw Envelope, a Receipt/runtime work-product mismatch,
  a changed Envelope hash, or changed snapshot/revision/target metadata instead
  of trusting caller-declared semantic coverage.
- Expanded model-call evidence with parent invocation, invocation type,
  Agent/prompt/Schema versions, context hash and Agent target hash. The
  observation validator deterministically recomputes `call_graph_hash` from
  those records, requires every causal parent to exist in the same graph, and
  rejects cycles or arbitrary graph hashes.
- Extended genuine call-proof requirements to deterministic replay whenever an
  observation claims model judgment. A replay or real-provider judgment must
  contain recorded Orchestrator and Agent calls and cannot use a
  `deterministic_only` Receipt; pure urgent deterministic behavior remains
  correctly exempt.
- Added release-governance tests for unaccepted semantic claims, automatic
  runtime semantic derivation, tampered work hashes/metadata, mismatched
  Receipt work-product sets, arbitrary call-graph hashes, missing parents and
  causal cycles. The checked-in empty v3 manifest parses and remains
  fail-closed with all twenty A–T scenarios absent.

### Codex verification

- Focused acceptance suite — `14 passed in 0.73s`.
- Product Agent focused suite — `167 passed in 1.33s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `771 passed, 5 skipped in 13.59s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Manual review
  confirmed exact Receipt/product-set equality, runtime-derived semantics,
  Evidence hash and metadata binding, replay call proof, reproducible graph
  hashing and acyclic causal records.
- Fix pass 1 added exact accepted-product metadata and made the call graph
  independently reproducible after review found that a claim hash and an
  arbitrary 64-character graph value were not sufficient offline evidence.
  Fix pass 2 added parent existence and cycle checks after the causal graph
  audit found that internally consistent but structurally invalid call records
  could otherwise pass.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 29 — Codex build: release-bound runtime identity

Date: 2026-07-18

- Upgraded the release evidence schema and checked-in manifest to
  `sleepagent-product-agent-acceptance.v4`. The manifest now carries an
  immutable `AcceptanceReleaseIdentity` covering the release version, product
  contract, registry version and structural hash, Episode runtime, Runner,
  RunResult schema, Tool runtime and acceptance schema; its `identity_hash` is
  deterministically recomputed on every parse.
- Bound every `AcceptanceObservation` to the exact release identity hash. A run
  recorded for another candidate cannot be moved into a newer manifest, even
  when its scenario, Receipt, domain review and expectation result are
  otherwise valid.
- Added `release_identity_mismatch` to the release report. A self-consistent
  archived/stale manifest remains parseable for audit, but it cannot be release
  eligible when any runtime, registry structure or acceptance version differs
  from the currently executing build.
- Versioned the complete runtime artifact as `ProductEpisodeRunResult.v2` and
  bumped the product Runner to `sleepagent-product-runner.v5`. Every result now
  carries product-contract, registry, Episode-runtime, Runner, Tool-runtime and
  result-schema identity, with a strict active-registry hash check.
- Centralized current RunResult construction so ordinary completion,
  publication delivery, waiting resume and supersede paths cannot omit or
  independently choose identity fields. The runtime snapshot is exported once
  per result and its Episode version must match the result metadata.
- Changed runtime-to-acceptance conversion to consume the complete versioned
  RunResult plus the requested release identity. It rejects stale identities or
  results before deriving semantic, model-call or work-product evidence.
- Added regression coverage for cross-release observation reuse, stale Runner
  and Tool versions, stale manifest release gating, mismatched RunResult
  identity, checked-in current identity and real Runner E2E version fields.

### Codex verification

- Focused acceptance suite — `17 passed in 0.67s`.
- Focused acceptance plus Runner suite — `96 passed in 1.53s` after both fix
  passes.
- Product Agent focused suite — `170 passed in 1.36s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `774 passed, 5 skipped in 13.09s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. The checked-in v4
  manifest parses with `release_identity_mismatch=false` and remains
  fail-closed because all twenty A–T observations are still absent.
- Fix pass 1 replaced scattered RunResult construction after the waiting
  supersede test exposed a missing identity migration; all result-producing
  paths now use the same runtime-owned factory. Fix pass 2 bound the product
  contract version into the RunResult after self-review found it was present in
  the release identity but not yet carried by the execution artifact.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 30 — Codex build: receipt-bound tool acceptance evidence

Date: 2026-07-19

- Upgraded the release evidence schema and checked-in manifest to
  `sleepagent-product-agent-acceptance.v5`. Every acceptance observation now
  carries the complete ordered `ToolReceipt` sequence, the authenticated
  authorization scope and a deterministic `tool_receipt_sequence_hash` over
  the strict receipt payloads.
- Bound tool evidence to all three runtime views: `ProductEpisodeRunResult`
  receipts, `EpisodeReceipt.tool_invocation_ids` and the final runtime
  snapshot must contain the same unique invocation IDs in the same execution
  order. Missing, extra, duplicate or reordered tool evidence is rejected
  before an observation can enter a release manifest.
- Preserved the full receipt semantics needed by scenarios O and T, including
  tool and schema versions, caller, FactSnapshot, input hash, authorization
  scope, effect classification, outcome, source refs, evidence kind, risk and
  urgent outputs, observed time, output summary, error, idempotency key,
  external receipt and attempt count. Scenario O cannot be represented by an
  observation with no actual ToolReceipt evidence.
- Enforced that every stored receipt binds the final Episode FactSnapshot and
  that its unique authorization scope is a subset of the authenticated runtime
  binding. Recomputing the sequence hash after expanding scope or replacing a
  receipt with one from another snapshot still fails validation.
- Kept release gating fail-closed: the empty checked-in v5 manifest has current
  release identity hash
  `6271c2943d4629c66314d003e912ab9bc5dc6658a530b8b4c70641033eb6e22d`,
  reports no identity mismatch and still reports all twenty A–T scenarios as
  missing.
- Added regression coverage for runtime derivation of full ToolReceipt
  evidence, missing and duplicate receipts, execution reordering, scope
  expansion, sequence-hash tampering and cross-snapshot substitution.

### Codex verification

- Focused acceptance suite — `19 passed in 0.78s`.
- Product Agent focused suite — `172 passed in 1.36s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `776 passed, 5 skipped in 13.37s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct parsing of
  the checked-in v5 manifest confirmed `release_identity_mismatch=false` and
  exactly twenty missing scenarios.
- Fix pass 1 added authenticated-scope preservation and subset validation after
  review found that storing a receipt scope alone did not prove it had not
  expanded beyond the runtime binding. Fix pass 2 added final FactSnapshot
  equality and per-receipt scope uniqueness after cross-run substitution was
  identified as the remaining offline-evidence gap.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 31 — Codex build: timing-bound urgent release proof

Date: 2026-07-19

- Upgraded the release evidence schema and checked-in manifest to
  `sleepagent-product-agent-acceptance.v6`. `AcceptanceModelCall` now preserves
  each runtime call's `started_at`, `ended_at` and `latency_ms`; parsing rejects
  negative durations, inconsistent latency and a causal child that starts
  before its recorded parent completes. The call-graph hash consequently binds
  both causality and timing metadata.
- Required every acceptance observation to contain a successful typed
  `risk.match_urgent_boundary` preflight from the current deterministic Tool
  runtime, using `ProductToolResult.v3`, the runtime caller and read-only
  effect. Ordinary observations require a negative result completed before the
  first model call and reject any positive urgent result that continued into a
  normal path.
- Made scenario H structural rather than declarative. A valid H observation is
  a terminal, goal-achieved `urgent_boundary` Receipt in
  `deterministic_only` mode with exactly one positive urgent runtime Receipt,
  no model judgment or call, no Agent/work product, no candidates,
  confirmation, side effect, failure, unknown outcome or unresolved item.
- Extended the real Runner urgent-preemption regression so its versioned
  `ProductEpisodeRunResult` is converted directly into scenario H acceptance
  evidence. This proves the release validator accepts the actual zero-model
  execution artifact, not only a hand-built observation fixture.
- Added adversarial coverage for latency tampering, parent/child temporal
  overlap, late or missing urgent preflight, a positive urgent result in an
  ordinary path, an Agent impersonating the runtime caller, H model-judgment
  claims and H external side effects.
- Kept release gating fail-closed: the empty checked-in v6 manifest has current
  release identity hash
  `db7d7ee71b55c041870a1df9b593eed4f65b62648cc4234265e346d2500fcbd2`,
  reports no identity mismatch and still reports all twenty A–T scenarios as
  missing.

### Codex verification

- Focused acceptance suite — `21 passed in 0.82s`.
- Actual Runner urgent conversion plus acceptance suite — `22 passed in
  1.12s`.
- Product Agent focused suite — `174 passed in 1.41s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `778 passed, 5 skipped in 13.23s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct parsing of
  the checked-in v6 manifest confirmed `release_identity_mismatch=false` and
  exactly twenty missing scenarios.
- Fix pass 1 rejected positive urgent matches on ordinary paths and limited H
  to its single urgent tool after review found that extra ordinary work could
  otherwise coexist with a nominal preemption. Fix pass 2 bound urgent proof to
  the current Tool runtime, result Schema and deterministic runtime caller so
  an Agent-crafted same-name Receipt cannot satisfy the hard gate.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 32 — Codex build: snapshot-lineage and registry-bound tool proof

Date: 2026-07-19

- Upgraded the Episode runtime to `sleepagent-product-episode-runtime.v3`.
  Every checkpoint now persists an immutable ordered `fact_snapshot_history`;
  restore retains it, and a successful Care-state commit appends its distinct
  post-commit FactSnapshot instead of erasing the entry snapshot. The history
  validator requires a unique chronological chain, stable subject/auth/scope/
  canonical identity, non-decreasing provenance, unchanged Ledger/Memory
  versions and strictly newer Action/Care Context versions.
- Upgraded the Runner to `sleepagent-product-runner.v6` and its complete result
  artifact to `ProductEpisodeRunResult.v3`. The result identity now includes
  `sleepagent-product-governance.v1`, covering the implementation that creates
  state-changing and external ToolReceipts rather than binding only the
  read-only Tool runtime.
- Upgraded release evidence and the checked-in manifest to
  `sleepagent-product-agent-acceptance.v7`. Every observation carries a
  non-sensitive ordered `AcceptanceFactSnapshotBinding` chain and a
  reproducible sequence hash. Historical ToolReceipts and accepted work
  products may bind any exact snapshot in that chain, while the final
  EpisodeReceipt must bind its last snapshot.
- Validated every acceptance ToolReceipt against the active registry: the tool
  must exist, its effect must match, its runtime/Agent/commit-controller caller
  must be authorized, confirmation-required tools need a recorded confirmation,
  and read-only versus governance versions/Schemas must match their executor.
  Successful external effects require an external Receipt; failed/denied tools
  require errors; state-changing and unknown Receipt IDs must exactly match the
  EpisodeReceipt audit lists.
- Extended causal graph validation to accept a recorded ToolReceipt as a model
  call parent. This supports the real post-commit replan edge while proving the
  replan began only after the state-changing Receipt was observed.
- Added an end-to-end release-evidence assertion to the real confirmed Care
  action path. Its initial evidence/tools, `state.commit_care` Receipt,
  post-commit continuity Agent calls and final snapshot now form one valid v7
  observation with a two-snapshot lineage.
- Added adversarial coverage for reordered/stale snapshot history, broken
  predecessor links and sequence hashes, unregistered tools, disallowed Agent
  callers, stale Tool runtime versions, registry-effect drift, missing
  confirmation, missing external Receipt and stale governance identity.
- Kept release gating fail-closed: the empty checked-in v7 manifest has current
  identity hash
  `29ba9757d4a484178e7de4604e5a5cc61e8c321dd2020784a59f91aa68a11e2f`,
  reports no identity mismatch and still reports all twenty A–T scenarios as
  missing.

### Codex verification

- Episode runtime, Runner and acceptance suites — `117 passed in 1.44s`.
- Product Agent focused suite — `177 passed in 1.62s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `781 passed, 5 skipped in 13.48s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct parsing of
  the checked-in v7 manifest confirmed `release_identity_mismatch=false` and
  exactly twenty missing scenarios.
- Fix pass 1 allowed a recorded ToolReceipt to be a causal parent after the
  real Care commit exposed its post-commit replan edge. Fix pass 2 mirrored the
  runtime's strict Action/Care version advance in checkpoint history after
  review found identity/provenance checks alone could admit a stale successor.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 33 — Codex build: confirmation-consumption audit binding

Date: 2026-07-19

- Upgraded the Episode runtime to `sleepagent-product-episode-runtime.v4`.
  Checkpoints now retain an immutable `ConfirmationConsumption` for every
  consumed confirmation instead of preserving only its identifier. Each record
  contains the full versioned `ConfirmationToken`, the one authorized tool
  invocation and the consumption time.
- Bound every recorded confirmation to the authenticated actor and subject,
  one displayed candidate, an exact FactSnapshot hash/Care Context version in
  the Episode lineage, a distinct executed-action Receipt and a pre-expiry
  consumption time. Confirmation IDs and consuming tool IDs are unique and
  preserve tool execution order across checkpoint export and restore.
- Updated the Care activation/adjustment, Care transition and external-share
  commit paths to use one normalized authorization timestamp for controller
  validation and audit consumption, while retaining the controller's existing
  candidate, identity, scope, freshness, replay and idempotency enforcement.
- Upgraded the Runner to `sleepagent-product-runner.v7`, its result artifact to
  `ProductEpisodeRunResult.v4`, and release evidence to
  `sleepagent-product-agent-acceptance.v8`.
- Added non-raw actor/subject identity hashes plus complete
  `AcceptanceConfirmationBinding` evidence. Each binding independently carries
  the token's candidate ID/version/hash and the strict candidate object, action
  scope, FactSnapshot/Care Context version, expiry, consumption time and exact
  ToolReceipt identity/name. A sequence hash covers the ordered confirmation
  evidence.
- Replaced the former observation-wide “some confirmation exists” check with
  per-ToolReceipt authorization. Every registry tool marked
  `confirmation_required` must have exactly one matching confirmation binding;
  a confirmation cannot authorize another tool, a read-only tool cannot consume
  one, and an arbitrary Receipt-level confirmation ID no longer unlocks all
  side effects.
- Made runtime-to-acceptance conversion resolve the token against the exact
  Care, transition, Memory or external candidate retained by the real result.
  Conversion fails if the consumed token has no single matching candidate or
  ToolReceipt, preventing the audit layer from manufacturing candidate proof.
- Extended the real confirmed Care regression to prove full runtime and
  acceptance bindings and to reject cross-actor checkpoint tampering and a
  changed token candidate hash even when the confirmation sequence hash is
  recomputed.
- Kept release gating fail-closed: the empty checked-in v8 manifest has current
  identity hash
  `603affb1e8aacccad138a69e9f3f1d3f1c641b155ccaf558dcd202a922e242b4`,
  reports no identity mismatch and still reports all twenty A–T scenarios as
  missing.

### Codex verification

- Episode runtime, Runner and acceptance suites — `117 passed in 1.70s`.
- Product Agent focused suite — `177 passed in 1.49s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `781 passed, 5 skipped in 13.60s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct parsing of
  the checked-in v8 manifest confirmed current release identity, zero
  observations and exactly twenty missing scenarios.
- Fix pass 1 corrected the self-review findings that Memory confirmation scope
  must be the controller's registered `state.commit_memory` scope and that a
  malformed consumption must fail validation cleanly when its tool ID is absent
  from execution order, rather than reaching an indexing error.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 34 — Codex build: confirmed Memory persistence and semantic registry identity

Date: 2026-07-19

- Completed the real grounded-dialogue Memory-write path. Explicit current
  user reports may ground an independently invoked Memory Agent candidate;
  Dialogue must consume that child result before the Orchestrator can enter
  `waiting_confirmation`.
- Episode runtime v5 now permits a FactSnapshot successor to advance exactly
  one state domain: either Action or Memory. Ledger remains stable and Care
  Context advances on every accepted successor; advancing both domains or
  neither is rejected.
- The waiting publication now binds the exact accepted Memory candidate and
  displays its value, operation/subject scope, retention, revocation path and
  the explicit guarantee that no notification, sharing or contact occurs.
  No state mutation happens before confirmation.
- Added `confirm_memory_write`, which checks the waiting checkpoint, actor and
  Episode binding, candidate ID/version/hash, action scope, snapshot freshness,
  expiry and budgets before the deterministic commit controller performs the
  Memory CAS. It records the full confirmation-consumption proof and exact
  state-changing ToolReceipt, advances Memory and Care Context versions,
  adopts the post-commit snapshot and returns an idempotent terminal result.
- Added a real scenario-Q regression covering candidate creation, child-result
  reintegration, exact confirmation display, rejection of the wrong action
  scope, one successful Memory write, snapshot lineage, acceptance conversion
  and replay without a second write.
- Registry v2 now includes policy semantics in its stable manifest hash:
  Agent definitions, invocation/A2A/tool allowlists, Episode participants,
  tools, Safety, exit conditions and budgets, tool confirmation requirements
  and commit-controller tools. Policy changes can no longer retain the same
  release identity merely because registry names stayed constant.
- Bumped the product Runner to v8 and RunResult schema to v5. The checked-in
  acceptance manifest remains v8 with current identity hash
  `5ad345dd8cfff79a24dd0fd769e5f64370585215068ad31b6bce37ba6a7b2334`.

### Codex verification

- Related architecture, Episode runtime, Runner and acceptance suites —
  `132 passed in 1.50s`.
- Product Agent focused suite — `179 passed in 2.02s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `783 passed, 5 skipped in 14.06s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct manifest
  verification confirmed registry hash
  `982b7f887999cec8f0ebac2e3f43c946d9addf3e015c9edc2619ffe61c2cc3d2`,
  current release identity, zero observations and exactly twenty missing
  scenarios.
- Fix pass 1 corrected restore-time indexing so Care transition candidates and
  their publication values are rebuilt from Care payloads, not Memory payloads.
- Fix pass 2 expanded the registry manifest from name-only coverage to the
  complete policy semantics that affect invocation and release behavior.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 35 — Codex build: truthful Memory correction and expiry execution

Date: 2026-07-19

- Closed the scenario-Q Memory correction gap across the real Runner path.
  Confirmed `replace` candidates now publish that the long-term record was
  updated, while confirmed `expire` candidates publish that it was stopped;
  neither operation is falsely described as a newly saved record.
- Made the Runner validate the deterministic commit Receipt before publishing:
  its operation must equal the confirmed candidate, create/replace must return
  a committed Memory ID, and expire must not claim a new Memory ID.
- Replaced model-authored Memory waiting prose with a deterministic exact
  confirmation summary. It states that the change is not yet executed and
  echoes the candidate value, create/replace/expire scope, retention,
  Memory-only effect and revocation path without preserving a potentially
  contradictory model verb.
- Added real grounded-dialogue regressions for both correction and expiry.
  They seed an existing confirmed Memory, prove zero mutation before exact
  confirmation, then prove CAS version advances, the old item moves to expired
  audit history, replacement leaves only the new current value, expiry leaves
  no current value, and the old value is absent from the current Memory set
  available to subsequent selection.
- Bumped the product Runner to v9 and RunResult schema to v6. The checked-in
  acceptance manifest remains v8 with current identity hash
  `b7c7e90abdf37f26f4162ce7cfcdb54d8d113865c23d6882c15aa940e8f92c0c`.

### Codex verification

- Focused create/replace/expire Memory Runner proof — `3 passed, 79 deselected
  in 1.12s`.
- Product Agent focused suite — `181 passed in 1.56s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `785 passed, 5 skipped in 13.78s`.
- Explicit compilation of every product Agent module and the reused
  `agents/risk.py` capability plus `git diff --check` passed. Direct manifest
  verification confirmed current release identity, zero observations and
  exactly twenty missing scenarios.
- Fix pass 1 recomputed the manifest identity using its actual `unreleased`
  release version after the first focused suite rejected a hash calculated for
  a different candidate label.
- Fix pass 2 made the pre-confirmation Memory publication fully deterministic
  after self-review found that retained Dialogue prose could still call a
  correction or expiry a save operation.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 36 — Codex build: confirmed AlertCare notification execution

Date: 2026-07-19

- Closed the AlertCare execution gap without allowing AlertCare to send
  directly. A completed Care Episode now retains the accepted coordination
  candidate and authenticated notification intent, while a new linked
  grounded-dialogue Episode receives a fresh budget for deterministic urgent
  preflight, targeted Safety review, exact display and confirmation.
- Extended coordination candidates with an explicit target reference and
  display label, non-empty stop conditions and an optional immutable copy of
  the original confirmed Care-action scope. Scheduled notifications must be
  timezone-aware and remain within that action's start/end window.
- Tightened the AlertCare acceptance gate so a scoped coordination candidate
  must exactly match the current active candidate ID, version and hash, Care
  Context version, and action time bounds. A later Care adjustment, stop or
  version change therefore invalidates an unexecuted reminder.
- Added an authenticated `ExternalNotificationIntent`. Requests require both
  `external.notify` and the exact `notify_target:<target_ref>` scope; candidate
  target role/reference/label must equal the intent before a deterministic
  `ExternalActionCandidate` is created.
- Added the `external_coordination_review` conditional Safety checkpoint to
  the grounded-dialogue, Care-plan and Care-followup registries. The linked
  Episode registers the accepted external candidate as its sole confirmation
  target, requires an approved unexpired Safety decision bound to its exact
  hash and then displays target, reason, time, original Care scope, stop
  conditions, one-send effect and no-retry behavior.
- Added `confirm_coordination_notification`, which validates the full
  candidate/version/hash, actor, subject, action scope, FactSnapshot, Care
  Context version and expiry binding before `external.notify`. It rechecks the
  current Care action, consumes one confirmation into one ToolReceipt, uses
  the deterministic controller's idempotency boundary and never automatically
  retries an unknown outcome.
- Preserved truthful replay semantics: an already persisted success is
  returned after exact immutable confirmation/binding validation and before
  the now-advanced mutable Care state is reconsidered, so replay cannot send a
  second notification.
- Upgraded the Registry to v3, Episode runtime to v6, Runner to v10 and
  RunResult schema to v7. The checked-in acceptance manifest remains v8 with
  current identity hash
  `032fad9a3ec9ed33efa614d4d91b2a220138ff7c3099c4691c210df227041549`.

### Codex verification

- New AlertCare notification end-to-end proof — `1 passed in 0.76s`, covering
  no send before confirmation, wrong-scope rejection, one successful notify,
  Care-state version advance, scenario-K audit conversion and replay without a
  second external call.
- Related governance, Episode runtime and Runner suites — `127 passed in
  1.86s`.
- Product Agent focused suite — `182 passed in 1.58s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `786 passed, 5 skipped in 13.90s`.
- Explicit compilation of all changed product Agent modules and focused tests
  plus `git diff --check` passed. Direct manifest verification confirmed the
  current release identity, zero observations and exactly twenty missing A–T
  scenarios.
- Fix pass 1 moved Safety and external confirmation into a linked Episode
  after the original Care Episode correctly exhausted its sixteen-call model
  budget during required post-commit continuity work.
- Fix pass 2 moved persisted-success replay detection ahead of mutable Care
  state validation, while retaining exact immutable confirmation and Episode
  binding checks. The related suite then exposed only a stale test fixture;
  it was aligned to generate AlertCare scope from versioned current Care state
  with no runtime behavior change.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 37 — Codex build: urgent preemption on confirmation resume

Date: 2026-07-19

- Closed the remaining urgent-resume gap between `waiting_user` and
  `waiting_confirmation`. Every confirmation path now revalidates the exact
  authenticated binding and waiting FactSnapshot/Care Context, then executes
  the deterministic urgent matcher before any Care, Memory or external
  side-effect commit.
- Applied one shared gate to Care activation/adjustment, Care status
  transitions, Memory persistence, AlertCare notification and Artifact share.
  Confirmation-time text is bounded to eight non-empty inputs and remains
  untrusted data for the registered urgent tool; it cannot alter identity,
  scope, candidate or tool authority.
- A positive urgent result marks the original checkpoint `blocked` with an
  `interrupted_by_urgent:<replacement>` causal link, preserves the old
  checkpoint as revision 1, appends an immutable interrupted revision 2 and
  creates a separate deterministic-only urgent Episode. No ordinary Agent is
  called after the positive recheck and no pending side effect is executed.
- Made the mandatory recheck fail closed. If the urgent matcher is unavailable,
  the waiting Episode becomes `blocked` with
  `confirmation_safety_recheck_failed`; a returned failed ToolReceipt is kept
  in the audit result, and the confirmation remains unconsumed with zero state
  mutation.
- Refactored initial and resume urgent checks through the same deterministic
  execution helper, while preserving the existing registered tool contract,
  authorization scope, retry bound and ToolReceipt identity.
- Upgraded the product Runner to v11 and RunResult schema to v8. Registry and
  Episode runtime contracts remain v3/v6 because their serialized policy and
  checkpoint schemas did not change. The checked-in acceptance manifest stays
  v8 with current identity hash
  `5d39ae64e68349d30cbc7bf9c2828a45f50dfac622296066905c755c241f0f5f`.

### Codex verification

- New confirmation urgent proofs — `3 passed, 83 deselected in 1.18s`,
  covering Care preemption, external-share preemption and fail-closed urgent
  tool outage with an auditable two-attempt failure Receipt.
- Product Runner suite — `86 passed in 1.41s`.
- Product Agent focused suite — `185 passed in 1.83s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `789 passed, 5 skipped in 14.01s`.
- Explicit compilation of the changed Runner and focused tests plus
  `git diff --check` passed. Direct manifest verification confirmed the current
  release identity, zero observations, no identity mismatch and exactly twenty
  missing A–T scenarios; no stale v10/v7 runtime identity remains in product
  code or tests.
- Fix pass 1 added the missing Runner import for the existing
  `ResumeRequiresReplan` identity/freshness exception exposed by the first
  focused run.
- Fix pass 2 retained a failed urgent ToolReceipt in the blocked checkpoint
  revision after self-review found that blocking was correct but its available
  failure evidence was not carried forward.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 38 — Codex build: cross-Episode AlertCare semantic deduplication

Date: 2026-07-19

- Closed the gap between AlertCare's accepted `deduplication_key` and the
  deterministic external-action boundary. Notification execution now carries
  that semantic key into the commit controller instead of relying only on a
  per-attempt idempotency key and confirmation ID.
- Added an atomic semantic namespace bound by the controller to the tool,
  authenticated subject and exact target. A model-authored key cannot collide
  across subjects or targets, and leading/trailing whitespace is normalized
  before hashing so cosmetic changes cannot bypass suppression.
- Successful and unknown notification outcomes reserve the semantic key.
  A later Episode with a new candidate, confirmation, Care Context version and
  idempotency key receives a cached-policy `external.notify` ToolReceipt with
  `outcome=failed`, `error_code=duplicate_coordination`, `executed=false` and
  the original invocation reference; the external executor is not called and
  Care state is not advanced.
- A definite external failure does not reserve the semantic key. This keeps an
  explicitly reconfirmed retry possible while preserving the existing rule
  that an unknown outcome is never retried blindly.
- Bound the semantic key to the idempotency record as well as the candidate
  payload and confirmation. Replaying the same idempotency key with a changed
  coordination key is rejected rather than silently rebinding the prior
  Receipt.
- Made duplicate suppression truthful at publication: the notification result
  states that the same reminder was already handled and was not sent again,
  while retaining a partial/failed audit state instead of claiming success.
- Upgraded Governance to v2, the product Runner to v12 and RunResult schema to
  v9. Registry and Episode runtime remain v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `2dd88514547ac395ba9906c6fe6fdfc31889fd0654b2ccddf3ae19a8f12bd864`.

### Codex verification

- New semantic-deduplication proofs — `3 passed, 29 deselected in 0.91s`,
  covering prior success, prior unknown, whitespace-normalized duplicate
  attempts, cached duplicate replay, distinct semantic reminders and an
  explicitly retryable definite failure.
- Governance and Runner suites — `118 passed in 1.58s`.
- Product Agent focused suite — `188 passed in 1.84s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `792 passed, 5 skipped in 13.74s`.
- Explicit compilation of the changed Governance/Runner modules and focused
  tests plus `git diff --check` passed. Direct manifest verification confirmed
  the current release identity, zero observations, no identity mismatch and
  exactly twenty missing A–T scenarios; no stale v11/v8/Governance-v1 identity
  remains in product code or tests.
- Fix pass 1 normalized surrounding whitespace before hashing the semantic key
  after self-review found that equivalent keys with cosmetic spacing could
  otherwise occupy different deduplication namespaces.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 39 — Codex build: action-status-bound AlertCare stop behavior

Date: 2026-07-19

- Closed an AlertCare stop-condition gap for candidates derived from an
  original confirmed Care action. The acceptance gate previously matched the
  action candidate ID/version/hash, Care Context version and time window but
  did not require the current action itself to remain `进行中`.
- Scoped coordination now requires `CareActionStatus.ACTIVE` before the child
  result can be accepted by its Care parent. Paused, completed and ended
  actions cannot produce a confirmation prompt for an old feedback reminder;
  non-action-scoped coordination for separately justified device/risk cases
  remains available under its own policy and confirmation path.
- Added the same active-status value to the final notification execution tuple
  as defense in depth. Even a restored or adversarial same-version Care state
  whose action identity and dates still match cannot execute when its status is
  paused, completed or ended.
- Preserved existing freshness guarantees: a normal state transition already
  advances Care Context and invalidates the old scope, while the explicit
  status binding prevents reliance on version drift alone.
- Normalized AlertCare deduplication keys in the acceptance gate as well as the
  execution controller. Whitespace-equivalent keys are rejected as duplicate
  candidates before display, and a pure-whitespace key is rejected as empty.
- Upgraded Governance to v3, the product Runner to v13 and RunResult schema to
  v10. Registry and Episode runtime remain v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `ee2fbac009b3f4491f0c1077807688383b088c451a510211fb1e6189a82d0b08`.

### Codex verification

- Action-status AlertCare gate — `3 passed, 32 deselected in 0.64s`, proving
  active acceptance and paused/completed/ended rejection.
- AlertCare gate and deduplication normalization — `4 passed, 31 deselected in
  0.74s`, including whitespace-equivalent and empty-key rejection.
- Governance and Runner suites — `121 passed in 1.37s`.
- Product Agent focused suite — `191 passed in 1.81s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `795 passed, 5 skipped in 13.71s`.
- Explicit compilation of the changed Governance/Runner modules and focused
  tests plus `git diff --check` passed. Direct manifest verification confirmed
  the current release identity, zero observations, no identity mismatch and
  exactly twenty missing A–T scenarios; no stale v12/v9/Governance-v2 identity
  remains in product code or tests.
- Fix pass 1 normalized and validated deduplication keys at acceptance time
  after self-review found that the execution boundary was safe but duplicate
  whitespace variants could still be accepted and shown for confirmation.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 40 — Codex build: time-bound AlertCare preparation and execution

Date: 2026-07-19

- Closed the remaining AlertCare scheduling gap between candidate acceptance
  and side-effect execution. A previously accepted coordination candidate
  could be prepared for confirmation from an old RunResult after its Care
  action had paused or expired, because preparation did not re-read the
  authoritative Care store.
- Coordination preparation now requires the deterministic commit controller
  and checks the exact candidate ID/version/hash, Care Context version, active
  status and original action window before any planning or Agent call. A stale
  candidate therefore cannot be turned into a new confirmation prompt.
- Notification confirmation now enforces the displayed `suggested_at` as the
  earliest send time and the original action end as the latest send time, in
  addition to the existing exact identity, ACTIVE status, Safety, permission
  and confirmation checks. Early and expired attempts remain side-effect free.
- Kept successful confirmation replay idempotent: when a newer Receipt already
  records the same confirmation, the Runner returns that Receipt before
  re-evaluating the now-advanced Care version and never invokes the executor
  again.
- Upgraded the product Runner to v14 and RunResult schema to v11. Governance,
  Registry and Episode runtime remain v3/v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `ea3e1deb3b54e7b7ae2b40f2c6169e83020918e591ff53fb4c9514b43aa292a2`.

### Codex verification

- AlertCare notification lifecycle — `1 passed, 85 deselected in 0.80s`,
  proving paused/expired preparation rejection, early/expired execution
  rejection, on-schedule single execution and successful Receipt replay.
- Product Runner suite — `86 passed in 1.42s`.
- Product Agent focused suite — `191 passed in 1.80s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `795 passed, 5 skipped in 14.69s`.
- Explicit compilation of the changed Runner and focused test plus
  `git diff --check` passed. Direct manifest verification confirmed the current
  release identity, zero observations, no identity mismatch and exactly twenty
  missing A–T scenarios; no stale Runner-v13/RunResult-v10 identity remains in
  product code or tests.
- Fix pass 1 synchronized the strict RunResult literals, moved the business
  time check ahead of the Episode soft-deadline check, and preserved completed
  confirmation replay after focused verification exposed those identity,
  ordering and idempotency interactions.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 41 — Codex build: subject-serialized Care and Memory commits

Date: 2026-07-19

- Closed a shared-state concurrency gap in the deterministic commit boundary.
  The previous controller-owned lock serialized calls only within one
  controller instance; two controllers sharing the same Care and Memory stores
  could interleave after the Memory CAS but before the Care Context CAS.
- Added a deterministic two-thread regression that pauses immediately after
  the Memory write and attempts a concurrent Care activation through another
  controller. Before the fix, the Care activation completed and the Memory
  operation then failed stale, leaving a partially committed Memory value.
- Moved commit locks into the shared in-memory stores and keyed them by
  `subject_id`. Every controller commit now acquires the same Care-then-Memory
  lock pair for that subject before validation and holds it through state CAS,
  Receipt construction and idempotency bookkeeping. Reads and direct store CAS
  use the same subject lock, so they cannot observe the cross-store midpoint.
- Different subjects retain independent locks instead of being globally
  serialized. The losing same-subject operation revalidates after the winner,
  rejects its stale confirmation and leaves the winner's Care/Memory state
  intact.
- Upgraded Governance to v4, the product Runner to v15 and RunResult schema to
  v12. Registry and Episode runtime remain v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `9409de7b32fa0ede81b4e18988c82f41b0103afd6be15381f60d350804339095`.

### Codex verification

- The new concurrency regression failed before implementation because the Care
  thread was not serialized, then passed as `1 passed, 35 deselected in 0.86s`.
- Governance suite — `36 passed in 0.82s` after final self-review cleanup.
- Governance and Runner suites — `122 passed in 1.72s`.
- Product Agent focused suite — `192 passed in 1.92s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `796 passed, 5 skipped in 14.55s`.
- Explicit compilation of the changed Governance/Runner modules and focused
  test plus `git diff --check` passed. Direct manifest verification confirmed
  the current release identity, zero observations, no identity mismatch and
  exactly twenty missing A–T scenarios; no stale Governance-v3/Runner-v14/
  RunResult-v11 identity remains in product code or tests.
- Fix pass 1 replaced the initially safe but globally serialized store lock
  with per-subject shared lock registries after self-review checked the PLAN's
  requirement that mutual state be serialized by subject rather than across
  all elders.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 42 — Codex build: Store-persistent side-effect receipts and deduplication

Date: 2026-07-22

- Closed a cross-controller persistence gap in the deterministic commit
  boundary. Receipt, consumed-confirmation and semantic notification maps were
  previously owned by one controller instance, so a later Episode using a new
  controller over the same Care/Memory stores could repeat an already
  successful or unknown notification.
- Added a Care-Store-bound `InMemoryCommitLedger` containing subject-scoped
  idempotency entries, consumed confirmation keys and successful/unknown
  semantic notification reservations. Controllers sharing the authoritative
  Care store now also share this continuity state automatically.
- Migrated Care activation, Care replacement/transition, Memory persistence
  and all external actions to the shared Ledger. Cached Receipts are copied on
  read, remain exactly bound to their original confirmation/input/dedup key and
  can be retrieved by a newly constructed controller without advancing shared
  state or invoking an executor.
- Semantic notification keys remain normalized over tool, subject, target and
  semantic purpose. Success and unknown reserve the key across controllers;
  a definite failure remains retryable; a duplicate attempt records its own
  truthful failed Receipt without replacing the original reservation.
- Upgraded Governance to v5, the product Runner to v16 and RunResult schema to
  v13. Registry and Episode runtime remain v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `a315e77febde25966ac8f6138d52d7318d7731e402ed28b7abf181d019221215`.

### Codex verification

- The expanded semantic-deduplication test initially failed twice (`succeeded`
  and `unknown`) because a second controller executed both reminders; after
  implementation it passed as `2 passed, 34 deselected in 0.87s` with one
  executor call and third-controller Receipt replay.
- Governance suite — `36 passed in 0.89s`, including cross-controller Care
  activation, Care transition, Memory write and notification idempotency.
- Governance and Runner suites — `122 passed in 1.60s`.
- Product Agent focused suite — `192 passed in 1.83s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `796 passed, 5 skipped in 18.44s`.
- Explicit compilation of the changed Governance/Runner modules and focused
  test plus `git diff --check` passed. Direct manifest verification confirmed
  the current release identity, zero observations, no identity mismatch and
  exactly twenty missing A–T scenarios; no stale Governance-v4/Runner-v15/
  RunResult-v12 identity remains in product code or tests.
- Fix pass 1 expanded idempotency proof beyond notifications so a fresh
  controller must also retrieve existing Care activation, Care transition and
  Memory Receipts from the shared Ledger.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 43 — Codex build: natural Care completion time boundary

Date: 2026-07-22

- Closed a Care status semantics gap between `已完成` and `已结束`. The same
  transition path previously allowed an action to be marked completed before
  its confirmed 3–7 day observation period had naturally ended, which could
  misrepresent an early stop as successful completion.
- The Care acceptance gate now permits `COMPLETED` only when the current
  Evidence SourceScope `as_of` has reached the action's exact
  `action_planned_end_at`. A model-generated early completion candidate is
  rejected before it can become an accepted confirmation target.
- The deterministic commit controller independently applies the same time
  condition to the real submission timestamp. An early confirmation leaves
  Care Context and the shared commit Ledger unchanged; the same candidate,
  confirmation and idempotency key can succeed exactly at the planned end.
- Preserved the separate `ENDED` meaning: an active action may still be
  actively stopped before its planned end after exact confirmation and the
  reason remains in the versioned transition candidate/Receipt path.
- Upgraded Governance to v6, the product Runner to v17 and RunResult schema to
  v14. Registry and Episode runtime remain v3/v6. The checked-in acceptance
  manifest remains v8 with current identity hash
  `27893099912ac9ade285d91b3f8560862122af76242669b1e553f20fc5f28343`.

### Codex verification

- The new acceptance/commit boundary tests initially failed twice because
  early completion was accepted and committed; after implementation they
  passed as `2 passed, 35 deselected in 0.70s`.
- Final boundary proof — `2 passed, 35 deselected in 0.88s`, including exact
  end-time acceptance and reuse of an idempotency key after an earlier
  side-effect-free rejection.
- Governance suite — `37 passed in 0.90s`.
- Governance and Runner suites — `123 passed in 1.71s`.
- Product Agent focused suite — `193 passed in 1.96s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `797 passed, 5 skipped in 14.56s`.
- Explicit compilation of the changed Governance/Runner modules and focused
  test plus `git diff --check` passed. Direct manifest verification confirmed
  the current release identity, zero observations, no identity mismatch and
  exactly twenty missing A–T scenarios; no stale Governance-v5/Runner-v16/
  RunResult-v13 identity remains in product code or tests.
- Fix pass 1 added exact-boundary acceptance and same-idempotency retry proof
  after self-review found the initial regression only demonstrated rejection
  before the planned end.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 44 — Codex build: timezone-aware product time contracts

Date: 2026-07-22

- Closed a wire-contract gap that allowed naive datetimes into SourceScope,
  Safety approvals and confirmation tokens. Those values could make expiry or
  ordering checks fail with ambiguous local-time semantics or a runtime
  comparison error instead of a deterministic schema rejection.
- Added one inherited `StrictContract` post-validation rule for every direct
  typed datetime field. A value must have both `tzinfo` and a concrete UTC
  offset; optional unset fields remain valid, and nested product contracts
  enforce the same rule through their own validation.
- Added explicit regressions for SourceScope `as_of`, SafetyPayload
  `expires_at` and ConfirmationToken `expires_at`. The contract-wide rule also
  covers typed timestamps on FactSnapshot, ToolReceipt, EpisodeReceipt,
  Memory, invocation, runtime checkpoint and acceptance evidence contracts.
- Upgraded the product contract to v2, Governance to v7, the product Runner to
  v18 and RunResult schema to v15. Registry and Episode runtime remain v3/v6.
  The checked-in acceptance manifest remains v8 with current identity hash
  `9446ebae89af6366cf0f66158f1fe5de52a3e2d78737204df66328a684be3ceb`.

### Codex verification

- The three new security-time regressions first failed because all three
  contracts accepted naive datetimes, then passed as
  `3 passed, 37 deselected in 0.86s` after the shared validation rule.
- Product Agent focused suite — `196 passed in 2.23s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `800 passed, 5 skipped in 15.49s`.
- Explicit compilation of the changed contracts/Governance/Runner modules and
  focused test plus `git diff --check` passed. A stale-version search found no
  contract-v1/Governance-v6/Runner-v17/RunResult-v14 identity in product code,
  tests or the checked-in manifest.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 0.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 45 — Codex build: complete authentication-bound FactSnapshots

Date: 2026-07-22

- Closed an identity continuity gap in the immutable FactSnapshot. It
  previously committed only `subject_ref` and `binding_version`, so another
  actor, role or expanded authorization scope could reuse the same Snapshot
  when an authorization service version string remained unchanged.
- FactSnapshot creation now requires the complete `AuthenticatedBinding` and
  stores its stable hash as immutable content. The Snapshot content hash binds
  that authentication hash alongside subject, scope, data/state versions,
  provenance and creation time, and model validation recomputes the hash to
  reject serialized-field tampering.
- ProductEpisodeRunRequest rejects any Snapshot/binding mismatch before the
  urgent preflight or another tool can execute. ProductEpisodeRuntime repeats
  the check for direct construction and restored checkpoints; Snapshot history
  and post-commit Care/Memory advances must retain the exact binding hash.
- Updated all Product Agent Snapshot producers and role/share/notification
  test fixtures to supply their exact authenticated binding. Post-commit
  Snapshot creation uses the runtime's already authenticated binding rather
  than caller-supplied subject/version fragments.
- Upgraded the product contract to v3, Episode runtime to v7, the product
  Runner to v19 and RunResult schema to v16. Governance and Registry remain
  v7/v3. The checked-in acceptance manifest remains v8 with current identity
  hash `408f5b8032f97c8691051131f7c3ed4414297c6a14fb9449d9478441b3155a43`.

### Codex verification

- The initial actor, role and authorization-scope regressions all failed with
  `DID NOT RAISE`; after implementation the Runtime boundary passed as
  `3 passed, 15 deselected in 0.83s`.
- Fix pass 1 first proved that a serialized authentication hash could be
  altered without invalidating a Snapshot, then added content-hash
  self-validation; the combined focused proof passed as
  `4 passed, 15 deselected in 0.82s`.
- Fix pass 2 moved rejection ahead of urgent preflight after review found the
  Runtime-only check occurred after one deterministic tool boundary. Its
  request-schema regression first failed and then passed as
  `1 passed, 86 deselected in 1.25s`.
- Product Agent focused suite — `201 passed in 2.71s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `805 passed, 5 skipped in 15.02s`.
- Explicit compilation of contracts/Episode/Runner and the focused tests plus
  `git diff --check` passed. A stale-version search found no contract-v2,
  Episode-v6, Runner-v18 or RunResult-v15 identity in product code, tests or
  the checked-in manifest.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 46 — Codex build: exact and timezone-valid SourceScopes

Date: 2026-07-22

- Closed a SourceScope contract gap that accepted arbitrary timezone labels,
  six-day `7_day` windows, twenty-nine-day `30_day` windows, personal coverage
  counts larger than their date ranges and personal dates later than `as_of`.
- SourceScope now resolves `timezone_name` through the standard IANA zoneinfo
  database and uses that zone when deciding the local `as_of` date. Personal
  ranges cannot include a future date, while old/stale historical data remains
  representable and is still handled by the existing freshness policy.
- Registered `current_night`, `7_day` and `30_day` scopes now require exactly
  1, 7 and 30 inclusive calendar days. Historical ranges remain variable, but
  every personal scope limits `valid_night_count` to its exact span.
- `general_knowledge` continues to forbid a personal date range and now also
  requires zero valid personal nights, preventing a non-personal answer from
  carrying fabricated coverage metadata.
- Upgraded the product contract to v4, Governance to v8, the product Runner to
  v20 and RunResult schema to v17. Episode runtime and Registry remain v7/v3.
  The checked-in acceptance manifest remains v8 with current identity hash
  `8a44c440ff0564ec8f0195cbc4dd08db411eee451b5bc7ecd874703574b12396`.

### Codex verification

- All six invalid-scope regressions initially failed with `DID NOT RAISE`,
  while the exact 7/30-day positive boundary passed. After implementation the
  focused proof passed as `7 passed, 14 deselected in 0.77s`.
- Fix pass 1 preserved the stable `current_night ... exactly one night` domain
  error after the first Product Agent run found the generic one-day message
  broke an existing contract assertion.
- Product Agent focused suite — `208 passed in 2.66s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `812 passed, 5 skipped in 14.61s`.
- Explicit compilation of contracts/Governance/Runner and the focused test
  plus `git diff --check` passed. A stale-version search found no contract-v3,
  Governance-v7, Runner-v19 or RunResult-v16 identity in product code, tests or
  the checked-in manifest.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 47 — Codex build: timezone-safe confirmation and commit clocks

Date: 2026-07-22

- Closed a runtime-clock gap where a naive `now` reached aware Safety,
  confirmation, Care-scope or Episode deadline comparisons and leaked a Python
  `TypeError` instead of failing as an explicit boundary contract.
- Added one shared timezone-aware datetime guard and applied it to Safety
  acceptance, ConfirmationToken validation, every deterministic Care/Memory/
  external commit, Episode deadline pause/resume accounting, coordination scope
  checks and all Runner entry points that accept an injected clock.
- Runner confirmation entry points now reject an invalid clock before restoring
  or resuming an Episode and, critically, before the confirmation-time urgent
  read-only recheck. No invalid clock can trigger a tool call or state mutation.
- Upgraded the product contract to v5, Governance to v9, Episode runtime to v8,
  the product Runner to v21 and RunResult schema to v18. Registry and tool
  runtime remain v3/v4. The checked-in acceptance manifest remains v8 with
  current identity hash
  `3112dcc012b2edc08775c537126f48316133cd9f682e79329a865f362bfc38d9`.

### Codex verification

- Four test-first regressions initially failed with offset-naive/offset-aware
  `TypeError`s. After implementation all four passed, including a counted-tool
  assertion proving Care confirmation did not perform another urgent recheck.
- Governance, Episode runtime and Runner suite — `149 passed in 1.77s`.
- Product Agent focused suite — `211 passed in 2.27s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `815 passed, 5 skipped in 14.40s`.
- Explicit compilation of contracts/Governance/Episode/Runner and focused tests,
  `git diff --check` and a trailing-whitespace scan passed. A stale-version
  search found no contract-v4, Governance-v8, Episode-v7, Runner-v20,
  RunResult-v17 or previous identity in product code, tests or the checked-in
  manifest.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 0.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 48 — Codex build: authoritative confirmation and Safety policy

Date: 2026-07-22

- Closed a Care contract gap that allowed an otherwise activatable catalog
  candidate to set `confirmation_required=false`. The strict candidate schema
  now rejects it, and the deterministic Care catalog/acceptance boundary repeats
  the check so an already-constructed or validation-bypassed object cannot reach
  publication or commit eligibility.
- Removed `current_policy_version` from ProductEpisodeRunRequest and from both
  external confirmation APIs. The current Safety policy is now trusted Runner
  configuration, not per-request or per-confirmation caller data.
- SafetyReview receives the exact Runner-owned policy version as a
  `system_policy` Context item. Acceptance, external notification and external
  share freshness checks all compare against that same authoritative version.
- Upgraded the product contract to v6, Governance to v10, the product Runner to
  v22 and RunResult schema to v19. Episode runtime, Registry and tool runtime
  remain v8/v3/v4. The checked-in acceptance manifest remains v8 with current
  identity hash
  `fac619aec91f970cbdeebfbb2291eb55be2ca697e5bbb0ad186ad954d5d30b65`.

### Codex verification

- The activatable-candidate Schema and request-policy override regressions first
  failed with `DID NOT RAISE`. After the acceptance test harness explicitly
  constructed a validation-bypassed object, its deterministic gate regression
  also failed with `DID NOT RAISE`; all three then passed after implementation.
- Fix pass 1 added an end-to-end policy-upgrade proof: a v1-approved external
  share restored under a v2 Runner is rejected before the executor or shared
  state is touched.
- Governance and Runner suites — `133 passed in 1.63s`.
- Product Agent focused suite — `215 passed in 2.09s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `819 passed, 5 skipped in 13.90s`.
- Explicit compilation of the public namespace, contracts, Governance, Runner
  and focused tests, `git diff --check` and a trailing-whitespace scan passed.
  A stale-version search found no contract-v5, Governance-v9, Runner-v21,
  RunResult-v18 or previous identity in product code, tests or the checked-in
  manifest; remaining `current_policy_version` uses are the internal
  `accept_safety` comparison and its direct governance tests, not caller-owned
  RunRequest/confirmation inputs.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 49 — Codex build: checkpoint and publication integrity chain

Date: 2026-07-22

- Closed a serialized-checkpoint gap where an accepted work-product hash and
  metadata were not recomputed against the original Agent Envelope. Runtime
  restore now binds every accepted or pending product to its raw output,
  invocation record, Episode, immutable FactSnapshot and target.
- Runtime snapshots now reject duplicate raw/invocation/work-product ids and
  require their accepted-primary and integrated-child sets to exactly match the
  accepted product records. Pending child references must remain unique,
  unaccepted and identical to their persisted raw Envelope.
- RunResult restore now requires its top-level Receipt and publication text to
  match the final runtime checkpoint. Runtime-created receipts carry a stable
  publication hash, so changing both serialized text copies still cannot forge
  a coherent publication record.
- Upgraded the product contract to v7, Episode runtime to v9, the product Runner
  to v23 and RunResult schema to v20. Governance, Registry and tool runtime
  remain v10/v3/v4. The checked-in acceptance manifest remains v8 with current
  identity hash
  `32afd8b25de7c3d8cacd015776a86a8d5ee971fbd89db6d927e77ac69567c59f`.

### Codex verification

- Four test-first tamper cases initially failed with `DID NOT RAISE`: changed
  accepted Evidence, changed accepted hash, changed top-level publication text
  and changed top-level Receipt truth. They passed after the first integrity
  implementation.
- Fix pass 1 followed adversarial self-review: changing both the RunResult and
  checkpoint publication text initially failed with `DID NOT RAISE`; adding the
  Receipt publication hash closed that path. The combined focused proof passed
  as `5 passed in 0.81s`.
- An older acceptance fixture represented a Receipt only at the RunResult top
  level. It was updated to the real checkpoint shape after the stricter contract
  exposed five shared-fixture failures; no production behavior was relaxed.
- Episode runtime and Runner suites — `115 passed in 1.57s`.
- Product Agent focused suite — `220 passed in 2.41s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `824 passed, 5 skipped in 14.51s`.
- Explicit compilation of contracts/Episode/Runner and changed focused tests,
  `git diff --check` and a trailing-whitespace scan passed. A stale-version
  search found no contract-v6, Episode-v8, Runner-v22, RunResult-v19 or previous
  identity in product code, tests or the checked-in manifest.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 1.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 50 — Codex build: Receipt and ToolReceipt restore truth

Date: 2026-07-22

- Closed a persisted-result gap where synchronously changing both RunResult and
  checkpoint Receipt fields could preserve shallow equality. Paused checkpoints
  now require contiguous Receipt revisions, Episode/FactSnapshot lineage and a
  final Receipt that exactly reflects status, invocation, accepted product,
  Safety, candidate, confirmation, action, failure, unknown and publication
  state.
- Complete snapshots now re-prove their plan-required Agent work, verified
  publication and known side-effect outcomes. Complete RunResults likewise
  require every registry-required deterministic tool instead of trusting a
  serialized `complete` flag.
- RunResult ToolReceipts must exactly match checkpoint execution order and
  FactSnapshot history, use the active issuer version and registry definition,
  remain within the authenticated scope, and exactly account for state-changing
  actions, unknown outcomes and confirmation consumption.
- Corrected the urgent registry so its deterministic minimum is only
  `risk.match_urgent_boundary`; common policy/snapshot tools can no longer delay
  the PLAN-mandated urgent response. The linked coordination-notification
  Episode now executes its registered snapshot and policy preplanning tools.
- Upgraded Episode runtime to v10, Registry to v4, the product Runner to v24 and
  RunResult schema to v21. Product contract, Governance and tool runtime remain
  v7/v10/v4. The checked-in acceptance manifest remains v8 with current identity
  hash `30d97c3d8b8e05d3037f8b34aa4b74b59aabbf834c89bd0bd01c269749cfe997`.

### Codex verification

- Three initial test-first cases failed with `DID NOT RAISE`: coherent Receipt
  truth tampering, a missing top-level ToolReceipt and a ToolReceipt bound to an
  unknown FactSnapshot. The focused proof passed after implementation.
- Fix pass 1 adversarially removed a required Agent or required tool from every
  serialized representation; both initially failed with `DID NOT RAISE`.
  Additional scope, registry-name and issuer-version tampering also initially
  failed. The implementation re-runs completion and ToolReceipt trust checks at
  restore time.
- Fix pass 1 exposed and corrected two real-path contract mismatches: commit
  receipts are Governance-versioned, and urgent must not inherit non-urgent
  common required tools. It also added missing deterministic preplanning to the
  linked coordination notification Episode. No completion check was relaxed.
- Fix pass 2 proved that coherently deleting the confirmation consumption for a
  confirmed Care commit initially failed with `DID NOT RAISE`; state-changing
  Receipt ids and unknown outcomes are now exactly cross-checked as well.
- Final tamper proof — `11 passed in 0.82s`; confirmed Care commit proof —
  `1 passed in 0.78s`; Episode runtime and Runner suites — `123 passed in
  1.72s`; Runner suite after the final fix — `101 passed in 1.70s`.
- Product Agent focused suite — `228 passed in 2.21s` across architecture,
  governance, invocation, Episode runtime, tooling, Runner and release gates.
- Full Python regression — `832 passed, 5 skipped in 14.35s`.
- Explicit compilation of Episode/Registry/Runner and changed tests,
  `git diff --check` and a trailing-whitespace scan passed. A stale-version
  search found no Episode-v9, Registry-v3, Runner-v23, RunResult-v20 or prior
  identity in product code, tests or the checked-in manifest. A direct registry
  assertion proved urgent requires exactly `risk.match_urgent_boundary`.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 51 — Codex build: Plan and budget restore provenance

Date: 2026-07-22

- Closed a persisted-checkpoint gap where a serialized EpisodePlan could stop
  matching the active registry and runtime counters could be lowered below the
  recorded Agent, Orchestrator and tool calls. Such a checkpoint could parse as
  a RunResult and regain runtime budget on restore.
- EpisodeRuntimeSnapshot now revalidates its EpisodePlan against the current
  registry and derives all five counters from immutable invocation evidence:
  initial planning from `plan`, replanning from `replan`, Agent and tool calls
  from their recorded ids, and total model calls from Agent plus Orchestrator
  records.
- Orchestrator and tool invocation ids must be unique, every Agent and
  Orchestrator invocation must belong to the plan Episode, and an intelligent
  plan's `created_by_invocation_id` must identify an accepted plan-producing
  Orchestrator call for the same plan revision.
- Upgraded Episode runtime to v11, the product Runner to v25 and RunResult
  schema to v22. Product contract, Registry, Governance, tool runtime and
  acceptance manifest remain v7/v4/v10/v4/v8. The checked-in manifest identity
  is now `88cdaf376ae4dfb0abd45fc9ab36d8794dd1a081287e450fe4773a413bc6f4bb`.
- Rebuilt the acceptance runtime fixture as a registry-valid morning Episode
  with independent Evidence and Dialogue work products, six required tools,
  exact counters and a revision-2 terminal Receipt. Existing coherent-tamper
  tests now preserve counter consistency so they continue reaching their
  intended deeper completion checks.

### Codex verification

- Four initial test-first cases failed with `DID NOT RAISE`: a plan that
  omitted a registry-required Agent and lowered Agent, tool or total-model
  counters. They passed after snapshot provenance validation was implemented.
- Fix pass 1 extended the proof to all five counters and added the missing plan
  creator, cross-Episode invocation and invocation-id uniqueness checks.
- Fix pass 2 upgraded the acceptance fixture and coherent deletion tests
  without relaxing any runtime or release validation.
- Final tamper proof — `7 passed in 0.85s`; Product Agent focused suite —
  `235 passed in 2.82s` across architecture, acceptance, Episode runtime,
  governance, invocation, Runner and tooling.
- Full Python regression — `839 passed, 5 skipped in 14.94s`.
- Explicit compilation of all nine Product Agent modules and three changed
  tests passed. `git diff --check`, a trailing-whitespace scan and a stale
  Episode-v10/Runner-v24/RunResult-v21/identity search passed.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 52 — Codex build: restored Agent authority provenance

Date: 2026-07-22

- Closed a checkpoint capability-injection gap where serialized delegation,
  A2A or shared-child owner entries could be restored without proving that the
  originating Agent actually emitted the corresponding request. Pending child
  ownership is now reconstructed through the causal invocation parent chain.
- Runtime snapshot validation replays every registered delegation and A2A
  policy check from the original AgentEnvelope, rejects duplicate authority
  ids, and exactly recomputes A2A semantic deduplication plus the two-round
  dispute counters. Clearing those counters can no longer reset the A2A limit.
- AgentInvocationRecord now records the delegation or A2A request consumed by
  a target call. Snapshot validation derives that consumption independently
  from source Envelope, unique target and invocation topology, so coherently
  clearing the audit field while restoring pending authority is rejected.
- A single Agent invocation may no longer delegate or send A2A to the same
  target twice, and one target call cannot consume delegation and A2A authority
  together. Authorization is removed only after the route has been validated
  and the budgeted call is ready to execute.
- Upgraded the product contract to v8, Episode runtime to v12, Runner to v26
  and RunResult schema to v23. Registry, Governance, tool runtime and
  acceptance manifest remain v4/v10/v4/v8. The checked-in manifest identity is
  `06cf8bc7cd3e6b4fc54fecc4b7d3873abd6bb11ea4e6657d96229723e60d030d`.

### Codex verification

- Five initial test-first cases failed with `DID NOT RAISE`: forged pending
  delegation, forged pending A2A, forged shared-child owner, changed pending
  child owner and cleared A2A replay state. All passed after causal authority
  reconstruction was implemented.
- Fix pass 1 showed that a consumed delegation or A2A request could be inserted
  back into the pending map. Both replay tests initially failed with
  `DID NOT RAISE`; consumed request ids were added to invocation audit records.
- Fix pass 2 synchronously cleared those new ids while restoring pending
  authority; both strengthened tests again failed with `DID NOT RAISE`.
  Consumption is now derived from the source-to-target invocation graph, with
  the serialized audit fields required to match that independent derivation.
- Final authority proof — `11 passed in 0.89s`; Episode/Runner/Acceptance
  suites — `160 passed in 2.36s`; Product Agent focused suite — `242 passed in
  2.99s` across architecture, acceptance, Episode runtime, governance,
  invocation, Runner and tooling.
- Full Python regression — `846 passed, 5 skipped in 14.95s`.
- Explicit compilation of all nine Product Agent modules and three changed
  tests passed. `git diff --check`, a trailing-whitespace scan and a stale
  contract-v7/Episode-v11/Runner-v25/RunResult-v22/identity search passed.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 53 — Codex build: Orchestrator checkpoint lifecycle provenance

Date: 2026-07-22

- Closed a persisted-checkpoint gap where `pending_evaluation_after` could be
  cleared to skip the PLAN-required Orchestrator checkpoint, forged to block
  progress or replayed after an accepted evaluation. Snapshot validation now
  derives lifecycle state from the complete Agent and Orchestrator invocation
  graph, including repaired Agent attempts.
- Every terminal Agent invocation must now be exactly one of evaluated by one
  causally later accepted Orchestrator call, explicitly abandoned by a terminal
  deterministic fallback, or the sole currently pending evaluation. Duplicate,
  missing, non-causal and replayed lifecycle records fail closed on validation.
- Added `AbandonedEvaluationSnapshot` as a strict public wire contract. Runner
  exception, degraded, blocked, deadline and waiting-termination paths now emit
  a bounded Reason Code plus a Receipt-bound failure marker instead of silently
  clearing the pending checkpoint. Active snapshots cannot forge abandonment.
- Upgraded Episode runtime to v13, the product Runner to v27 and RunResult
  schema to v24. Product contract, Registry, Governance, tool runtime and
  acceptance manifest remain v8/v4/v10/v4/v8. The checked-in manifest identity
  is now `c65691157d5678241cc10581439195c968839a7b668fc29384be797fc1201d18`.

### Codex verification

- Three initial test-first cases failed with `DID NOT RAISE`: clearing a real
  pending checkpoint, forging a nonexistent pending invocation and replaying a
  checkpoint after accepted evaluation. The invocation-derived validator made
  all three pass.
- The first combined suite exposed twenty honest compatibility failures: legacy
  Runner terminal paths silently cleared checkpoints, the acceptance runtime
  fixture omitted its two evaluation calls, and a coherent deletion test left
  an orphan evaluation. Those paths now record explicit abandonment or complete
  causal evaluation evidence; no lifecycle check was relaxed.
- Fix pass 1 proved an active checkpoint could not synchronously forge both an
  abandonment record and matching failure marker. It also asserted an actual
  AcceptanceError fallback persists the abandonment in both snapshot and final
  Receipt.
- Fix pass 2 bounded fallback Reason Codes independently of caller text and
  publicly exported the new strict snapshot type so downstream serializers do
  not depend on an internal module path.
- Final lifecycle subset — `11 passed, 41 deselected in 1.37s`; targeted
  fallback/waiting proof — `5 passed in 1.24s`; Episode/Runner/Acceptance suites
  — `164 passed in 1.90s`.
- Product Agent focused suite — `246 passed in 2.40s` across architecture,
  acceptance, Episode runtime, governance, invocation, Runner and tooling.
- Full Python regression — `850 passed, 5 skipped in 14.23s`.
- Explicit compilation of all nine Product Agent modules and three changed
  tests, `git diff --check`, a trailing-whitespace scan and a stale
  Episode-v12/Runner-v26/RunResult-v23/identity search passed.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 54 — Codex build: replan trigger lifecycle provenance

Date: 2026-07-22

- Closed a persisted-checkpoint gap where `pending_replan_trigger` could be
  cleared, forged or replayed independently of the Orchestrator decision that
  created it. Accepted evaluation invocations now retain their structured
  decision and bounded trigger rather than only a free-text summary.
- Added strict pending and abandoned replan contracts. Snapshot validation now
  proves every Orchestrator-triggered replan is exactly pending, consumed by one
  causally later replan invocation, or explicitly abandoned by a terminal
  fallback whose marker is bound into the final Receipt.
- Deterministic policy replans must identify a recorded ToolReceipt, action
  Receipt or genuinely advanced FactSnapshot. Agent invocation ids and the
  initial snapshot cannot be repackaged as deterministic new evidence, and one
  source cannot be consumed, abandoned or left pending more than once.
- Replan invocation records now preserve source kind, source ref and trigger;
  Runner exception/deadline/degraded paths audit an unperformed replan instead
  of silently clearing it. The strict lifecycle types are publicly exported.
- Upgraded Episode runtime to v14, the product Runner to v28 and RunResult
  schema to v25. Product contract, Registry, Governance, tool runtime and
  acceptance manifest remain v8/v4/v10/v4/v8. The checked-in manifest identity
  is now `fad91427f1bacea9f7b9e5e108068b165d86d7c2271728b303f08dc1befaa1e9`.

### Codex verification

- Three initial test-first cases failed with `DID NOT RAISE`: coherently
  clearing an Orchestrator replan request, forging one from a `continue`
  decision and replaying an evaluation after its replan completed. All pass
  under invocation-derived lifecycle validation.
- Added positive proofs that a ToolReceipt-triggered deterministic request
  round-trips through restore and is recorded on its replan invocation, while a
  deadline terminal Receipt explicitly audits an unperformed Orchestrator
  replan.
- Fix pass 1 produced two further `DID NOT RAISE` proofs and then excluded Agent
  invocation ids and an unchanged initial FactSnapshot from deterministic
  trigger authority.
- Fix pass 2 proved one ToolReceipt could trigger repeated replans before
  enforcing mutual exclusion across consumed, abandoned and pending source
  sets. This implements the PLAN prohibition on rewrite-only replan without new
  information.
- Final replan proof — `10 passed, 38 deselected in 0.78s`;
  Episode/Runner/Acceptance suites — `172 passed in 2.20s`.
- Product Agent focused suite — `254 passed in 2.21s` across architecture,
  acceptance, Episode runtime, governance, invocation, Runner and tooling.
- Full Python regression — `858 passed, 5 skipped in 14.32s`.
- Explicit compilation of all nine Product Agent modules and three changed
  tests, `git diff --check`, a trailing-whitespace scan and a stale
  Episode-v13/Runner-v27/RunResult-v24/identity search passed.
- Direct manifest verification recomputed the checked-in identity exactly and
  confirmed zero observations, no identity mismatch, all twenty A–T scenarios
  missing and release eligibility false.

Fix rounds used: 2.

No implementation deviation was required. The release manifest remains empty
and fail-closed pending actual A–T observations, three-run real-provider
evidence and domain-reviewed expectations. No commit, push, frontend/API
mutation or legacy removal was made.

### Round 55 — Codex build: repository architecture audit and legacy removal

Date: 2026-07-24

- Audited the repository from the active FastAPI routes, Next.js entrypoint,
  package imports and remaining tests rather than deleting by filename alone.
  The current radar runtime, Product Agent migration base, product-device
  compatibility API, Perceptor integration and four locked design-plan
  directories remain in place.
- Removed the retired research runtime as one dependency-closed unit: old
  Agent/task graph, preprocessing, model, training, evaluation, metrics,
  schemas, services and eighteen scripts. Its backend endpoints and Python/
  frontend clients were removed at the same time so no dead compatibility path
  remains reachable.
- Removed fifty-five tests that exclusively protected deleted behavior. The
  remaining fifty-five test files cover the active radar runtime, Product Agent
  migration base, product-device layer, Perceptor, observability, frontend
  contracts and health endpoints.
- Removed the retired frontend workbench and its isolated component/type/API
  tree. `/` and `/radar` remain the only product pages, with the same-origin
  radar BFF retained.
- Removed completed or superseded root plans, implementation logs, legacy
  architecture/data/demo documents and the stale v1 operations/acceptance
  material. `README.md` now distinguishes locked target architecture from the
  transitional implementation and links only the four current plan authorities
  plus the Perceptor boundary document.
- Decoupled two valid product paths before deleting their old dependencies:
  product radar run events/artifacts now own their narrow schemas, and the
  Perceptor webhook uses only its product-specific store setting.
- Removed stale research dependencies and optional extras from `pyproject.toml`,
  removed retired environment/configuration fields, and corrected Docker/
  Compose inputs so container builds no longer copy or configure deleted paths.
- Removed the explicitly authorized local research data tree, third-party source
  checkout and an unreferenced 179 MB VSIX archive. These untracked local files
  are not recoverable through Git.

### Codex verification

- Focused import/API boundary suite — `62 passed in 1.99s`.
- Full remaining Python regression — `599 passed in 11.95s`.
- Frontend typecheck — passed after deleting a stale `.next` reference to the
  removed page.
- Frontend production build — passed; generated routes are `/`, `/radar`,
  `/api/radar/[...path]` and the framework not-found page.
- Docker Compose configuration validation passed.
- FastAPI route audit reported `routes=33`, all required current endpoints
  present and all explicitly retired endpoint families absent.
- Static scans found no imports of deleted package namespaces and no dangling
  references to removed routes or documents in active source.
- `py_compile` for the changed backend/product/integration boundary and
  `git diff --check` passed.

Fix rounds used: 1.

The explicit repository-cleanup request overrides only the locked plan's
old-research-cleanup out-of-scope item. This build does not implement the target
four-Agent roster, does not modify Agent/Skill behavior online and does not
commit or push changes.

### Round 56 — Codex build: four-role 1+2+1 Product Agent convergence

Date: 2026-07-26

- Replaced the transitional nine-identity Product Agent roster in place with
  exactly four model identities: `SleepCareAgent`,
  `EvidenceReasoningAgent`, `CareStrategyAgent` and conditionally invoked
  `SafetyReviewAgent`. Planning and final communication are separate
  SleepCare Invocations represented by `WorkProductKind`, not fake Agents.
- Rebuilt the Product Agent Registry around center routing, typed
  collaboration and deny-by-default capability matrices. Trend calculation,
  report rendering, coordination, questionnaire, reviewed knowledge and
  Memory access are deterministic Tools/Services; all writes and external
  effects remain exclusive to the Commit Controller.
- Added strict four-role payloads and Envelopes, immutable FactSnapshot and
  SourceScope binding, independent Evidence/Care/Safety/Communication
  acceptance gates, one-primary-action enforcement, target-bound Safety
  approval, confirmation binding, idempotent commits and publication
  postflight.
- Rebuilt the runtime-owned Episode lifecycle and runner around the registered
  minimal paths. SleepCare performs bounded planning/evaluation/replanning;
  runtime owns budgets, revisions, waiting/resume, completion and Receipts;
  urgent/data-quality paths are deterministic; specialist failure cannot be
  taken over by another Agent.
- Safety is determined after each target forms rather than trusted to the
  model plan. Low-confidence/conflicting Evidence and unsafe Care trigger
  review before consumption/publication. Doctor material is reviewed against
  the exact final Communication target; ordinary elder/family material does
  not fixed-call Safety. Revision returns to the responsible Agent, changes the
  target hash and is bounded to two rounds.
- Added the Product Skill Foundation interfaces required by the plan:
  four-role AgentProfile, 18 approved baseline Skill Packages, Registry
  snapshot, deterministic Resolver, exact SkillLock, Prompt Compiler receipt
  and structured SkillOutcome. This does not enable candidate generation,
  online prompt mutation, automatic approval or automatic promotion.
- Migrated `skills_design/PLAN.md` owner/resolution mapping to the four roles,
  updated README migration truth, raised Product contract/Registry/runtime/
  runner/tool/governance/acceptance identities, and rebound the fail-closed
  acceptance manifest to identity
  `da40f84fd62355e74ac0bc94e5ed5b74000444d4501c86746edcb9a9d8958e61`.
- Replaced the old Product Agent behavioral tests with four-role assertions
  covering roster, authority, gates, minimal paths, Safety ordering and
  revision, deterministic tools, Memory/commit state, resume, degradation,
  urgent preemption and release/Skill Foundation invariants.

### Codex verification

- Product Agent focused proof:
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest
  tests/test_product_agent_architecture.py
  tests/test_product_agent_governance.py
  tests/test_product_agent_invocation.py
  tests/test_product_agent_episode_runtime.py
  tests/test_product_agent_tooling.py
  tests/test_product_agent_runner.py
  tests/test_product_agent_acceptance.py -q` — `59 passed in 0.33s`.
- Full Python regression:
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q` —
  `398 passed in 9.96s`.
- Explicit compilation of all ten Product Agent modules passed.
- Acceptance identity, exact 18-Skill owner roster and checked-in manifest
  equality passed.
- Static scans found no removed Product `AgentId`, old child-Agent payload,
  stale `1+4+4`/nine-role owner name, TODO or FIXME in the migrated scope.
- `git diff --check` passed.
- Ruff was not available in the current Python environment
  (`No module named ruff`); no Ruff result is claimed.

Fix rounds used: 2. Round 1 made the budget cover the specified two Safety
revision rounds and bound the complete Registry policy into the release hash.
Round 2 corrected Safety ordering so doctor material reviews the exact final
Communication target while ordinary role material remains conditional.

No implementation deviation was required. The acceptance observation manifest
remains intentionally empty and release-ineligible until separate real-provider
repetitions and domain-reviewed observations are collected; this does not
re-enable any old Agent identity or block the completed architecture build. No
commit, push, release or remote mutation was made.

### Round 57 — Codex build: production wiring and governed execution closure

Date: 2026-07-26

- Connected `/product/radar/chat`, `/product/radar/agent-runs` and run
  follow-ups to the same `ProductEpisodeRunner`. The product run service now
  acts only as a compatibility DTO/store shell when injected by the backend;
  it does not invoke its previous single-JSON decision path. Unconfigured
  models remain a deterministic failure state rather than a legacy fallback.
- Added fail-closed server-side actor/role/subject configuration for every
  configured product-model path and documented the controlled single-subject
  deployment variables. The API no longer creates an authenticated
  FactSnapshot from placeholder identity values.
- Executed Agent Tool requests and typed collaboration requests centrally,
  returned ToolReceipt/collaboration Context, and reinvoked the requesting
  Agent with duplicate detection, causal/version/expiry binding and a two-round
  budget. Episode plans, Agent/Profile allowlists and the selected Skill
  Package all constrain Tool requests.
- Bound every runtime target hash to the exact typed output payload plus
  FactSnapshot, SourceScope, revision, input refs, Agent/Profile/Skill/Schema
  and policy versions. Runtime acceptance recomputes this hash so payload
  mutation invalidates the result.
- Added exact Communication semantic bindings and repeated them at publication
  postflight. Personal claims, Care actions, reviewed-knowledge source payloads
  and every rendered number must remain mapped to accepted upstream content;
  audience role is also an explicit request and gate invariant.
- Applied Agent-specific Context cropping, including ToolReceipt visibility,
  accepted-work-product visibility, user text, exact Safety target and
  requested audience. Every planning, evaluation and specialist Invocation now
  resolves an approved champion or explicitly assigned subject canary, compiles
  the prompt and records Profile, Package, SkillLock and prompt-bundle hashes.
- Added governed Memory candidate commit, confirmation-bound Care activation
  and transition, bounded cross-day Care history, active-constraint rejection,
  exact external-action Safety review and confirmation-bound Commit Controller
  execution. UNKNOWN external outcomes remain cached and are not blindly
  retried.
- Raised the product contract, Registry, Episode runtime, Runner/RunResult,
  Governance, Skill Foundation and acceptance manifest versions. The
  fail-closed checked-in acceptance identity is
  `23104093e6edad47cff4910a36fe87ad8dd3119c199408ad5bf4a735e4e5eef8`.

### Codex verification

- Product Agent/API focused regression — `118 passed` across architecture,
  acceptance, Episode runtime, governance, invocation, Runner, tooling,
  product API and product run compatibility tests.
- Full Python regression — `488 passed in 10.18s`.
- `git diff --check` and explicit compilation of the active backend,
  product-device and Product Agent packages passed.
- Added proofs for output-payload target hash drift, exact reviewed-knowledge
  number preservation, subject-only Skill canary selection, strict Context
  visibility, Tool and collaboration feedback loops, Memory confirmation,
  Care activation/transition history, contraindication rejection, external
  Safety/confirmation/commit and both configured product API entry paths.
- Direct release verification reports no hard violation or identity mismatch,
  but remains ineligible: all 24 scenarios lack checked-in real observations,
  non-deterministic scenarios lack three real-provider repetitions, and the
  Habit domain-review and 3–5 participant usability reports are absent.
- The environment had no `DEEPSEEK_API_KEY`,
  `SLEEPAGENT_RADAR_AGENT_LLM_API_KEY` or product API auth credential, so no
  real-provider or human evidence was fabricated.

Fix rounds used: 1. The fix aligned ScenarioModel and runtime Context with the
new audience-role gate; the subsequent focused and full proofs passed.

No implementation deviation was required. Formal release eligibility remains
an external evidence-collection gate, not a code fallback. No commit, push,
release or remote mutation was made.

### Round 58 — Codex build: real task API single-track and resumable Product Episodes

Date: 2026-07-27

- Made `product_episode` the `/radar-agent/tasks` request default and
  `product` the production runtime default. Production rejects creation or
  mutation of `legacy_fixed`/`dynamic_goal` tasks, does not instantiate the
  dynamic worker, and requires server-bound actor, role, subject,
  authorization and role-binding identity.
- Routed task execution and task chat through the shared
  `ProductEpisodeRunner`, persisted Product receipts/invocation metadata, and
  rendered the Product Communication artifact and `EpisodeReceipt` directly
  in the frontend. Hidden resume checkpoints are excluded from task-detail
  artifacts.
- Removed the retired single-model generation implementation from
  `RadarSleepAgentService`; create, role-material and follow-up paths have no
  selectable fallback and start with `graph_mode=product_episode_runner`.
  Historical status/schema helpers remain available only for reading old
  records.
- Added exact pending-confirmation and user-input contracts to
  `ProductEpisodeRunResult`. Memory, Care, Habit and external-action
  confirmations expose candidate ID, actual payload/target hash, actor,
  subject, action scope and expiry. Declined targets are explicitly bound and
  suppressed rather than silently re-requested.
- Added durable Product request checkpoints and synchronous task resume.
  Confirmation and user-fact continuation reuse the frozen FactSnapshot and
  tool inputs; approved tokens are rebuilt only from server-held target
  material, candidate drift fails closed, and successful commits update the
  confirmation execution receipt.
- Represented resumed user facts as typed authenticated records. Elder input
  receives `user_report` provenance; family/doctor input receives
  `authorized_observer_report` provenance. Evidence acceptance now requires
  those exact runtime-authorized refs, so free-form context cannot masquerade
  as a reviewed personal fact.
- Added the Product API adapter to release identity, raised Runner/Result,
  Governance and acceptance versions, and rebound the fail-closed manifest to
  identity
  `c620d7050539bc21bd01e357b24265b59d59b116c068b35e8d6e6ebcd7f5dfc4`.
- Generated
  `sleep_habit_profile/real-evidence-v20-collection` as deliberately
  ineligible, current-identity-bound collection templates. Template markers
  and schema rules prevent these files from being promoted as real evidence.

### Codex verification

- Full Python regression:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q` —
  `497 passed in 10.72s`.
- Focused Product Runner/governance/task API proof passed, including default
  Product routing, production authorization, no dynamic worker, frozen
  confirmation resume, exact target hash, confirmation execution receipt,
  reviewed user-fact resume and authorized observer provenance.
- Frontend typecheck passed.
- Frontend production build passed; Next.js generated `/`, `/radar`,
  `/habit-profile` and `/api/radar/[...path]`.
- Explicit `py_compile`, checked-in acceptance identity equality and
  `git diff --check` passed.
- Current formal-material audit correctly exited ineligible. It found 68
  provider observation slots, 10 domain concept reviews and three usability
  slots, but all are templates/simulated until replaced with actually
  executed provider receipts, named professional review/signature and
  observed 60+ participant sessions.
- Runtime environment check found no `DEEPSEEK_API_KEY`, product actor binding
  or API credentials; no real-provider run or human sign-off was fabricated.

Fix rounds used: 2. The first made explicit demo CLI compatibility independent
of the new Product default and completed frontend receipt rendering. The
second removed the retired service path, made the dynamic worker absent in
production, and bound resumed user facts to authenticated provenance.

The code implementation is complete for the scoped architecture. Formal
release eligibility is still blocked on external evidence collection and
human review; the checked-in manifest remains intentionally empty and
fail-closed. No commit, push, release or remote mutation was made.

### Round 59 — Codex build: durable Product state, real external effects and evidence-bound release gate

Date: 2026-07-27

- Replaced the remaining process-local Product Memory/Care stores with
  database-backed CAS stores and added durable Product Episode result history.
  Memory candidates, active Care state and cross-day Care transitions now
  survive runtime restart through the same persistence boundary used by the
  production API.
- Added a durable Product commit journal that reserves the idempotency binding
  before a state mutation or external effect and finalizes the exact
  `ToolReceipt` afterward. A recovered pending entry is reported as an
  indeterminate prior attempt and is never blindly re-executed; finalized
  receipts replay without repeating the effect.
- Removed the in-process external-action success stub. Production now uses an
  explicitly configured HTTP executor for notification, share and export
  targets, validates HTTPS for non-local endpoints, binds the complete
  confirmed target, requires a provider request ID and accepted/delivered
  status, returns only sanitized receipt fields, and fails closed when no
  endpoint is configured.
- Wired the persistent Habit, Memory, Care, commit-journal and Product result
  stores plus the configured external executor into the shared
  `ProductEpisodeRunner` factory. The public Product API and task API therefore
  use one production Runner and one durable state/commit boundary rather than
  process-local substitutes.
- Strengthened formal observations with a hash-bound sanitized runtime
  receipt, exact external-action gateway receipt and restart-verified state
  persistence receipt. Provider scenarios cannot claim deterministic-only
  runs; external-action evidence requires the real gateway receipt; Memory and
  Care scenarios require an advancing version observed after restart.
- Raised the Runner/Result, Governance, Product API adapter and acceptance
  versions and rebound the empty fail-closed manifest and v23 collection
  templates to release identity
  `e2d721ca0d8906bf9fd53ab7844e4f49a5b892acf0ad14675471c1ed14c689d2`.
  Historical v18/v20 materials remain explicitly stale simulation/regression
  fixtures.

### Codex verification

- Full Python regression:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q` —
  `508 passed in 14.89s`.
- Frontend `npm run typecheck` passed.
- Frontend production `npm run build` passed; generated routes are `/`,
  `/radar`, `/habit-profile` and `/api/radar/[...path]`.
- Explicit `compileall` of the active backend, Radar Agent, Product Device and
  integration packages passed.
- Focused persistence/external-action coverage proves restart survival,
  receipt replay without a second mutation/effect, pending-journal
  fail-closed behavior, exact HTTP target delivery and rejection of
  unconfigured or insecure endpoints.
- The checked-in manifest exactly matches the current release identity. The
  release verifier reports `eligible=false`, all 24 scenarios missing and no
  hard violations.
- The v23 material audit intentionally exited `2`: it found 68 provider
  observation slots, 10 domain concept slots and three usability slots, but
  correctly rejected them as simulated/templates rather than real evidence.
- Environment checks found no model-provider credential, Product actor/API
  binding, external-action endpoint or gateway credential. No provider
  receipt, professional signature or participant observation was fabricated.
- Final `git diff --check` passed after the log append.

Fix rounds used: 2. The first replaced process-local Product state and commit
receipt caching with durable CAS/journal persistence. The second removed the
fake external success path and made release observations prove the runtime,
external gateway and restart state they claim.

The requested local architecture implementation is complete. Formal release
eligibility remains blocked on actual provider executions, a configured
external-action gateway, named qualified domain/medical approval and observed
3–5 participant sessions aged 60 or older. Those are external evidence, not
values that can be generated truthfully by repository code. No commit, push,
release or remote mutation was made.

## Act 3 — Build

### Round 60 — Codex build: Human-in-the-Loop runtime governance

Date: 2026-07-30

- Implemented HITL as a deterministic cross-layer governance protocol rather
  than a fifth Agent. Added versioned R0–R4 policy routing, exact
  `ActionProposal` contracts, single/dual approval requirements, hard blocks,
  revocation/expiry/execution states, one-time `ApprovalGrant` capabilities and
  append-only decision audit events.
- Added migration `009_human_decision_governance` and persistence adapters for
  authoritative human decisions, decision events and pending Habit Profile
  change sets. Habit proposals now survive an application restart before elder
  confirmation.
- Extended the Product Episode checkpoint with the complete frozen result and
  added `commit_frozen_confirmations`. Confirmation resume now performs only
  exact-target Commit Controller work: it does not call an Agent, replan,
  regenerate the target or republish the communication draft.
- Bound Commit Controller confirmation tokens to approver role, decision,
  policy, FactSnapshot, authorization, role binding and grant hash. Memory,
  Care, Profile and external effects require the elder owner; API authority is
  revalidated both when the person decides and immediately before a grant is
  minted.
- Made Product human decisions authoritative while retaining
  `HumanConfirmationRequest` as a compatibility projection. Task details expose
  decision state and the frontend shows risk, required role, exact changes,
  reason, affected party, duration and revocation limits; wrong-role controls
  are disabled.
- Documented runtime versus offline release governance, including the separate
  administrator/independent-reviewer path for skills, models, gateways and
  production releases. Automatic skill release remains deliberately out of
  scope and fail-closed.

### Codex verification

- Focused HITL, Runner, governance, persistence, Habit, task API, UI-contract
  and manifest-identity proof:
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
  tests/test_product_human_decision.py tests/test_product_agent_governance.py
  tests/test_product_agent_runner.py tests/test_product_agent_persistence.py
  tests/test_radar_task_api.py tests/test_habit_profile_api.py
  tests/test_habit_profile_persistence.py tests/test_radar_agent_persistence.py
  tests/test_habit_profile_frontend_contract.py
  tests/test_radar_human_confirmation_matrix.py
  tests/test_product_agent_acceptance.py::test_checked_in_manifest_matches_current_identity_and_starts_fail_closed`
  — `120 passed in 6.54s`.
- A dedicated API regression proves a family-originated Episode cannot be
  approved by the family role, can be approved by the elder role, commits the
  exact frozen Care target and performs no second Agent run.
- Frontend `npm run typecheck` passed.
- Frontend production `npm run build` passed and generated `/`, `/radar`,
  `/habit-profile` and `/api/radar/[...path]`.
- Python compilation of every changed backend module passed; SQLite migration
  bootstrap reached `009_human_decision_governance`.
- Full regression: `551 passed, 3 failed in 17.76s`. The remaining failures
  are outside this build scope and reproduce existing repository drift: one
  HealthClaw fixture creates a governed item that is filtered before it can
  test handle invalidation, and two historical acceptance fixtures contain 10
  reviewed concepts while the current catalog contains 22. The HITL proof set
  and current release-identity check are green.
- The repository was already broadly dirty and the active architecture tree is
  untracked, so a meaningful baseline `git diff` is unavailable. The explicit
  changed-file set was reviewed, Python sources compiled, frontend built and a
  trailing-whitespace scan passed. No unrelated user changes were reverted.

Fix rounds used: 2. The first corrected legacy compatibility expectations and
added the new migration to persistence proof. The second stabilized the
time-sensitive external-token fixtures, updated the fail-closed release
identity and fixed decision event/authority edge cases.

No commit, push, release or remote mutation was made.

### Round 61 — Codex build: Phase 1 closed four-role roster freeze

Date: 2026-08-07

- Froze the only production Agent roster as `SleepCareAgent`,
  `EvidenceReasoningAgent`, `CareStrategyAgent` and conditionally invoked
  `SafetyReviewAgent`. `AgentId` is alias-free, the ordered roster is immutable,
  and registry surfaces now fail closed if they contain an extra identity,
  string alias, historic route, additional publisher or Agent-owned side effect.
- Made the outer registry maps immutable and added import-time plus manifest-time
  roster validation. Authorization entry points reject plain strings that are
  value-equal to `str`-backed Enum members; only the explicit `runtime` control
  principal remains a permitted non-Agent caller.
- Updated the radar boundary so `sleepagent.radar_agent.product_agent` is the
  sole production Agent namespace and old Agent/orchestrator/dynamic identity
  packages are not canonical architecture surfaces.
- Synchronized the architecture, positioning, information architecture, HITL,
  README and presentation documents around the closed four-role invariant.
- Deliberately preserved the Product manifest format, registry version and hash,
  existing Episode state machine, `ProductEpisodeRunner`, governance behavior
  and all legacy source files. Legacy removal and capability migration remain
  Phase 2 work; concrete role classes remain Phase 3 work.

### Codex verification

- Roster, registry, Episode runtime, invocation, Tool, governance, deterministic
  model, HITL, cold-start and namespace group: `94 passed in 1.31s`.
- Runner, persistence, Memory governance, Habit Profile, worker, provider,
  acceptance identity, API and authority-boundary group: `182 passed in 6.83s`.
- The Product registry manifest hash remains
  `bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22`;
  the checked-in acceptance identity tests pass without a version or manifest
  update.
- Changed Python files compile, scoped `git diff --check` passes, and the
  authoritative documents contain none of the obsolete fifth/sixth-Agent or
  Dynamic-backend permission phrases.
- The pre-existing dirty worktree was preserved; no unrelated file was reverted
  or folded into the Phase 1 scope.

Fix rounds used: 2. The first closed `str`-Enum equality masking inside roster
registries. The second closed the same alias class at invocation and
collaboration authorization entry points and added negative tests.

No commit, push, release or remote mutation was made.

### Round 62 — Codex build: Phase 2 concrete four-role boundaries

Date: 2026-08-07

- Followed the approved revised phase order: concrete four-role boundaries were
  established before any legacy capability deletion or migration. Added
  `product_agent/agents/` with role-specific modules, typed invocation and
  control Contracts, an exact immutable roster Factory and a fail-closed
  implementation manifest.
- `SleepCareAgent` now owns Communication generation plus typed Episode plan and
  evaluation judgments. `EvidenceReasoningAgent`, `CareStrategyAgent` and
  `SafetyReviewAgent` own their Evidence, accepted-Evidence-to-Care and exact
  hash-bound review inputs respectively. Each role declares its responsibility,
  success/wait/failure conditions, Skill/Tool/collaboration allowlists, Context
  visibility and state/side-effect permissions.
- The shared low-level model invoker is private to each concrete role. A role
  re-resolves its Skill and lock, recompiles the prompt from the authorized
  Context/Profile and compares the complete canonical binding before any model
  call. Prompt, hash, version and cross-role Context tampering therefore fail
  before provider execution.
- `ProductEpisodeRunner._invoke_and_accept` now receives a concrete
  `RuntimeAgentPort`; Agent identity, work-product kind and Skill are derived
  from that port. Production Factory and all readiness gates require an exact
  `ProductAgentRoster`. The old public `runner.invokers` and
  `runner.sleepcare_model` readiness surfaces were removed.
- Kept deterministic acceptance, Tool execution, collaboration routing,
  Safety revision sequencing, HITL, budgets, counters and Episode state
  transitions in Runtime/Policy. `ProductEpisodeRunner` was not split and its
  lifecycle/state machine was not redesigned.
- Added direct unit entry points for all four roles, negative Context/prompt
  boundary tests, exact Factory/configuration tests and concrete-roster end-to-end
  coverage. A fixed Phase 1 Episode audit projection locks invocation ID,
  Agent/Profile/Skill identity, Context/target/prompt hashes and versions.
- Deliberately did not delete or migrate `radar_agent/agents`, `orchestrator` or
  `dynamic`, and did not migrate Trend/Risk/RAG/Report/Memory capabilities.
  The non-production deterministic replay composition may still enter through
  the model-binding compatibility constructor, which immediately materializes
  the same exact concrete roster; it is not a production Factory or alternate
  execution path.

### Codex verification

- Role, Factory, architecture, Episode, Invoker, Tooling, Governance,
  deterministic replay, HITL, Cold Start and namespace group:
  `109 passed in 1.72s`.
- Runner, persistence, Memory governance, Habit Profile, worker, provider,
  acceptance, Product API, authority, SleepDomain bridge and radar run group:
  `200 passed in 8.84s`.
- The fixed Phase 1 audit projection hash remains
  `00329051d0dc6b633b2546a47b7616d8cd57ddc6062cf1898c79f3acf3494bfd`.
- Product Contract version remains `sleepagent-product-agent.v14`; Product
  registry manifest hash remains
  `bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22`;
  release identity remains
  `02d4eff71d5d208288133237e6846da72ba45e984ac081e16544c90e47d344ae`.
- All changed Python sources compile and `git diff --check` passes. Independent
  architecture and Runtime reviews found no remaining high-risk bypass after
  the final readiness and prompt-binding fixes.

Fix rounds used: 2. The first established concrete role delegation and restored
configuration-probe compatibility. The second removed callable/generic bypass
surfaces, bound prompts and control Contexts inside concrete roles, made
readiness exact-roster-only and locked Phase 1 audit identity.

Phase 1 was committed locally as `241d9be`; Phase 2 remains uncommitted. No
push, release or remote mutation was made.

### Round 63 — Codex build: Phase 3A legacy capability migration and parity

Date: 2026-08-07

- Committed the accepted Phase 2 scope locally as `08f20d8` with message
  `refactor(agent): establish concrete four-role agent boundaries`; no unrelated
  dirty-worktree file was included and nothing was pushed.
- Migrated RadarData ownership to a subject/device-bound `RadarDataAdapter` and
  canonical evidence Tool; Trend to `TrendAnalysisTool` plus an Evidence method;
  Risk to `RiskClassificationTool` plus deterministic urgent/risk Policy; RAG to
  a reviewed-only Knowledge Tool/Service; Report to an Artifact Tool plus the
  SleepCare role-material method; AlertCare to a Care coordination Tool/Policy;
  and Memory parity to the existing governed `LongitudinalMemoryService` plus a
  SleepCare memory method.
- Preserved the legacy Trend/Risk semantics that were missing from Product:
  7/30/90-day windows, per-window minimums, latest-revision de-duplication,
  baseline separation and fallback, coverage/quality confidence,
  cohort/calibration isolation, structured multifactor risk and explicit
  uncertain/fail-closed outcomes.
- Added a provider-neutral `CanonicalWorkflowPolicy` describing the valuable
  fixed-orchestrator invariants. Enforcement remains in the existing Runner,
  governance and acceptance layers; `ProductEpisodeRunner` and its lifecycle
  state machine were not split or redesigned.
- Runtime now owns exact Tool result selectors. Successful outputs are bound to
  the FactSnapshot and invocation receipt, so an Agent cannot self-attest a
  Tool result. Role-material artifact basis is prepared once after accepted
  Evidence and before SleepCare; Care coordination is bound to accepted
  Evidence plus post-Evidence Risk receipts and fails before Care on mismatch.
- Doctor-targeted generated material consistently selects the doctor SleepCare
  method, requires `draft_material` authority and passes a registered Safety
  checkpoint. The agentless deterministic data-quality-recovery path remains a
  deliberate exception and preserves the existing three role views.
- The Python classes under `skill_methods/` are typed parity/reference
  implementations, not a second callable Skill runtime. Production continues to
  use the versioned `SkillPackage`/prompt registry: Trend and role-material IDs
  are selected directly, Care uses `propose_single_care_action`, and Memory
  remains on the existing governed SleepCare/LongitudinalMemory path.
- Legacy Agent packages remain present only as migration sources and test
  oracles for Phase 3A. API/CLI/worker/backend runtime cutover, bounded Tool
  selector cleanup, scalar/content-hash compatibility removal, golden fixture
  conversion and physical legacy deletion are explicitly deferred to Phase
  3B/3C.

### Codex verification

- Per-capability characterization/parity and boundary suite:
  `128 passed in 1.84s`.
- Four-role Product acceptance, architecture, runtime, Factory, governance,
  invocation, persistence, provider, role, Runner and Tooling suite:
  `171 passed in 4.02s`.
- Product worker, SleepDomain bridge and PostgreSQL integration boundary:
  `18 passed, 2 skipped in 1.61s`.
- Full repository regression after all fixes:
  `1086 passed, 5 skipped in 59.01s`.
- The Product Contract remains `sleepagent-product-agent.v14`; Product registry
  manifest hash remains
  `bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22`.
  Phase 1 manifest and concrete-roster identity tests pass unchanged.
- Independent boundary and adversarial reviews found no Phase 3A blocker.
  Scoped `git diff --check` and legacy-identity boundary tests pass.

Fix rounds used: 4. Reviews tightened Tool-result binding and Artifact truth,
closed explicit and implicit doctor/Safety bypasses, moved Care coordination to
accepted Evidence plus exact Risk receipts, and restored deterministic doctor
views for data-quality recovery without widening generated-material permissions.

Phase 3A remains uncommitted. No push, release or remote mutation was made, and
execution is paused before Phase 3B as required.

### Round 64 — Codex build: Phase 3B canonical Runtime cutover

Date: 2026-08-09

- Committed the accepted Phase 3A scope locally as `2400c94` with message
  `refactor(agent): migrate legacy capabilities to canonical boundaries`; no
  unrelated dirty-worktree file was included and nothing was pushed.
- Made `product_episode/product-episode.v1` the only creatable Agent Runtime at
  both API Contract and `TaskService` boundaries. API run, chat, user-input,
  confirmation and revocation paths now fail closed for historical Runtime
  kinds; legacy/dynamic task rows, events, traces and receipts remain readable.
- Removed API and CLI Runtime selectors. CLI demo/goal/retry/resume paths now
  delegate through `RadarApiRuntime` to `ProductEpisodeRunner`; historical
  retry, rerun and answer operations are rejected rather than converted.
- Removed Dynamic worker construction, wake-up and application lifecycle hooks,
  and retired both the Dynamic acceptance console entry and module execution.
  `backend.main` now has one modular composition root with no legacy switch;
  non-Agent health/status surfaces remain available.
- Replaced the `product_device` Dashboard Agent construction with the
  deterministic `RadarDashboardProjectionTool`. The package root and API no
  longer import, instantiate or export old Product-device Agent identities.
- Added static anti-backflow tests: canonical Product code cannot import legacy
  Agent/Orchestrator/Dynamic packages, and `skill_methods/` cannot be imported by
  production Runtime, Agent or Registry code. No production composition surface
  constructs an old worker, fixed Orchestrator or Product-device Agent.
- Closed the Phase 3A tails: live Trend inputs are structured night summaries
  with exact FactSnapshot source refs; untyped/content-hash-only Artifact input
  fails closed; transient Tool outputs are Episode-scoped, lock-protected and
  released in `finally` after terminal Episode persistence.
- Kept migrations, historical tables and old implementation directories intact.
  `TaskService.execute` remains solely as a characterization oracle with zero
  production callers, and persistence still imports historical Dynamic DTO
  decoders. Both are explicit Phase 3C deletion/migration items.

### Codex verification

- Phase 3A parity, Product Tool/Runner and static boundary group:
  `204 passed in 4.94s`.
- API, CLI, backend, product-device, worker and acceptance cutover group:
  `97 passed in 11.60s`.
- Direct canonical creation, composition and four-role manifest/Factory group:
  `70 passed in 9.50s`.
- Full repository regression after the final fixes:
  `1094 passed, 5 skipped in 55.20s`.
- All Python sources under `sleepagent` and `backend` compile, `git diff --check`
  passes, and independent entry-reachability plus boundary reviews found no
  Phase 3B blocker.
- Product Contract remains `sleepagent-product-agent.v14`; the exact four-role
  manifest hash remains
  `bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22`.

Fix rounds used: 2. The first cut over all executable composition surfaces and
closed Tool-cache/content-binding tails. The second aligned characterization
fixtures with Episode binding, added direct `TaskService` rejection coverage,
and removed stale compatibility naming/documentation.

Phase 3B remains uncommitted. No push, release, database rewrite, migration
deletion or Phase 3C source deletion was performed; execution is paused for
Phase 3B acceptance.

### Round 65 — Codex build: Phase 3C physical legacy removal

Date: 2026-08-09

- Committed the accepted Phase 3B scope locally as `ae3e0e2` with message
  `refactor(agent): cut over to canonical product runtime`; no unrelated
  dirty-worktree file was included and nothing was pushed.
- Replaced the remaining Phase 3A execution-based characterization oracles with
  checked-in JSON golden fixtures. Parity tests now exercise only canonical
  Tool, Service, Skill-method and Policy boundaries and do not import,
  instantiate or execute a retired Agent, Orchestrator or Dynamic Runtime.
- Added `persistence.history` as an opaque, immutable and SELECT-only decoder
  for historical tasks, events, artifacts, confirmations, collaboration and
  conflict records, plans, invocations, budgets and receipts. Historical
  API/CLI detail and trace reads remain supported, while task creation,
  execution, retry, rerun, input, confirmation, resume and replanning fail
  closed before a persistence mutation.
- Physically removed the retired `agents`, `orchestrator`, `dynamic`, `a2a`,
  `prompts`, `skills`, `tools`, `evidence` and `memory` packages. Also removed
  the old product-device Agent modules and all tests that existed solely to
  execute the retired identities. No importable archive was created; applied
  migrations and historical database tables were left intact.
- Removed old A2A/roster DTO exports, the legacy micro-questionnaire path,
  `TaskService.execute`, stale Runtime branches and LangGraph from the package
  dependency surface. Canonical reviewed knowledge, report rendering,
  confirmation, replay, provider, governance and longitudinal memory
  capabilities remain in their final boundaries.
- A clean wheel was built and inspected. It contains none of the retired
  packages or product-device Agent modules, exposes no retired console entry,
  contains none of the forbidden identities/runtime kinds in production Python,
  and has no LangGraph dependency. Generated build artifacts and stale caches
  were moved recoverably to `/tmp` after inspection.
- `ProductEpisodeRunner`, its lifecycle/state machine, the concrete Agent files,
  typed ports and four-role manifest were not modified in this phase.

### Codex verification

- Frozen Phase 3A capability/golden suite: `128 passed in 1.41s`.
- Canonical static boundary, physical deletion, history compatibility and
  four-role Factory/architecture suite: `51 passed in 3.66s`.
- Full repository regression: `943 passed, 5 skipped in 48.33s`.
- `python -m compileall -q sleepagent backend reference_client tests` and
  `git diff --check` pass. Production static scans across every packaged Python
  root find no retired identity, runtime kind, import, `TaskService.execute`
  caller or old package path.
- Product Contract remains `sleepagent-product-agent.v14`; the exact four-role
  manifest remains `sleep_care`, `evidence_reasoning`, `care_strategy`,
  `safety_review`, with hash
  `bc5879c7c10636f5df02cc7132e99edd3a200e48f98488c64e9ca52a0d60fc22`.

Fix rounds used: 3. The first isolated historical decoding and removed the dead
execution graph. The second updated one governance test that still opened a
physically deleted legacy Tool file. The independent final review then restored
opaque read compatibility for historical artifact, confirmation, collaboration
and conflict trace records and expanded package-root anti-backflow coverage
before the full regression and package proof were repeated.

Phase 3C remains uncommitted. No push, release, database rewrite or migration
deletion was performed, unrelated dirty-worktree changes remain untouched, and
execution is paused before any `ProductEpisodeRunner` decomposition.
