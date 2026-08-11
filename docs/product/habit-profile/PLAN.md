# Plan: SleepAgent 渐进式睡眠习惯画像
_Locked via grill — by Codex + user_

## Goal

在已经锁定的 SleepAgent 产品定位、信息架构和 `SleepCareAgent + EvidenceReasoningAgent + CareStrategyAgent + SafetyReviewAgent` 四角色架构内，将附件中的一次性“睡眠习惯自查清单”改造为低负担、可跳过、按需补充的内部结构化睡眠习惯画像。画像只为个性化解释和经确认的低风险照护提供上下文，不生成综合健康分、固定人群标签、疾病筛查结果或诊断结论；即使大部分字段未知，晨间解释、趋势问答和照护流程仍必须正常工作。

## Inherited constraints

1. 居家老人是第一用户，家属和医生是经授权的协同角色；用户只面对统一的 SleepAgent。
2. 用户自述、家属观察、雷达数据、一般知识和未知项必须保持来源区分，不能相互冒充。
3. 个人基线、覆盖率和趋势是带时间范围、数据版本和质量信息的分析 Artifact，不是无来源的长期记忆。
4. `SleepCareAgent` 是唯一对话入口；个人 Evidence 由 `EvidenceReasoningAgent` 形成；具体行动由 `CareStrategyAgent` 形成；危险信号和高风险成果遵守确定性规则及既有 Safety 路径。
5. Agent 没有长期状态直接写权限。任何长期画像写入、更正或遗忘都经既有 Memory Service、确认规则和 Commit Controller。
6. 复用现有版本化 Questionnaire Bank、Policy、Selection、Evidence Ledger、ToolReceipt、目标 `product_agent` Memory/Commit Controller 和审计链，不建设平行问卷运行时或第五个“画像 Agent”。
7. 现行 `radar_agent.memory.OrchestratedMemoryWriter` 的 family-only 确认和旧 `product_agent` 自由文本 `MemoryItem` 都只是迁移基础，不是画像权威合同；实施必须扩展目标 `product_agent` 合同并删除冲突路径，不能让两套长期画像写入并存。

## Architecture freeze artifacts

本计划的架构语义已经由以下四个冻结附件具体化；附件与本计划共同构成后续工程实现的输入：

1. [`V1-CONCEPT-COVERAGE.md`](V1-CONCEPT-COVERAGE.md)：冻结 v1/v1.1/unsupported concept、上下文归属、回答者、持久化资格、触发器和决策影响。当前 Python `DEFAULT_HABIT_CONCEPTS` 只是冻结前工程种子，缺少的 v1 concept 是明确工程增量，不反向缩小架构范围。
2. [`HABIT-SKILL-TOOL-ROUTING.md`](HABIT-SKILL-TOOL-ROUTING.md)：冻结现有 Skill 到 Questionnaire/Profile/Evidence/Care/Commit Controller 的输入、输出、返回方和权限边界，不增加 Agent 或第十九个 Skill。
3. [`DECISION-GAP-MAPPING.md`](DECISION-GAP-MAPPING.md)：冻结 `evidence | care | profile_review | optional_intake` 四种 decision kind，以及有限 `E-*`/`C-*` gap code、可问 concept、答案回流和允许改变的决策。
4. [`ONLINE-REASONING-FREEZE.md`](ONLINE-REASONING-FREEZE.md)：冻结在线事件自动解析、夜间离床双层语义、Care Delivery Decision 和六因子 Safety 融合及强制路由。

四份附件解决“收集什么、为什么此时问、由谁使用、怎样更新、怎样进入在线决策”的闭环。具体阈值验证、前后端、数据库、真实设备/用户验证、正式医学审核和发布证据仍属于后续工程或治理，不改变本轮架构冻结状态。

## Core model

“个人睡眠画像”不是一份填完即定型的报告，而是四类严格分开的上下文：

| 上下文 | 典型内容 | 来源与生命周期 | 能否自动形成 |
| --- | --- | --- | --- |
| `ConfirmedHabitProfile` | 午睡习惯、睡前行为、环境偏好、作息约束、刺激物习惯 | 老人直接回答或允许范围内的家属观察；结构化字段级有效期 | 只有老人明确确认后可长期保存 |
| `ObjectiveBaselineArtifact` | 入床/离床时相、规律性、夜间离床、有效夜晚分布 | 雷达与确定性 Trend/Baseline Tool；绑定窗口、覆盖、质量和算法版本 | 可以，但只是计算结果 |
| `RecentReportedContext` | 本人最近睡眠满意度/困倦，以及家属近期可观察行为 | 本人自述与授权观察者报告严格分型；短时有效 | 当前 Episode 可使用，长期保存仍需老人确认 |
| `ClinicalSafetyContext` | 疾病、详细用药、呼吸暂停线索、跌倒、不可控制嗜睡等 | 独立临床/安全流程 | 不写入习惯画像 |

