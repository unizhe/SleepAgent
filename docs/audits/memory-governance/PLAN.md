# Plan: HealthClaw 启发的 SleepAgent 纵向记忆治理
_Locked via grill — by Codex + user_

## Goal

在现有 `SleepCareAgent + EvidenceReasoningAgent + CareStrategyAgent + SafetyReviewAgent`
四责任角色、`ProductEpisodeRunner`、FactSnapshot、ToolReceipt、Habit Profile、
Care State、Commit Controller 和受控 Skill 演进基础上，吸收 HealthClaw
“共享治理与个人状态分离、Episode 后统一判断信息去向、按任务最小检索、
工具产生证据而 Agent 综合判断”的概念，补齐 SleepAgent 当前纵向记忆链路。

首期只交付纵向记忆治理：修复通用 `memory.read` 返回全部 active Memory 的
隐私缺口；把完整 Episode 审计记录与可检索的最小 `EpisodeDigest` 分开；
在 Episode 终态后通过可靠异步、确定性的归纳流程生成 Digest、画像候选和
结构化 SkillOutcome；并以权限硬过滤、来源重验证、保留期、撤权传播和纵向
基准约束未来检索。HealthClaw 不替换现有四角色架构，不引入个人 SOP Store，
不复用其文件目录、自动画像/SOP 写入、医学任务工具栈或跨设备 Demo 服务。

## Plan authority and evidence

1. [`../docs/audits/agent-architecture/PLAN.md`](../docs/audits/agent-architecture/PLAN.md) 继续决定 Agent
   名单、责任、Episode、Memory/State、权限、Safety、Tool 和 Commit 边界。
2. [`../docs/audits/skills-design/PLAN.md`](../docs/audits/skills-design/PLAN.md) 继续决定 Skill、
   Registry、Resolver、Compiler、SkillLock、Outcome 和离线演进治理。
3. [`../docs/product/habit-profile/PLAN.md`](../docs/product/habit-profile/PLAN.md) 继续决定
   Habit Profile 的概念、来源、确认、角色访问和持久化资格。
4. 本计划只补充通用长期 Memory、Episode 派生记忆、归纳 Outbox、最小检索
   与纵向验收；发生冲突时，上述既有权威计划优先。
5. HealthClaw 论文与仓库只作为设计输入，不作为临床有效性、生产安全性或
   可直接复用代码的证据：
   - 论文：[`../HealthClaw_paper/full.md`](../HealthClaw_paper/full.md)
   - 本地官方仓库：[`../../HealthClaw/README.md`](../../HealthClaw/README.md)

## What is borrowed and what is not

### Semantic mapping

| HealthClaw concept | SleepAgent mapping | Decision |
| --- | --- | --- |
| L0 behavioural rules | GlobalPolicy、AgentProfile、EpisodeDefinition、确定性 Safety/权限/确认/Commit | 已有共享治理，不属于个人 Memory，不由老人 Episode 更新 |
| L1 domain knowledge index | reviewed Knowledge、Care catalog、Questionnaire Bank、Tool/Skill Registry | 保持多个权威 Store/Registry，不合并成一个物理“L1 文件” |
| L2 personal profile | typed Habit Profile + 经治理的通用长期 Memory | 保留；Episode 归纳只能产候选，不能自动写真值 |
| L3 reusable SOP | 共享 Skill、Care catalog 和确定性 EpisodeDefinition | 不建立个人 SOP Store，不创建用户私有 Skill |
| L4 episodic memory | 原始 `ProductEpisodeRunResult` + 派生 `EpisodeDigest` | 原始结果只审计；只有受控 Digest 可以被最小检索 |
| Post-episode induction | terminal result → Outbox → deterministic induction | 借“统一去向判定”，不借侧车 LLM 自由抽取 |
| Tool as evidence producer | ToolReceipt → Evidence acceptance → Care/SleepCare | 现有方向正确；Digest 也不能绕过 Evidence gate |
| Multiple surfaces/devices | 统一身份、FactSnapshot、事件/Provider adapter 合同 | 仅作兼容约束；本轮不扩建设备、模型或入口 |

### Explicitly rejected HealthClaw implementation patterns

1. 不复制 Markdown/TXT/JSONL 五层目录或仓库代码结构。
2. 不允许会话末尾的 LLM 根据轮次或关键词直接更新画像。
3. 不允许 LLM 自动覆盖 L3 SOP、修改 L1 或把单次经历变成运行规则。
4. 不把 SelfEvaluator、StrategyDistiller 或论文指标视为已验证生产闭环。
5. 不复用饮食、慢病、医学影像、生信或 benchmark 特化工具。
6. 不复用本地 JSON bindings、可选 token、无完整鉴权的跨设备 HTTP 服务。
7. 不把 L0/L1、个人画像、共享 Skill、Episode 审计和检索索引混为一个
   可由 Agent 任意读写的“Memory”。

### Evidence caveats

1. HealthClaw 论文支持的是 L0–L4 职责分层、Episode 后判断信息未来用途、工具
   产出 evidence input 和多源/多入口的统一框架；它没有证明生产执行与离线
   Skill 演进已经被安全隔离。本文的 online/offline boundary 来自 SleepAgent
   既有 Skill 治理，不冒充 HealthClaw 的实证结论。
2. “多设备/多入口”可作为 adapter 与身份一致性的兼容约束；论文和仓库没有
   提供足以直接继承的通用多模型资格、路由、回滚与安全证明，因此本轮不把
   “多模型统一”列为交付。
3. 官方仓库是研究/demo 参考，并且出现会话后自动画像/SOP 写入、全画像加载、
   文件型状态和弱鉴权跨设备服务等与本项目边界冲突的实现；这些是反例和测试
   输入，不是代码复用基础。
4. 论文结果主要基于合成轨迹、自动 evaluator 和受限 ablation，context
   exposure 口径也不能替代本计划对完整实际 model inputs 的统计；不能据此
   宣称老人获益、临床有效性或生产安全。

## Target invariants

1. 用户仍只面对统一 SleepAgent；不新增 MemoryAgent、EvolutionAgent 或第五个
   产品 Agent。
2. 原始 `ProductEpisodeRunResult`、完整对话、完整 Tool 输出和 Agent 内部推理
   永不成为在线 Memory/RAG payload。
3. 只有 `EpisodeDigest` 可作为跨 Episode 检索线索；Digest 永远不是当前事实
   或证据。
4. 每个不可变且 `receipt.terminal=true` 的 `ProductEpisodeRunResult` revision
   都有 canonical `InductionJob` 和至少一个不可变 terminal
   `InductionReceipt`；但不是每个终态结果都产生 Digest、画像候选或
   SkillOutcome。
5. 归纳只消费已经通过现有 acceptance gate 的 typed work product、ToolReceipt
   元数据、确认、Safety 和 Episode 状态；没有足够输入就 `exclude`。
6. 归纳流程不调用 LLM，不读取思维过程，不从自由文本创造新事实。
7. 系统推断的画像变化只能成为待确认候选；重复出现不能替代老人确认。
8. 生产运行不能创建、修改、批准或发布 Skill；单个 Episode 只能形成最小
   SkillOutcome。
9. 任何查询必须先做身份、角色、purpose、类型、SourceScope、时效、授权和
   保留期硬过滤，再进行确定性排序。
10. 空条件、通配符、“全部历史”、自动扩大时间范围和权限过滤前的语义检索
    一律拒绝。
11. 撤回授权、允许的遗忘、过期、纠错和 supersede 必须传播到索引与缓存；
    审计保留不能成为重新暴露给模型的后门。
