# Plan Review Log: SleepAgent 轻量冷启动策略
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

1. **一个请求级 ceiling 会让成熟指标给不成熟指标“搭便车”。** 多指标晨间回答
   可能同时包含稳定作息和只有一晚的呼吸数据，单一 ceiling 无法安全表达。
   **Fix:** 按 `claim_requirement_id + metric_id` 独立计算 decision，并定义整体
   supported/degraded/blocked 的含义。
2. **只有模型调用前检查，无法阻止模型越过 ceiling 或调用期间撤权。** Prompt
   约束不是发布门，授权/source revocation 也可能在运行中变化。
   **Fix:** 绑定 FactSnapshot，发布前重验授权与来源，并增加确定性 claim
   postflight。
3. **临时 readiness 可重算却没有因果 receipt。** 事后无法说明某次回答为何
   使用了 3 晚、哪个 cohort 和哪个策略版本。
   **Fix:** 复用既有 Episode/Tool receipt 保存最小 decision 因果字段，不新增
   状态表。
4. **measurement cohort 过于宽松且未说明 revision 去重。** 只写设备测量域和
   算法主版本可能混入不同配置、校准、binding 或更正前后的同一夜。
   **Fix:** 默认使用精确来源代际键，兼容证据才能合并；每夜每指标只由当前
   NightEpisode revision 贡献一次。
5. **`1–3` 档与“门槛完全按指标配置”存在冲突。** 若某指标配置成 3 夜即可
   provisional，计划会同时允许和禁止趋势。
   **Fix:** 冻结少于 4 夜绝不 provisional 的首版安全下限，指标只能要求更多；
   15 夜仍只是 established 的实验候选。
6. **Adapter verification 与 deployment 被写成一个顺序生命周期。** 现有合同
   明确两者正交，且 shadow 输出落到何处没有定义，存在污染生产 canonical data
   和健康告警的风险。
   **Fix:** 生产要求 `ENABLED AND capability VERIFIED`；shadow 只进隔离候选
   范围并显式禁止用户结果、基线、Memory 和健康告警。
7. **Skill 控制面缺失时仅写“不能默认”仍可能被代码内 champion 字段绕过。**
   **Fix:** 要求生产 resolver 对未完成治理的新 Skill/版本保持不可达。

VERDICT: REVISE

### Codex response

接受全部七项。计划已改为 per-claim decisions，补充 FactSnapshot、发布前
revalidation/postflight 和最小 decision receipt；收紧 cohort/revision、少于
4 夜的 provisional 下限，以及 Adapter 的正交双门与 shadow 隔离。Skill 未完成
控制面时改为 resolver 不可达。所有修订复用现有快照、receipt 与 registry
边界，没有引入通用冷启动平台或新数据库。

## Round 2 — Codex review

1. **ClaimRequirement 没有权威生产者。** 如果由模型自由提交 metric 或 claim
   kind，它可以选择更宽 ceiling 或绕过当前意图。
   **Fix:** 只允许 runtime 从注册 Episode/plan step、意图和 Tool Schema 产生
   合法 requirements；无法映射时一般回答或最小澄清。
2. **现有 `SourceScope.valid_night_count` 是单个调用方字段，与 per-metric
   decisions 冲突。** 不定义迁移会留下第二个权威计数。
   **Fix:** 将 scalar 降为单指标兼容展示字段，服务端覆盖；多指标权威计数只在
   typed `MetricReadinessDecision` 中。
3. **现有 publication postflight 不理解“趋势/规律”语义。** 它只验证 claim
   refs、semantic bindings 和数字，不能可靠地从中文自由文本判断 ceiling。
   **Fix:** 在 `EvidenceClaim`/acceptance 加 typed `claim_strength` 和 decision
   ref；degraded 个人路径使用确定性边界句，不构建 NLP 猜测器。
