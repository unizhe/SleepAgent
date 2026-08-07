# Plan: SleepAgent 四角色 Agent 架构收口
_Locked via grill — by Codex + user_

## Goal

在既有产品定位、信息架构和 `sleepagent.radar_agent.product_agent` 运行时基础上，将当前九个逻辑 Agent 收口为四个有充分责任依据的 Agent：`SleepCareAgent`、`EvidenceReasoningAgent`、`CareStrategyAgent` 和条件触发的 `SafetyReviewAgent`。四者分别承担用户目标与发布、个人证据形成、跨天照护策略、安全复核四类不可互相替代的闭环责任；计算、检索、模型推理、渲染、存储、权限和外部执行下沉为 Tool、Service 或确定性 Policy。SleepAgent 的 Agent roster 在本规范下是唯一且封闭的四角色集合；禁止增加第五身份、通过 alias 恢复旧身份，或让 runtime 按历史 Agent 名称分流。实现时必须改造和复用现有 `product_agent` 合同、Episode runtime、runner、治理、工具和测试，不建设平行版本。

## Inherited product decisions

1. 第一用户是居家老人，家属和医生是经授权的协同角色；用户始终只面对统一的 SleepAgent。
2. 产品主线是“持续观察 → 晨间解释 → 按需对话 → 用户确认行动 → 轻量跟进 → 新数据与反馈进入下一轮”。
3. 产品提供健康观察、解释、低风险照护建议和必要协同，不替代医疗诊断或医生决策。
4. 正常状态可以明确无需特别处理；证据不足必须明确不知道；急症边界必须优先提示线下医疗或急救协助。
5. 同一时间最多只有一个主要照护行动，任何行动建立或实质修改都必须遵守确认、权限和版本规则。
6. 本计划只重新定义 Agent 架构及其 Skill、Tool、状态与协作边界，不改变已经锁定的用户入口和身份授权原则。

## Plan authority

1. `product_positioning/PLAN.md` 与 `product_information_architecture/PLAN.md` 分别继续决定产品定位和信息架构。
2. 本文件是目标 Agent 名单、职责、协作、降级和 Agent/Skill/Tool 边界的权威规范。
3. `skills_design/PLAN.md` 的 Package、Registry、Resolver、Compiler、版本锁、Outcome、审批、灰度和回滚机制继续复用；其 owner 映射必须严格属于本计划的四角色 roster，不得引入额外 Agent 身份。
4. 旧三 Agent、固定多 Agent、Dynamic runtime 和现有九角色文档仅作为迁移来源与 Git 历史；最终实现不得 import、调用、注册或通过开发、fallback、backup 路径执行这些旧身份。

## Agent definition

Agent 是稳定的逻辑责任与权限主体，不是进程、模型、Prompt 名称、工作流节点或一次 LLM 调用。以下条件描述角色在其生命周期内必须具备的能力；并非要求每次 Invocation 都机械地调用工具或进行多轮循环。一个模块只有同时满足以下条件才称为 Agent：

1. 对一类独立、可验收的结果承担明确责任，并有自己的成功、等待、保守退出和失败条件。
2. 接收随 Episode 或跨天过程变化的授权状态，而不是只处理固定函数输入。
3. 能在授权范围内自主选择下一步，例如选择 Skill、调用白名单 Tool、请求另一个责任主体、请求用户信息或终止。
4. 至少形成一次“观察 → 决策 → 工具或协作动作 → 读取反馈 → 调整或终止”的闭环。
5. 拥有与职责匹配且不能被其他 Agent 随意代行的 Context、输出合同、权限上限和审计身份。

仅使用 LLM、拥有独立 Prompt/Schema，或生成一段自然语言，都不足以成为 Agent。

- `Invocation` 是 Agent 在特定 Context、SkillBundle 和 Schema 下的一次执行；同一 Agent 可以在一个 Episode 中有多次 Invocation。
- `Skill` 是某个 Agent 完成一项原子判断的版本化方法，不拥有身份、状态或权限。
- `Tool` 是边界清晰、可调用和可审计的能力，不拥有持续目标或自主闭环。
- `Service` 负责持久化、查询、执行或基础设施生命周期。
- `Policy/Middleware` 强制执行不允许模型改变的安全、权限、确认、预算、并发和发布不变量。

## Current target architecture

```text
用户感知：一个统一的 SleepAgent
└─ SleepCareAgent
   ├─ EvidenceReasoningAgent
   ├─ CareStrategyAgent
   └─ SafetyReviewAgent（条件触发的强制安全闸门）

确定性运行与能力层
├─ Episode runtime / FactSnapshot / Context assembly / Receipt
├─ 数据与质量、趋势计算、风险与急症、reviewed knowledge
├─ Care catalog、协同策略、Artifact、Memory
├─ 身份、权限、隐私、确认、预算、版本和幂等
└─ Commit Controller 与外部动作执行
```

当前四个 Agent 是唯一且封闭的生产 Agent roster：