12. 所有新增状态变化使用追加记录、哈希绑定、CAS/唯一约束和幂等键；不得以
    原地覆盖隐藏历史。

## Data and contract design

### 1. Raw Episode audit remains authoritative

`ProductEpisodeRunResult` 继续保存 EpisodeReceipt、AgentEnvelope、
AgentInvocationRecord、ToolReceipt、accepted work products、确认、Care/Memory
提交结果和外部动作状态。它是执行与审计的权威记录，但：

- 只能按受限审计接口读取；
- 不注册为 Memory Tool 数据源；
- 不进入 Prompt、RAG、向量索引或语义检索；
- 不因创建 Digest 而被删除、重写或简化；
- 其保留期继续服从独立的数据治理政策，本计划不把审计保留等同于个性化同意。

新持久化合同把 strict result model 的 canonical JSON（UTF-8、稳定字段顺序、
明确 datetime/enum 编码）计算为 `source_result_hash`；当前 persistence
`result_id` 收敛为同一 hash，并作为 `terminal_result_id`，不再维护第二套身份。
数据库唯一约束保证 `(episode_id, receipt_revision)` 只能绑定一个
`source_result_hash`，重复同内容幂等，不同内容同 revision 拒绝。repository
在 Result + Manifest + Job 事务中只生成一次 UTC `terminal_recorded_at` 并
持久化到三者；
Digest TTL 以该时间为起点，而不是 worker 开始/完成时间或客户端时间。

### 2. Immutable `EpisodeDigest` and status events

新增严格、拒绝未知字段、内容寻址且不可变的 `EpisodeDigest` payload。最少字段为：

- `schema_version`
- `digest_id`、`digest_hash`
- `subject_id`（仅存储/授权绑定使用，不复制到模型文本）
- `episode_id`、`episode_type`
- `terminal_result_id`、`source_receipt_revision`
- `source_result_ref`、`source_result_hash`
- `fact_snapshot_id`、`fact_snapshot_hash`
- `source_scope`
- `terminal_recorded_at`
- `concept_ids`
- `event_type_codes`、`outcome_codes`
- `observation_window_start`、`observation_window_end`
- `accepted_evidence_refs`
- `accepted_care_refs` 与有限 disposition code
- `feedback_event_refs`
- `safety_decision_refs` 与有限 reason codes
- `confirmation_refs`
- `source_lineage_refs`
- `eligible_purposes`
- `eligible_roles`
- `sensitivity_class`
- `valid_from`、`expires_at`
- `retention_policy_version`
- `supersedes_digest_id`、`conflict_refs`

`concept_ids`、event/outcome codes 和 observation window 只能从当前
EpisodeDefinition 及 accepted typed work product 的版本化 allowlist 复制，
不能由文本关键词、LLM 或任意 Tool 字符串生成。Digest 只保存引用、有限枚举、
时间、状态和最小派生元数据，不保存：

- 原始用户消息或整段对话；
- Agent 自由文本总结、推理过程或“经验教训”；
- 完整 Evidence/Care/Communication payload；
- 完整 Tool 输入/输出；
- 姓名、电话、证件号、地址等强身份字段；
- 未通过 acceptance 的 claim；
- 诊断、确定因果或新医学结论。

Digest 使用 `EPISODIC_HINT_UNTRUSTED` trust label。它只能帮助 Evidence
定位原始来源，不能直接支持当前个人 claim、Risk 或 Care。

可变生命周期不写回 Digest payload。每次创建、activate、expire、supersede、
withdraw、forget 或 correction 都追加不可变 `EpisodeDigestStatusEvent`
（event id/hash、digest ref、from/to status、reason、policy/privacy epoch、
causal request/receipt ref、server time）。当前状态是按事件序列重建的投影；
允许用 CAS 更新 materialized status/index 提高查询速度，但投影不是权威记录，
可从事件重建，且不能改变 `digest_hash`。若事件流、投影和索引不一致，查询
fail closed。

### 3. `InductionInputManifest`

terminalization runtime 使用版本化、无 LLM 的纯 projector，从当次内存中的 strict
result 对象只复制归纳 allowlist，生成不可变、内容寻址
`InductionInputManifest`。它与 Result、Job 在同一事务保存，至少包含：

- `manifest_schema_version`、`projector_version`
- `manifest_id`、`manifest_hash`
- `subject_id`（仅存储授权）
- `terminal_result_id`、`source_result_hash`、`source_receipt_revision`
- `terminal_recorded_at`
- `episode_type/status`、FactSnapshot hash、SourceScope
- 按 work-product kind 定义的 `accepted_typed_views`
- 仅含 tool identity/version/outcome/source-class/quality codes 的
  `tool_metadata_views`
- confirmation refs、Safety verdict/reason codes
- 已确认的 structured feedback event views
- `privacy_epoch`、`authorization_epoch`、retention policy version
- `created_at`、`expires_at`

Manifest schema 对每种 accepted work-product view 使用字段级 allowlist；未知 kind
或字段 fail closed。它不包含原始对话、Agent prose/reasoning、完整 work
product、Tool input/output、Communication 文本或 audit blob ref。测试在所有
禁用字段中放置 sentinel，证明 projector 输出与访问日志均不触及它们。

Induction worker 只有 Manifest Store 权限，没有 ProductEpisodeRunResult audit
reader 权限。Manifest payload 的 TTL 不超过 terminal 记录后 7 天，读取/retry/
lease/dead-letter 不刷新；自动 retry 窗口必须短于 TTL。Job terminal 且恢复
窗口结束或 TTL 到达时清除 payload、handle 和索引，只保留 manifest hash、
purge Receipt 与非敏感状态元数据。payload 清除后的人工重放返回
`source_manifest_expired`，不得回退读取 raw audit。

### 4. `InductionJob`

每个 `receipt.terminal=true` 的不可变结果 revision 在保存时都无条件原子创建
一个 canonical Outbox `InductionJob`；不得因其是 urgent、partial、blocked
或预计会被 exclude 而跳过：

- `job_id`
- `idempotency_key = hash(episode_id, source_result_hash, induction_version)`
- `episode_id`
- `terminal_result_id`、`source_receipt_revision`
- `source_result_ref`、`source_result_hash`
- `terminal_recorded_at`
- `input_manifest_ref`、`input_manifest_hash`、`manifest_expires_at`
- `induction_version`
- `retention_policy_version`
- `state = pending | leased | succeeded | retryable_failed | dead_letter`
- `attempt_count`
- `lease_owner`、`lease_expires_at`
- `next_attempt_at`
- `last_error_code`
- `created_at`、`updated_at`

处理语义是 at-least-once delivery + 幂等效果，不声称分布式 exactly-once。
唯一键、CAS 和内容哈希保证重复消费不能创建不同 Digest 或重复候选。
`InductionJob.state` 是操作投影；每次 reserve/lease/retry/succeed/dead-letter/
replay 都追加 `InductionJobEvent` 或 `InductionAttemptRecord`，状态投影可从
事件重建。CAS 更新投影不能删除失败历史。
同一 Episode 后续出现更高的 terminal result revision 时，它拥有不同
`source_result_hash` 和 Job；新 Job 必须确定性 supersede 旧 revision 的
可检索 Digest/候选。旧 Receipt 和原始结果仍保留，旧 SkillOutcome 标为同
lineage 已被后续结果取代，离线统计不得把多个 revision 当作独立成功样本。

### 5. `InductionReceipt`

每个终态结果最终形成不可变 Receipt：