4. **FactSnapshot 当前不绑定 readiness decision。** 仅把 receipt 另存会允许
   调用/恢复/发布使用不同的夜数或策略判断。
   **Fix:** additive 扩展 FactSnapshot，保存 decision refs/hash，并在恢复和
   发布时复核。
5. **Adapter shadow 引入了未定义的 `eligible_for_user_result` 状态。** 这会
   反向制造新数据模式和持久化语义。
   **Fix:** 生产 shadow 在 `AdapterObservationCandidate`/comparison receipt
   处停止；完整端到端 shadow 只在隔离非生产环境/测试 subject。
6. **Approach 对 Policy 的代码所有者仍写成“新建或并入”，可能形成两套规则。**
   **Fix:** 冻结一个无状态 `product_agent/cold_start.py` 为唯一 claim policy
   所有者，所有旧入口调用同一纯函数。

VERDICT: REVISE

### Codex response

接受全部六项。计划现在定义 runtime-owned ClaimRequirement、typed
MetricReadinessDecision、SourceScope scalar 迁移、FactSnapshot 绑定和
Evidence claim-strength gate；Adapter shadow 不再发明数据状态。实现所有者收敛
到一个无状态模块，degraded 表达使用审核边界句，避免新增自由文本分类平台。

## Round 3 — Codex review

1. **“有效夜数”仍混合当前查询窗口和历史 baseline 构建窗口。** 现有 Trend
   本来就分别使用 current window 与 older baseline；合成一个 count 会让成熟
   用户的一晚新记录重新冷启动，或让一晚数据借成熟旧基线冒充新趋势。
   **Fix:** `MetricReadinessDecision` 分别记录 scope/baseline count、refs 和
   maturity，并由 claim kind 决定两者要求。
2. **1–3 夜验收条件会错误限制已有稳定基线的用户。** 一晚相对稳定基线的
   描述可以成立，但一晚纵向趋势不能成立。
   **Fix:** 将 0/1–3 表限定为“尚无可用 established baseline 的新 cohort”，
   并区分 current-night-vs-baseline 与 longitudinal-trend。
3. **从自由文本“确定性产生 ClaimRequirement”不可实现，也可能演变成新的意图
   规则引擎。**
   **Fix:** 使用有限版本化 catalog；runtime 先形成合法候选，SleepCare 只能选
   ID，runtime 展开不可变定义。
4. **`general_knowledge` 与个人证据强度不是同一条质量轴。** 若把它当作最低
   Evidence strength，模型可能把一般知识升级为个人结论。
   **Fix:** 明确该值表示“禁止个人 claim”；reviewed general knowledge 可伴随
   任何 ceiling，但永不支持个人事实。
5. **薄策略若自己读取 Adapter/Tool/Skill 生命周期，会复制三套发布逻辑。**
   **Fix:** 各 owning Registry 先产 eligibility receipt，ColdStartPolicy 只
   消费结果。

VERDICT: REVISE

### Codex response

接受全部五项。计划拆开 scope 与 baseline 两个窗口/计数，修正成熟用户单晚
比较的语义，采用有限 ClaimRequirement catalog，并明确一般知识不能升级为个人
Evidence。能力资格继续由各 Registry 决定，薄策略不复制发布控制。

## Round 4 — Codex review

1. **冻结计划没有定义生产缺少已审核阈值时的行为。** `4/15` 若只是候选而代码
   又需要数值，实施者很可能偷偷写默认值并在生产自动晋级。
   **Fix:** 缺少 reviewed BaselinePolicy 时 fail closed 为 unavailable；4/15
   只作为非生产 fixture，真实实验后发布精确配置。
2. **ClaimRequirement catalog 仍是抽象概念。** 没有最小 kind 集和日期选择
   规则，模型可挑选数值有利的夜晚制造“变化”。
   **Fix:** 首版固定五类 claim，metric 来自 allowlist；日期只由用户明确指定
   或注册固定窗口决定，禁止 value-dependent selection。