系统可以表达“近一段有效数据中，通常约在 22:45 入床”，不能因此自动写成“老人习惯 22:45 睡觉”。`ObjectiveBaselineArtifact` 应有 `unavailable | provisional | established` 成熟度；具体有效夜晚门槛沿用并统一到经过验证的 Trend/Baseline Tool 配置，不由 LLM、Questionnaire 或 Memory 自行定义。

## First-version profile scope

首版只登记会实际改变个性化解释或低风险行动的概念：

1. **通常作息、规律性、作息约束与本人偏好**：区分本人/观察者描述的通常上床起床时间与规律性、必须早起/照护他人/固定活动等约束，以及本人希望维持的睡眠时间偏好；主观通常作息与雷达客观时相分别保存，证据充分且不影响当前决策时不重复询问。
2. **午睡模式**：是否午睡、通常时间段和大致时长；允许“不固定、不清楚、不愿回答”。
3. **睡前行为**：屏幕、阅读、洗漱、聊天、放松活动等与当前问题相关的重复行为，不穷举所有仪式。
4. **睡眠环境与辅助偏好**：光线、噪声、温度和必要辅助物，只在会影响解释或行动时收集。
5. **刺激物相关习惯**：咖啡、浓茶、酒精及大量饮水的相对时间模式；不以此建立道德化的“好/坏习惯”标签。
6. **本人睡眠感受与日间影响**：仅由老人本人回答，并作为有短时效的主观上下文。
7. **可观察夜间行为**：明显打鼾、憋醒、离床和异常行为等可由本人或家属按实际观察提供；命中危险信号时从画像采集转入独立安全流程。
8. **个性化目标与非临床行动约束**：老人当前最希望改善的问题、可接受负担、时间/环境限制和个人偏好，作为照护个性化上下文，不混成睡眠质量评分；过敏、疾病和医学禁忌属于 Clinical/Care Safety Context，不进入习惯画像。

疾病史、完整药物清单、人口学档案、正式风险筛查和标准化量表不属于该画像。PSQI、ISI、ESS 等工具首版不直接整合，也不得拆题混入自建清单；未来如有明确评估场景，必须保持原量表语义、时间窗和评分规则，核查授权并经过医学评审，作为独立评估结果使用。

并非所有 Questionnaire 概念都可进入长期画像。设备是否移动、昨晚是否睡在别处、某一晚起夜次数、一次数据缺失原因等只服务当前 Evidence/Data Quality，必须标记为 `episode_only`；只有经过审核且确实具有跨 Episode 个性化价值的概念才可标记为 `profile_eligible`。Questionnaire Entry 本身永远不会因为多次出现而自动升级为 Habit fact。

首版概念的逐项权威范围、当前 22 个已登记 concept、仍需实现的 16 个 v1 concept、ObjectiveBaseline metric 和安全边界见 [`V1-CONCEPT-COVERAGE.md`](V1-CONCEPT-COVERAGE.md)。未来实现不得用一个宽泛字段吞并多个已冻结语义，也不得把 v1.1/unsupported 项目作为 v1 完成条件。

## Structured contracts

### Habit concept definition

每个可写入画像的概念必须是版本化、经过领域审核的 `HabitConceptDefinition`，至少声明：

- `concept_id`、版本、领域和用户可理解的目的；
- canonical 问题语义、老人措辞和允许的家属措辞；
- `choice | scale | bounded_number | short_text` 答案类型、合法选项、单位和范围；
- 允许回答者：`elder_only | elder_or_observer`；
- 可表达的 `unknown | variable | prefer_not_to_answer | not_applicable` 回答 disposition；其中 `variable`、`not_applicable` 可以是结构化值，`unknown` 和 `prefer_not_to_answer` 默认不是 Habit fact；
- 字段级有效期类别、冷却时间和重新确认条件；
- 允许触发场景、会影响的 Evidence/Care 决策类型和禁止用途；
- `episode_only | profile_eligible` 持久化资格；默认 `episode_only`，只有领域审核显式批准后才能生成 Profile candidate；
- 中性问法要求、可能答案的对称处理和禁止的引导性表述，SelectionRequest 不得携带模型“希望得到的答案”；
- 是否含安全路由条件、领域/医学审核状态和内容版本。

LLM 不得创建新概念、改变问题语义、选项、单位、阈值、回答者权限或安全路由。它只能在受控范围内调整语言，使问法更自然、适老，同时保留 canonical concept ID 和答案结构。