- `receipt_id`、`receipt_hash`
- `job_id`、`processing_generation`
- `episode_id`
- `terminal_result_id`、`source_receipt_revision`
- `source_result_hash`
- `input_manifest_hash`
- `induction_version`
- `status = succeeded | excluded | dead_letter`
- `decision_codes`
- `episode_digest_ref`（可空）
- `profile_candidate_refs`
- `skill_outcome_refs`
- `exclusion_reasons`
- `attempt_count`
- `parent_receipt_ref`（人工重放时必填）
- `completed_at`

自动 retry 只增加有界的 `InductionAttemptRecord`，不会为每次瞬时失败制造
一个语义 outcome。达到上限才形成 `dead_letter` Receipt。Dead-letter 记录
不能伪装成归纳成功且没有 active publication；管理员重放通过经授权的
`InductionReplayRequest` 创建 `processing_generation + 1`，必须引用父
Receipt，不能改写旧 Receipt。唯一约束保证同一 Job 最多出现一个
`succeeded` 或 `excluded` 语义结果；重放成功后，旧 dead-letter 仍可审计但
不再是当前运行状态。不同 `induction_version` 的再归纳属于显式迁移，不是
普通 retry，并必须通过独立评审和 supersede 事件。

### 6. `MemoryQueryIntent` and resolved `MemoryQuery`

把现有 `memory.read` 改为两段严格合同。Agent Tool arguments 只能形成
`MemoryQueryIntent`，包含：

- `purpose`
- `memory_types`
- `selector_kind = concept_ids | item_handles | inventory_page`
- 与 selector kind 对应的 `requested_concept_ids`、短期 opaque
  `item_handles` 或 `inventory_cursor`
- 请求的 typed time scope

Agent 不能提交身份、绝对 `as_of`、授权、epoch 或提高预算。runtime 将合法
intent 编译成 resolved `MemoryQuery`，至少包含：

- `query_id`
- `episode_id`
- `plan_revision`、`plan_step_id`
- `invocation_id`
- `requesting_agent`
- `purpose` 枚举
- `memory_types`
- `selector_kind = concept_ids | item_handles | inventory_page`
- 与 selector kind 对应的 `requested_concept_ids`、精确 `item_handles` 或
  `inventory_cursor`
- `source_scope`
- `as_of`
- `max_items`
- `token_budget`

以下字段只能由 runtime 从 AuthenticatedBinding、Episode 和 AgentProfile 注入，
模型或客户端不能声明或覆盖：

- `actor_id`
- `subject_id`
- `actor_role`
- `authorization_scope`
- `fact_snapshot_id/hash`
- 当前 `privacy_epoch`、`authorization_epoch`、`retrieval_policy_epoch`
- 当前顶层认证用户 turn 的 `user_intent_ref/hash`（显式 Memory 管理时必填）
- 允许的 purpose、memory type、最大 item/token 上限

`as_of` 由 runtime 取服务端当前时间、FactSnapshot cutoff 和 Episode
SourceScope 的最严格交集；历史问题只能缩小 typed time scope，不能选择未来
时间或绕过 retention。Tool schema 对 actor/subject/role/as_of/epoch 等字段
`extra=forbid`，因此模型或客户端“自报同名字段”不是覆盖，而是整个调用拒绝。

合法 purpose 首版固定为：

- Evidence：`personal_evidence_context`
- SleepCare：`explicit_memory_review`
- SleepCare：`explicit_memory_change`
- SleepCare：`explicit_memory_forget`

`memory_types` 首版只允许 governed generic Memory 与 `EpisodeDigest`；
Habit Profile 不属于该 Tool 的 federated source。`profile.read` 继续使用既有
独立合同、purpose/role/concept gate 和 Receipt。Context assembler 若同一
Evidence plan step 同时接收 profile.read 与 memory.read，必须按 Agent/Episode
共享总 item/token ceiling 再做一次合并检查，不能让两个 Tool 各拿一份完整
预算。

CareStrategyAgent 不直接查询个人 Memory；SafetyReviewAgent 不发起 MemoryQuery，
只读取当前精确 review target 已引用且对 Safety 可见的最小信息。

`inventory_page` 只允许当前认证老人明确要求“查看系统记得什么”时由
SleepCare 使用。首页使用显式 sentinel 而不是 `all`，后续使用服务端签名、
绑定 subject/purpose/三个 epoch 的不透明 cursor；每页最多 8 items，采用
稳定顺序且不能自动翻页。它不能供 Evidence 使用，不能扩大 memory type，
也不能一次返回全集。显式 intent 只能由当前认证顶层用户 turn 建立，不能从
引用文本、历史 Memory、Tool output、Agent 建议或家属转述推导。需要修改/
遗忘时，下一步必须使用用户点名的 exact、短期 `item_handles`。

任一请求出现下列情况都在访问数据前拒绝：

- 缺少 purpose、合法 typed selector、SourceScope、`as_of` 或预算；
- selector 为空、为 `*`、`all` 或自由文本“相关内容”；
- target subject 与认证绑定不一致；
- purpose 与 Agent/Episode/plan step 不匹配；
- 请求扩大角色、时间、类型或 token 权限；
- 试图读取原始 Episode、完整 Prompt、完整 Tool payload 或审计 Store。

### 7. `MemoryReadReceipt` and returned slice

成功查询返回版本化 ToolReceipt 及最小 slice。模型可见 item 只带：

- invocation-bound `retrieval_handle`
- typed concept/event/outcome/provenance codes 和必要的 sanitized display value
- selection reason codes
- 非识别性的 source label
- valid/expired/conflict/superseded status
- source availability status
- allowed current use
- `expires_at`

持久化 `episode_id`、digest/item/source refs、lineage root、subject ID 和
source resolver 参数只存在 server-side Receipt/mapping，不复制进 Prompt 或
Tool arguments。Handle 随 invocation、subject、purpose、三个 epoch、TTL 绑定且
不可跨调用/角色使用；runtime 在执行 source resolver 时将 handle 映射为真实
ref。Receipt 记录请求 hash、策略版本、候选数量、过滤/选择 reason codes、
最终 item refs/hash 和最终序列化 slice 的 token 数，但常规日志不复制敏感
value。所有 Memory/Digest 文本值使用 data-only trust label 和结构化分隔，
不能作为指令。

Handle mapping 不是长期 Store：TTL 取 item 剩余有效期、invocation/explicit
review session 期限和 policy 上限中的最短值。invocation/session 终止、
任一 epoch 改变、item expire/supersede/forget/withdraw 时
立即清除 mapping 和缓存；任何 binding/epoch/TTL 不匹配都在解析真实 ref 前
拒绝。持久化查询 Receipt 可留 ref/hash 用于受限审计，但不能被 handle resolver
当作恢复 mapping 的来源。

### 8. Generic governed Memory

现有自由文本 `MemoryItem(value, source_ref, version, active, confirmed)` 不能继续
作为无类型、全量可读的个人事实源。首期采用带 discriminator 的版本化 union，
而不是给旧行填授权默认值：

- `LegacyMemoryItemV1` 精确表达现有字段，加载后固定
  `classification_status=legacy_unclassified`；
- `GovernedMemoryItemV2` 至少包含：
  - `memory_type`
  - `concept_id`
  - `value_schema_id`、`value_schema_version`
  - 经该 schema 验证的 `typed_value`
  - `value_hash`
  - `trust_label=USER_MEMORY_UNTRUSTED_DATA`
  - `provenance_type`
  - `recorded_at`
  - `valid_from`、`valid_until`
  - `sensitivity_class`
  - `allowed_roles`
  - `allowed_purposes`
  - `status`
  - `supersedes_ref`、`conflict_refs`
  - `confirmation_ref`
  - `retention_policy_version`