3. **“历史 Artifact 不可变”与“无新持久化表”之间缺少重启语义。** 如果只存在
   内存 Store，重启后既无法审计旧 Artifact，也无法保证当前 readiness 一致。
   **Fix:** 当前 projection 从有界 canonical window 重算；Artifact/decision
   序列化进既有 ToolReceipt/ProductEpisodeResult，缓存明确为非权威。
4. **capability eligibility receipt 只说不可伪造，没有最小绑定字段。** 旧
   Registry 状态或另一环境的 verdict 可能被重放。
   **Fix:** 绑定 owning Registry snapshot/hash、精确能力/版本、环境、配置/
   内容 hash、权威 refs 和解析时间，并纳入 FactSnapshot。
5. **时相指标的 cohort 未绑定时区与 sleep-day 规则。** 旅行或 boundary policy
   变化会把本地 23:00 与另一时区的 23:00 错误混为稳定作息。
   **Fix:** 时间敏感 metric 将 IANA timezone 与 sleep-day/boundary policy
   version 纳入精确 cohort。

VERDICT: REVISE

### Codex response

接受全部五项。计划补充 production fail-closed policy、五类静态 claim、
防 cherry-pick 窗口、receipt-based restart/replay、精确 capability receipt
绑定和时区敏感 cohort。实现仍是一个纯策略模块、现有 receipt 和静态配置，
没有新增服务、数据库或发布平台。

## Round 5 — Codex review

No blocking findings.

1. 三个就绪维度只在具体 claim 的必要依赖上合成，没有退化成全局用户等级。
2. scope 与 baseline 计数、成熟用户单晚比较与新趋势、一般知识与个人 Evidence
   已分别建模，未留下明显的 claim 升权路径。
3. 现有调用方 scalar、跨设备混算、revision 重复、运行中撤权、模型越过
   ceiling 和生产 Adapter 未验证解析都有明确迁移/验收门。
4. 未校准的 per-metric 门槛没有被伪装成产品事实；production policy 缺失时
   fail closed，4/15 仅是非生产 fixture/实验起点。
5. 方案的新增面保持为一个纯策略模块、additive typed contracts、静态配置和
   既有 receipts；未引入新 Agent、服务、数据库、规则 DSL 或统一发布平台，符合
   用户要求的不过度工程化边界。

VERDICT: APPROVED

## Act 3 — Codex build

Date: 2026-07-31

### Implementation

- Added the single stateless policy owner
  `sleepagent/product_runtime/cold_start.py` with the finite claim
  catalog, reviewed metric allowlist, exact measurement cohorts, server-derived
  per-metric night counts, non-production `4/15` fixture policies, baseline
  projections, capability receipts, typed readiness decisions, deterministic
  degraded copy, and FactSnapshot binding helpers.
- Extended existing contracts and receipts rather than adding a service or
  table. FactSnapshot now binds decision/capability refs and hashes;
  EvidenceClaim binds typed strength, metric, cohort, and decision; the
  existing ToolReceipt/ProductEpisodeResult path carries the audit material.
- Added deterministic Evidence acceptance and publication checks for forged,
  stale, mismatched, or above-ceiling personal claims. Source-dependent cold
  decisions require current prepublication revalidation; source-independent
  general-knowledge/degraded boundaries remain available.
- Converged legacy scalar counts to display-only semantics and isolated Trend
  comparisons by subject and exact source generation. Latest revision wins for
  the same sleep day.
- Tightened production Adapter resolution to require enabled deployment plus
  exact verified capability, and made unreleased Skill versions unreachable as
  champion defaults.
- Reused the existing optional Habit/Profile flow and urgent preemption. No
  fifth Agent, onboarding questionnaire, rule DSL, database, worker, or common
  release control plane was added.
- Migrated all four production ProductEpisodeRunRequest construction paths
  (NightEpisode bridge, product API, task API, and product-device dialogue) to
  bind cold-start decisions. A legacy entry that cannot yet prove an exact
  cohort and owning-Registry receipt fails closed without inventing either.