对可进入 Evidence/Profile 的正式问题，“受控调整”只允许选择经过审核的角色化措辞 variant，或在固定 canonical 问题外添加不改变正文的礼貌 wrapper。自由 LLM 改写不能被确定性验证为语义等价，因此其回答只能作为未结构化对话候选；在进入结构化流程前必须用审核问题或忠实结构化摘要重新确认。

### Profile fact

长期 `HabitProfileFact` 至少包含：

- fact、subject 和 concept 的 ID/版本；
- 结构化 value、单位和 `variable | not_applicable` 等明确状态；
- 回答者 actor、认证角色和 `elder_self_report | family_observation` 来源类型；
- 该回答描述的 observation window、当地时区、适用日型（如工作日/周末/不区分）；跨午夜时间必须使用明确的 sleep-day 归属规则；
- 采集、确认、最后核验和失效时间；
- `confirmed | stale | disputed | superseded | forgotten` 状态；
- 原始 Questionnaire Entry / Evidence ref、确认 receipt 和变更因果引用；
- 可访问 scope、保留/遗忘策略和禁止用途。
- `trust_label=user_data`、最大文本长度和规范化结果；任何 `short_text` 只作为数据，不能被 Prompt Compiler、Agent 或 Tool 当成指令。

长期存储必须保存这些 typed 字段，不能只保存 `value_summary` 自由文本。自然语言摘要是由结构化值派生的展示层，不是画像事实源。

Evidence 合同必须新增并保留 `observer_reported` semantic 和 `authorized_observer_report` source kind（命名可按现有规范调整，但语义必须独立），不能把家属观察编码为 `user_reported`、`observed_fact` 或 canonical observation。Profile fact 经老人确认后，确认只授权持久化，不改变其 origin semantic；后续从 `confirmed_memory` 读取时仍必须携带最初 `elder_self_report | family_observation`、actor、窗口和 observation opportunity，不能因为“已确认”就把家属报告升级为老人自述或客观事实。

`unknown` 只表示当前无法形成事实，不创建长期 Habit fact。`prefer_not_to_answer` 也不保存问题内容或推测值；如果已认证老人通过独立 typed acknowledgement 明确表示“以后不要问这个”，系统可单独保存最小化的 `QuestionSuppression`（subject、concept、scope、到期时间、服务端派生的 opt-out command ref），仅用于抑制提问，不能作为 Evidence 或画像值。它是数据主体的直接撤回命令而非待审批 proposal；不接受 caller 提供的 confirmation/token 字符串。普通跳过只触发当前 Episode/Policy 冷却，不产生长期 suppression。

字段到达 `valid_until` 后，由确定性读取策略计算 `effective_status=stale`，不依赖定时任务或模型写状态；数据库原记录仍保持不可变。重新确认产生 replace/supersede 事件。对于同一 concept，只有在适用日型和 observation window 重叠、值不可兼容时才标记冲突；时间段不同或明确描述“近期发生变化”的记录按版本演进处理，不能误判为冲突。每个来源/适用窗口最多有一个 current fact，多来源可以并列供 Evidence 处理。

Concept 版本不可变。题意、答案范围、单位、回答者权限、持久化资格或安全路由发生不兼容变化时必须提升 major version；已有 facts 保留原 concept version，不能后台重解释或批量改写。只有提供经过审核的显式迁移函数、保留原来源且语义等价时才可机械迁移；否则旧 fact 变为 stale，并在未来确有决策需要时重新确认。

### Atomic profile change set

每轮拟长期保存的 1–3 个 fact 必须组成一个 `HabitProfileChangeSet`，至少包含：

- change-set ID、版本、subject、精确 fact candidate 清单及排序；
- 每个 candidate 的 operation、concept/value/source/observation-window/有效期和替换目标；
- 全集 manifest hash、FactSnapshot/Memory 版本、确认 actor、scope 和确认过期时间；
- `create | replace | expire | forget` 的原子提交语义和幂等键。

一条整体确认绑定整个 manifest hash，而不是只绑定一段汇总文本或其中一个 fact。提交时任一 candidate 失效、权限不足、替换目标变化或版本冲突，都使整个 change set 失败并要求重新汇总确认；不能部分写入后把其余项静默丢弃。确认 token 一次性消费，重试只能使用同一幂等键返回原 receipt。

整体确认前必须允许老人说“只记住其中一项”或删除任一候选；系统据此生成新的 change set、manifest hash 和摘要后再确认，不能把无关或敏感字段捆绑成全有或全无的同意。

画像不计算“完整度分数”。系统内部可以为了选题判断某概念是 `known | unknown | stale | disputed`，但不能把覆盖率当作用户任务、健康分数或主动追问目标。

## Dialogue and collection policy

### Optional light intake