每个 concept 的 value schema 明确允许 enum/boolean/有界 number/有界字符串或
严格 object 中的哪一种、单位、长度与规范化；`typed_value` 不是 arbitrary
JSON/free-text 逃生口。确需保存老人原话的受支持 concept 必须使用有长度上限、
转义和 data-only trust 的专用 string schema，且不能仅凭该字符串进入当前
Evidence claim。

Habit Profile 继续使用自身 typed Store 和 `profile.read`，不复制到通用 Memory。
现有 guard 继续拒绝把 Habit 数据写入 generic Memory。

Store 读取显式按 schema version/discriminator 解析；缺少 discriminator 的
历史行只能被识别为 V1，不能猜成 V2。未分类的 legacy Memory 不自动做 LLM
或规则语义迁移：

- 默认不允许进入个人 Evidence 推理；
- 只允许老人通过显式 Memory review 查看带 data-only label、长度上限和转义
  的最小摘要，并选择确认、分类或遗忘；
- 不提供旧 `memory.read` 全量 fallback；
- 不从历史自由文本自动生成 Profile 或 Digest。

cutover 后 `memory.write`/Commit Controller 只允许创建 V2，且必须验证 concept/
value schema、provenance、confirmation、role/purpose、retention 与 source ref。
无法映射到已注册 concept/value schema 的自由文本请求返回 unsupported/需要
结构化澄清，不得继续写 V1，也不得伪装为通用 `note` 绕过治理。

## Episode-type/status induction matrix

| Episode type/status | Induction behavior | Retrievable result |
| --- | --- | --- |
| `complete` | 检查 accepted typed inputs；可输出 Digest、Profile candidate、SkillOutcome 或 exclude | 只有通过全部门的 Digest |
| `partial` | 记录运行失败/缺失成果；可输出最小 SkillOutcome | 无 Digest、无 Profile candidate |
| `blocked` | 记录阻断与 Safety/Policy reason codes；可输出最小 SkillOutcome | 无 Digest、无 Profile candidate |
| `waiting_user` | Episode 未终止，不创建归纳 Job | 无 |
| `waiting_confirmation` | Episode 未终止，不创建归纳 Job | 无 |
| EpisodeType=`urgent_boundary`（当前 status=`complete`） | Job 必建并确定性创建 `excluded` InductionReceipt；业务/安全审计继续独立保存 | 无普通情景记忆 |

`data_quality_recovery` 等确定性 Episode 即使状态为 complete，也必须存在足够的
accepted typed input 才能产生 Digest；否则确定性 `exclude`。
上述矩阵只决定派生物，不决定是否建 Job：所有 terminal result revision 均
建 Job。若同一 Episode 后续追加 terminal revision，只有最高且仍获授权的
revision 可以保持 active Digest/候选；旧 revision 的派生物通过追加
supersede 事件失效。

## Deterministic induction rules

1. Induction worker 只按 Job 中的 `input_manifest_ref/hash` 读取不可变
   Manifest，验证 projector/schema/hash、terminal result identity/revision 和
   Manifest TTL；它不读取或反序列化 ProductEpisodeRunResult。发现更高 terminal
   revision 时仍为本 revision 产 Receipt，但不得让旧派生物重新 active。
2. 重新检查当前撤权、遗忘、删除请求和更严格的隐私 revocation overlay；命中
   时排除相应派生物，不能因 Job 锁定旧策略而继续暴露。Job、Receipt 与派生
   事务绑定检查时的 `privacy_epoch/authorization_epoch`，提交前再做 CAS
   检查；epoch 已变化则丢弃中间结果并按新状态重算或 exclude。
3. Manifest 输入 allowlist 仅包括：
   - accepted work-product refs 与各 kind 显式列出的有限 typed fields；
   - ToolReceipt identity、版本、outcome、source class 和允许的质量元数据；
   - confirmation refs；
   - Safety verdict/reason codes；
   - Episode status/type、FactSnapshot/SourceScope refs；
   - 已确认的结构化 feedback event refs。
4. 不向新的模型发 Prompt，不解析自由文本寻找事实，不读取内部 reasoning。
5. Profile candidate 只能复制/规范化已经由上游 typed contract 表达且满足持久化
   资格的候选；Induction 不创造新 concept 或猜测用户偏好。
6. 多个派生物保留同一 lineage；重复处理按唯一键返回相同 hashes。后续
   terminal revision 或获批的新 induction version 只能通过追加 supersede
   事件切换 active 派生物，不能覆盖旧记录。
7. 任何 Schema、来源、权限、有效期、敏感度或大小检查失败都 fail closed 为
   `exclude` 或 typed failure，不生成“尽量可用”的自由文本 Digest。
8. 终态 Result、Manifest 和 Outbox Job 必须在 API 确认完成前持久化；Digest
   处理不阻塞用户回复。归纳失败不改变已发布内容或 Episode truth。
9. Worker 有有限重试、指数退避、lease 超时恢复和 dead-letter 告警；禁止无限
   循环或未经人工处置的永久重放，且 retry/dead-letter 不能延长 Manifest TTL。
10. Digest、候选、supersede 状态和 Receipt 只有在同一提交成功后才同时可见；
    处理中间文件或行记录永不进入查询结果。Profile candidate 只能写入隔离的
    pending-candidate repository；该 repository 不是真值 Profile Store，且
    worker 不持有 Commit Controller 能力。

## Profile write boundary

1. 当前认证老人最外层消息中明确的“记住、修改、忘记”继续走现有 SleepCare
   Memory candidate、精确确认/授权和 Commit Controller 路径。
2. Episode 归纳、设备观察、用户行为或模型推断只能形成 pending candidate，
   永远不能代替老人确认。Phase 1 对跨 Episode 重复只做 exact lineage dedupe
   与受限审计 repeat count；repeat count 不进入 confidence、优先级、检索、
   问题选择或任何产品行为。
3. 未确认 `ProfileChangeCandidate` 默认 30 天过期；过期后不得套用旧确认。
4. 设备测量、趋势、覆盖和个人基线继续属于带时间窗/版本的数据或 Artifact，
   不转写成无来源的自然语言画像。
5. 疾病、完整用药、急症和临床风险进入独立 Clinical/Safety Context，不混入
   Habit Profile；需要长期保存时走独立治理和确认。
6. 家属观察保持 `observer_reported` provenance，不能变成老人自述；一般家属
   授权不获得老人画像提交权。
7. Commit Controller 仍是 Profile/generic Memory/Care State 的唯一真值写
   路径；Induction worker 只能写 pending-candidate repository 和归纳
   Store，没有上述真值 Store 或 Skill Registry 的直接写权限。只有后续
   SleepCare 显示 exact change set、老人精确确认并重新验证资格后，Commit
   Controller 才能把候选提交为真值。
8. Pending candidate 在确认前不能进入 Evidence/Memory/Profile retrieval，
   不能影响排序、提问优先级、Care、Reminder、Safety、Communication 或任何
   个性化行为。它只能在老人主动 explicit Memory review，或既有 Habit
   Profile 流程已经因独立 DecisionGap 合法进入确认步骤时显示；不得自行创建
   新通知、新 Episode 或“顺便确认”问题。重复候选只做 lineage/dedupe，不提高
   权限或自动转真。
9. 显式查看 pending candidate 使用独立的 candidate-review service/Receipt，
   要求同一当前顶层老人 intent，并明确标记“尚未保存”；它不并入
   `memory.read` 或 `profile.read` 的推理结果。老人选择某项后，SleepCare
   才能为该 exact candidate 构造既有 change set/confirmation。

