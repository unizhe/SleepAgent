# Plan Review Log: SleepAgent 渐进式睡眠习惯画像
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **长期确认角色与现有代码冲突。** 现行 confirmation matrix 将 `write_long_term_memory` 设为 family-only，而计划要求老人确认自己的画像。**Fix:** 明确新增 elder-owned Profile action scope，迁移前 fail closed，不能沿用旧规则。
2. **“一次整体确认”无法由当前单 candidate token 表达。** 循环提交 2–3 个单候选会出现部分成功和确认重放。**Fix:** 增加带全集 manifest hash 的原子 `HabitProfileChangeSet`，一次确认、一次性消费、全成或全败。
3. **当前 Memory 只保存自由文本摘要。** `MemoryItem.value_summary` 无法承载 concept、typed value、来源、窗口、有效期和冲突状态，计划可能最终退化为不可审计文本。**Fix:** 要求 typed Profile fact 是权威存储，摘要只能派生。
4. **“LLM 不改变语义地改写”无法确定性验证。** 现有 tone rewriter 只限制长度，仍可产生语义漂移。**Fix:** 正式采集只允许审核 variant 或固定 wrapper；自由改写必须重新用审核语义确认。
5. **安全路由只描述问题级触发，答案本身可能暴露急症。** 如果等到一轮结束或长期确认才处理，会延迟安全抢占。**Fix:** 每个回答 capture 后立即执行 response-level deterministic scan。
6. **画像读取权限仍过于抽象。** 家属参与回答可能被误解为持续读取授权，医生材料也可能获得完整画像。**Fix:** 锁定老人、家属、医生的最小读取原则，并明确来源身份不授予读取权。
7. **“复用 Memory Service”存在双轨歧义。** 仓库同时有 legacy family-only writer 和目标 `product_agent` Memory；实现者可能选错权威或双写。**Fix:** 明确只扩展目标 `product_agent`，旧路径只读迁移且不自动升级。
8. **计划只验证交互负担，没有验证画像是否正确影响个性化。** 系统可能读取无关或过期字段却仍通过 UI 测试。**Fix:** 增加有/无画像配对回放，检查相关性、时效、权限和因果边界。
9. **“遗忘”可能被用户理解为删除所有记录。** 既有架构要求部分审计历史保留。**Fix:** 验收中要求准确说明个性化失效与审计保留边界。

VERDICT: REVISE

### Codex response

Accepted all nine findings. The plan now makes Profile persistence elder-owned, introduces an atomic manifest-bound change set, requires typed facts, constrains wording adaptation, scans every answer for safety, fixes per-role access semantics, selects the target `product_agent` Memory as the sole authority, adds paired personalization replay, and clarifies forgetting versus retained audit history. No finding was rejected.

## Round 2 — Codex review

1. **每轮最多三题仍可被多次 selection/replan 绕过。** 一个 Episode 可以连续调用工具并反复盘问。**Fix:** 将上限提升为 Episode 共享预算，绑定 plan step 和 selection receipt。
2. **unknown/拒答被混在 Profile fact 状态中。** 长期保存“拒绝回答”可能泄露隐私意图并被误用为 Evidence。**Fix:** unknown 默认不成 fact；明确“不再问”只形成最小化 QuestionSuppression。
3. **字段过期没有确定性状态转换规则。** 若依赖定时写或模型判断，过期信息可能继续被使用。**Fix:** read-time 计算 effective stale，重新确认才追加 replace/supersede。
4. **冲突定义忽略时间窗。** “过去午睡、现在不午睡”可能被错误标成来源冲突。**Fix:** 只比较重叠 observation window/日型，区分真实演进。
5. **短文本会成为持久化 Prompt 注入载体。** 画像长期进入 Context 后，恶意或偶然指令文本可能改写 Agent 行为。**Fix:** 长度限制、规范化和永久 `user_data` trust label，禁止执行语义。
6. **整体确认可能捆绑敏感字段。** 全成或全败若没有逐项移除能力，会形成被迫同意。**Fix:** 确认前允许删项，重建 manifest 和摘要后再确认。
7. **安全答案“不写画像”可能导致证据丢失。** 抢占流程仍需要原回答及来源。**Fix:** 保存最小 Safety/Evidence 事件，但禁止生成普通 Habit fact。
8. **睡眠时间缺少跨午夜与时区语义。** 旅行或夏令时会让 00:30 属于哪一“晚”不明确。**Fix:** fact/concept 明确 timezone、sleep-day 和适用日型，无法统一时输出 unknown。

VERDICT: REVISE

### Codex response

Accepted all eight findings. The plan now has an Episode-wide question budget and receipt binding, separates nonanswers from facts, computes staleness at read time, resolves conflicts only across comparable windows, hardens persisted text against prompt injection, supports deselection before grouped consent, preserves safety evidence outside the profile, and defines timezone/sleep-day semantics. No finding was rejected.