1. 首次使用不设置前置问卷，不阻塞任何核心功能。
2. 在说明“回答可跳过、用于个性化、长期保存另行确认”后，最多询问 2–3 个当前最有价值的问题。
3. 默认优先了解当前主要困扰/目标、雷达无法观察且会改变解释的约束，以及一个高价值主观或行为事实；不重复询问已有可靠数据可以回答的内容。
4. 老人可以跳过、回答“不清楚”或中途退出，系统不得降低服务或使用施压措辞。

### Progressive collection

首轮以后，只有以下触发器允许主动选择画像问题：

- `explicit_habit_question`：老人明确询问自己的睡眠习惯；
- `decision_relevant_gap`：缺失事实会改变当前 Evidence 结论或 Care 候选；
- `stale_fact_needed`：过期事实即将在当前决策中使用；
- `source_conflict`：本人、家属或既有记录冲突且当前任务确实需要解析；
- `explicit_profile_review`：老人主动要求查看或更新画像；系统必须先展示已有摘要，只有老人选择更新或明确存在决策缺口时才提问。

不得因为画像不完整、提高参与度、例行月度刷新或正常晨间结果而提问。每个 Episode 总计最多 3 个画像问题，而不只是每次 Tool selection/对话轮次最多 3 题；模型重试、replan 和多次 selection 共用同一计数。Questionnaire Policy 对 concept/trigger/actor 设置跨 Episode 冷却和去重。若问题未阻塞当前任务，系统应先给出能够诚实支持的结果，而不是先补问。

问题选择使用确定性合法候选集，再按以下顺序排序：安全抢占（转独立路径）→ 是否改变当前决策 → 信息时效/冲突 → 用户负担 → 最近是否已问。`QuestionSelectionRequest` 必须绑定 Episode plan step、缺失的 Evidence/Care 决策、trigger reason code、已知 concept 状态、仍成立的替代解释和剩余问题预算；不得包含预期/期望答案。Tool 重新验证这些条件并优先选择能区分多个解释而不是只证实模型首选假设的中性问题。模型只能从合法候选概念中选择，不能通过自由生成问题、伪造 preferred ID 或重复调用绕过 Registry。每个回答在作为普通 Profile 候选前还要执行确定性的 response-level urgent/risk scan；命中时立即停止剩余画像问题并进入安全路径，不能等到长期保存确认。

`decision_relevant_gap` 的生产者、有限 reason code、允许 concept、最小问题、返回责任主体和“回答后允许改变/保持什么”以 [`DECISION-GAP-MAPPING.md`](DECISION-GAP-MAPPING.md) 为准。未知字段、画像完整度或笼统的“以后可能有用”不能生成 gap；Care 提出的 gap 回答也必须先由 Evidence 验收，再回到 Care，不能让 Care 直接解释原始问卷答案。

### Current use and long-term persistence

1. 老人对已下发问题的直接回答可立即作为当前 Episode 的 `user_reported` Evidence 候选；经授权家属对允许问题的回答只能成为 `observer_reported` Evidence 候选，绝不能映射成老人自述或客观事实。
2. 当前回答不会默认永久保存。每轮最多 2–3 个回答结束后，由 SleepCare 忠实汇总拟保存内容，并取得绑定精确 `HabitProfileChangeSet`、actor、subject、scope、版本/hash 和有效期的整体确认。
3. 未确认、拒绝或中途退出的答案只按当前 Episode/Evidence 保留规则处理，不进入长期画像。
4. 老人主动说出的新习惯先映射为已有 concept 的候选；结构化值、时间窗或语义有歧义时只追问一个必要问题，确认后才可长期保存。
5. 默认只有已认证老人本人可以确认向自己的长期画像写入，包括确认家属提交的可观察事实；确认只决定是否长期保存，不改变家属观察的来源类型。老人拒绝长期保存时，该报告仍按当前 Episode/Safety/Evidence 的正常保留规则处理，不能为迎合画像选择而删除已发生的安全证据。一般家属协同授权不自动获得画像持久化确认权。认知障碍等法定代理场景必须由未来独立治理方案明确授权，且仍不能把代理回答伪装成老人主观感受。
6. 老人可以在确认前删除 change set 中的个别候选；SleepCare 必须重新显示精确摘要并取得新 manifest 的确认。
7. 任何值、来源、时间窗、scope 或 change-set manifest hash 改变都会使旧确认失效。

## Respondent and conflict rules