1. `SleepCareAgent`
2. `EvidenceReasoningAgent`
3. `CareStrategyAgent`
4. `SafetyReviewAgent`

`SafetyReviewAgent` 条件执行，但始终是 roster 成员，不是按场景注册或移除的可选身份。Contract、manifest、factory、runtime、worker、API、CLI、调试面和插件均只能承认这四个身份。禁止增加第五身份，禁止通过 alias、历史名称、临时/后台/子 Agent 或独立模型 Prompt 绕过 roster，也禁止把旧 Runtime 当作开发或备用执行路径。

新能力必须归入现有 Agent 的 Skill、Tool、Service 或 Policy。任何改变 roster 的需求都与本规范和当前收口目标冲突；实现必须停止并等待用户另行推翻本规范，不能把它设计成当前架构的扩展点。

## Agent responsibilities

### 1. SleepCareAgent

`SleepCareAgent` 合并现有 `OrchestratorAgent` 与 `DialogueAgent`，是正常智能路径的唯一用户入口、中心编排者和最终发布者。急症抢占及系统/数据故障时，runtime 可以发布预先审定且明确标记的确定性模板；这不是第五个 Agent，也不能生成新的个性化结论。

职责：

1. 在 runtime 创建或恢复 Care Episode 后，理解已规范化的用户目标、身份允许的任务范围和当前对话焦点，并提出受 EpisodeDefinition 约束的计划。
2. 选择当前场景所需的最少 Agent、Skill 和 Tool，管理预算、等待、检查点和有界重规划。
3. 为其他 Agent 请求最小必要 Context，接收并验收其结构化工作成果。
4. 处理结构化跨 Agent 请求、冲突、失败和 Safety 退回。
5. 将已验收成果转换为适合老人、家属或医生的表达，并作为统一 SleepAgent 发布。
6. 识别“记住、修改、遗忘”意图，生成长期记忆变更候选，解释影响并在需要时取得用户确认。
7. 对明确的非个性化一般睡眠知识问题，可以直接使用 reviewed Knowledge Tool 回答，并显式标注非个人结论。

禁止：

- 自行形成新的个人睡眠事实、因果解释或照护行动。
- 在最终表达中改变 Evidence 的数值、范围、置信度、未知项，或改变 Care 的行动参数与停止条件。
- 以发布权替代权限、用户确认、Safety 或确定性提交控制。
- 在专业 Agent 失败时临时接管其责任并伪装为完整结果。

SleepCare 可以逐字或忠实地确认当前已认证用户刚刚表达的感受，例如“你刚才说今天很疲惫”，但这仍标记为 `user_reported`，不能被当作已验证个人事实；只要该陈述将用于个人解释、风险或行动决策，就必须进入 Evidence 的 SourceScope 和验收流程。

Episode 状态、等待/恢复、预算计数、完成判断、FactSnapshot 和 Receipt 始终由 runtime 拥有。SleepCare 只能提出 plan/evaluate/finish 建议，不能自行把 Episode 标记完成、跳过必经成果或恢复过期状态。

### 2. EvidenceReasoningAgent

`EvidenceReasoningAgent` 是个人睡眠 Evidence claim、有限解释和不确定性的唯一形成者，承担认识论责任。canonical 数据和确定性 ToolReceipt 才是观测值来源；Evidence 不创造或改写原始事实，而是决定在当前 SourceScope 中哪些观测可被接受、如何分层引用以及能够支持什么结论。

职责闭环：

1. 根据目标选择恰当 SourceScope、时间范围和证据需求。
2. 调用授权的数据、质量、趋势、历史和 reviewed Knowledge Tool。
3. 检查数据覆盖、时效、来源、矛盾、缺失和历史记忆有效性。
4. 必要时调整查询范围、请求一个关键补充事实或保守终止。
5. 输出版本化 `EvidencePacket`，明确区分：
   - `observed_fact`
   - `user_reported`
   - `grounded_knowledge`
   - `inference`
   - `unknown`
6. 对有限推断给出证据引用、置信度、替代解释和不能回答的边界。

禁止：

- 制定照护行动、直接发布用户回复、执行风险升级或写入长期记忆。
- 把一般医学知识、历史记忆或同时发生的现象直接宣布为当前个人事实或确定因果。
- 绕过质量、时间范围和来源检查。

### 3. CareStrategyAgent

`CareStrategyAgent` 对照护行动从提出到结束的跨天决策闭环承担责任，不是一次性建议生成器。

职责闭环：

1. 读取已验收 Evidence、当前行动状态、已确认偏好/禁忌、行动历史和相关反馈。
2. 判断是否需要行动；正常状态允许明确输出“无需新行动”。
3. 从版本化、审定的 Care catalog 中选择零个或一个主要行动，并定义目标、负担、观察周期、客观指标、主观问题、停止条件和确认要求。
4. 规划必要的家属、医生或设备协同候选，包括对象、原因、时机、去重和停止条件。
5. 在后续 Episode 中综合新 Evidence 与用户反馈，决定保持、调整、暂停、完成或结束。
6. 证据不足时，通过 SleepCare/运行时提交结构化 `EvidenceRequest` 或请求一个必要用户事实。
7. 对需要主动交付的目录内行动形成类型化 Care Delivery Decision，明确立即/早晨、语音/灯光/静默、打扰负担、家属通知、音量、安静时段及保守默认值，并接受 Care catalog、device policy 和 coordination policy 验收。