## Round 3 — Codex review

1. **计划仍暗示所有问卷答案都可能进入画像。** 现有设备位置、单夜离床和数据质量问题没有长期个性化价值。**Fix:** 给 concept 增加默认 `episode_only`/显式 `profile_eligible` 资格，重复出现也不自动升级。
2. **家属的否定观察会被过度解释。** “没看到打鼾”在不与老人同室时不能证明没有打鼾。**Fix:** family negative 需要 observation opportunity、窗口和把握程度，否则只能 unknown。
3. **Concept 升级缺少历史语义规则。** 改选项或安全阈值后，旧 facts 可能被新代码静默重解释。**Fix:** 不可变版本、major 兼容规则和显式等价迁移；否则 stale/reconfirm。
4. **按“当前决策”选题可能制造确认偏差。** 模型可以只问支持首选解释的问题。**Fix:** SelectionRequest 保留替代解释、禁止期望答案，Tool 优先选择区分性中性问题并做配对测试。
5. **现有 runtime 尚未完成四 Agent 收口。** 直接实现本计划可能在 legacy Dialogue/Memory 上形成又一套过渡画像，违背原地迁移原则。**Fix:** 增加 Phase A/B/C 依赖门；Foundation 可先行，但生产接入等待目标 roster/唯一 Memory，禁止双写。
6. **“查看画像”触发器可能变成查看即盘问。** 用户只想看已有信息时不应被迫补充。**Fix:** 先展示，只有明确更新或真实决策缺口才提问。

VERDICT: REVISE

### Codex response

Accepted all six findings. The plan now separates episode-only questions from profile-eligible concepts, qualifies family negative observations, versions concept semantics safely, reduces confirmation bias, stages delivery behind the four-Agent migration, and makes profile viewing non-interrogative. No finding was rejected.

## Round 4 — Codex review

1. **计划要求区分家属观察，但当前 Evidence 枚举没有该语义。** 现有 `USER_REPORTED` 会把家属观察和老人自述混在一起，`OBSERVED_FACT` 又会错误提升为客观事实。**Fix:** 新增独立 observer semantic/source 和 acceptance mapping；未完成前关闭家属观察路径。
2. **老人确认持久化可能洗掉原始来源。** 当前 confirmed Memory 作为新 source kind 读取，容易让家属观察变成“已确认事实”。**Fix:** 持久化和回读始终携带 origin semantic/actor/window/opportunity，确认只授权存储。
3. **拒绝长期保存可能误删安全证据。** Profile 同意与当前 Episode/Safety 的合法保留目的不同。**Fix:** 拒绝只阻止 Profile commit，不删除当前 observer Evidence 或 Safety event。
4. **“明确禁忌”重新混入临床数据。** 过敏、疾病和医学禁忌不属于睡眠习惯，与先前范围决定冲突。**Fix:** 画像只存非临床行动约束，医学禁忌进入 Clinical/Care Safety Context。

VERDICT: REVISE

### Codex response

Accepted all four findings. The plan now gives observer reports an independent evidence semantic, preserves their origin through confirmed Memory, separates persistence refusal from safety evidence retention, and removes medical contraindications from the habit profile. No finding was rejected.

## Round 5 — Codex review

1. The four context classes are now disjoint enough to prevent habit, objective baseline, recent report and clinical safety data from silently substituting for one another.
2. Collection is bounded by reviewed concepts, Episode budget, decision relevance, neutral selection, receipt binding and explicit persistence eligibility.
3. Long-term state has a single target authority, typed provenance, elder-owned atomic consent, deterministic staleness, conflict/version semantics and reversible personalization use.
4. Family observations retain a distinct non-canonical Evidence semantic, require observation opportunity for negative reports and cannot replace elder subjective experience.
5. Delivery is staged behind the locked four-Agent migration, so the plan does not require a parallel legacy implementation.
6. Remaining items—reviewer identity, concept wording/TTL, baseline maturity thresholds and proxy governance—are explicitly identified pre-production gates rather than hidden contradictions in the architecture.

VERDICT: APPROVED

## Act 3 — Build

### Round 1 — Codex build

Implemented the reviewed Habit concept catalog and progressive Questionnaire
contracts, including plan-step and decision-gap binding, reviewed role variants,
Episode-wide three-question budget, cooldown/suppression handling, strict answer
validation, capture receipts, short-text normalization and response-level safety
events. Added typed `ObjectiveBaselineArtifact`, `HabitProfileFact`,
`HabitProfileChangeSet`, elder-bound confirmation, deterministic stale/conflict/
forget semantics and the single Product Agent Profile store. Extended Evidence
with independent observer semantics and source kinds.

Integrated the capability into the existing four-Agent runner: Profile read and
Questionnaire capture enter only the minimum authorized Context, Care sees
accepted Evidence rather than raw Profile data, valid answers produce a pending
atomic change set, and `state.commit_habit_profile` remains a deterministic
Commit Controller operation. No Agent identity or legacy writer was added.