1. 家属只可回答 `elder_or_observer` 概念中的可观察事实，例如明显打鼾或亲眼观察到的夜间离床；设备移动等数据质量事实是 `episode_only`，不属于长期画像。
2. 睡眠满意度、焦虑、入睡体验、疲惫和主观困倦等只允许 `elder_only`；家属描述只能另存为“家属观察到的行为”，不能代填老人感受。
3. 家属信息不能覆盖老人自述，雷达统计也不能宣判主观体验错误。
4. 来源冲突时并列保留为 `disputed`，由 SleepCare 在当前任务需要时请老人核验，或由 Evidence 保守处理；Memory Service 不自行选真。
5. “未亲眼观察、不清楚、听老人转述”不能记录成家属直接观察。
6. “没有看到打鼾/离床”等否定观察只有在记录了合理 observation opportunity（实际同室/可听见、观察时间窗和把握程度）时才可成为弱的 family observation；没有观察机会只能记为 unknown，绝不能被 Evidence 当作排除某风险的证据。

最小访问原则固定为：老人可以查看自己的有效画像及来源摘要；家属只能在当前授权 scope 和 Episode 目的确有需要时读取最小的可观察/协同字段；医生只在医生材料或获授权专业协同中读取明确引用的最小子集。老人主观感受、刺激物信息和行动禁忌不因家属曾参与回答而自动向家属或医生开放。来源 actor 的身份只用于审计和冲突说明，不赋予持续读取权。

## Agent and capability mapping

### SleepCareAgent

- 使用现有 `ask_minimal_clarification` 解释采集目的、选择对话时机，并从 Questionnaire Tool 的合法候选中请求/呈现最少问题；
- 忠实处理跳过；使用 `select_memory_context` 读取显式画像复核所需的最小切片；使用 `propose_memory_change` 生成画像变更候选摘要并请求确认；
- 回答“系统目前了解什么”，以及处理更正、遗忘和来源冲突；
- 不自行形成个人事实、定义新概念或直接写长期状态。

### EvidenceReasoningAgent

- 使用 `interpret_scoped_evidence` 在明确 SourceScope 内读取最小必要的有效 Profile facts 和 ObjectiveBaseline refs；
- 保留 `user_reported`、`observer_reported`、`observed_fact`、`inference` 和 `unknown` 语义；家属观察不得借 confirmed Memory source 丢失 origin semantic；
- 判断画像事实是否足以支持当前解释；只在 `DECISION-GAP-MAPPING.md` 的 `E-*` gap 成立时请求一个关键事实，实际冲突由 `synthesize_evidence_conflict` 保守处理；
- 不把习惯与睡眠变化的共现宣布为因果。

### CareStrategyAgent

- 只消费已验收 Evidence 中当前相关、仍有效的画像引用；
- 使用 `propose_single_care_action`/`assess_followup_outcome` 根据目标、约束和偏好选择零个或一个低风险行动；只有有限 `C-*` gap 可以回请 SleepCare 提问；
- 不根据“坏习惯标签”制定行动，不把填写问卷当成治疗。

### SafetyReviewAgent and deterministic policy

- 危险信号首先由确定性 urgent/risk policy 抢占并离开画像采集路径；
- 对每个用户答案执行 response-level urgent/risk scan；命中后不生成普通 HabitProfile 候选，不等待本轮问卷结束或长期确认；
- 命中安全路由的原回答仍以最小必要、带来源的 Safety/Evidence 事件保存，供抢占流程使用；“不写入 HabitProfile”不等于丢失风险证据；
- 画像本身不运行诊断评分；涉及诊断、药物、确定因果或医生材料时沿用既有 Safety 触发和发布门；
- Safety 不修改 Profile fact，只能要求责任主体修订、删除或阻断其使用。

### Tools and services

- 扩展现有 Questionnaire Bank/Policy/Selection，而不是建立新问卷框架；
- Questionnaire capture 必须校验答案属于已下发 concept、版本、选项/范围和允许回答者，拒绝任意字符串冒充合法 choice；
- Questionnaire selection 必须验证 plan-step/decision-gap reason code 和 Episode 总预算；capture 必须携带 selection receipt，防止伪造或跨 Episode 重放；
- Profile candidate builder 必须额外验证 concept 为 `profile_eligible`；`episode_only` 答案即使重复出现或取得长期确认也不得写入 Profile；
- 现有 Evidence Ledger 保存当前回答及来源；长期 Profile 只扩展目标 `product_agent` Memory/Commit Controller，使用 typed `HabitProfileFact` 和原子 change set，不复用 legacy family-only writer，也不把结构化值压成 `MemoryItem.value_summary`；
- 扩展 EvidenceSemantic/EvidenceSourceKind 与 acceptance mapping，使 authorized observer report 有独立语义、来源和 actor/window 校验，且不能支持 canonical observed-fact claim；
- Profile 写入确认规则必须新增 elder-owned 的精确 action scope，不能沿用现行 `write_long_term_memory` 的 family-only 角色规则；迁移完成前该路径 fail closed；
- ObjectiveBaseline 继续由确定性 Trend/Baseline Tool 产生；
- Commit Controller 是长期画像提交的唯一写路径。