## No personalized L3

1. 不建立 per-user SOP、private Skill、自由文本流程 Memory 或个人 Prompt。
2. 老人的偏好、负担、可接受提醒方式进入 typed Profile；当前行动与跨天状态
   进入 Care State。
3. 共享方法继续由 Skill、Care catalog 和 EpisodeDefinition 承担。
4. 单个 Episode 只形成 SkillOutcome，不形成可上线 procedure candidate。
5. 未来共享 Skill PATCH 必须由既有离线控制面基于去标识化 cohort、root-cause
   分类、固定输入 replay、单变量 ablation、完整评测和人工审批产生。
6. 生产内 `SkillOutcome` 可以保留受控审计 lineage，但离线出口只能发送
   allowlisted `OfflineSkillOutcomeEnvelope`：包含 Skill/package/version、
   typed status/root-cause/reason codes、Episode type、时间桶和必要评测维度；
   不包含 subject/actor ID、直接 episode ID、input/output/source refs、自由
   文本或可逆的生产 Store key。若 cohort 去重确需稳定键，只能由隔离服务生成
   限期、按研究目的加盐的 HMAC token，离线消费者不能取得输入键。
7. Offline envelope 在导出前检查当前 eligibility/privacy epoch；forget、
   withdraw/delete 必须阻止尚未导出项，并向允许持有的离线集合传播撤回
   tombstone。无法满足撤回合同的离线接收方不得接入。Phase 1 接收方只允许
   append、read 和按 tombstone 重建 eligible view，不得训练模型、生成 PATCH/
   candidate、更新不可逆 aggregate 或消费为生产决策；因此本计划不声称能从
   已训练模型中“删除贡献”。
8. 任何未来聚合、训练或 PATCH 都必须另立计划，定义 consent/retention、
   purpose-specific cohort key、withdraw 后数据集重建或适用的 unlearning
   边界，并重新批准；不能把 Phase 1 的只读接线视为授权。
9. 本轮只持久化/导出上述最小 Outcome，不实现 Failure Miner、Candidate
   Generator、自动审批、shadow/canary 控制器或自动晋级。

## Retrieval and epistemic boundary

### Agent access

| Agent | Direct longitudinal read | Boundary |
| --- | --- | --- |
| EvidenceReasoningAgent | typed Profile slice、governed Memory、EpisodeDigest | 仅当前个人证据目的；必须重验证原始来源 |
| SleepCareAgent | 显式查看/修改/遗忘请求所需最小 slice | 不能用 Digest 自行形成个人事实 |
| CareStrategyAgent | 无 | 只消费 accepted Evidence 和 Care State |
| SafetyReviewAgent | 无新查询 | 只看精确 review target 及已有 refs |

### Deterministic retrieval

1. 在任何相关性计算前完成 subject、role、purpose、type、SourceScope、授权、
   sensitivity、有效期、withdraw/forget/supersede、terminal revision 和
   retention 硬过滤。
2. 首版不使用 embedding、向量数据库、自由 query expansion 或 LLM reranker。
3. 过滤后只用索引中的非内容 resolver capability/availability code 按固定顺序
   排序；不能为计算 rank 打开 canonical source payload：
   - exact purpose 与 Digest `concept_ids`/event/outcome codes 或 item ref match；
   - 当前原始来源可重验证；
   - 与 Episode type/SourceScope 精确匹配；
   - confirmed/active 且无未解决冲突；
   - `valid_from`/`terminal_recorded_at` 的时效；
   - 稳定 digest/item ID。
4. 冲突信息不能被静默丢弃；如果冲突与任务相关，所有仍合法的成员组成 atomic
   conflict group，按一个选择单位排序，但每个成员分别计 item/token budget。
   完整 group 放不下时两侧都不返回，并记录 `conflict_group_omitted_budget`；
   绝不只给 Evidence 一侧。
5. 同一 lineage 的多条 Digest 去重，不能因重复派生人为提高置信度。
6. 默认总上限为 8 items（conflict group 的成员逐个计数）；实际
   AgentProfile/Episode 可以进一步收紧。
7. token budget 是硬上限；使用锁定 tokenizer 对最终模型可见的规范化序列化
   slice 计数。溢出时按确定性顺序减少完整 items，不截断成可能改变语义的
   自由文本片段；单个 item 已超限时以 `item_oversize_omitted` 排除并继续，
   不提高预算。
8. 无合法结果时返回空集及 reason code，不扩大查询范围。
9. 未来语义检索只能在硬过滤后的候选集内重排，并作为独立计划重新评测、
   审批和版本锁定。
10. 查询结果和缓存键绑定当前 privacy/authorization/retrieval-policy epoch。Context
    assembler 在每次 provider 调用前、SleepCare 发布前再次检查 epoch、TTL
    和 item 状态；任一变化都丢弃 slice 并返回 typed stale/empty 结果，不能
    把查询时合法当成持续授权。
11. Provider adapter 在实际 dispatch 前对完整序列化 request（包括所有消息、
    Tool result 和重试历史）重新计数并验证 Agent/Episode 总预算；Receipt 中
    的 slice token 不能代替这一最终检查。
12. 排序完成后只对最终有界 selection/conflict groups 调用 canonical source
    resolver；未选候选不会触发源读取。任一成员重验证失败时整个 conflict
    group 从当前 claim 输入移除，不能退化为单侧。

### Source revalidation

1. Digest 只告诉 Evidence “历史上存在某个已验收成果及其来源引用”。
2. Evidence 必须通过授权的 canonical source resolver/Tool 重新读取仍存在、
   仍有权、仍在有效期内的 Evidence ledger、Profile/Care version 或原始设备/
   服务数据，并取得新的 ToolReceipt，才能形成当前 claim。
3. `ProductEpisodeRunResult` audit reader、其中保存的旧 Tool output、Agent
   payload 或旧 ToolReceipt 不能实现 canonical source resolver，也不能因为
   Evidence 持有一个 Digest ref 就解包给模型。
4. canonical 来源不可用时，只能基于 Digest 的有限 code/ref 形成带时间限定
   的历史过程陈述，不能恢复被删除 payload，也不能宣布当前状态。
5. Digest 不得单独支持因果、诊断、风险升级、Care 行动或对外材料。
6. 工具输出同样只是 evidence-producing input；Evidence gate 必须检查来源、
   时间、质量、校准、局限、替代解释和反证。

## Retention, correction and deletion

1. Active EpisodeDigest 默认可检索 90 天；每次读取不刷新期限。
2. 未确认 ProfileChangeCandidate 默认保留 30 天。
3. 期限由版本化 `DataRetentionPolicy` 管理；更严格的法律、授权或产品政策可
   缩短，不能静默延长。
4. 已确认 Profile 与 Care State 按自身有效期、supersede 和生命周期管理，
   不依赖 Digest 续命。
5. 过期任务必须以 CAS/追加状态事件把 Digest 标成 `expired`，并同步删除查询
   索引和缓存；不得只在 UI 隐藏。
6. 允许的忘记、撤回授权和数据删除传播到派生 Digest、候选、索引、缓存和
   future SkillOutcome eligibility。
7. 必须保留的审计记录按独立规则限制访问；它不恢复 Digest 或模型可见内容。
8. 来源 correction/supersede 会使依赖 Digest 失效；需要新 Digest 时创建带
   lineage 的新版本，不原地改写旧来源或伪装历史从未发生。