Initial focused proof: 120 relevant architecture, governance, Questionnaire,
runner and Habit Profile tests passed.

### Round 2 — Codex build fix pass

Self-review found two cross-Episode issues:

1. A family-authored answer originally produced a change set bound to the
   family FactSnapshot, which could never satisfy the elder-only commit rule.
   Capture now remains current observer Evidence; an authenticated elder review
   later builds the change set from the verified, unexpired captured answer.
2. Response-level safety capture originally waited for a new SleepCare planning
   call. Capture/risk scanning now runs as a deterministic preflight, so a safety
   event stops the path before any new model call.

The pass also made concept definitions immutable, made Questionnaire budget and
capture state thread-safe, blocked Habit dual-write through generic free-text
Memory, tightened mutation target/version integrity, preserved pseudonymous
source/opportunity metadata on Profile reads, and added the fail-closed
3–5-participant usability release gate.

### Codex verification

- Frozen spec remained unchanged.
- Four-Agent roster remains exactly SleepCare, EvidenceReasoning, CareStrategy
  and conditional SafetyReview; Habit is implemented as existing
  Questionnaire/Profile/Tool/Commit capabilities.
- All 28 implementation acceptance criteria are mapped in
  `docs/product/habit-profile/ACCEPTANCE-MATRIX.md`.
- Final proof: `PYTHONDONTWRITEBYTECODE=1 pytest -q` → `428 passed in 9.53s`.
- `python -m py_compile` passed for every modified Questionnaire/Product Agent
  module. Ruff was not available in the environment, so no Ruff result is
  claimed.
- Release remains intentionally fail-closed: no real 3–5-participant
  target-age usability report has been conducted or fabricated. The protocol is
  in `docs/product/habit-profile/USABILITY-TEST-PROTOCOL.md`, and the checked-in
  acceptance manifest keeps `habit_usability_report: null`.

Deviations/open production gates retained from the approved plan: real user
testing, named domain/medical reviewer sign-off for wording/TTL, and
metric-specific ObjectiveBaseline maturity thresholds require external evidence
and were not invented during implementation.

### Post-build completion audit

A follow-up audit found that Phase C had only internal runner contracts and no
actual product mount. The implementation now adds an authenticated,
fail-closed Habit Profile application/API over the same Questionnaire, typed
Profile store and Commit Controller used by the four-Agent runtime. It exposes
optional intake, current answer capture, exact pending manifests, elder
confirmation, candidate pruning, Profile read/correction/forget and later elder
review of observer reports without creating another Agent.

The Next.js product now has a discoverable `/habit-profile` elder workspace.
It does not start intake on load, separates current use from long-term
persistence, shows exact candidates and source semantics, allows removal before
confirmation, and exposes correction/forget controls with an accurate audit
retention notice. Its BFF keeps API credentials and actor/role/subject binding
server-side and returns 503 when the controlled identity is absent. A fixed
environment binding is documented as single-subject usability infrastructure,
not production multi-user authentication.

Additional verification:

- Habit API, frontend contract, acceptance, domain and runner focus suite:
  57 passed.
- Full Python suite: 439 passed.
- Frontend TypeScript check and optimized Next.js production build: passed;
  `/habit-profile` was emitted successfully.
- Acceptance release identity now includes
  `sleepagent-habit-application.v1`; the checked-in manifest matches exactly
  and retains `habit_usability_report: null`.

The only implementation-acceptance activity that cannot be completed inside
the repository is the real 3–5 participant target-age usability session.
No participant result has been fabricated; default proactive intake and the
release gate remain off/fail-closed pending that external evidence.

### Simulated acceptance material hardening

The supplied `sleepagent_simulated_acceptance_materials` package was audited as
evidence rather than copied into the production manifest. It contains five
simulated usability observations, a simulated 12-concept domain review and 68
rows labeled as real-provider observations. The package is useful for pipeline
rehearsal, but it does not bind the current catalog, scenario enum, release
identity or provider invocation receipts.

The audit prompted a fail-closed acceptance v12 update:

- evidence now carries explicit `real | simulated` provenance;
- simulation markers cannot be promoted by changing one field;
- real provider claims require sanitized provider/model/request/invocation
  receipt bindings with unique receipt hashes;
- Episode results retain typed Agent invocation records so receipts can be
  derived from the actual run;
- every observation must bind the exact release identity;
- domain approval has a dedicated reviewer/scope/per-concept/signoff Schema and
  recomputes the current catalog hash;
- the checked-in manifest keeps domain review and usability reports null until
  real evidence exists;
- `acceptance_materials` produces a machine-readable audit without rewriting
  supplied evidence.

Verification after hardening: 448 Python tests passed; frontend production
build and the subsequent standalone TypeScript check passed. The supplied
package returns `usable_as_simulation_fixture: true` and
`release_evidence_eligible: false`, with no hard architecture violations.

