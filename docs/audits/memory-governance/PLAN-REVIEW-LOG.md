# Plan Review Log: HealthClaw 启发的 SleepAgent 纵向记忆治理
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **“每个终态 Episode”与当前追加式结果模型冲突。** 同一 Episode 可以先产生
   terminal `partial`，恢复后再追加 terminal `complete`；按 Episode 只建一个
   Receipt 会漏掉一个不可变结果，按两者都建又与“恰有一个结果”验收冲突。
   **Fix:** 把归纳单位锁定为不可变 terminal result revision，并定义后续结果对
   旧派生物的 supersede 规则。
2. **`urgent_boundary` 被写成状态，但代码中它是 `EpisodeType`，其 receipt
   status 是 `complete`。** “每个需归纳的终态”也给实现者留下了跳过
   urgent/partial/blocked Outbox 的空间。**Fix:** 所有 terminal result 无条件
   原子创建 Job；状态矩阵同时按 EpisodeType 与 EpisodeStatus 判定，urgent
   必须确定性 `excluded`。
3. **dead-letter、人工重放与“唯一当前有效 outcome”没有一致模型。** 在旧
   Receipt 不可变的前提下，重放会产生第二个 Receipt；若复用同一幂等键又可能
   被唯一约束吞掉。**Fix:** 区分 canonical Job、不可变处理 generation/Receipt
   和当前 publication；dead-letter 永不生效，人工重放显式引用父 Receipt。
4. **Digest 只有 refs，无法支持计划承诺的 typed selector 与确定性排序。**
   实现者最后只能读原始 payload、做自由文本匹配或让所有 Digest 都成为候选。
   **Fix:** 增加只可从 accepted typed allowlist 复制的 concept/event/outcome/
   temporal 索引字段，并继续禁止自由文本摘要。
5. **禁止 `all` 与合法的“你记得我什么”请求相互冲突。** 当前合同要求精确
   concept，但 SleepCare 的显式 Memory review 本来就需要发现已有条目。
   **Fix:** 增加独立、用户显式触发、稳定分页的 `inventory_page` selector；
   每页受硬上限约束，cursor 绑定 subject/purpose/policy epoch，不能给 Evidence
   使用或一次返回全集。
6. **source revalidation 可能绕回 audit Store。** Digest 指向
   ProductEpisodeRunResult/ToolReceipt，而计划又声明这些审计 payload 不可供
   模型读取；“原始来源”一词会诱导实现者直接解包审计记录。**Fix:** 只允许
   authorized canonical source resolver 重新取证；audit payload 不能充当该
   resolver，来源消失时仅允许有限历史过程陈述。
7. **查询时过滤不足以关闭撤权竞态。** 查询通过后、模型调用前或输出发布前
   发生 forget/withdraw，旧 slice 仍可能进入 provider 或回复。**Fix:** 引入
   单调 privacy/authorization epoch，绑定 query/cache，并在模型调用和发布前
   重检；失配即丢弃、不得复用。
8. **SkillOutcome 的离线出口仍可泄露直接标识与个人 refs。** 现有合同包含
   `episode_id`、`input_refs`、`output_ref`，仅写“去标识化”不能防止把生产
   lineage 搬进离线控制面。**Fix:** 分离生产内的 SkillOutcome 与 allowlisted
   OfflineSkillOutcomeEnvelope；离线包不得含 subject/actor/raw episode/source
   refs，并受撤权/删除 eligibility 控制。
9. **纵向 benchmark 的 exposure 与“明显优于”不可复现。** 只写“实际 token”
   未说明多次调用如何累计，聚合平均也会掩盖关键场景回退；“明显”没有统计
   判据。**Fix:** 固定 tokenizer，累计 Episode 内所有 provider 请求的完整
   model-input tokens；预注册主指标/分层，并用置信界限验证相对 current-only
   的严格优势与相对 full-history 的 5pp 非劣。

VERDICT: REVISE

### Codex response

Accepted all nine findings. The plan now separates immutable Digest/Receipt
payloads from append-only lifecycle events and rebuildable projections, derives
result identity and TTL time from one transaction, uses an explicit V1/V2 Memory
union, replaces persistent model-visible refs with scoped handles, makes
`as_of` and explicit-review intent runtime-owned, isolates the three-arm
benchmark from production audit data, limits phase-1 offline use to reversible
read-only ingestion, blocks cutover on unrecoverable publication routes, and
checks both item and final provider-request budgets. No finding was rejected.