9. 存储/索引清理提供 receipt 和可观测计数，不在普通日志记录敏感值。
10. 首期不自动回填历史 EpisodeDigest；cutover 前的原始结果维持审计-only。
    未来回填需独立授权、迁移评审和纵向隐私基准。
11. 所有期限使用 UTC 的权威服务端时间：Digest TTL 从 terminal result 的
    `terminal_recorded_at` 起算，候选 TTL 从 `candidate_created_at` 起算。
    暂停、读取、retry、dead-letter 或服务停机均不刷新期限。
12. Forget/withdraw/delete/correction 每次递增 subject-scoped privacy epoch；
    查询快照、cursor、缓存、worker lease 和离线 eligibility 都绑定 epoch。
    旧 epoch 的在途结果即使已完成计算，也不能进入 provider、发布或导出。
13. 每个 Digest status event 带单调 `status_sequence` 和 expected prior
    status/hash；CAS 冲突后从事件流重算。`forgotten/withdrawn` 具有最高且
    不可逆的隐私优先级，`expired/superseded` 也不能回到 active。授权恢复或
    correction 不复活旧 Digest；若仍有合法来源，只能由新的 terminal result
    或独立获批再归纳产生新 digest/hash/lineage event。

## Persistence, authorization and observability

1. 复用现有 Product Agent 数据库和 repository factory；使用 additive migration，
   不建立第二数据库或文件型 Memory。
2. SQLite 只用于受控本地测试；生产并发语义以 PostgreSQL 唯一约束、事务、
   lease/CAS 和行级一致性为准。
3. Terminal bundle（Result + Manifest + Outbox Job）创建、Job lease、派生物
   提交、过期/撤权、dead-letter 重放都有明确事务边界和幂等键。
4. Worker 和查询服务每次操作都从可信 runtime 获取 actor/subject/binding，
   不接受浏览器、模型、Digest 或 Tool payload 自报身份。
5. Manifest 只存在受保护的 Product 数据库，不落地到 JSON/临时文件、普通
   日志、trace 或分析导出；使用部署环境既有的传输/静态加密，并以可按
   Manifest TTL 销毁的 envelope key 保护 payload。Worker 使用只读 Manifest
   + 限定归纳写入的独立最小权限身份，不能访问 raw result 表。TTL purge
   删除在线 payload/key；备份要么在同一期限删除，要么因 key 销毁而不可恢复，
   并生成不含值的 purge Receipt。缺少这些部署控制时只能本地/影子测试。
6. 查询审计至少记录 request hash、策略/Schema 版本、Agent、purpose、允许/
   拒绝、selected refs/hash、item/token 数和 reason codes。
7. 常规 telemetry 只记录：
   - Outbox lag、重试、dead-letter；
   - Digest 创建/exclude/过期/撤回数量；
   - Query allow/deny、返回 item/token 分布；
   - source revalidation 成功/失败；
   - 实际上下文 exposure；
   - lineage 去重和 conflict 命中。
8. 常规 telemetry 不记录原始对话、画像值、完整 Tool 输出或 Digest 内容。
9. 隐私 revocation、删除和紧急停用优先于旧 Job/Query snapshot；旧授权不能
   因 replay、恢复或长任务继续生效。
   Digest kill switch 必须单调递增 `retrieval_policy_epoch`，同步清除 Digest
   handle/cursor/cache；query、handle resolver、provider dispatch 和 publication
   均重检该 epoch。普通配置回滚不能降低 epoch 或保留在途 slice。
10. result repository 暴露单一 `append_terminal_bundle` 事务接口：terminal
   Result、InductionInputManifest 与 canonical Job 三者同成同败；非 terminal
   waiting result 使用只追加的非终态接口。任何持久化实现（包括测试 double）
   都不能让 runner 先调用旧 `append` 再“尽力”补 Manifest/Job。
11. API 不等待 induction worker，但在向调用方确认该次 terminal run 已完成前，
    terminal bundle 必须持久化成功。现有 publication/external-action 的
    exactly-once 问题不由 Induction 假装解决；实现必须保留既有
    idempotency/journal 语义，并对“副作用已发生但 terminal bundle 提交失败”
    做显式故障测试和恢复记录。
12. 当前 runner 存在 publisher 先于 `_store` 的路径。任何这类 route 在生产
    cutover 前必须具备持久化的 idempotent publication intent/delivery journal
    或等价的可恢复 reservation，能在 terminal bundle 提交失败后识别已发生副
    作用并安全对账；没有该能力的 route 只能保持 Digest shadow/off，不能启用
    governed retrieval。不得通过重复发送回复来“补齐”审计。
13. mixed-version rollout 期间，数据库约束/受控写入 gateway 必须拒绝
    `receipt.terminal=true` 但没有同事务 canonical Job + Manifest 的新写入；
    writer 声明并被校验最低 schema/projector version。旧 runner replica 全部
    fencing/下线且 orphan scan 为零后，才能启动 shadow worker。不能依赖应用
    团队“同时部署”来保证原子性。

## Implementation approach

1. **Freeze current behavior and migration boundary**
   - 为当前全量 `memory.read` 建立失败复现测试。
   - 记录现有 ProductEpisodeResult、Habit Profile、generic Memory、Care State、
     ToolReceipt 和 SkillOutcome schema/version。
   - 明确 cutover 后禁止旧全量 handler 和历史 Episode 自动回填。
   - 枚举所有 publisher/external-action route 的 result-store 顺序和现有
     idempotency journal；缺少可恢复 reservation 的 route 标为 cutover
     blocker，而不是留到上线后观察。

2. **Add strict contracts**
   - 增加 EpisodeDigest/StatusEvent、InductionInputManifest、
     InductionJob/JobEvent/Receipt、
     MemoryQueryIntent/resolved MemoryQuery/MemoryReadReceipt、
     InductionAttemptRecord/ReplayRequest、OfflineSkillOutcomeEnvelope、
     DataRetentionPolicy 和所需 enum/trust label。
   - 以 LegacyMemoryItemV1/GovernedMemoryItemV2 discriminated union 扩展
     generic Memory；保持 Habit Profile 独立权威。
   - 所有合同拒绝未知字段、绑定稳定 hash 和版本。

3. **Add additive persistence**
   - 增加 Digest、Outbox Job、Induction Receipt、查询审计、retention/correction
     事件表和唯一约束。
   - 实现 PostgreSQL 事务/lease/CAS；本地 SQLite 实现等价可测试语义。
   - 将每个 terminal result revision 与 allowlisted InputManifest、canonical
     Outbox Job 原子保存，并以 supersede 事件管理同 Episode 的后续 terminal
     revision。
   - 统一 canonical result hash/result ID、receipt revision 唯一约束和
     transaction-owned `terminal_recorded_at`；Digest/Job 状态由追加事件重建。

4. **Implement deterministic induction worker**
   - 按 Episode 状态矩阵、输入 allowlist 和 privacy overlay 构建派生物。
   - 实现幂等消费、有限重试、lease recovery、dead-letter、处理 generation
     和带父引用的人工重放 receipt。
   - worker 只获 Manifest Store 权限；用 poison/sentinel 测试证明 projector/
     worker 无任何 LLM/provider 调用或 raw conversation/tool payload 访问，
     并验证 7 天内 retry、payload purge 和 late replay 拒绝。

5. **Replace broad `memory.read`**
   - 保留单一 Tool Registry，升级现有 tool contract，不建立 parallel legacy
     fallback。
   - runtime 从当前顶层认证 user intent 注入 binding/purpose/as_of/三个 epoch/
     budget 上限，验证 plan-step causality；持久化 refs 只映射为短期 handles。
   - `memory.read` 只接入 governed generic Memory 与 Digest Store；Habit
     Profile 保持独立 `profile.read`，不被 federate/复制。Context assembler
     对多个 read Receipt 执行共享总预算。
   - legacy untyped Memory 默认只允许显式、受限、稳定分页的 inventory
     review；修改/遗忘必须转为 exact、短期 item handles。