Acceptance v13 additionally requires every real usability report to carry a
facilitator attestation bound to exactly the same 3–5 participant refs. The
attestation declares that the interactions were observed, no synthetic data
was used, and records a signature reference. This prevents the supplied
simulation report from becoming release evidence merely by changing
`evidence_kind`.

Acceptance v14 binds a new
`sleepagent-product-structured-provider.v1` adapter into the release identity.
The adapter makes the existing OpenAI-compatible product provider implement the
four-Agent `StructuredAgentModel` contract, adds the exact output Schema and
ContextPacket identity to each request, validates strict JSON without unknown
fields, and preserves the raw provider response ID. Missing response IDs are
not invented, so such runs remain ineligible as real-provider receipts.

### Round 3 — Codex build: real-evidence collection templates

The supplied simulated package exposed a practical handoff problem: manually
copying its stale 12-concept/legacy-scenario structure would keep producing
catalog, scenario and release-identity drift. The acceptance material CLI now
has `--initialize-templates`, which derives a collection package from the
current 10-concept catalog, all 24 current scenarios, their required 68
repetition slots and the checked-in release identity.

The initializer is deliberately fail-closed. Every placeholder is marked
simulated, contains an explicit `template` identifier, has no provider receipt
and has `passed: false`. The real-evidence validators now treat `template` as a
simulation marker, so changing only `evidence_kind` cannot promote a
placeholder. File creation uses exclusive-create semantics and preflights all
canonical targets, so existing user material is not overwritten. Acceptance
was advanced to v15 and the checked-in fail-closed manifest was rebound to the
new identity.

One fix pass was used: the first focused test showed that the prior simulation
marker list covered `simulated`, `synthetic` and `fixture`, but not `template`.
The marker list and release identity were corrected before full verification.

### Codex verification

- Focused acceptance suite:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_product_agent_acceptance.py`
  → `10 passed`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `451 passed in 10.02s`.
- `python -m py_compile` passed for the acceptance contracts, material CLI and
  acceptance tests; `git diff --check` passed.
- CLI smoke generation produced the current catalog hash, 10 concept review
  rows and 68 observation slots. Re-audit reported no catalog, scenario or
  release-identity drift and remained ineligible as intended.
- The original supplied simulated package still exits `2`, remains usable as a
  simulation fixture and cannot unlock the release.
- The checked-in manifest equals the current v15 release identity, has zero hard
  violations and remains ineligible because real scenario runs, named review
  signoff and the 3–5 participant study are absent.

No 24-scenario executor was fabricated: the repository currently has scenario
labels and test-only model doubles, but no authoritative scenario execution
registry capable of proving each architecture behavior against a real
provider. The initializer therefore creates collection slots only. Real
provider runs, named domain/medical signoff and observed 60+ participant
sessions remain external release activities.

### Round 4 — Codex build: v15 complete simulated archive

The user supplied
`sleepagent-v15-complete-simulated-evidence.zip` (SHA-256
`72281080cc693c694e956ec80443327d0f2ecb3d53dcef105699db5429a4383c`).
The archive has one bounded root and six regular members. Its three canonical
materials bind the current v15 release identity, all 10 current Habit concepts,
all 24 acceptance scenarios and 68 required repetition slots.

Initial audit exposed two fixture-contract mismatches rather than production
evidence:

1. The usability attestation truthfully declared
   `synthetic_data_used: true`, while the old field type unconditionally
   required false.
2. Sixty-six simulated provider rows carried unique synthetic receipt shapes
   while correctly keeping `real_provider: false`; the old validator required
   receipt presence to equal the real-provider claim.

The contracts now permit those two honest simulated forms but retain the real
gate: a real usability report still rejects synthetic data, a simulated
observation still cannot claim a real provider, and a real deterministic
observation cannot carry a provider receipt. Because production real-evidence
eligibility did not change, the acceptance/release identity remains v15.

The material CLI now accepts ZIP input through a bounded reader. It rejects
path traversal, absolute/backslash paths, symlinks, encryption, excessive file
counts/sizes, ambiguous canonical files and materials split across archive
directories. Only the three exact canonical files are copied into a temporary
audit directory; preview manifests and self-reported validation summaries are
not trusted as release evidence.

One fix pass was used: the unsafe-path regression correctly emitted
`archive.invalid`, but `usable_as_simulation_fixture` initially remained true.
Archive and material Schema failures now make that summary flag false, while
intentional catalog/scenario drift fixtures remain usable negative tests.

### Codex verification

- Focused acceptance suite:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_product_agent_acceptance.py`
  → `13 passed`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `454 passed in 9.80s`.
- `python -m py_compile` passed for the acceptance contracts, archive/material
  CLI and acceptance tests; `git diff --check` passed.