### Codex response

Accepted all nine findings. The plan now treats each immutable terminal result
revision as the induction unit, creates a Job for every terminal result including
urgent/partial/blocked, separates automatic attempts from replay generations,
adds typed Digest indexing fields, supports only bounded explicit inventory
pagination, forbids audit payloads from acting as canonical sources, closes
revocation races with epochs, sanitizes the offline Outcome envelope, and makes
the longitudinal benchmark cumulative and statistically testable. No finding
was rejected.

## Round 2 — Codex review

1. **Content-addressed Digest still contains mutable state.** `retrieval_status`
   changes on expiry/forget/supersede, so either `digest_hash` changes in place
   or the hash no longer describes the object; `InductionJob.state` has the same
   tension with the append-only invariant. **Fix:** split immutable payloads from
   append-only status/attempt events and permit mutable projections only as
   rebuildable CAS indexes.
2. **Terminal identity and TTL origin are undefined in the current schema.**
   `ProductEpisodeRunResult` has no `terminal_result_id` or `completed_at`;
   persistence currently computes `result_id` and `recorded_at` externally.
   **Fix:** define canonical result hashing, server-generated immutable
   `terminal_recorded_at`, revision uniqueness, and exactly which timestamp
   starts TTL.
3. **“Additive” required Memory fields would break or accidentally authorize
   legacy rows.** The current strict `MemoryItem` has no schema discriminator,
   and permissive defaults would silently classify old free text. **Fix:** use a
   versioned discriminated union (`LegacyMemoryItemV1` /
   `GovernedMemoryItemV2`); V1 is fail-closed and cannot be read by Evidence.
4. **Persistent refs can themselves leak identity or topology to a model.**
   Returning episode/source/item refs as model-visible fields defeats the claim
   that subject IDs and audit identities stay server-side. **Fix:** expose only
   short-lived, subject/purpose/invocation-bound opaque handles and sanitized
   labels; keep persistent refs solely in server-side Receipt/audit data.
5. **`as_of` and explicit-review intent can still be model-forged.** They appear
   in the request contract without a trusted derivation rule, and quoted text or
   retrieved content could mimic “show me everything.” **Fix:** runtime derives
   `as_of` from the authenticated Episode/cutoff and recognizes review intent
   only from the current authenticated top-level user turn; handles/cursors are
   short-lived and non-transferable.
6. **The full-history comparator conflicts with “no full Memory in Prompt.”**
   An implementer could read production audit history to create the baseline, or
   the zero-exposure criterion would make the comparator impossible.
   **Fix:** run all three arms only in an isolated frozen harness over synthetic
   or separately consented/de-identified structured fixtures; full-history means
   all authorized, unexpired fixture items, never raw audit/dialogue.
7. **Offline deletion language overpromises after learning.** A tombstone cannot
   untrain an already produced shared PATCH or aggregate. **Fix:** phase 1 offline
   flow is ingestion/read-only and cannot train, generate candidates or update
   aggregates; future use requires a new approved retention/rebuild/unlearning
   contract.
8. **Publication-before-persistence remains a hidden cutover hazard.** Current
   runner calls publisher before `_store`; testing the failure without a release
   rule still permits “reply sent, no Result/Job” in production. **Fix:** make a
   recoverable idempotent publication reservation/journal a prerequisite for any
   affected route, or keep induction cutover disabled there; never claim the
   induction transaction itself solves external exactly-once delivery.
9. **A single oversized item has no deterministic budget behavior.** “Drop
   items” is ambiguous when the first item exceeds the budget, and reported
   token counts can differ from actual provider serialization. **Fix:** measure
   the final serialized model-facing slice with the locked tokenizer; omit an
   oversized item with a reason code and recheck the complete provider request
   before dispatch.

VERDICT: REVISE

### Codex response

Accepted all nine findings. The plan now separates immutable Digest/Receipt
payloads from append-only lifecycle events and rebuildable projections, derives
result identity and TTL time from one transaction, uses an explicit V1/V2 Memory
union, replaces persistent model-visible refs with scoped handles, makes
`as_of` and explicit-review intent runtime-owned, isolates the three-arm
benchmark from production audit data, limits phase-1 offline use to reversible
read-only ingestion, blocks cutover on unrecoverable publication routes, and
checks both item and final provider-request budgets. No finding was rejected.