6. **Enforce Agent visibility and Evidence revalidation**
   - 更新 Agent/Tool allowlist、Context assembler 和 trust labels。
   - Evidence 可以消费 hint refs，但只有通过 canonical source resolver
     重新取得的 authorized source receipt 能进入 Evidence claim；audit
     reader 明确不注册为 resolver。
   - Care/Safety/SleepCare 按已锁定矩阵拒绝越权读取。

7. **Route profile candidates and SkillOutcome**
   - 将合格的 typed Profile candidate 接回现有 change set、确认和 Commit
     Controller，不新增写路径。
   - 归纳只写 pending-candidate repository，现有确认/Commit Controller 是
     唯一真值写路径。
   - candidate-review service 只响应当前老人显式 intent，返回标记为未保存的
     exact candidates；不向 Evidence/Profile/Memory reasoning 暴露。
   - 将 allowlisted OfflineSkillOutcomeEnvelope 送入隔离、只追加且支持撤回
     tombstone 的离线出口；生产 Registry 无写权限。

8. **Implement retention and revocation propagation**
   - 增加 90/30 天策略、定时过期、withdraw/forget/supersede、索引/缓存清理
     和 receipts。
   - 用 subject-scoped privacy epoch 绑定 Job、query、cursor、cache 和离线
     eligibility，并用 retrieval-policy epoch 绑定 Digest read/handle；测试
     provider 调用/发布前竞态与 kill switch，更严格 revocation 必须获胜。

9. **Build longitudinal benchmark and security suite**
   - 覆盖最新状态、supersede、习惯偏好、Care 反馈、冲突、陈旧数据、数据缺失、
     家属/医生边界、跨老人攻击、撤权/遗忘、Prompt injection、Outbox 重试/
     dead-letter、进程恢复和并发。
   - 只在隔离、冻结的 synthetic 或另行 consent/de-identification 合格的
     structured fixtures 上运行 current-only、full-history、governed
     retrieval；harness 不具备生产 audit reader 凭据。
   - full-history 仅表示截至同一 cutoff 的全部 authorized、unexpired fixture
     items，不含 raw 对话、raw Episode、撤权/忘记数据或越权角色数据；恶意/
     禁止项另在安全套件验证三种条件都不会读取。
   - full-history loader/handler 只存在 evaluation-only package/process，没有
     生产数据库凭据，不注册到 Product Tool Registry，也不进入生产构建产物；
     build/manifest test 对其模块、route、tool name 和依赖做否定断言。
   - 固定同一模型、参数、时间截止、数据、rubric、tokenizer 和统计方案；累计
     每个 Episode 的所有 provider model-input tokens，并分别报告关键场景。

10. **Cut over fail closed**
    - 先迁移 schema/gateway，部署新 writer/projector，fence 旧 writer，并证明
      新 terminal orphan Result/Job/Manifest 均为零。
    - 再在测试/影子环境生成 Digest，不向模型返回。
    - 通过全部硬门后启用小范围 governed retrieval。
    - 任一隐私/权限/来源硬门失败，或 affected publication route 没有可恢复
      reservation，立即递增 retrieval-policy epoch、清空 handle/cursor/cache
      并关闭该 route 的 Digest read；不回退全量 Memory。
    - 保留原始 Receipt 和修正路径，完成管理文档与停用演练。

## Acceptance criteria

### Contract and lifecycle

1. 每个不可变 terminal result revision 恰有一个 canonical Job；自动 retry
   不会产生不同 Digest、候选或 SkillOutcome，且最终得到 succeeded/excluded
   或可见的 dead-letter Receipt。
2. 同一 Episode 后续 terminal revision 会使旧 revision 的 Digest/候选失效，
   不会双计 SkillOutcome；waiting result 不创建归纳 Job；urgent/partial/
   blocked 的 terminal result 必建 Job，但不产生可检索 Digest 或 Profile
   candidate。
3. Digest 不含原始用户消息、完整 Tool payload、完整 work product、思维过程或
   强身份字段；immutable payload hash 在 expire/forget/supersede 后不变，
   生命周期只通过追加 status events 改变。
4. Terminal bundle、派生物 + Receipt、过期/撤权 + 索引清理满足事务和恢复测试。
   canonical result ID/hash、receipt revision 唯一约束和 transaction-owned
   `terminal_recorded_at` 在重复提交/恢复后保持一致；Result + Manifest + Job
   三者同成同败。Manifest 不落普通文件/日志/导出，worker 无 raw-result
   权限，在线与备份 payload 在 TTL 后删除或因 envelope key 销毁而不可恢复。
5. dead-letter 明确可见且不能被读取为成功结果。
6. 人工重放产生更高 processing generation 和 parent Receipt ref；不改写旧
   dead-letter，同一 canonical Job 最多有一个 succeeded/excluded 语义结果；
   Job/Digest mutable projection 可从完整事件流重建且不一致时 fail closed。
   Manifest 最长 7 天且不被 retry 刷新，purge 后 replay 不读取 audit。

### Authorization and minimization

7. `memory.read` 缺少任一必填范围或出现 wildcard/all 时在读 Store 前拒绝；
   显式 inventory review 只能由当前认证顶层 user intent 触发、稳定分页且
   不能自动翻页；模型提交 actor/subject/as_of/epoch 等 runtime 字段会被拒绝。
8. 跨 subject、跨 role、跨 purpose、跨 SourceScope、过期、撤权、遗忘和
   superseded item 返回数均为零。
9. Evidence 是唯一能为个人解释直接查询 Digest 的 Agent；Care 无直接读，
   Safety 不能扩 target，SleepCare 只在显式记忆管理时读取。
10. Context/ToolReceipt 中不存在全量 active Memory fallback；`memory.read`
    不 federate Habit Profile，profile.read 与 memory.read 即使并用也受同一
    Agent/Episode 总预算。
11. 每个模型可见 item 都有短期 retrieval handle、selection reason、非识别性
    source label、有效期、conflict 状态和当前允许用途；persistent item/
    episode/source refs 与 lineage 只留在 server-side Receipt。
12. 查询后发生 forget/withdraw/epoch 变化时，旧 slice 在 provider 调用和
    发布前被拒绝；旧 handle/cursor/cache 不能复活内容。单个 oversized item
    被完整排除而不截断或加预算，最终完整 provider request 仍通过总预算检查。
    Handle mapping 在 invocation/session/epoch/item 失效时清除，不能从审计
    Receipt 重建；Digest kill switch 后旧 query/handle/in-flight provider
    request/publication 全部因 retrieval-policy epoch 失配而失败。

### Epistemic safety

13. Digest 单独存在而 canonical 来源不可用时，系统不能形成当前个人事实、风险或
    Care 行动。
14. audit reader 不能作为 canonical source resolver，旧 Tool payload 不会因
    Digest ref 被解包进模型上下文。
15. 同 lineage 重复 Digest 不增加置信度；相关冲突不能被排序器静默隐藏。
    相关 conflict group 必须完整通过 item/token/source revalidation 门，否则
    整组不进入模型，不能只返回单侧。
16. Tool evidence、Memory 和 Digest 均不能改写 Policy、Skill、Schema、路由
    或权限。