- Direct bounded ZIP audit exits `2` with `5` usability observations, `10`
  reviewed concepts and `68` observation rows. It reports exactly
  `usability.simulated`, `domain.simulated` and `release.gate_failed`; there is
  no catalog, scenario, identity or receipt drift.
- The old supplied directory remains a separate negative drift fixture.
- The official manifest still equals the current v15 identity, contains no
  hard violations and remains fail-closed with all 24 real scenarios absent.

The archive is therefore accepted as a complete development-time simulation
fixture, not as real acceptance evidence. Its named people, interactions,
signatures, provider executions and request IDs are explicitly simulated and
were not copied into the official release manifest.

### Round 5 — Codex build: durable confirmed Profile authority

A renewed completion audit found that the authenticated product API still
constructed a module-level `InMemoryHabitProfileStore`. The typed contracts and
Commit Controller were correct, but confirmed facts, audit receipts,
idempotency bindings and consumed confirmation IDs disappeared on process
restart. That contradicted the PLAN's long-term Profile and single Memory
authority requirements.

The implementation now adds migration `003_habit_profile` to the existing
Radar PostgreSQL/SQLite migration chain. It stores one typed
`HabitProfileState` document per subject and one immutable commit row per
idempotency key, including the exact payload hash, confirmation ID, Profile
receipt and commit time. `PersistentHabitProfileStore` reuses the same
candidate validation and atomic application rules as the in-memory reference
implementation and writes state plus commit audit in one transaction.

Production `/product/habit-profile` now builds this store from the existing
`SLEEPAGENT_RADAR_AGENT_DATABASE_URL` or
`SLEEPAGENT_RADAR_AGENT_SQLITE_PATH`; there is no second Profile database or
dual-write path. The same store instance is injected into
`HabitProfileRuntimeService` and `DeterministicCommitController`. Tests retain
an explicit in-memory reset path.

Idempotency was tightened while making it durable. The binding hash now covers
the complete change set and exact confirmation rather than the change set
alone. An exact replay returns the original Profile receipt even after the
confirmation window has elapsed, while a changed confirmation under the same
key collides and a consumed confirmation under another key is rejected.
Database reads cross-check the indexed version with the typed JSON, recompute
every fact hash and verify replay receipts against both their row identity and
the state's audit sequence.

First-write concurrency is deterministic across processes: the transaction
creates a version-zero state with `ON CONFLICT DO NOTHING`, then locks that row
(`FOR UPDATE` on PostgreSQL; `BEGIN IMMEDIATE` on SQLite) before checking
idempotency, confirmation consumption and expected Memory version. State
updates use a version-qualified CAS. Unconfirmed pending change sets remain
ephemeral by design; after restart they must be summarized and confirmed
again, so no unconfirmed answers become long-term state.

Habit Profile advanced to v2, Habit Application to v2 and the new persistence
contract is `sleepagent-habit-persistence.v1`. Acceptance advanced to v16 and
the release identity now binds all three. The user-supplied v15 ZIP remains a
bounded historical simulation fixture and correctly reports release-identity
drift against v16.

Two fix passes were used:

1. Rebound the checked-in manifest and historical ZIP expectation after the
   initial focused suite correctly rejected the missing persistence identity.
2. Added indexed-version/receipt integrity checks and a first-write state row
   lock after database concurrency self-review.

### Codex verification

- Persistence/Profile/API/migration/acceptance focused suite:
  `53 passed in 1.45s`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `461 passed in 10.29s`.
- A real SQLite-file API test commits a confirmed Profile, closes the first
  connection, rebuilds the application with a second connection and reads the
  same typed fact and Memory version.
- Store tests cover restart recovery, audit preservation, exact replay after
  expiry, changed-confirmation collision, confirmation reuse, cross-connection
  stale CAS and persisted index/receipt corruption.
- The additive migration suite proves both new tables and all three migration
  ledger versions on SQLite. PostgreSQL SQL is generated through the existing
  migration path; no live PostgreSQL service was available or claimed.
- Explicit `py_compile`, frontend `npm run typecheck` and
  `git diff --check` passed.
- The checked-in manifest matches current v16 identity and remains fail-closed
  pending real release evidence.

No Agent roster, Questionnaire semantics, Profile confirmation ownership or
Commit Controller boundary was changed. No commit, push or remote mutation was
performed.

### Round 6 — Codex build: durable confirmed question suppression

A final PLAN-by-PLAN audit found one remaining long-term-state gap. Explicit
“以后不要再问” answers produced the correct minimal `QuestionSuppression` and
required a separate confirmation reference, but the suppression lived only in
the Questionnaire service's process-local dictionary. Restarting production
therefore restored confirmed Profile facts while silently forgetting the
elder's confirmed question preference.