现有 Skill、Tool、确定性 runtime 和 Commit Controller 的逐跳路由及所需工程 allowlist 增量以 [`HABIT-SKILL-TOOL-ROUTING.md`](HABIT-SKILL-TOOL-ROUTING.md) 为准。Skill 只作原子判断，Tool 负责确定性选择/校验/读取，确认与提交不进入模型 Skill。

## Delivery dependencies and phases

当前四角色、概念范围、Skill/Tool 权限路由和决策缺口回流在架构层面已经冻结，具备进入工程实现的条件。当前 Python 目录中的 catalog、Skill allowlist 和 runtime 只按“工程骨架/部分实现”理解，不能用其已存在或缺失反向改写冻结语义。本计划不得在 legacy DialogueAgent/MemoryAgent 或 `radar_agent.memory` 上另建一套画像。后续工程交付仍按依赖拆分：

1. **Phase A — reviewed foundation（可先行）**：冻结 concept 分类、版本/持久化资格、Schema、Policy、安全路由、场景集和医学/领域审批；只做目标 `product_agent` namespace 内的确定性合同与 Tool 测试，不上线画像写入。
2. **Phase B — target runtime integration**：在四 Agent roster、唯一 Memory Service 和 SleepCare ownership 已具备后，接通 Episode Evidence、elder-owned atomic persistence、Context minimization 和 Safety 抢占。
3. **Phase C — product exposure**：接通可跳过轻建档、查看/更正/遗忘、适老语言与用户测试；在 Gate A/B 未通过前不开启默认主动提问。

任一阶段不得通过双写、legacy 角色适配器或隐藏 feature path 让冲突架构进入生产。Phase A 可以独立验收，但不能宣称“睡眠画像已实现”。

上述 Phase A–C 是后续工程、治理和产品暴露阶段，不是本轮“架构设计是否完成”的判定门。架构冻结不表示题库代码、数据库、API、前后端、医学审核、真实用户/设备验证或正式发布已经完成。

## Approach

1. 将本计划作为睡眠习惯画像的权威产品/架构补充，并在实施时引用现有产品定位、Agent 架构和信息架构，不复制其内容。
2. 建立首版 `HabitConceptDefinition` 目录和领域审核清单；将附件及现有问题逐项分类为 `profile_eligible`、`episode_only`、转安全/临床上下文或删除。
3. 为 `HabitProfileFact`、原子 `HabitProfileChangeSet`、确认 receipt、字段状态和 ObjectiveBaseline ref 定义严格 Schema，扩展 observer Evidence semantic/source，拒绝未知字段，并使确认 token 绑定全集 manifest hash。
4. 扩展现有 Questionnaire Tool 的触发器、plan-step 绑定、Episode 总预算、候选筛选、回答者权限、selection receipt、答案校验、concept 级冷却与受控措辞适配。
5. 先完成 Phase A 合同/题库/场景验收；只有四 Agent roster 与唯一 Memory Service 依赖满足后，才在 Phase B 将当前回答接入 Evidence，并将确认后的长期变更接入目标 `product_agent` Memory/Commit Controller。
6. 在 Context assembly 中按 Episode 目标、actor 权限、字段敏感度、确定性 `effective_status`、有效期和最小必要原则裁剪 Profile；Skill 声明不能扩大读取范围，短文本始终以不可执行 data trust label 编译。
7. 实现轻建档、显式习惯问答、决策缺口、过期字段和冲突核验五条对话路径，并保证正常晨间流程不被补全问题打断。
8. 在答案 capture 与 Profile candidate 形成之间增加确定性 response-level urgent/risk scan，将危险信号、标准化量表和临床资料明确路由到画像外的现有安全/评估边界。
9. 添加合同、权限、状态、确认、冲突、遗忘、最小披露和端到端对话测试。
10. 用至少 3–5 名目标年龄段用户完成适老可用性测试，记录理解度、跳过率、单轮负担、错误确认和打扰感；这只验证交互，不宣称医学有效性。

## Acceptance criteria