禁止：

- 读取未经裁剪的原始传感器数据、自行创造个人事实或改变 Evidence。
- 提供诊断、药物调整或确定疗效承诺。
- 未经确认激活行动、直接发送通知、写状态或执行外部动作。
- 在已有主要行动时自行开启第二个主要行动。

Care 可以读取带 actor、时间和来源的已确认行动反馈业务事件，用于决定保持、暂停或请求进一步 Evidence；它不能把“用户说感觉好些”改写成客观改善或确定疗效。任何客观变化、跨时间比较、原因解释或准备对外发布的效果 claim 都必须先由 Evidence 接受。

### 4. SafetyReviewAgent

`SafetyReviewAgent` 是范围受限、条件触发的强制安全闸门；它补充开放式语义审查，不取代确定性安全规则。

必须触发：

1. Evidence 存在冲突或低置信度推断，却准备形成个人化结论。
2. Care 候选超出审定低风险目录、存在禁忌/基础状态冲突或关键不确定性。
3. 内容可能涉及诊断、药物、确定因果或超出设备与产品能力的表述。
4. 生成医生材料。
5. 准备通知、分享、导出或其他外部动作。
6. 前次 Safety 退回后的修订版本。

高质量普通晨间解释、明确的数据不足说明和目录内低风险行动不固定调用 Safety。

是否触发 Safety 不能由 SleepCare 的计划自行决定。运行时必须在 Agent 规划后、每个待发布/待执行 target 形成后以及最终发布前，依据 target 类型、Care catalog、风险/禁忌、外部动作和受限表述扫描执行确定性触发检查；命中规则而计划未包含 Safety 时，运行时补入强制 checkpoint 或阻断，不能按原计划继续。

Safety 只能输出 `approve | revise | block`：

- `approve` 只对精确目标、版本、哈希、策略版本和有效期成立。
- `revise` 必须明确问题位置、问题类型、修改要求和责任 Agent。
- `block` 必须给出可审计 Reason Code 和允许的保守退路。

Safety 不得重写 Evidence、推断、Care 行动或最终材料。事实/推断问题由 Evidence 修订，行动问题由 Care 修订，仅涉及最终措辞或角色表达的问题由 SleepCare 修订；SleepCare 不能借“改措辞”改变事实或行动。首次审查后最多允许两次“责任 Agent 修订 → 重新验收 → Safety 复审”提交；每次修订都产生新的 target hash，旧决定立即失效。仍未通过时删除争议内容、退回保守结果或阻断动作；任何删减后的新 target 仍须重新通过确定性 Safety 触发检查和 publication postflight，不能把“删除部分内容”当作自动获批。Safety 批准不能授予权限、替代确认或绕过确定性规则。

## Why these four are Agents

1. `SleepCareAgent` 拥有用户目标、动态任务路径、多轮对话、检查点、重规划和最终发布闭环。
2. `EvidenceReasoningAgent` 拥有从证据需求到查询、冲突处理、补充信息和可回答边界的独立认识闭环。
3. `CareStrategyAgent` 拥有跨天行动提出、确认后跟进、调整和终止的独立策略闭环。
4. `SafetyReviewAgent` 拥有对特定高风险成果进行独立审查、退回、复审和阻断的对抗闭环，并与生成者保持职责分离。

四者的目标、输出、权限和失败后果均不同。合并 Evidence 或 Care 会让主 Agent 同时生成并自证专业结论；合并 Safety 会让生成者审查自己。继续保留原四个子 Agent则会把同一责任闭环切成没有独立目标的小步骤。

## Reclassified former Agents

| 原角色 | 新分类 | 理由与复用方式 |
| --- | --- | --- |
| `OrchestratorAgent` | 合并进 `SleepCareAgent` | 规划、检查点评估和重规划继续作为 SleepCare 的 Invocation/Skill |
| `DialogueAgent` | 合并进 `SleepCareAgent` | 多轮焦点、角色化解释和最终发布属于统一用户责任 |
| `TrendAgent` | Trend Tool + Evidence Skill | 窗口、基线、覆盖和变化是确定性计算；意义与冲突解释归 Evidence |
| `AlertCareAgent` | Care Skill + Coordination Tool/Policy | 协同是照护策略生命周期的一部分；调度、去重和发送是工具/策略 |
| `ReportAgent` | SleepCare Skill + Artifact Tool | 报告不拥有独立环境闭环；内容必须来自已验收成果 |
| `MemoryAgent` | Memory Service/Tool + SleepCare Skill | 长期记忆是受治理存储；语义候选与用户核验归统一交互主体 |