Migration `004_habit_question_suppression` now adds one minimal row per
subject/concept/scope to the existing Radar database. It stores only the
suppression identity, subject, concept, fixed scope, confirmation reference,
expiry, typed JSON and update time; it stores no answer value and cannot become
Evidence or a Profile fact. Production injects a
`PersistentQuestionSuppressionStore` into the existing Questionnaire
capability using the exact same `RadarPersistenceStore` connection as the
Profile authority. Ordinary skip, cooldown, selection receipts, Episode
budgets and unconfirmed answers remain bounded process/Episode state.

Suppression writes are staged until all submitted answers validate, then saved
as one database transaction before the selection is consumed. A malformed
later answer therefore cannot leave a partial long-term preference. Reads
cross-check subject, concept, scope, confirmation reference and expiry columns
against the typed JSON and ignore expired suppressions deterministically.
Shared Profile and suppression operations use the persistence facade's common
connection transaction lock.

Questionnaire advanced to v2, persistence to v2, Habit Application to v3 and
Acceptance to v17. The current release identity is
`d26d4e6c38669da4fc4a6bae2594c218ac56073c9409fe455e820d42d7c26030`.
The v15 user-supplied ZIP remains an honest historical simulation fixture and
cannot unlock the v17 release gate.

One fix pass was used to rebind the checked-in fail-closed manifest after the
focused acceptance test correctly detected the v16 identity.

### Codex verification

- Focused Habit/Profile/API/migration/acceptance suite:
  `56 passed in 1.55s`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `464 passed in 10.71s`.
- A real SQLite-file API test captures confirmed `never_ask`, closes the first
  connection, rebuilds the application with a second connection and proves the
  concept remains suppressed.
- Store tests prove expiry filtering and fail-closed indexed-column/typed-JSON
  corruption detection; capture tests prove failed multi-answer validation
  leaves no partial suppression.
- The additive migration suite proves the suppression table and all four
  migration ledger versions on SQLite. PostgreSQL SQL is generated through the
  existing migration path; no live PostgreSQL service was available or
  claimed.

No fifth Agent, parallel questionnaire framework, answer-value persistence or
Profile write bypass was introduced. No commit, push or remote mutation was
performed.

### Round 7 — Codex build: durable Questionnaire issuance state

The completion audit found that Episode question counts and cross-Episode
cooldowns were still process-local. A production restart could therefore
reset a three-question Episode budget and forget recently asked concepts even
though confirmed Profile and suppression state survived.

Migration `005_habit_questionnaire_state` adds bounded Episode counters,
selection receipts with consumed state, and actor-scoped cooldown timestamps
to the existing Radar database. The Questionnaire capability now uses one
state-store protocol for issue, lookup, atomic capture finalization and
confirmed suppression. Production injects the persistent implementation;
tests retain an in-memory implementation. Selection IDs are unique and
database issuance uses a locked count plus CAS, so cross-process stale
issuance cannot exceed the Episode budget. Capture consumes the receipt and
writes any confirmed suppression in one transaction.

Two self-review fix passes were used:

1. Changed cooldown identity from role-level to the PLAN-required
   subject/actor/role/trigger/concept key.
2. Removed externally supplied `habit_suppressions` from
   `ProductEpisodeRunRequest` and the selection Tool input so suppression can
   only enter through authoritative answer capture.

### Codex verification

- Focused Habit/Profile/API/persistence/runner/acceptance tests reached
  `83 passed` before the planned release-identity rebind.
- The first full suite exposed one localized regression:
  `QuestionnaireService.select()` accidentally gained a required `actor_id`
  parameter when the actor-scoped Habit protocol patch matched the legacy
  service signature. Result: `431 passed, 38 failed`.
- The Habit-specific persistent state tests themselves pass, including
  restart budget enforcement, cross-Episode cooldown, selection continuation
  after restart, consumed-receipt replay rejection and cross-connection CAS.

Per `MAX_FIX_ROUNDS=2`, the accidental legacy signature change remains for the
next build round rather than being hidden behind an unbounded third pass.
Round 7 is not signed off, and no green full-suite claim is made.

### Round 8 — Codex build: Questionnaire state verification and sign-off

The accidental Round 7 regression was isolated to one line: the actor-scoped
Habit cooldown edit had added `actor_id` to the unrelated legacy
`QuestionnaireService.select()` signature. Removing that parameter restored
all existing radar questionnaire, orchestration, CLI, LangGraph and task API
callers without weakening the new Habit state-store contract.

Self-review then identified a cross-Episode concurrency edge. Two processes
could compute the same concept as eligible before either created its cooldown
row. `issue_selection` now receives the exact catalog cooldown for every
issued concept and performs an atomic conditional cooldown upsert inside the
same transaction as the Episode count CAS and selection receipt. A stale
second Episode rolls back completely instead of asking a duplicate question.
The cooldown key is subject/actor/role/trigger/concept as required by the
PLAN. External request-supplied suppressions remain removed.

The finalized release contracts are Questionnaire v3, persistence v3, Habit
Application v4, Product Runner v33 and Acceptance v18. Current release identity:
`a9e7408883759e8d914c2f1fc37e8525659d09d4c017cd8f42815de04c32fe2b`.