Log-order note: this Round 2 response summary was accidentally appended once
above the Round 1 response before the Round 2 heading. It is repeated here at
the correct append position; the earlier line was retained because this review
log is append-only.

## Round 3 — Codex review

1. **The worker still needs raw-audit privilege.** Reading and validating a full
   `ProductEpisodeRunResult` necessarily deserializes old Tool output and
   conversation-bearing Agent payloads before the allowlist is applied.
   **Fix:** terminalization must create a separately hashed
   `InductionInputManifest` from allowlisted typed projections in the same
   transaction; the worker gets manifest access but no audit-reader capability.
2. **Reliable retry has no bounded processing-input retention.** If the new
   manifest lives forever it becomes another sensitive history store; if it
   disappears arbitrarily, dead-letter replay silently falls back to audit.
   **Fix:** give the manifest a non-refreshing seven-day maximum TTL, keep retry
   bounds inside it, reject late replay without audit fallback, and purge its
   payload while retaining only hash/status metadata.
3. **Conflict handling is not atomic under item/token limits.** Ranking can
   select one side of a conflict and drop the other, despite the plan saying
   conflicts are never hidden. **Fix:** retrieve relevant conflict groups as
   atomic units, count all members against budgets, and return neither side if
   the complete group cannot fit.
4. **Pending profile candidates can become shadow personalization.** The plan
   does not say whether they may affect ranking, questions, Care, reminders or
   proactive prompts before confirmation. **Fix:** make pending candidates
   non-retrievable for reasoning and non-operative; surface them only in an
   explicit Memory review or an already-authorized existing profile-confirmation
   flow, never through a new unsolicited message.
5. **Digest status transitions can race or reactivate forgotten content.**
   Append-only events alone do not define precedence when expiry, correction,
   supersede and forget occur concurrently. **Fix:** add a per-digest monotonic
   sequence/CAS state machine with terminal privacy precedence and forbid
   reactivation; corrected content gets a new Digest.
6. **“Source can be revalidated” before ranking could fan out privileged reads.**
   An implementation might open every candidate source just to calculate rank,
   increasing exposure beyond selected items. **Fix:** rank on non-content
   resolver capability/availability metadata, then revalidate only the bounded
   selected group through authorized Tools.
7. **Rolling deployment can leave terminal results without Jobs.** A new table
   and repository method do not stop an old replica from calling the existing
   `append` during mixed-version rollout. **Fix:** stage schema first, make the
   database/gateway reject terminal inserts without an atomic Job, fence old
   writer versions, prove zero orphan terminal rows, then enable shadow worker
   and finally retrieval.
8. **Opaque handle mappings can become an undeclared cache of personal refs.**
   Without their own TTL/purge rules, an expired handle table could bypass
   Memory retention even if the model-facing token is invalid. **Fix:** keep
   mappings server-side with the shortest item/invocation/epoch TTL, purge them
   on terminal invocation, forget/withdraw/expiry, and deny lookup after any
   binding change.

VERDICT: REVISE

### Codex response

Accepted all eight findings. The plan now creates a seven-day maximum,
allowlisted InductionInputManifest atomically with each terminal Result/Job and
denies the worker audit-reader access; late replay cannot fall back to raw data.
It also makes conflict groups atomic, keeps pending profile candidates entirely
non-operative before confirmation, defines monotonic privacy-first Digest state
transitions, limits source revalidation to selected groups, fences old writers
during rollout, and gives opaque-handle mappings explicit purge semantics. No
finding was rejected.

## Round 4 — Codex review

1. **A stale timestamp and transaction name undermine the now-precise
   contract.** Retention still says `completed_at`, and
   `append_result_with_induction_job` omits the mandatory Manifest.
   **Fix:** consistently use `terminal_recorded_at` and a terminal-bundle API
   whose name and atomic members are Result + Manifest + Job.
2. **GovernedMemoryItemV2 has metadata but no governed value schema.** New writes
   could keep storing arbitrary `value: str` while claiming V2, or continue
   creating V1 forever. **Fix:** require concept-bound typed value schemas,
   data-only trust labels and content hashes; after cutover all new writes are V2
   and unsupported free text fails closed rather than becoming legacy.