已经作为确定性能力存在的数据读取、质量评估、风险分类、急症匹配、知识检索、Ledger、权限和执行继续保持非 Agent 身份。

## Tool, Service and Policy boundary

### Read and analysis tools

- 授权后的当前/历史睡眠与设备数据读取。
- 数据质量、覆盖率和设备状态评估。
- 趋势窗口、个人基线、变化幅度和统计计算。
- reviewed-only 医学知识检索与引用校验。
- Evidence Ledger、Care State、Care catalog、业务事件和长期记忆的受限查询。

### Render and coordination tools

- 角色材料和结构化 Artifact 渲染。
- 通知时间、去重、频率限制和协同策略查询。
- 问卷选择与结构化采集。

### Deterministic policies and middleware

- 身份、主体关系、角色和授权 scope。
- 数据最小化、隐私、确认、预算、超时和 Agent/Tool allowlist。
- 急症文本与确定性风险边界。
- Schema、证据引用、目录参数和发布一致性校验。
- Episode 并发、CAS、幂等、重试和 unknown 外部结果处理。
- system policy、用户输入、Memory、Agent 成果、Knowledge 和 Tool 输出的显式 trust label 与指令/数据隔离；引用文本、设备字段和检索内容不能改写目标、权限、Skill、Schema 或路由。

### Side effects

Ledger、Care State、长期 Memory、Artifact 状态以及通知、分享和导出只能由确定性 Commit Controller 提交或执行。四个模型 Agent一律没有共享状态直接写权限和外部副作用权限。

### Deny-by-default capability matrix

未列出的 Agent/Tool 请求一律拒绝；具体工具名沿用并收口现有 Registry，不另建平行注册表。

| Agent | 可请求的只读能力 | 明确禁止 |
| --- | --- | --- |
| SleepCare | Policy/confirmation 只读、reviewed Knowledge、questionnaire、Artifact read/render、Memory query/compare | 原始个人数据、专业 Evidence 计算、Care 状态写入、Memory 写入、外部执行 |
| Evidence | 授权个人数据、质量、设备状态、Trend、reviewed Knowledge、Evidence Ledger、最小相关 Memory | Care catalog 决策、状态写入、通知/分享/导出 |
| Care | 已验收 Evidence refs、Care State/catalog/constraints、相关行动反馈、coordination policy/schedule、reviewed Knowledge | 原始个人数据、改写 Evidence、状态写入、外部执行 |
| Safety | 精确 review target、Policy、确定性风险结果、Care catalog/constraints、权限/确认状态只读 | 扩大 target Context、写回专业成果、状态写入、外部执行 |

SleepCare 的 Artifact render 只生成待提交字节/结构，不代表保存或导出成功；Memory query 只返回经过 scope、有效期和最小化处理的候选。所有只读工具仍返回版本化 ToolReceipt。

## State and memory model

必须区分三类状态：

1. **Episode 工作记忆**：当前执行中的目标、计划、临时上下文和工作成果，由 runtime 管理，随 Episode 结束或过期。
2. **业务与审计记录**：行动状态转换、发布、Safety 审查、确认、ToolReceipt 和外部执行结果，是不可随意修改的真实过程记录。
3. **受治理的长期记忆**：只保存会影响未来解释和照护的用户偏好、长期状态、行动反馈和已确认纠错。

Memory Service/Tool 是长期记忆唯一权威存储，负责查询、来源、版本、有效期、权限范围、重复/冲突候选检测、替换、过期、遗忘、持久化与审计。各 Agent不得维护私有长期记忆副本，只能保留有生命周期限制的运行上下文或缓存。

业务与审计记录出现事实错误时使用追加的 correction/supersede 事件，不原地改写历史。由确定性计算产生的个人基线、覆盖统计和趋势参考属于带数据版本与时间范围的分析状态/Artifact，不作为无来源的自然语言长期记忆；Evidence 通过 ToolReceipt 读取并验证其时效。

`SleepCareAgent` 的记忆 Skill 识别明确意图并生成结构化变更候选：

- 只有已认证用户在当前最外层消息中直接发出的“记住、修改、忘记”指令才可视为该操作的明确授权；引用/转述内容、检索结果、Tool 输出、设备文本和其他 Agent 消息都不能授权记忆变更。它仍须通过主体关系、scope、Schema 和提交检查。
- 系统根据行为推断出的长期记忆候选必须另行确认。
- 客观业务过程写入业务事件，不复制成长记忆叙述。

“忘记”只影响允许删除或失效的受治理长期记忆，不擦除必须保留的业务事实、确认、Safety、外部执行和审计记录；后者按数据治理规则限制访问、保留或匿名化，并向用户准确说明操作范围。

Memory Service 可以确定性处理结构化冲突并标记语义冲突候选，但不能自行决定哪个个人陈述为真。习惯变化、时间范围差异和身份歧义由 SleepCare 向用户核验，或由 Evidence 在当前分析中保守处理。