One fix pass was used after the inherited signature correction: adding the
transactional cooldown recheck and its stale-two-Episode regression.

### Codex verification

- Legacy/Habit/runner/acceptance recovery suite: `99 passed in 2.28s`.
- Post-concurrency focused suite: `92 passed in 1.95s`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `470 passed in 10.83s`.
- Restart tests prove the Episode budget, cooldown, selection lookup,
  selection consumption and confirmed suppression survive reopening a real
  SQLite file.
- Cross-connection tests prove stale Episode-count issuance and stale
  cross-Episode cooldown issuance both fail without partial state.
- PostgreSQL statements use the existing additive migration and row-lock
  path; no live PostgreSQL service was available or claimed.

No extra Agent, caller-provided suppression, parallel questionnaire runtime,
answer-value persistence or alternate Profile writer was introduced. No
commit, push or remote mutation was performed.

### Round 9 — Codex build: production assembly audit and v18 collection handoff

The production assembly audit searched all non-test `ProductEpisodeRunner`,
`HabitProfileRuntimeService`, `HabitProfileApplicationService` and Commit
Controller construction sites. No backend path constructs a default in-memory
`ProductEpisodeRunner`. The authenticated Habit product application injects
the Profile store and unified Questionnaire state store from the same
`RadarPersistenceStore`, and its Commit Controller shares that Profile
authority. In-memory implementations remain explicit reference/test defaults,
not a second production writer.

No real provider configuration is present in the current process environment,
so producing real model request IDs or receipts would require external
credentials and execution. To make that handoff concrete, the bounded
initializer generated
`docs/product/habit-profile/real-evidence-v18-collection` with:

- the current release identity
  `a9e7408883759e8d914c2f1fc37e8525659d09d4c017cd8f42815de04c32fe2b`;
- the current 10-concept catalog hash
  `a75aa424e3260f042b65be82dbd45600d370515b073d8d17f31e69c539f44ae1`;
- three target-age usability observation slots, all ten exact concept review
  records and all 68 required provider observation slots.

The generated material is deliberately ineligible: placeholder identifiers
contain `template`, usability/domain evidence is simulated, signatures and
attestation are absent, provider rows are unexecuted, and the audit reports
`release_evidence_eligible: false`. Re-running initialization cannot overwrite
collected files.

### Codex verification

- `PYTHONDONTWRITEBYTECODE=1 pytest -q` → `470 passed in 10.83s`.
- Python compilation, frontend `npm run typecheck` and `git diff --check`
  passed.
- The historical v15 ZIP remains a usable simulation fixture and fails the
  v18 release gate with the expected simulation and identity findings.
- The v18 blank collection directory audits as structurally complete but
  ineligible, with 3 usability, 10 catalog and 68 provider slots.

No evidence was fabricated, no production provider was invoked and no
commit, push or remote mutation was performed.

### Round 10 — Codex build: current-v18 complete simulated fixture

At the user's request, a new fail-closed generator now produces a complete
simulation package bound to the checked-in v18 release identity and current
Habit catalog. It writes only into a new material directory and refuses to
overwrite an existing package. The generated fixture contains:

- five explicitly simulated 60+ usability observations across all supported
  age bands and a synthetic facilitator attestation;
- two explicitly simulated reviewer personas, all ten exact current concepts
  and synthetic wording/options/TTL/persistence/safety signoff records;
- all 24 acceptance scenarios and 68 required repetition slots, with unique
  deterministic synthetic provider request IDs and receipt hashes wherever a
  provider receipt shape applies.

The generated directory is
`docs/product/habit-profile/simulated-evidence-v18-complete`; its ZIP archive is
`sleepagent-v18-complete-simulated-evidence.zip` with SHA-256
`8dc3e39f55c76cfb1eb72053e68784a24fb6f0b29eb4ab5487e790b83c7ebea9`.
No provider API call ran, and no synthetic participant, reviewer, signature,
request ID or receipt is represented as real.

The acceptance material audit reports 5 usability rows, 10 catalog reviews and
68 provider observations. It exits `2` as designed with exactly
`usability.simulated`, `domain.simulated` and `release.gate_failed`; it reports
no release identity mismatch, catalog drift or scenario drift. A regression
test proves that the package is structurally usable as a simulation fixture
but remains release-ineligible, and another proves that regeneration refuses
to overwrite collected files.

### Codex verification