1. 新用户跳过全部画像问题后，仍能获得数据边界允许的晨间解释和一般知识回答。
2. 轻建档和任一 Episode 总计最多提出 3 个画像问题，模型重试/replan/多次 selection 不能重置预算；每个问题都有 concept、版本、plan step、触发理由和合法回答结构。
3. 正常、证据充分的晨间解释不会为了画像完整度追加问题。
4. 模型自由生成的 concept、选项、阈值或未下发问题答案不能写入 Evidence 或长期画像。
5. choice/scale/number 答案必须通过选项、单位和范围校验；`unknown`、跳过和拒绝是合法结果但默认不形成长期 Habit fact；明确“以后不要问”只形成最小 QuestionSuppression。
6. 家属尝试代答老人主观感受时被拒绝或重新表达为可观察行为，不形成老人自述。
7. 老人回答后可以在当前 Episode 使用；未取得长期保存确认时，Memory 中没有对应 Profile fact。
8. 一次整体确认必须绑定完整 `HabitProfileChangeSet` manifest；老人可先移除任意候选并查看新摘要；任一候选变化后旧确认不能重放，任一项失败时不得部分提交。
9. 雷达计算结果只以带窗口、有效夜晚、质量、算法版本和成熟度的 ObjectiveBaseline ref 使用，不能自动转成确认习惯。
10. 同一 concept 的本人、家属和雷达来源在重叠观察窗口内冲突时同时保留，结果明确不确定；不重叠窗口的真实变化不会被误报为冲突或静默覆盖。
11. 过期字段由读取策略确定性视为 stale，不依赖后台定时写或模型判断；不会继续作为当前事实使用，也不会仅因过期自动打扰用户。
12. 任一答案命中危险信号时立即停止剩余画像追问并进入现有安全路径；该答案以最小 Safety/Evidence 事件保留但不生成普通 Profile 候选，也不被归类为“坏习惯”。
13. 任何输出都不生成睡眠习惯综合分、固定类型标签、诊断或确定因果。
14. 用户可以查看系统保存的习惯摘要、来源和最近确认时间，并可更正或遗忘可删除信息；遗忘后历史审计若依法/按治理必须保留，界面必须准确说明其不可再用于个性化而非声称物理删除全部记录。
15. 每次 Agent 调用只接收当前目标所需的最小 Profile 子集，家属/医生身份不能因画像存在、参与回答或来源 actor 身份而扩大访问权限。
16. 画像服务或问卷工具不可用时，系统诚实降级，继续使用已有有效 Evidence 或一般知识，不伪造个性化信息。
17. 现有 family-only `write_long_term_memory` 不能确认 Profile change set；只有新增的 elder-owned action scope 和全集 hash 验证通过时才能提交。
18. 在固定场景集上做有/无画像的配对回放：有效画像只在相关问题中改变解释或行动约束；无关、过期、未确认或越权字段不改变结果，也不引入确定因果。
19. `short_text` 中的指令、URL、工具名或 Prompt 注入内容保持 `user_data` trust label，只能作为回答数据，不能改变 Agent 目标、问题选择、权限、Tool 调用或输出 Schema。
20. selection receipt 不能跨 Episode、subject、role、concept version 或有效期重放；伪造 preferred ID 和多次 Tool 调用不能突破总问题预算。
21. `episode_only` 的设备位置、单夜事件和数据质量答案无法生成 Profile candidate，即使重复回答、模型请求或用户误确认也不能持久化为习惯。
22. 家属否定观察缺少 observation opportunity 时只能形成 unknown；不能用于排除打鼾、憋醒、离床或其他风险。
23. 不兼容 concept 新版本不会后台重解释旧 facts；没有显式等价迁移时旧事实 stale，并保留原版本与来源。
24. 配对场景覆盖确认偏差：模型首选解释不同但输入相同的运行必须获得相同合法问题候选；问题措辞和排序不能暗示期望答案。
25. Phase A 在四 Agent runtime 依赖完成前只交付离线合同、审核题库与测试，不注册生产写路径、不双写，也不声称画像功能已上线。
26. 家属回答进入 Evidence 时语义为 observer-reported；老人确认长期保存后再次读取仍保持原 observer semantic，不能转成 user-reported、observed-fact 或 canonical observation。
27. 老人拒绝持久化家属观察不会删除当前 Episode 中合法的 observer Evidence 或必须保留的 Safety 事件；它们按各自保留规则到期且不能被后续个性化越期使用。
28. 过敏、疾病和医学禁忌无法写入 HabitProfile；这些字段只允许进入经授权的 Clinical/Care Safety Context。
29. 提交 `night_out_of_bed` 在线事件时，即使 API 调用方未填写 `profile_relevant_concept_ids`，Runtime 仍确定性产生 `E-NIGHT-OBSERVATION`、读取所需 Profile slice 与 `baseline.night_out_of_bed`，并把质量、趋势和临床 refs 交给 Evidence。
30. `CareDeliveryDecision` 同时冻结立即/早晨、语音/灯光/静默、打扰负担、家属通知、音量、安静时段及保守默认值；catalog、device policy 与 coordination policy 任一不满足时 Care 不得验收。
31. 在线风险必须联合 `absolute_red_flag`、相对基线偏离、多源一致性、数据质量、当前场景和纵向趋势；绝对红旗存在时个性化只能改变解释，不能降低风险等级。
32. `risk_level=escalate` 必须确定性触发 SafetyReviewAgent；带 urgent 标记的绝对红旗必须在任何模型 Agent 调用前进入 urgent boundary。