## Collaboration protocol

采用“中心编排 + 类型化协作”。所有跨 Agent 请求由 `SleepCareAgent`/runtime 路由，不允许自由形式、无边界的 Agent-to-Agent 对话。运行时可以依据已验收的结构化请求直接完成转发和策略校验，不要求为了每次转发额外调用一次 SleepCare 模型；SleepCare 保持逻辑编排责任，runtime 保持路由与强制策略执行责任。

每个工作成果或请求必须绑定：

- `episode_id`
- sender / receiver
- `fact_snapshot_id/hash`
- `episode_state_revision`
- SourceScope
- 输入工作成果 refs/hash
- target type/id/hash
- Skill/Profile/Schema/Policy 版本
- causal parent invocation
- 状态、Reason Codes 和过期信息

任一上游输入、FactSnapshot、SourceScope、Care 候选、Policy 或被审 target hash 发生变化，都使依赖它的旧验收和 Safety 批准失效；runtime 必须重新验收、重审或结束旧 Episode，不能让旧结果覆盖新状态。

四个 Agent 继续复用严格 `AgentEnvelope` 思路：通用层只包含身份/版本/因果、`completed | needs_input | revise | blocked` 状态、Tool/协作请求和各自唯一的 typed payload。Evidence、Care、Safety、Communication payload 互不替代，未知字段拒绝；自然语言摘要不能代替结构化 claim、action、review target 或来源引用。

允许的核心协作：

1. SleepCare → Evidence：请求个人化事实、趋势解释、冲突处理或补充证据。
2. SleepCare → Care：基于已验收 Evidence 请求行动/跟进/协同策略。
3. Care → SleepCare/runtime → Evidence：以 `EvidenceRequest` 请求缺失证据或复核；Care 不能直接改 Evidence。
4. SleepCare/runtime → Safety：提交精确 Evidence、Care、Communication 或 external-action target。
5. Safety → SleepCare/runtime → 责任 Agent：提交结构化 `RevisionRequest`。
6. 任一 Agent → SleepCare：请求一个必要用户事实或确认；只有 SleepCare 与用户交互。

跨 Agent 只共享任务所需字段，不共享完整私有 Context。一个 Agent不能直接写另一个 Agent 的 payload，也不能把未验收草稿当作事实。

相同 Episode、sender、receiver、request type、SourceScope 和输入 hash 的请求必须去重。Care↔Evidence 的补证/复核最多两个有新信息或新 target 的往返，SleepCare 的模型 replan 最多两次；重复请求、仅改写措辞但没有新证据的请求和超过预算的请求进入保守退出，不能继续消耗模型调用。

## Minimal runtime paths

运行时按场景选择最少必要 Agent，不固定全员执行：

| 场景 | 最小智能路径 | 条件分支 |
| --- | --- | --- |
| 晨间解释、个人数据问答 | SleepCare → Evidence → SleepCare | 冲突/高风险内容追加 Safety |
| 趋势分析 | SleepCare → Evidence（Trend Tool）→ SleepCare | 低置信度个人结论追加 Safety |
| 一般睡眠知识 | SleepCare（Knowledge Tool） | 必须标注非个性化；转为个人问题时调用 Evidence |
| 制定照护行动 | SleepCare → Evidence → Care → SleepCare | 目录外/禁忌/高风险追加 Safety |
| 行动跟进、调整或停止 | SleepCare → Evidence（需要客观变化时）→ Care → SleepCare | 重大调整或协同追加 Safety |
| 医生材料 | SleepCare → 必要 Evidence/Care → SleepCare 草稿 → Safety | 之后仍需权限/确认/Artifact Tool |
| 外部通知、分享或导出 | 已验收成果 → Safety | 之后仍需权限/确认/Commit Controller |
| 急症边界 | 确定性规则立即抢占 | 不等待模型 Agent；普通 Episode 标记被中断 |

Care 只消费已验收 Evidence，不读取原始传感器数据。Safety 审查精确成果及其来源，不重新完成专业工作。

一般知识路径只允许解释概念和通用健康教育。只要回答开始使用个人数据、个人记忆或用户当前状态形成结论，就必须调用 Evidence；只要开始为该用户选择、调整或跟进具体行动，就必须调用 Care。

## Safety and authority order

从高到低：

1. 急症、身份、权限、隐私、用户确认和外部执行等确定性规则。
2. 版本化数据/ToolReceipt 与已验收 Evidence。
3. 范围受限的 Safety 决策。
4. 已验收 Care 策略。
5. SleepCare 的编排与表达选择。
6. 长期记忆、一般知识和未确认用户输入作为带来源上下文。

低层不得覆盖高层。Safety 可以阻断某个 Evidence claim 的使用或要求 Evidence 重新处理，但不能把自己的判断写成新的 canonical 事实；Safety 批准不等于允许执行。SleepCare 的最终发布不得改变上游已验收语义。发布前继续执行确定性 postflight，检查 claim、数值、范围、Care 参数、Safety target、权限和确认是否漂移。