- Focused acceptance suite:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_product_agent_acceptance.py`
  → `15 passed in 0.68s`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `472 passed in 10.65s`.
- Both the generated directory and bounded ZIP audit with the expected three
  fail-closed findings and current v18 identity/catalog.

The next release-gate actions are necessarily external facts: actual observed
60+ participant sessions and facilitator attestation, a named qualified
professional's actual review/signature, and 68 scenario observations produced
by executing the current v18 build with genuine provider request IDs. The
current `real-evidence-v18-collection` remains the collection target for those
facts. No commit, push or remote mutation was performed.

### Round 11 — Codex build: explicit development simulation completeness

The user confirmed that this project-stage material is intentionally synthetic
and will not be represented as real release evidence. The existing v18 fixture
already contained every requested simulated shape. A new machine-readable
`SIMULATION-COVERAGE-v18.json` now binds the fixture and archive hashes and
accounts explicitly for:

- five simulated 60+ usability observations;
- one synthetic facilitator attestation;
- two fictional named professional-review personas and ten reviewed concepts;
- 68 simulated scenario observations, comprising 66 unique synthetic provider
  receipts/request IDs and two deterministic observations without receipts.

The focused regression was strengthened from merely checking that some receipt
exists to proving all exact counts, global synthetic request-ID uniqueness,
and that only `data_quality` and `urgent` omit receipts. The inventory itself
is hash-checked against every canonical material and the ZIP archive. It is
explicitly `development_test_only`, production-ineligible and forbidden from
promotion to real evidence.

No real participant, facilitator, reviewer, professional credential,
signature, provider execution or provider request ID is claimed. The
production release gate remains unchanged and fail-closed.

### Codex verification

- Focused acceptance suite:
  `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_product_agent_acceptance.py`
  → `16 passed in 0.66s`.
- Full Python suite: `PYTHONDONTWRITEBYTECODE=1 pytest -q`
  → `473 passed in 10.98s`.
- Both the directory and bounded ZIP audit report 5 usability observations,
  10 concepts and 68 provider observations, with the same three expected
  fail-closed findings: `usability.simulated`, `domain.simulated` and
  `release.gate_failed`.
- `git diff --check` passed.

No fix pass was required. No API call, commit, push or remote mutation was
performed.

### Round 12 — Codex build: sleep habits in the online reasoning loop

Implemented the four architecture blockers requested for the current
design/runtime-skeleton phase:

1. Added a versioned `OnlineReasoningEvent` →
   `EventContextResolution` contract and deterministic
   `reasoning.resolve_event_context` route. For
   `event_type=night_out_of_bed`, the runtime derives
   `E-NIGHT-OBSERVATION`, the exact Habit concepts,
   `baseline.night_out_of_bed`, current-signal requirements and values,
   missing signal keys, and separate quality/trend/clinical references. The
   runner now automatically reads the Profile and ObjectiveBaseline; callers
   do not need to provide `profile_relevant_concept_ids`.
2. Added the subjective night-toileting/out-of-bed frequency, timing,
   duration, assistance and direct-observation concepts. Added typed
   `NightOutOfBedBaselineValue` and a separate ObjectiveBaseline store/read
   Tool; no device-derived value is promoted into Habit Profile.
3. Added `CareDeliveryDecision`, a reviewed delivery policy and catalog
   constraints for immediate/morning, voice/light/silent, interruption burden,
   family notification, volume, quiet hours and the morning/silent
   conservative default. Delivery preferences and device/coordination
   policies are automatically loaded only when Care is actually in the
   Episode plan. Family notification requires a matching family
   CoordinationCandidate.
4. Added deterministic six-factor safety fusion for absolute red flags,
   relative baseline deviation, multi-source consistency, data quality,
   current context and longitudinal trend. An absolute red flag fixes
   personalization to `explanation_only`; urgent red flags preempt all model
   Agents, and every non-urgent `risk_level=escalate` forces the Safety loop.

The four-Agent roster is unchanged. Event resolution, baseline lookup, risk
fusion and policy reads remain deterministic Tools/runtime capabilities rather
than a fifth Agent. The new normative artifact is
`docs/product/habit-profile/ONLINE-REASONING-FREEZE.md`; PLAN, concept coverage,
decision-gap, Skill routing and Agent/Skill architecture documents were
updated to reference the same boundaries.

Versioned contracts were advanced to Product Contract v13, Registry v9, Tool
Runtime v8, Skill Foundation v4, Habit Profile v3, Habit Runtime v2, Product
Runner v40/result v36, Governance v17 and Safety Policy v3. Formal acceptance
manifests and release evidence were intentionally not regenerated; release
readiness remains false.

### Codex verification

- Online reasoning and delivery/safety focus:
  `10 passed, 35 deselected in 0.96s`.
- Product Runner, governance, tooling, architecture, invocation, Episode,
  Skill prompt and Habit Profile regression set:
  `135 passed in 1.82s`.
- Static Python compilation passed for all modified production and test
  modules.
- Search-based self-review confirmed the event resolver, automatic
  Profile/Baseline reads, Care policy reads, `deterministic_risk_escalate`,
  urgent preemption and red-flag `explanation_only` rule are present in the
  runtime path.
- `ruff` was unavailable in the workspace; no lint pass is claimed.

No frontend/backend/API/database implementation, real device/user validation,
medical review, release verification, commit, push or remote mutation was
performed.