## Key decisions & tradeoffs

1. 选择渐进式多源画像而不是一次性完整问卷，牺牲快速获得“完整档案”的表象，换取低负担、低回忆偏差和真实业务相关性。
2. 画像主要作为内部结构化上下文，用户侧只按需展示可解释摘要，避免标签化和健康焦虑。
3. 客观统计、本人感受和家属观察并列保存而不强行融合，牺牲单一结论的简洁，换取认识论诚实。
4. 当前回答与长期保存分两步，增加一次整体确认，换取符合既有长期记忆治理和用户控制。
5. LLM 只能选择、自然表达和映射已审核概念，牺牲任意提问自由，换取数据可比性、权限和审计能力。
6. 不把标准量表和安全筛查混入习惯画像，牺牲“一张表覆盖一切”，换取清晰的产品和医学边界。
7. 不以完整度驱动提问或留存，接受长期存在未知字段，换取老人不被系统持续盘问。
8. 不新增 Agent 或平行 Questionnaire/Profile runtime，降低架构膨胀，但要求对现有 Tool、Memory 和 Evidence 合同进行严格扩展。

## Risks / open questions

1. 首版 concept 的语义、归属、持久化类别、触发器和决策影响已经冻结；具体中文措辞、审核选项、类别内精确有效期和医学/领域审核人仍需在工程/治理阶段逐项登记，不能改变冻结语义。
2. 不同 ObjectiveBaseline 指标的 `provisional | established` 有效夜晚门槛需要与雷达验证结果和现有 Trend Tool 统一，不能凭产品文案设定。
3. 对认知障碍、失语或无法独立确认的老人，代理决策、监护授权和主观信息缺失如何处理需要单独的数据治理方案；本计划不允许默认由家属代填。
4. “整体确认”仍可能被老人机械同意，需通过适老测试验证汇总措辞、语音交互和撤销可发现性。
5. 刺激物、酒精和近期行为可能涉及敏感信息，需要字段级访问范围和保留期评审。
6. legacy/历史 Questionnaire Entry 可能缺少完整 actor、observation-window 或答案合法性语义；实施迁移必须兼容只读历史记录，但不能把旧记录自动升级为已确认画像。
7. 画像改善了个性化输入质量，不等于证明建议有效；照护效果仍需独立产品评测和医学治理。
8. 工程接入必须以 typed 原子 change set 和 elder-owned confirmation 为唯一画像变更合同；任何仍可达的单自由文本 Memory 候选路径不得用于画像，也不能通过一次确认后循环提交多个单候选来模拟原子性。
9. 首版 concept 和 ObjectiveBaseline 已冻结为同一 IANA timezone、`wake_date` sleep-day 规则；工程必须一致编码。旅行/时区变化无法按同一规则比较时输出 unknown，不自行换算，重复旅行模式按 v1.1 处理。
10. 家属对夜间行为的否定观察高度依赖其观察机会；concept 和 Evidence gate 必须避免把“没看见”解释为“没有发生”。
11. 当前 `product_agent` 骨架已经出现独立 `observer_reported`/`authorized_observer_report` 语义；后续工程必须保持 actor、observation window/opportunity 和 origin semantic 的端到端验收，任何缺失该语义的 legacy 路径保持不可用于家属观察。

## Architecture status

- `sleep_habit_architecture_complete=true`
- `sleep_habit_architecture_frozen=true`
- `sleep_habit_online_reasoning_loop_complete=true`
- `ready_for_engineering_implementation=true`
- `frontend_backend_complete=false`
- `release_ready=false`

前三项只表示产品定位、四角色归属、概念范围、Skill/Tool/Profile/Evidence/ObjectiveBaseline 边界、采集/更新/冲突流程和决策回流已足够明确，可以作为工程输入；不表示后续验收或发布阶段已经完成。

## Out of scope

1. 本轮不编写实现代码，也不改变已锁定的四 Agent 名单。
2. 不开发疾病诊断、用药建议、急救判断、PSQI/ISI/ESS 等正式量表或临床决策支持。
3. 不建立统一睡眠健康分、好坏标签、用户排名或画像完整度激励。
4. 不要求老人填写完整睡眠日记；未来若引入 Consensus Sleep Diary，应作为独立、明确目的的前瞻性记录工具评审。
5. 不重新设计雷达算法、个人基线统计方法、医生/家属完整信息架构或数据治理制度。
6. 不允许 LLM、Skill 自进化或家属授权绕过 concept 审核、本人主观感受边界、长期保存确认和 Commit Controller。