Safety checkpoint 按 target 分开，不能用一次批准覆盖后续阶段：

1. 有争议/低置信 Evidence 在被 Care 消费或作为个人结论发布前审查。
2. 高风险或目录外 Care 候选在展示并请求用户确认前审查。
3. 医生材料和命中受限表述的 Communication Draft 在发布前审查。
4. 通知、分享、导出等 external-action target 在请求最终确认和执行前审查；执行时再次校验权限、确认、target hash、有效期与幂等键。

## Deterministic acceptance gates

模型输出只有通过其责任类型的确定性门才成为已验收工作成果：

1. **Evidence gate**：验证 SourceScope、canonical/ToolReceipt 引用、时间与覆盖、claim 类型、置信度/未知项和禁止的行动字段；无来源个人 claim 不得进入 Ledger。
2. **Care gate**：验证所引用 Evidence 仍有效、行动 ID/版本属于 Care catalog、参数在允许范围、单一主要行动不变量、确认要求和禁止的新事实字段。目录外自由文本只能作为不可激活的讨论内容；在完成领域审阅和 catalog 登记前不得通过确认变成行动。
3. **Safety gate**：验证 review target 的类型、ID、hash、FactSnapshot、Episode revision、Policy 版本、问题位置、责任 Agent、决定有效期和复审轮次。
4. **Communication gate**：验证最终文本/Artifact 的每个个人 claim、数值和行动均映射到已验收 ID，角色权限正确，未增加因果、诊断、药物或执行成功表述，并重新运行 Safety 触发检查。

验收 Gate 与 Agent 输出 Schema 分离并由 runtime 执行；Agent 自报 `complete`、`safe` 或高置信度不能替代验收。

任何可激活 Care 候选和外部动作确认必须绑定 `candidate_id + candidate_version/hash + actor + subject + action_scope + expiry`。候选内容、目标对象、SourceScope、事实版本、权限或时间范围变化后，旧确认自动失效且不能重放。

## Risk evaluation order

风险与急症能力继续是确定性 Tool/Policy，而不是第五个 Agent：

1. runtime 在任何模型调用前执行 urgent boundary、身份、权限和最小数据可用性 preflight；命中急症时立即抢占。
2. Evidence 通过验收后，runtime 才对已接受 claim 执行证据相关风险分类，并把版本化结果提供给后续 Care/Safety/SleepCare。
3. Agent 不得自行降低或覆盖确定性风险结果；Safety 可以提高发布审慎程度，但不能把硬风险降级。
4. 在线事件的风险输入固定为绝对红旗、相对个人基线偏离、多源一致性、数据质量、当前场景和纵向趋势。`absolute_red_flag=true` 时习惯只能改变解释；`risk_level=escalate` 时 runtime 必须补入 Safety，urgent 红旗必须在模型调用前抢占。

## Failure and bounded recovery

职责不可越权接管：

- Evidence 失败：不产生新的个性化事实或解释；只展示与当前 SourceScope/权限/有效期匹配且已验收的已有记录、一般知识或明确的暂时无法判断。
- Care 失败：模型层不创建或修改行动，默认保持现有状态并说明暂不能形成建议；但急症、权限撤回、确认失效或确定性禁忌策略仍可阻止/暂停相关执行，不能因为 Care 不可用而继续一个已被硬规则禁止的动作，也不能自动生成替代行动。
- 必须触发的 Safety 失败/超时：阻断对应内容、材料或动作。
- SleepCare 失败：不让其他 Agent冒充发布者；runtime 只可使用预先审定的急症、数据不足或系统故障模板。

Safety 修订最多两轮；普通 replan、模型重试和 Tool 重试也必须有 Episode 预算。未知外部执行结果不得当作失败后盲目重试。任何降级均在 Receipt 中记录执行模式、失败位置、缺失成果和用户可见边界，不伪装成完整智能路径。

## Skills and controlled evolution

### Stable kernel

继续复用 `GlobalPolicy + AgentProfile + SkillBundle + ContextPacket + OutputSchema` 的编译模型：

- `GlobalPolicy` 保存所有 Agent不能降低的医学、安全、隐私、确认和证据底线。
- `AgentProfile` 固定四个 Agent 的责任、Context、输出和权限上限。
- `Skill` 只定义 owner Agent 如何完成一项可独立评测的原子判断。
- `EpisodeDefinition`、Schema、allowlist、Policy 和 Commit Controller 不属于 Skill。

### Skill ownership migration

保留现有原子能力，重新归属而不复制：

| Owner | Skill 能力 |
| --- | --- |
| SleepCareAgent | Episode 规划、检查点评估、冲突协调、一般知识问答、最小澄清、角色化解释、角色材料草拟、记忆变更候选 |
| EvidenceReasoningAgent | 范围证据解释、证据冲突综合、纵向趋势解释 |
| CareStrategyAgent | 单一照护行动规划、跟进结果评估、协同候选规划 |
| SafetyReviewAgent | 事实与能力边界审查、行动与发布审查 |