17. Profile candidate 未经老人精确确认不能提交；重复 Episode 不绕过确认。
    Pending candidate 在确认前对推理、排序、Care、提醒和主动 Communication
    完全不可见，也不会自行触发问题或 Episode；repeat count 只供受限审计，
    不改变 confidence、priority 或行为。
18. Induction worker 和离线 Outcome 出口均不能写生产 Skill Registry。
19. OfflineSkillOutcomeEnvelope 不含直接个人/episode/source refs 或自由文本；
    不符合撤回合同的离线接收方无法接入；Phase 1 接收方不能训练、生成
    candidate/PATCH、更新不可逆 aggregate 或驱动生产决策。

### Retention and migration

20. Digest 90 天、未确认候选 30 天按 UTC 服务端时间和版本化策略过期；读取、
    retry、暂停和停机不刷新 TTL。
21. Forget/withdraw/delete 能清除允许清除的 Digest、候选、索引和缓存，并
    保留准确的操作范围说明；并发 status events 服从单调 sequence/CAS 和
    privacy precedence，任何旧 Digest 都不能重新 active。
22. 缺少 discriminator 的 legacy Memory 只能加载为
    `LegacyMemoryItemV1/legacy_unclassified`，与 cutover 前 Episode 一样不会
    自动进入个人推理或被默认字段授权。cutover 后所有新写入必须是 concept-
    bound、schema-validated 的 GovernedMemoryItemV2；未知自由文本不能新建 V1
    或通用 note。
23. Habit Profile 仍只有既有 typed Store/Commit Controller 写路径。

### Longitudinal comparative gate

24. current-only、full-history、governed retrieval 只在无生产 audit 权限的
    隔离冻结 harness 中使用同模型、参数、任务、时间截止和评分规则；数据只
    来自 synthetic 或另行 consent/de-identification 合格的 structured fixtures。
    full-history loader 在生产 Tool Registry、route manifest、依赖图和构建
    artifact 中均不存在。
25. exposure 使用锁定 tokenizer，按 Episode 累加每一次实际 provider 请求的
    system/developer/user/assistant/tool 消息、Memory/Digest slice、工具结果
    和重试输入 token；不能通过搬到 Tool、增加调用或只取最后一次 Prompt
    来排除不利部分。
26. 跨老人、跨角色、未授权披露为零。
27. 过期、已忘记、已撤权内容召回为零。
28. 生产路径及 current-only/governed 条件中，原始 Episode、完整对话或全量
    Memory 进入 Prompt 为零；隔离 full-history 条件只使用截至 cutoff 的全部
    authorized/unexpired structured fixture items，仍不得使用 raw audit/dialogue。
29. governed retrieval 的每 Episode 累计上下文 exposure 相对 full-history
    的预注册汇总指标至少下降 50%，并报告尾部与关键场景而不只报告平均。
30. 在冻结测试集和预注册主质量指标上，`full-history - governed` 的单侧
    95% 置信上界不超过 5 个百分点，且 `governed - current-only` 的单侧
    95% 置信下界大于 0；Safety、跨角色、冲突、撤权/遗忘等关键分层不得出现
    预注册的实质回退。样本或统计证据不足视为不通过。
31. 现有 Evidence、Safety、确认、Care、Habit Profile、Product Agent 和隐私
    验收无回退；publisher 先于 result-store 的 route 若没有可恢复、幂等的
    intent/delivery journal，不得生产启用 Digest read。mixed-version rollout
    中旧 writer 被 fencing，数据库/gateway 拒绝 terminal orphan，orphan scan
    为零后才能启用 shadow worker。
32. 合成/离线结果只用于工程发布判断，不宣称真实老人获益或临床有效性。

## Key decisions & tradeoffs

1. **借语义职责，不借五层物理结构。** 保留多个既有权威 Store/Registry，
   牺牲“五层目录”的展示整齐，换取责任、权限与迁移一致性。
2. **原始 Episode 审计与 Digest 分离。** 增加派生表、lineage 和清理复杂度，
   换取原始敏感轨迹不进入未来 Prompt。
3. **Digest 只是 hint。** 每次当前判断需要重新验证来源，增加 Tool 调用和
   延迟，换取不让旧结论或错误记忆自我强化。
4. **首版归纳与检索均完全确定性。** 放弃 LLM 总结与向量召回的灵活性，换取
   可重放、可解释、可最小化和更清晰的隐私边界。
5. **异步 Outbox。** 接受刚结束 Episode 的 Digest 短暂不可见，换取用户路径
   不被归纳失败阻塞以及可靠恢复能力。
6. **画像只产候选。** 增加确认负担，换取避免把模型推断、设备噪声或重复错误
   固化成老人事实。
7. **没有个人 L3。** 个性化留在 Profile/Care State，共享行为留在 Skill，
   放弃“每个老人自带 SOP”的展示效果，换取权限、评测和回滚清晰。
8. **90 天 Digest + 30 天候选。** 可能失去低频历史细节；真正长期有价值的
   内容需经确认进入 Profile，而不能靠无限 Episode 堆积。
9. **首期不扩设备/模型/入口。** 延后统一平台能力，换取先关闭已存在的全量
   Memory 暴露缺口并得到可验收的最小增量。
10. **用自己的纵向基准。** 增加场景和评测成本，避免把 HealthClaw 的合成、
    自动评分、非严格消融或不完整 exposure 指标误当成发布证据。

## Risks / open questions

1. 原始 Episode 审计记录的法定/合同保留期需由独立数据治理方案确定；无论
   结果如何，都不得扩大模型访问。
2. 当前 runner 的结果持久化、用户发布和外部动作顺序需在实现前画出精确事务
   时序，保证 terminal bundle 已可靠保存而不重复发布外部动作。
3. 现有 generic Memory 的真实生产数据量与内容类型未知；首期按 fail-closed
   legacy review 处理，迁移工作量需只读盘点后估算。
4. 现有 SkillOutcome 只有基础合同，离线接收方、访问控制与数据撤回传播接口
   需在实现阶段选用现有基础设施，但不能因此扩大首期为完整进化平台。
5. 纵向任务质量的统计样本量、置信区间方法和人工/领域复核人数需在生成候选
   数据前预注册；50% exposure 和 5 个百分点非劣门槛保持不变。
6. Outbox lag、查询延迟和 source revalidation 的 SLO 需在影子阶段测量后冻结；
   功能/隐私硬门不因 SLO 压力放宽。

## Out of scope

1. 修改现有四责任角色名单或用 HealthClaw 替换所谓“1+2+1”。
2. 新建 MemoryAgent、EvolutionAgent、个体 SOP Store 或个人 Skill。
3. 自动更新 L0/L1、GlobalPolicy、Knowledge、Skill、Care catalog、Schema、
   权限、Tool allowlist 或 Agent 路由。
4. HealthClaw 的饮食、慢病、医学影像、生信、诊断或 benchmark 特化任务。
5. HealthClaw 仓库的文件 Memory、自动侧车画像/SOP 写入、StrategyDistiller、
   SelfEvaluator 或跨设备 HTTP/JSON bindings 实现。
6. 新设备协议、跨设备绑定、模型路由、ModelProfile 完整资格、前端入口或 UI。
7. 向量数据库、embedding、LLM query expansion、LLM reranker 或自由文本
   Episode 总结。
8. Failure Miner、Candidate Generator、自动评测/审批、自动 shadow/canary、
   自动 Skill 晋级或回滚平台。
9. 对 cutover 前 Episode 或 legacy Memory 做自动 backfill/语义分类。
10. 用合成 benchmark 宣称临床效果、真实老人获益或替代医生判断。
11. 本轮编写或修改任何实现代码。