3. **`memory.read` can become a new “all personal context” federation layer.**
   The implementation says it connects generic Memory, Habit Profile and
   Digest, which could bypass the profile-specific purpose/role controls.
   **Fix:** keep `profile.read` physically and contractually separate;
   `memory.read` covers only governed generic Memory and EpisodeDigest, with a
   shared runtime budget applied only when Context assembler combines receipts.
4. **The new Manifest contains sensitive typed values without storage controls.**
   Short TTL alone does not protect files, backups or a broadly privileged DB
   account. **Fix:** keep it in the protected Product DB only, encrypt at
   rest/backups under deployment controls, deny exports/telemetry, use a
   least-privilege worker account and verify payload/backup expiry receipts.
5. **The benchmark-only full-history arm could leak into production packaging.**
   Isolation prose does not stop someone registering its loader as a Tool.
   **Fix:** put it in an evaluation-only package/process with no production
   credentials or Tool Registry entry, and add a build/manifest test proving it
   is absent from production artifacts.
6. **Repeated pending candidates still have an “internal evidence” loophole.**
   The profile section says repetition can raise evidence state, while later
   saying candidates are non-operative; frequency could indirectly change
   question ranking. **Fix:** phase 1 does only exact lineage dedupe and repeat
   count for restricted audit, with no confidence, priority or behavior effect.
7. **Digest rollback is only described as “close read.”** Existing handles,
   cursors, cached slices and in-flight provider requests could survive a plain
   feature flag. **Fix:** use a monotonic retrieval-policy epoch checked at
   query, handle resolution, provider dispatch and publication; kill switch
   increments it and synchronously invalidates mappings/caches.

VERDICT: REVISE

### Codex response

Accepted all seven findings. The plan now uses one terminal-bundle transaction
and timestamp vocabulary, requires schema-bound V2 values and forbids all new V1
writes, keeps profile.read outside memory.read under a shared Context budget,
protects the short-lived Manifest with least privilege and crypto-expiry,
excludes the full-history loader from production builds, makes repeat candidates
audit-only, and turns rollback into a monotonic retrieval-policy revocation that
invalidates in-flight data. No finding was rejected.

## Round 5 — Codex review

1. The borrowing boundary is now evidence-honest: HealthClaw supplies useful
   responsibility concepts, while production/offline separation, multi-model
   qualification and clinical claims are explicitly not attributed to evidence
   the paper or demo repository does not provide.
2. The information lifecycle is closed: raw Episode audit is inaccessible to
   induction/retrieval; a short-lived allowlisted Manifest feeds deterministic
   processing; Digest remains an untrusted hint; current claims require a fresh
   authorized canonical-source Receipt.
3. Terminal revisions, bundle persistence, retries, dead-letter replay,
   supersession, retention, correction, forget/withdraw and rolling deployment
   form one coherent append-only/idempotent state model with no raw-audit
   fallback.
4. Least privilege remains closed under composition: profile.read stays
   separate, memory.read has no wildcard/full-history path, returned identities
   are scoped handles, conflicts are atomic, budgets are shared, and all three
   revocation epochs are checked through provider dispatch and publication.
5. Neither pending profile candidates nor repeat counts can influence behavior
   before exact elder confirmation; production cannot mutate Skills, and the
   phase-1 offline outcome path cannot train, aggregate or generate PATCHes.
6. The isolated three-arm benchmark has reproducible context accounting,
   statistical gates and build-level separation from production; HealthClaw
   results cannot be substituted for SleepAgent evidence.
7. Remaining items—legal audit retention, production data inventory, concrete
   SLOs, sample-size/reviewer registration and choice of existing offline
   infrastructure—are named implementation/release inputs with fail-closed
   defaults, not unresolved authority or architecture contradictions.

VERDICT: APPROVED

## Act 3 — Build

### Round 1 — Codex build

Implemented the frozen longitudinal-memory governance plan without replacing
the existing SleepAgent `1+2+1` Product Agent architecture. The build adds:

- strict `GovernedMemoryItemV2`, `MemoryQueryIntent`, scoped retrieval handles,
  immutable `EpisodeDigest`, lifecycle events, encrypted
  `InductionInputManifest`, Jobs/attempts/Receipts, pending profile candidates,
  SkillOutcome records, offline envelopes and deployment attestations;