每个部署 Skill 只有一个 owner。共享计算下沉 Tool，共享知识进入 reviewed Knowledge Store，共享硬边界进入 Policy；不得通过复制 Prompt 让多个 Agent各自产生同类事实或行动。

睡眠习惯是上述四角色上的能力组合，不满足新增 Agent 的条件。其架构冻结附件为：

- [`../sleep_habit_profile/V1-CONCEPT-COVERAGE.md`](../sleep_habit_profile/V1-CONCEPT-COVERAGE.md)：定义 Subjective Habit Profile、Recent Context、ObjectiveBaseline 和 Safety 的概念归属；
- [`../sleep_habit_profile/HABIT-SKILL-TOOL-ROUTING.md`](../sleep_habit_profile/HABIT-SKILL-TOOL-ROUTING.md)：把 SleepCare 最小提问/画像候选、Evidence 解释、Care 使用、Safety 抢占和 Commit Controller 唯一写入连接到现有 Skill/Tool；
- [`../sleep_habit_profile/DECISION-GAP-MAPPING.md`](../sleep_habit_profile/DECISION-GAP-MAPPING.md)：规定 Evidence/Care 何时可以发现缺口、SleepCare 何时可以问、回答必须回到哪个责任主体。
- [`../sleep_habit_profile/ONLINE-REASONING-FREEZE.md`](../sleep_habit_profile/ONLINE-REASONING-FREEZE.md)：规定在线事件自动解析、夜间离床语义、Care Delivery 和多因子 Safety 强制路由。

因此，SleepCare 拥有交互但不拥有个人事实，Evidence 拥有解释和 `E-*` gap 但不拥有行动，Care 拥有行动和 `C-*` gap 但不解释原始答案，Safety 只审查/抢占，Commit Controller 才能写画像。家属回答先进入带 observer provenance 的 Evidence，不能直接覆盖或写入老人画像。这一专题路由不得通过复制成“HabitAgent”、让 Care 直读原始问卷、或让 Questionnaire Tool 自主决定业务目的来实现。

### Only allowed self-evolution path

生产 Agent 的行为能力只能通过受控离线 Skill 演进：

```text
Invocation / feedback / Safety return / failure
→ structured SkillOutcome
→ root-cause classification
→ fixed-input replay + single-variable comparison + ablation
→ existing Skill PATCH candidate
→ normal / edge / historical-failure / safety / regression evaluation
→ human approval
→ shadow
→ canary
→ champion or rollback
```

硬边界：

1. 生产运行面只通过版本锁读取已人工 `approved` 且按发布控制器分配为当前 champion 或当前 subject/household canary 的 Registry 版本；`approved` 但尚未进入对应发布阶段的候选不能承载生产流量。Agent不能在线修改 Prompt、Skill、Schema、权限、Tool 或协作边界。
2. 运行时反思、用户反馈、Safety 退回和失败日志只能形成结构化 `SkillOutcome`，不能直接改变后续生产行为或进入长期记忆成为隐式规则。
3. 自动流程最多生成现有 Skill 的 `PATCH` 候选，不能创建、删除、重命名 Skill，不能改变 Agent/Profile/Episode/Schema/模型路由/权限/allowlist/Policy/确认条件。
4. 单次失败不能触发修改；必须先区分数据、Tool、Context、模型、runtime 和 Skill 问题，并建立可重复归因。
5. 所有候选必须人工审批；医疗解释、风险判断、医生材料和 Safety Skill 还需医学或安全审批。
6. 任一硬安全事件立即停止灰度并回滚上一 champion，同时保留完成调查所需的输入、输出、版本、评测、审批和发布证据；原始敏感内容仅在合法、最小必要、加密和严格访问控制的安全审计区保存，常规 `SkillOutcome` 仍只记录结构化字段、引用和哈希。
7. 演进控制面只能实现为与生产 Agent 隔离的 Service/Policy；不得命名、实现或路由为 `EvolutionAgent` 等额外 Agent 身份。

第一阶段只实现或补齐 Skill Package、Registry、Resolver、Compiler、版本锁和 `SkillOutcome` 接口，不实现自动候选生成、线上自动改写或自动晋级。用户确认的偏好、个人基线与行动状态按 Memory/业务规则更新；底层模型学习与医学知识库更新走各自独立的离线评测和受控发布，不属于 Agent 在线自改。

## Known architectural problems and controls