### Acceptance audit

- Criteria 1–12: covered by catalog, zero/one/two/three-night, threshold,
  per-metric validity, explicit-window, scalar-count, production-policy,
  coverage/freshness/recovery, and multi-metric tests in
  `tests/test_cold_start_policy.py`.
- Criteria 13–16: covered by cohort compatibility, revision deduplication,
  cross-subject/device isolation, immutable Artifact downgrade/recovery, and
  one-night-vs-established-baseline tests in the cold-start and Trend suites.
- Criteria 17–19: existing optional questionnaire, skip, question-budget,
  profile-field scope, and urgent-preemption suites remain green.
- Criteria 20–22: production Adapter dual-gate, Skill release-stage, allowlist,
  and capability-receipt binding tests remain green.
- Criteria 23–28: FactSnapshot/ToolReceipt traceability, restart-deterministic
  projection, typed Evidence ceiling, current-source revalidation, historical
  Memory boundary, and reviewed user-facing degraded copy are covered.
- Criterion 29: all tests outside three pre-existing unrelated failures pass.

### Bounded fix rounds

1. Exact cohort filtering exposed a replay-only timezone mismatch: historical
   replay summaries were emitted in UTC while the current scenario used its
   configured timezone. The replay summary now inherits the scenario timezone;
   the safety replay regression passed.
2. Contract/governance/runner version changes correctly caused release identity
   drift. The checked-in acceptance manifest was refreshed to the exact current
   identity. The same final audit also closed the legacy-entry opt-in bypass by
   binding fail-closed decisions at every real request constructor.

### Proof

- Focused cold-start and migrated-entry proof: `23 passed`.
- Product/SleepDomain related suite: `321 passed`; only the same three unrelated
  repository failures remained.
- Full suite: `762 passed, 3 failed` in 34.01s.
- Full suite excluding the three known unrelated failures:
  `762 passed, 3 deselected` in 34.54s.
- `git diff --check`, targeted `py_compile`, and acceptance release-identity
  equality all passed. Final identity:
  `02d4eff71d5d208288133237e6846da72ba45e984ac081e16544c90e47d344ae`.

The three pre-existing failures are:

1. `test_governed_memory_revisions_append_and_invalidate_old_handle` — an old
   governed-Memory handle is not invalidated after replacement.
2. `test_checked_in_v23_collection_skeleton_is_current_and_fail_closed` — the
   checked-in historical material contains 10 concepts while the current
   catalog contains 22.
3. `test_v15_complete_simulated_archive_is_a_bounded_historical_fixture` — the
   immutable historical archive has the same 10-versus-22 concept mismatch.

No Memory implementation or historical acceptance archive was changed by this
build.

### Changed paths

- `docs/audits/agent-architecture/ACCEPTANCE-MANIFEST.json`
- `backend/main.py`
- `sleepagent/product_device/radar_agent.py`
- `sleepagent/product_api/diagnostics/http.py`
- `sleepagent/radar_agent/agents/trend.py`
- `sleepagent/product_runtime/__init__.py`
- `sleepagent/product_runtime/cold_start.py`
- `sleepagent/product_runtime/contracts.py`
- `sleepagent/product_runtime/governance.py`
- `sleepagent/product_runtime/habit_profile.py`
- `sleepagent/product_runtime/registry.py`
- `sleepagent/product_runtime/runner.py`
- `sleepagent/product_runtime/skills.py`
- `sleepagent/sleep_domain/agent_bridge.py`
- `sleepagent/sleep_domain/product_data.py`
- `sleepagent/sleep_domain/registry.py`
- `tests/test_cold_start_policy.py`
- `tests/test_product_agent_runner.py`
- `tests/test_product_api_endpoints.py`
- `tests/test_radar_task_api.py`
- `tests/test_radar_trend_agent.py`
- `tests/test_sleep_domain_adapter_registry.py`
- `tests/test_sleep_domain_agent_bridge.py`