- one atomic terminal Result + Manifest + Job boundary, a deterministic
  least-privilege worker, bounded retry/dead-letter/replay, append-only revision
  handling, privacy/authorization/retrieval-policy epochs and a Digest
  kill-switch;
- purpose/role/SourceScope-bound retrieval, shared Agent/Episode exposure
  limits, provider and publication revalidation, canonical-source resolution,
  exact elder confirmation for candidates, and publication intent journaling;
- additive persistence migration `007_longitudinal_memory_governance`, AES-GCM
  envelope encryption with a required PostgreSQL KEK, writer fencing, orphan
  scanning, cross-process Job lease and publication CAS, encrypted-payload
  crypto-expiry, and restart hydration;
- an evaluation-only three-arm longitudinal benchmark with a locked tokenizer,
  cumulative and tail exposure, one-sided bootstrap gates, security counters
  and explicit non-clinical claim scope.

The production package contains no HealthClaw code, full-history loader,
personal Skill mutation path or new medical-domain task stack. `profile.read`
remains separate from `memory.read`; Habit Profile retains its existing typed
Store and Commit Controller.

Initial focused proof:

```text
pytest -q tests/test_healthclaw_memory_governance.py \
  tests/test_longitudinal_memory_benchmark.py \
  tests/test_product_agent_persistence.py \
  tests/test_product_agent_runner.py \
  tests/test_product_agent_governance.py \
  tests/test_product_agent_acceptance.py \
  tests/test_radar_agent_persistence.py
93 passed in 2.55s
```

### Round 2 — Codex build fix pass

Full-diff review exercised the persistent Worker across a real SQLite restart
and found that an application-clock skew could leave the durable Job projection
at `leased` after the in-memory Worker had completed. The persistence boundary
now commits completion/failure only with an exact durable lease-owner,
attempt-count and processing-generation CAS. Generic projection sync is
monotonic, cannot downgrade terminal state, and permits manual replay only by
advancing processing generation. Durable Receipts, Digests, Digest events,
candidates, SkillOutcomes and read Receipts are rehydrated from their explicit
tables rather than trusting only a process-local aggregate snapshot.

Added proof for successful Worker completion across two restarts, expired-lease
recovery, dead-letter persistence, higher-generation manual replay and
cross-process publication reservation/finalization.

### Round 3 — Codex build fix pass

The second bounded diff-review pass found two fail-closed edge cases:

1. one-way or transitive conflict references did not necessarily share the same
   grouping key; conflict selection now constructs connected components,
   rejects incomplete components and admits a complete component only when the
   whole group fits item and token budgets;
2. a failed durable terminal-bundle transaction could leave speculative
   in-memory state in the persistent adapter; the adapter now discards that
   state and rehydrates exclusively from the committed database before
   returning the failure.

It also records explicit candidate declines in the frozen Manifest so the
offline projector cannot surface the same unconfirmed candidate again.

### Codex verification

The complete relevant diff was compared against the pre-build snapshot at
`/tmp/sleepagent-healthclaw-build.ESi2sS`, including Product Agent contracts,
runner/governance, persistence, migration, benchmark and tests. An AST duplicate
definition scan found no duplicate module/class definitions. A production-tree
search found no HealthClaw or `full_history` implementation in
`sleepagent/radar_agent`; the benchmark package is excluded by the
`sleepagent*` package-discovery rule.

Final proof:

```text
PYTHONDONTWRITEBYTECODE=1 pytest -q
525 passed in 16.07s
```

The acceptance release identity is bound to runner v39, result schema v35,
longitudinal-memory v1 and deterministic-induction v2; all 68 existing provider
observations were rebound to identity
`045ee728fb1b2325a46d6a4a5556eb51c0019d9b0968ac5789454694b74b4b56`.

No architectural deviation from the frozen plan was required. Production
Digest read intentionally remains disabled until a complete
`DeploymentControlAttestation` proves encryption/backup expiry, worker least
privilege, publication journaling, writer fencing, a zero-orphan scan and the
benchmark gate. The included benchmark evidence is synthetic engineering
evidence only; it does not claim real-elder benefit or clinical effectiveness.