1. **SleepCare 可能成为超级 Agent。** 通过专业事实/行动形成权分离、严格输入输出合同和 publication postflight 限制其权力。
2. **Evidence 与 Care 可能来回争论。** 只允许类型化请求、中心路由、精确 target 和有界重试；不能自由聊天。
3. **Safety 可能成为延迟瓶颈或万能审查。** 采用明确条件触发、范围受限否决、两轮上限和确定性规则优先。
4. **多个 Agent 可能重复读取和解释同一数据。** FactSnapshot 固定外部输入，Care 只读已验收 Evidence，共享计算只做一次并以 ToolReceipt 引用。
5. **主 Agent 最终措辞可能篡改专业成果。** 发布前比较 claim/action/number/target hash，禁止通过自由改写引入新语义。
6. **长期记忆可能污染当前事实。** 唯一 Memory Service、来源/有效期/确认元数据和 Evidence 当前验证共同控制。
7. **多 Agent 增加成本、延迟和失败面。** 场景最小路径、条件 Safety、调用预算、缓存不变工作成果和显式降级。
8. **Skill 自进化可能形成隐式越权。** 生产/控制面隔离、PATCH diff allowlist、版本锁、人工审批和回滚。
9. **新旧架构可能长期双轨。** 原地迁移同一 namespace，版本升级后删除旧 roster，不建 v2 平行实现。
10. **旧身份、alias 或动态 Agent 可能重新泄漏。** 通过封闭 roster、manifest/registry 不变量、import 检查和生产入口搜索门禁阻断。

## Approach

1. 将本计划设为目标 Agent 架构规范，并同步标记冲突旧文档为迁移资料。
2. 在现有 `product_agent` namespace 内将 Orchestrator 与 Dialogue 合并为 SleepCare，重命名 Evidence/Care，并保留 Safety。
3. 从 Agent registry、payload、allowlist、EpisodeDefinition 和 runner 中移除 Trend、AlertCare、Report、Memory 的 Agent 身份，把现有能力接回对应 Tool/Service/Skill。
4. 更新 Agent contracts、Context、工作成果、中心路由、Safety 两轮修订和职责不可接管的降级规则。
5. 复用现有 FactSnapshot、revision、CAS、Receipt、确认、Commit Controller 和 publication postflight，只做新 roster 所需调整。
6. 按三类状态模型收口 Memory，确保唯一长期存储和无私有长期记忆。
7. 更新现有 Skill 计划和 owner/Profile/Resolver 映射，补齐第一阶段 Skill Foundation 接口。
8. 更新现有测试而不是复制测试套件；验证角色名单、最小路径、越权拒绝、Safety、Memory、并发、确认、发布和降级。
9. 提升内部 Schema/Registry/Receipt 版本；旧结果只允许离线只读查看，不提供生产双轨恢复。
10. 新路径验收后删除旧 AgentId、payload、allowlist、专属兼容代码和冲突文档表述。

## Key decisions & tradeoffs

1. 当前锁定精确四个 Agent，并放弃在本规范内动态扩展 roster；新增能力必须落入 Skill、Tool、Service 或 Policy，换取责任和生产路径的长期确定性。
2. SleepCare 合并编排与对话，减少一次角色转交；代价是必须用专业成果独占权和发布检查约束主 Agent。
3. Evidence 与 Care 保持独立，换取事实和行动责任清晰；代价是多一次结构化交接。
4. Safety 保持独立但条件触发，换取生成/审查职责分离；代价是高风险路径延迟增加。
5. 四个旧子 Agent降级为 Skill/Tool/Service，减少上下文重复、成本和责任重叠；这些能力不得再次升级为独立 Agent 身份。
6. Agent无直接写权限，牺牲部分“自主执行”表象，换取医疗健康场景的可控、可确认和可审计。
7. 自进化只作用于离线 Skill PATCH，放弃在线自改的展示效果，换取版本可追踪、审批和回滚。
8. 不把消融实验作为本轮设计成立或实施的前置条件；架构依据是产品责任、闭环、权限和失败后果。实验比较可在未来研究设计中另行讨论。

## Risks / open questions

1. 四个 Agent 的最终模型选择、延迟预算和单次 Context/token 上限仍需实现阶段测量。
2. reviewed Care catalog 的具体行动、禁忌和医学审批人需要领域工作单独确认。
3. Safety 触发规则的误阻断率需要上线前通过场景集调优，但不能因此放宽硬规则。
4. 长期 Memory 的具体保留期、敏感字段分类和授权撤回传播规则需与数据治理方案对齐。
5. 当前 `product_agent` runner 体积较大；本轮不拆 `ProductEpisodeRunner`、不改变其状态机，只允许引入明确的四角色边界并让 Runner 受控委托。
6. 新需求可能被误分类为新 Agent；本规范要求先证明其应归入现有四角色的 Skill、Tool、Service 或 Policy，并由静态 roster 门禁拒绝额外身份。

## Out of scope

1. 不新增面向用户的产品功能；实现修改仅限 Agent 架构收口。
2. 不把单 Agent对照或消融实验设计作为当前交付物。
3. 不实现自动 Candidate Generator、自动审批、线上 Skill 改写或自动晋级。
4. 不定义、命名、实现或注册任何第五 Agent，也不预留空身份或 alias。
5. 不重做产品定位、前端信息架构或底层睡眠模型训练方案。
6. 不执行与本 Agent 架构收口无关的旧研究代码清理。
