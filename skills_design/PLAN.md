# Plan: SleepAgent Skills 与受控自进化基础
_Locked via grill — by Codex + user_

## Goal

在已经锁定的产品定位和四角色产品级 Agent 架构之上，为新版 `product_agent` 建立唯一、可组合、可评测、可追溯的 Skills 体系。Skill 是不携带执行代码的版本化行为包；确定性策略先计算合法候选集，SleepCareAgent 只能在集合内选择，运行时将 Global Policy、AgentProfile、Skill、Context 和 Schema 编译为可重建的 Prompt Bundle，并为每个 Episode 固定完整 `SkillLock`。本轮同时锁定未来自进化的控制面、评测、审批、灰度和回滚边界，但下一次实施只交付 Foundation：18 个新版基线 Skill、Registry、Resolver、Compiler、SkillLock、最小 Outcome 合同和审计链；不启用候选生成或线上自动进化。

## Inherited decisions and authority

1. `product_positioning/PLAN.md` 继续决定第一用户、非诊断边界和“晨间解释 + 个性化对话照护”产品主线。
2. `product_information_architecture/PLAN.md` 继续决定老人、家属、医生的用户入口与信息归属。
3. `agent_architecture/PLAN.md` 是 Agent 名单、职责、Episode、工具、权限、Safety、Memory 和发布权的上位规范；Skill 不得改变这些决定。
4. 本计划是新版 `product_agent` 的 Skill 定义、Catalog、解析、编译、版本、评测和演进治理规范。
5. 旧 `sleepagent/radar_agent/skills`、旧三层 Prompt、fixed/dynamic legacy runtime 不进入目标设计，不提供兼容适配、fallback 或双轨 Registry。Foundation 先证明新版路径零 import/零调用；旧文件的物理删除留给新版验收后的独立清理。
6. 本计划不改变确定性 Agent/Tool/A2A allowlist、Episode 必经路径、硬安全策略或确认机制；这些边界的版本引用可以进入 Skill 合同，但控制权始终在运行时。

## Core model and invariants

### 1. Separate the stable kernel from evolvable behavior

1. `GlobalPolicy` 保存所有 Agent 共享且不可由 Skill 或自进化降低的医学、安全、隐私、权限、急症、确认和证据底线。
2. `AgentProfile` 定义一个 Agent 的身份、职责、授权上限、输入信任边界、输出合同和绝对禁区；Profile 稳定、显式版本化，不属于普通自进化可变面。
3. `Skill` 只承担一个可独立评测的判断能力。它声明适用条件、任务指令、所需上下文、完成标准、允许申请的工具/A2A 子集、失败/拒绝/降级行为、示例和 Scorecard。
4. `SkillBundle` 是某次 Agent 调用所使用的一个或多个兼容 Skill 的有序集合。
5. `EpisodeDefinition` 继续定义流程必经项、允许集合、预算、安全检查点和退出条件；它不是 Skill，模型也不能通过 Skill 改写它。
6. Prompt 是 `GlobalPolicy + AgentProfile + SkillBundle + ContextPacket + OutputSchema` 经版本化 Compiler 生成的派生产物，不是可独立手工修改的第二事实来源。

### 2. Skill authority boundary

Skill 可以指导 Agent 如何完成其已有职责，但不能：

- 授予或扩大身份、数据、Tool、A2A、发布、记忆写入、确认或副作用权限。
- 携带 Python、Shell、模板扩展插件或任何其他可执行代码。
- 实现确定性数据计算、风险分级、urgent matching、Schema 校验、权限检查或提交逻辑。
- 持有模型权重、长期记忆、原始用户记录、RAG 医学正文或工具实现。
- 改变 owning Agent、Episode 类型、Agent 层级、唯一发布者或固定安全骨架。
- 直接选择、批准、发布、回滚或撤销自己的版本。

### 3. Single ownership and atomic responsibility

1. 每个可部署 Skill 只有一个 owning Agent 和一个主要工作成果合同。
2. 不同 Agent 即使处理相似主题，也使用不同 Skill；同一 Agent 的不同原子判断也必须用独立 Skill 和输出合同，例如 Evidence 的趋势证据判断不能与 SleepCare 的角色化表达合成一个部署单元。
3. 共享计算下沉 Tool，共享医学知识进入 reviewed RAG，共享硬边界进入 Policy。
4. 可复用的只读措辞片段只是 Compiler 构建材料，不是可独立选择或进化的 Skill。
5. 不建立跨 Agent 巨型 Skill，也不把完整 `morning_review` 或 `care_plan` 流程包装为 Skill。

## Skill Package contract

### 4. Package layout and source of truth

新版 Package 采用不可执行、内容寻址、发布后不可变的目录：

```text
sleepagent/radar_agent/product_agent/skills/
├── contracts.py
├── loader.py
├── registry.py
├── resolver.py
├── compiler.py
├── locking.py
├── policy/global/<semver>/
├── profiles/<owning_agent>/<semver>/
└── catalog/<owning_agent>/<skill_id>/<variant_id>/<semver>/
    ├── manifest.yaml
    ├── instructions.md
    ├── examples/          # 可进入 Prompt 的正反例
    ├── evals/
    └── changelog.md
```

Package 文件是 Skill 内容的唯一事实来源；GlobalPolicy 与 AgentProfile 同样采用不可变、内容寻址、Registry-attested 的只读 artifact，不允许在 Compiler 代码中藏第二份领域指令。数据库或运行时状态只记录生命周期、资格、流量、审批、评测和精确内容哈希，不复制一份可编辑正文。`package_hash` 使用规范化文件清单计算：路径按 UTF-8 字节序排序，内容按原始字节哈希，`manifest.yaml` 中的 `package_hash`/签名字段不进入自身 digest；签名采用独立的 Registry attestation，绑定 package hash、资格、状态和发布环境，避免自引用哈希。生产运行时只信任配置的发布公钥并在模型调用前验证 attestation、包哈希、文件 allowlist 和路径完整性；开发/测试使用显式 test trust root，不能静默关闭验证。Loader 一次性读取并验证全部字节后生成不可变内存对象，Compiler 只能消费该对象而不能重新按路径打开文件，消除校验后替换的 TOCTOU 窗口。

### 5. Required manifest fields

每个 `manifest.yaml` 至少声明：

1. `skill_id`、`variant_id`（无变体时为 `default`）、`version`、同 variant 的 `parent_version`、`package_hash`、状态无关的内容元数据。
2. `owning_agent`、单一职责 `objective`、`risk_tier`、支持的语言/角色 variant。
3. `applicability`：允许的 Episode、SourceScope、输入语义、数据质量条件和触发条件。
4. `required_context_labels`、最小必要字段、禁止字段和最大 Context/Skill token 预算。它们只是 Skill 可消费字段的上限：Context assembler 仍先按认证、AgentProfile、Episode objective 和最小必要原则裁剪，不能因为 Skill 声明需要就扩大披露。
5. `output_schema_id/version` 与确定性验收合同引用。
6. `allowed_tool_requests`、`allowed_delegation_requests`、`allowed_a2a_requests`；均必须是上位 Registry allowlist 的子集。
7. `requires`、`conflicts_with`、顺序约束和兼容版本范围。
8. `failure_modes`、拒绝条件、降级行为和禁止行为。
9. `prompt_example_refs`、每次最大 example 数和选择规则；只有显式引用、通过静态扫描且计入 token 预算的 `examples/` 文件可进入 Prompt。`scorecard_id/version`、开发集引用、公开回归集引用位于独立 `evals/`；隐藏集只由评测服务解析，不能写入包内容。
10. `model_capability_requirements`，不包含供应商专用 Prompt 语法。
11. 作者/生成来源、变更假设、适用数据授权级别和 changelog 引用。

Loader 必须使用无对象构造能力的安全 YAML 解析器，并以字节大小、文档深度、集合长度和总节点数限制防止资源耗尽；拒绝未知 tag、anchor/alias/merge key、未知字段、缺失引用、路径穿越、软链接、非 allowlist 文件、代码文件、重复 ID/version、Unicode 归一化后碰撞、哈希不一致和签名异常。Candidate static validator 还必须对父版本做字段级 diff allowlist：自动 PATCH 只能改变 instructions、Prompt examples、changelog 和允许收紧的 applicability 字段，不能改 `evals/`、Scorecard、manifest 权限/依赖/风险字段或发布证明。

### 6. Version semantics

1. `PATCH`：同一 `skill_id + variant_id` 内职责、适用范围、依赖和输出合同不变的指令、示例、错误处理或表达改进；自动候选只能创建 PATCH。
2. `MINOR`：同一 Agent、同一输出合同下由人工设计的新可选行为或受控 variant。
3. `MAJOR`：职责、范围、Schema、依赖、工具范围或语义不兼容变化；必须进入 Agent/运行时架构评审，不能由普通自进化产生。
4. Registry 可用 SemVer 范围做静态兼容校验，但每次调用和 Episode 必须解析并锁定精确版本与内容哈希，禁止浮动依赖。

## Initial Skill Catalog

### 7. Foundation baseline

Foundation 人工编写并以 `1.0.0` 发布以下 18 个逻辑原子 Skill；`default` 以外的 variant 是独立目录、独立版本/哈希/资格/流量单元，不增加逻辑 Skill 数，也不能与同 Skill 的其他 variant 共享晋级结论：

| Owning Agent | Skill | Responsibility |
| --- | --- | --- |
| SleepCareAgent | `plan_episode` | 在注册 Episode、权限、必经成果和预算内提出最小计划 |
| SleepCareAgent | `evaluate_work_product` | 基于已验收成果和完成合同提出 continue/replan/wait/finish 建议 |
| SleepCareAgent | `resolve_agent_conflict` | 对显式冲突请求复核、保留分歧或选择安全下一步 |
| EvidenceReasoningAgent | `interpret_scoped_evidence` | 将一个明确 SourceScope 内质量门控后的单晚或范围证据分层为事实、自述、知识、推断与未知 |
| EvidenceReasoningAgent | `synthesize_evidence_conflict` | 暴露冲突、替代解释、缺口和可回答范围 |
| EvidenceReasoningAgent | `interpret_longitudinal_pattern` | 解释确定性 Trend Tool 产生的窗口、覆盖、基线、幅度和不确定性 |
| CareStrategyAgent | `propose_single_care_action` | 从已接受 Evidence 生成目录内低负担候选或无需行动结论 |
| CareStrategyAgent | `assess_followup_outcome` | 综合后续客观变化与主观反馈，避免因果/疗效宣称 |
| CareStrategyAgent | `draft_coordination_candidate` | 生成去重、有限时、待确认的家属/医生/设备协同候选 |
| SafetyReviewAgent | `review_claim_and_boundary` | 审查事实、推断、能力边界和医疗表述，不能覆盖硬规则 |
| SafetyReviewAgent | `review_action_and_publication` | 审查候选行动、材料、共享和外部动作目标 |
| SleepCareAgent | `answer_grounded_question` | 基于已接受 Evidence/RAG 回答并区分个人/一般信息 |
| SleepCareAgent | `ask_minimal_clarification` | 从版本化问题库选择一个必要、低负担、可回答的问题 |
| SleepCareAgent | `explain_for_elder` | 把同一事实转为老人可理解的结论、依据、未知和下一步 |
| SleepCareAgent | `draft_user_material` | 基于同一 claim 集生成老人或家属材料草稿；elder/family 是分别批准、分别评测的 variant |
| SleepCareAgent | `draft_doctor_material` | 生成带日期、覆盖、来源和 caveat 的医生材料草稿 |
| SleepCareAgent | `propose_memory_change` | 生成 create/replace/expire 候选，不执行持久化 |
| SleepCareAgent | `select_memory_context` | 选择 Memory Tool 返回的最小相关、带来源、确认、时间和过期状态的上下文 |

趋势计算、数据质量、风险分级、急症匹配、权限、RAG 检索、Artifact render、Memory commit 和外部动作继续属于 Tool/Policy，不进入 Catalog。

### 7.1 Baseline resolution matrix

Skill 选择必须服从下表；“mandatory”指该 Agent 一旦被本 Episode 调用就必须加载，不改变 `agent_architecture/PLAN.md` 对 Agent 是否必经的上位规定：

| Invocation / Episode | Mandatory Skill | Optional/conditional Skill |
| --- | --- | --- |
| SleepCare initial plan/replan | `plan_episode` | `resolve_agent_conflict` 仅在已有显式冲突输入时 |
| SleepCare checkpoint | `evaluate_work_product` | `resolve_agent_conflict` 仅在冲突 checkpoint |
| Evidence / `morning_review` | `interpret_scoped_evidence` | `synthesize_evidence_conflict` 仅在冲突/矛盾 |
| Evidence / `trend_review` | `interpret_scoped_evidence` | `synthesize_evidence_conflict` 仅在窗口/指标冲突 |
| Evidence / `data_quality_recovery` | `interpret_scoped_evidence` | 无；输出仍受 unknown-only deterministic acceptance |
| Evidence / care/dialogue/material | `interpret_scoped_evidence` | `synthesize_evidence_conflict` 仅在实际冲突 |
| Evidence / trend or follow-up comparison | `interpret_longitudinal_pattern` | 无；窗口与统计由 Trend Tool 产生 |
| Care / `care_plan` | `propose_single_care_action` | 无 |
| Care / `care_followup` | `assess_followup_outcome` | `propose_single_care_action` 仅在用户明确请求新/改行动且 Episode 合同允许 |
| Care / coordination candidate | `draft_coordination_candidate` | 无；调度、去重与发送仍由 Tool/Policy/Commit Controller 执行 |
| Safety / evidence target | `review_claim_and_boundary` | 无 |
| Safety / action, material, share/export target | `review_action_and_publication` | `review_claim_and_boundary` 仅当同一 target 含新的个人推断 |
| SleepCare / `morning_review`, `trend_review`, care | `explain_for_elder` | `ask_minimal_clarification` 仅在已有 accepted gap/question-bank candidate |
| SleepCare / `grounded_dialogue` | `answer_grounded_question` | `ask_minimal_clarification`、`explain_for_elder` 按角色/缺口 |
| SleepCare / elder or family material | `draft_user_material` 的精确 role variant | 无 |
| SleepCare / doctor material | `draft_doctor_material` | 无，且之后必须 Safety review |
| SleepCare / Memory read | `select_memory_context` | 无；底层查询由 Memory Tool/Service 完成 |
| SleepCare / Memory write candidate | `propose_memory_change` | `select_memory_context` 仅为 compare/replace/expire；提交仍由 Commit Controller 完成 |
| Runtime / `data_quality_recovery` | 不加载 Skill，走注册的确定性模板 | 不得生成新的个人结论 |

`urgent_boundary` 不加载任何 Skill，继续由确定性抢占和固定安全提示完成。若表中没有合法组合，运行时不得临时选择“最接近”的 Skill。

### 7.2 Sleep habit specialization of existing Skills

睡眠习惯能力不创建第十九个 Skill，也不创建画像专用 Agent。其权威逐跳路由见 [`../sleep_habit_profile/HABIT-SKILL-TOOL-ROUTING.md`](../sleep_habit_profile/HABIT-SKILL-TOOL-ROUTING.md)，有限决策缺口见 [`../sleep_habit_profile/DECISION-GAP-MAPPING.md`](../sleep_habit_profile/DECISION-GAP-MAPPING.md)，在线事件与 Safety/Care Delivery 合同见 [`../sleep_habit_profile/ONLINE-REASONING-FREEZE.md`](../sleep_habit_profile/ONLINE-REASONING-FREEZE.md)。现有 Skill 的冻结职责如下：

| Existing Skill | Habit responsibility | 可请求的确定性 Tool 子集 | 明确禁止 |
| --- | --- | --- | --- |
| `ask_minimal_clarification` | 只在显式用户目的或已注册的 Evidence/Care gap 下请求、呈现和接收一个最小问题 | `questionnaire.select_profile`, `questionnaire.capture_profile` | 自由发明 concept、以完整度驱动追问、解释原始回答、写 Profile |
| `select_memory_context` | 在显式画像复核中选择最小相关 typed Profile slice | `profile.read` 及已有 Memory query/compare | 扩大角色 scope、把 ObjectiveBaseline 当 Memory、整库读取 |
| `propose_memory_change` | 对 profile-eligible、来源有效的候选建立 create/replace/expire/forget change set 和确认摘要 | `profile.read`, `profile.build_change_set` 及已有 compare | 执行确认、提交、把家属报告升级为老人自述 |
| `interpret_scoped_evidence` | 解释当前回答、观察报告、最小 Profile、ObjectiveBaseline 和在线事件的关系 | 已有 Evidence reads、`reasoning.resolve_event_context`, `profile.read`, `baseline.read` | 因果宣称、Care 选行动、Profile 写入 |
| `synthesize_evidence_conflict` / `interpret_longitudinal_pattern` | 保留并解释主观/家属/Profile/客观来源的实际冲突和变化 | Evidence/Knowledge/Memory reads、`profile.read`, `baseline.read` | 为“整理档案”静默选真或覆盖 |
| `propose_single_care_action` / `assess_followup_outcome` | 只从已验收 Evidence 使用目标、约束、偏好和负担，并形成目录内 delivery；需要补问时生成有限 `C-*` gap | 既有 Care/coordination 只读 Tool与 `device.read_delivery_policy` | 读取原始 Questionnaire capture、直接读写 Profile、绕过安静时段/家属通知 Policy |

Questionnaire selection/capture 可以由 runtime 在相应 plan-step 边界确定性执行，但语义上仍受 `ask_minimal_clarification` 的单 owner、适用条件和 Scorecard 约束。Habit Profile 的变更 Skill 不调用 `confirmation.validate` 或 `state.commit_habit_profile`：画像确认/提交由确定性 runtime 与 Commit Controller 完成。既有 Safety 发布审查 Skill 可以为其独立审查目的只读校验 confirmation，但不会因此获得 Profile 写权限；`state.commit_habit_profile` 始终只属于 Commit Controller。

Foundation 实现必须让 Skill Package 声明、Agent/Profile Tool allowlist、runtime 可见性和 Tool receipt 路由共同满足上表；仅在 runner 中硬编码调用而 Skill Package 不声明职责，或只在 Skill 文案中声明而底层 allowlist 不允许，均不算工程完成。本段冻结职责与权限，不表示当前代码已经完成该增量。

## Runtime resolution, compilation, and replay

### 8. Deterministic resolver with bounded SleepCare choice

1. `SkillResolver` 先使用认证身份、Agent、Episode、SourceScope、角色、语言、数据质量、Schema、ModelProfile、工具/A2A allowlist 和 Registry snapshot 计算合法候选集。
2. Episode/Agent 注册表声明 mandatory Skill；SleepCare 的严格计划只能为具体 `plan_step_id + target_agent + invocation_purpose` 提交 `SkillSelectionRequest(selected_optional_skill_ids, reason_codes)`，不能提交指令文本、版本或路径。运行时在真正调用时以当前 accepted state 重算候选，把请求字段与 mandatory 集合合并，并对最终 bundle 再执行 Resolver 校验；跨 Agent、陈旧 step、当前已不适用或不在候选集的 ID 一律拒绝。
3. Resolver 检查 `requires`、冲突、顺序、token 上限、最大 Skill 数、生命周期、资格和精确版本兼容性。
4. 固定编译顺序为 `GlobalPolicy → AgentProfile → mandatory Skills → task Skills → expression Skills → ContextPacket → OutputSchema`。
5. Resolver/Compiler 冲突、依赖缺失、无合格模型组合、包被撤销或预算溢出时，在模型调用前拒绝并进入 Episode 注册的安全降级/partial/blocked 路径；模型不得自行调和。
6. 相同输入、上位 Registry、Registry snapshot 和 revocation epoch 必须产生相同的候选集与有序 SkillBundle；Compiler 对相同 bundle/Context 产生相同 Prompt Bundle hash。

### 9. Prompt compiler and invocation contract

1. 移除新版 `ProductAgentInvoker` 和 SleepCare plan/evaluate 中散落的 `product-*.prompt.v1` 行为来源；所有新版模型调用必须消费 Compiler 产出的消息和元数据。
2. Compiler 将用户文本、Memory、Tool 输出、RAG 和设备内容标为不可信数据，使用结构化边界与 system/developer 指令隔离。
3. 编译结果记录 Policy、Profile、Skill、Context、Schema、Compiler、ModelProfile 的精确版本/哈希和最终 Bundle hash。
4. 默认不持久化原始敏感 Prompt；保存可重建的不可变 Package/Profile/Policy/Schema 引用、Context hash、token 统计和允许的脱敏调试快照。只要授权 Context 快照仍在保留期内，就能重编译 Prompt；快照到期后只能验证版本/哈希和因果链，不能声称可恢复已删除的敏感内容。任何调试快照服从认证授权与保留策略。
5. 输出仍由既有严格 Pydantic Schema、invocation policy、Agent acceptance 和 publication postflight 验收；Skill 文本不是接受结果的依据。

### 10. SkillLock and Episode consistency

1. Episode 创建时生成不可变 `SkillLock`：固定 Registry snapshot、revocation epoch、Profile、Policy、Compiler、Schema、按优先级排列且分别取得资格的 primary/fallback ModelProfile 集合，以及本快照下每个 Skill ID/variant 可解析到的精确版本映射。它固定版本宇宙，但不冻结尚未取得的数据质量、SourceScope 或 Agent 调用资格，也不假装未来 optional 选择已经发生。
2. 每次 Invocation 使用当前已验收 Episode state 和固定版本宇宙重新计算 applicability，再生成不可变 `InvocationSkillLock`，记录实际 mandatory + optional SkillBundle、顺序、内容哈希、实际 ModelProfile、Resolver 输入/决策、Prompt Bundle hash 和父因果引用；该锁只能引用 Episode SkillLock 中的精确版本和已认证模型集合。主模型失败只能按既有预算切到锁内已认证 fallback，并创建新 Invocation/Bundle hash；没有合格 fallback 时诚实降级。若新外部事实按上位架构要求创建新 FactSnapshot/replan，也仍不能引入锁外版本；确需新版本时结束旧 Episode 并创建关联 Episode。
3. 等待用户/确认后恢复同一 Episode 时继续使用原 SkillLock；普通 Registry 晋级或回滚不改变它。
4. 普通回滚只原子改变新 Episode 的默认 champion。安全 revocation 使用独立、单调递增且 fail-closed 的 overlay/epoch，不受旧 Registry snapshot 固定规则限制；运行时在创建 Episode、每次模型调用前、恢复和发布前重新检查。被 `revoked` 的旧锁不可继续：未完成 Episode 诚实终止为 partial/blocked，需要继续则基于新 champion 创建关联 Episode，并只复用已验收事实。
5. 历史 Artifact、Receipt 和调用记录永远引用实际旧版本，不因晋级而重写。
6. Foundation 扩展现有 Episode result/checkpoint/persistence 合同，使 Episode SkillLock、InvocationSkillLock 和 revocation check receipt 能跨进程恢复；如果现有持久化层尚未覆盖 product Episode，则采用 additive schema/record，不做破坏性迁移。

### 11. Model compatibility

1. Skill 只声明能力要求；供应商格式差异由 `ModelAdapter` 处理。ModelAdapter 只能改变协议封装和供应商字段映射，不能注入新的领域指令或修改 Skill/Policy 语义。
2. 发布资格绑定 `Skill version × variant × ModelProfile`。主模型与每个降级模型分别认证，未认证组合不可写入 Episode SkillLock 或加载。
3. ModelProfile 固定 provider/model、关键推理参数、结构化输出能力和 adapter version。模型或关键参数变化必须重跑受影响 Skill/Bundle 的兼容与安全回归。
4. Skill 优化若只利用某个模型的偶然措辞或失败模式，且不能在声明的模型范围内稳定通过，视为过拟合并拒绝。

### 12. Personalization boundary

1. 用户偏好、作息、已确认行动、纠正和表达习惯进入版本化 Memory/Context，不生成用户专属 Skill。
2. 老人/家属/医生、语言和无障碍表达只能使用人工批准的 variant。
3. 自动问题聚类只使用去标识化、聚合结果；单个用户输入不能生成并加载私有 Skill。
4. 只有具备足够样本、明确适用条件、独立 Scorecard 和人工批准时，才能新增人群 variant。

## SkillOutcome and future evolution control plane

### 13. Minimal outcome contract in Foundation

Foundation 定义但只做最小记录的 `SkillOutcome`：

- Episode/invocation/SkillLock/ModelProfile/Context hash 因果引用。
- Schema、确定性 acceptance、Safety、Evidence 和 publication postflight 结果。
- 工具/A2A 请求合法性、replan/澄清/降级/失败 Reason Codes。
- 用户明确纠错、没听懂、拒绝建议等结构化事件；不能把点赞视为真值。
- 3–7 天 follow-up 的可执行性/完成信息；不得解释为医学疗效或确定因果。
- 延迟、token、模型/工具调用次数与成本。
- 数据授权级别、去标识化状态和保留类别。

停留时长、对话轮数、点击率或诱导继续对话的参与度指标不得作为主要优化目标。

### 14. Separate evolution control plane

未来自进化是与生产 Agent 隔离的控制面，不增加产品 `EvolutionAgent`：

```text
SkillOutcome
→ Failure Miner
→ Root-cause Cluster
→ Candidate Generator
→ Static Validator
→ Sandbox Evaluator
→ Human Approval
→ Skill Registry
→ Shadow / Canary / Rollback
```

生产 Agent 只能读取已批准 Registry，不能调用控制面。Candidate Generator 可使用 LLM，但只能写隔离候选工作区；生成器看不到隐藏/最终保留集，也没有评测结果修改、审批或生产写权限。重复失败聚类必须满足预注册的最小去标识化 cohort 才能进入自动候选流程；单个严重安全事件可以立即触发人工调查和紧急撤销，但不能凭单案例自动改写 Skill。

### 15. Root-cause gate before candidate generation

Failure Miner 必须先把问题分类为数据/设备、Tool/Schema/权限、ModelProfile、Context 裁剪、Resolver/Compiler、Skill、不可满足目标或评测标签问题。`SkillOutcome` 对多 Skill Invocation 默认只能提供 bundle-level 关联，不能把相关性冒充单 Skill 因果；只有固定其他变量后仍稳定复现，并通过单变量 replay/ablation 证明修改目标 Skill 能修复时，才创建该 Skill 候选。一个失败不能同时触发多个组件自动改写；跨组件问题由人工拆解。

### 16. Evolvable and immutable surfaces

自动系统只能提出 PATCH 候选，允许修改：

- instructions 中的任务策略和表达指引。
- 正反例、错误处理、不确定性说明。
- 在原适用范围内收紧触发条件。
- token 预算内的压缩和去冗余。

不能自动修改 owning Agent、权限、allowlist、Schema、确定性完成标准、Policy、Profile、确认、安全边界、适用范围扩张、依赖、风险等级、评测门槛、隐藏集、评分器、审批规则，也不能创建/删除 Skill。系统可以提交“需要新 Skill/Schema/Tool”的诊断建议，但必须进入人工架构流程。

## Evaluation, governance, and release

### 17. Data governance and split integrity

1. 默认不把原始对话或完整健康记录送入候选生成或通用评测。
2. 日常流水线只使用去标识化 SkillOutcome、错误标签和最小证据摘要；开发优先使用合成场景、专家案例和 replay 数据。
3. 真实案例只有在明确授权、去标识化、最小化、访问审计和可追踪删除后，才能进入隔离受控数据集；不得直接复制进 Skill examples。
4. 数据分成问题分析集、候选开发集、隐藏验证集和最终保留集。生成器永远看不到后两者；隐藏验证只返回足以修复类别问题的粗粒度 Reason Codes，最终保留集限制尝试次数并定期轮换，防止通过反复提交反向推断标签。评测服务按不可变数据集版本和哈希记录结果。
5. 数据撤回可定位受影响的数据集与认证；必要时撤销数据集版本并重新认证相关 Skill。

### 18. Five evaluation gates

1. `Static Gate`：Package、签名、哈希、引用、兼容、长度、禁用字段、注入和代码载荷扫描。
2. `Contract Gate`：Schema、工具/A2A、证据引用、拒绝、降级和确定性 acceptance。
3. `Scenario Gate`：覆盖 Agent 架构锁定的正常、数据不足、趋势、冲突、深聊、行动、跟进、医生材料、急症、权限、恢复和模型失败场景。
4. `Adversarial Gate`：医疗越界、隐私、角色越权、Memory/RAG/用户文本投毒、诱导诊断、虚假引用、陈旧确认和上下文冲突。
5. `Comparative Gate`：在隐藏集上对 champion/challenger 做多次、配对、盲评比较，检查质量、可理解性、成本和延迟。

任何硬门失败即淘汰。安全、隐私、事实一致性和权限必须零回退；关键子群分别满足非劣门槛；至少一个预注册目标有实质改善。LLM Judge 只能辅助，硬规则与人工抽检有否决权。

### 19. Per-Skill preregistered scorecards

1. 禁止用单一总分抵消安全或事实退化。
2. 所有 Skill 共享 Schema、权限、隐私、引用、非诊断和急症硬门；每个 Skill 另有与职责对应的主指标、非劣界限、样本量和停止规则。
3. 指标和阈值在生成候选前固定，候选完成后不得修改。
4. 关键安全套件要求零严重失败；普通质量指标使用配对结果和置信区间。统计把握不足的结论是“证据不足”，不得发布。
5. Foundation 为 18 个基线 Package 建立 Scorecard 结构与确定性/场景测试；统计 champion/challenger 门槛和真实数据集在 Evaluation 阶段按治理流程预注册。

### 20. Composition evaluation

1. 候选先做单 Skill 隔离评测，再对所有受影响 SkillBundle 和 Episode 路径做集成回归。
2. 一次实验只改变一个 Skill；模型、Compiler、Schema 或其他 Skill 同时变化时不归因为该 Skill 效果。
3. 多个候选依次晋级，每次重建 champion 基线，禁止批量合并后直接发布。
4. SleepCare 编排 Skill 变更必须回放所有 Episode 类型和 Resolver 分布，因为它会改变其他 Skill 的选择频率与上下文。

### 21. Lifecycle and separation of duties

状态机为：

```text
draft → candidate → validated → approved → shadow → canary → champion → superseded
                      ↘ rejected       ↘ rolled_back
                                         ↘ revoked
```

1. 控制面创建 candidate，评测服务写 validated，具名审批人写 approved，发布控制器推进 shadow/canary/champion。
2. `Skill + variant + ModelProfile` 同时只有一个 champion。
3. rejected 不可复活，修订形成新版本；rolled_back 不得自动重上；revoked 禁止任何新调用。
4. 所有状态变化记录操作者、原因、证据、时间、前后哈希并使用 compare-and-swap。
5. 管理层共用一个治理/追踪视图，但保留职责分离：开发/运营可诊断、建候选、发起评测而不能自批；审批人不能改候选或结果；审计只读；发布器只能执行已满足门槛的状态迁移。紧急撤销可单人执行但必须事后复核。
6. 用户层（老人、家属、医生）不显示 Skill 名称、版本或进化过程，只显示证据、不确定性、建议原因和诚实降级。

### 22. Risk-tiered release

1. Tier 0 表达类：人工批准后 shadow，再按稳定哈希 `1% → 5% → 25% → 100%` 灰度；只有长期证明安全后才可能允许自动晋级。
2. Tier 1 判断类：产品/工程批准，涉及医学内容增加医学审阅；shadow 后小流量灰度并观察完整窗口。
3. Tier 2 SleepCare 编排、Safety、医生材料和外部协同：双人批准及安全/医学审阅；首版永不自动晋级。
4. 灰度按服务端 HMAC(subject/household stable ID, per-experiment salt) 分桶，使同一照护链不会在 champion/challenger 间交叉污染；分桶服务只返回 bucket，不向评测/Skill/模型暴露主体 ID 或 HMAC 原值。同一 Episode 固定实验桶和版本，候选不能因结果不佳在 Episode 内静默切回 champion。
5. 任何硬安全事件立即停止灰度并原子回滚 Registry 默认指针，同时保留全部 Receipt 和候选证据。

## Foundation implementation approach

1. 在 `product_agent/skills/` 定义严格 Pydantic 合同：AgentProfile、SkillManifest、SkillPackage、SkillQualification、ResolvedSkillBundle、SkillLock、InvocationSkillLock、PromptCompilationReceipt、SkillOutcome；拒绝未知字段。
2. 实现只读 Loader 与静态 validator，使用受限安全 YAML parser 按规范化清单读取 allowlist 文件，验证 detached Registry attestation、哈希、引用、父版本字段级 diff 和无代码，并从已验证字节构造不可变内存对象；建立 18 个 `1.0.0` 逻辑 Skill 及 elder/family 等所需 variant Package，测试 trust root 与生产 trust root 显式分离。
3. 从现有 `AGENT_DEFINITIONS`、`EPISODE_DEFINITIONS`、Tool/Agent/A2A allowlist 和输出 Schema 生成不可扩权的 Resolver 输入；任何 Skill 声明必须是这些上位权限的子集。
4. 按 baseline resolution matrix 为八类 Episode 建立 mandatory/optional Skill resolution 规则、最大 Skill 数、顺序和 token 预算；扩展严格 plan/invocation contract，增加绑定 plan revision/step、目标 Agent 和目的的 `SkillSelectionRequest`，使 SleepCare optional 选择只提交候选 ID，并由运行时在调用时合并 mandatory 集、重验适用性/权限和记录 Reason Codes，不接受自由 Prompt/版本/路径。跨 Agent结构化请求产生的新调用必须先由 SleepCare/runtime 建立合法的新 plan step/revision，再生成选择请求，不能绕过该链路。
5. 实现 deterministic Compiler 与 Prompt injection 边界；编译 SleepCare plan/evaluate 和四个 Agent 的所有 Invocation，不留散落 Prompt 行为入口。
6. 扩展 ProductAgentInvoker、SleepCare invocation 和 AgentInvocationRecord，使它们接收/记录 Compiler receipt、SkillBundle、SkillLock、Profile/Policy、实际 ModelProfile 和最终 Bundle hash；主到 fallback 的每次尝试均为独立 Invocation 并消耗同一预算。
7. Episode 创建时解析并保存 Episode SkillLock，每次调用保存 InvocationSkillLock；checkpoint/resume/replay、result receipt、Artifact provenance 和开发 trace 均引用它们。实现独立 revocation overlay 在调用/恢复/发布前 fail-closed 检查，证明普通晋级不改变旧锁而 revoked 能阻止继续。
8. 添加只读 Registry snapshot、单调 revocation overlay 和原子 champion pointer 合同；18 个逻辑 baseline Skill 的每个实际 variant Package 必须各自拥有具名工程/产品审批，涉及 Safety、医生材料和医学表达的包还需安全/医学审批，审批集合与测试报告由 release attestation 绑定。Foundation 不提供 candidate/approval/canary 写 API。新版生产切换只能在 staging/验收通过后一次性启用，启动或校验失败时 fail closed/safe degraded，不回退旧 Skill 系统。
9. 实现最小 SkillOutcome 记录，严格限制内容为结构化结果、Reason Codes、哈希、性能和数据授权元数据，不落原始健康 Prompt 或私有思维过程。
10. 删除新版 `product_agent` 对硬编码 prompt version/拼接行为的依赖，并增加静态/import 测试，证明新路径不会调用旧 Skills/Prompt Registry 或 legacy fallback；旧文件暂不物理删除。
11. 为 Package、父版本 diff、Resolver、Compiler、SkillLock、ModelProfile/Adapter、resume/revoke、上下文最小化、权限子集、Prompt 注入、组合冲突和 18 个 baseline Scorecard 增加单元/属性/集成测试。
12. 运行 Agent 架构的完整行为验收，比较接入 Skills 前后的 accepted work products、Safety、Episode receipt 和 Artifact；任何硬规则回退阻止 Foundation 完成。
13. 编写管理层开发文档：Package 编写规范、版本规则、Scorecard、调用追踪、Registry snapshot、紧急撤销演练和未来控制面接口；不新增用户侧 Skill UI。
14. 增加 Artifact safety correction 合同：Skill 被安全撤销不会静默重写历史材料；若撤销分析确认旧材料可能误导，创建带原 Artifact/SkillLock 引用的 correction/retraction 事件并按原角色授权投递。

## Foundation acceptance criteria

1. 新版所有模型调用都经 Registry、Resolver 和 Compiler，仓库不存在绕过式新版 Prompt 拼装入口。
2. 18 个 baseline 逻辑 Skill 的全部实际 variant 都有合法 Package、单一 owner、Scorecard、SemVer、内容哈希、具名风险级审批、签名/发布证明和测试；`draft_user_material` 的 elder/family variant 分别取得资格和版本锁。
3. 相同输入、上位权限 Registry、Skill Registry snapshot 和 revocation epoch 产生相同候选集；相同 optional 选择与 Context 产生相同 InvocationSkillLock 和 Prompt Bundle hash。
4. 不兼容、冲突、越权、缺依赖、revoked、签名或哈希异常均在模型调用前被拒绝。
5. 每个 Episode 持久化 Episode SkillLock，每次调用持久化 InvocationSkillLock 与 revocation check receipt；暂停恢复、重放、Invocation、Receipt 和 Artifact 能追溯精确版本。
6. 新版路径对旧 Skill/Prompt Registry 零 import、零调用、零 fallback。
7. Agent 架构锁定的行为场景全部通过，urgent、权限、证据、确认、Memory、安全和降级硬规则无回退。
8. Package 无法携带/触发代码，也无法扩大 Tool/A2A/数据范围。
9. Foundation 只记录最小 Outcome，不存在 candidate generator、自动审批、灰度或线上自进化入口。
10. 管理人员可以从 Episode 追到 Agent、Skill、Resolver、Compiler、ModelProfile、Context、输出、验收和 Artifact 的完整因果链，而用户侧不暴露内部 Skill 技术过程。
11. Baseline resolution matrix 覆盖八类 Episode 的每个实际 Agent 调用；`urgent_boundary` 零 Skill，任何未注册组合在模型调用前失败。
12. 安全撤销不会改写历史 Artifact；受影响材料可通过可审计 correction/retraction 流程被定位和更正。
13. Skill 的 Context 声明不能增加 AgentProfile/Episode 原本无权或不必要的字段；最小披露回归逐 Agent/Skill 通过。
14. 主模型失败只能调用 Episode SkillLock 内已对当前 SkillBundle 认证的 fallback ModelProfile；每次尝试独立记录且没有合格 fallback 时诚实降级。
15. Loader/Compiler 只使用同一份已验证不可变字节对象；测试在校验后替换磁盘文件不能改变本次 Registry snapshot 或 Prompt。

## Key decisions & tradeoffs

- 选择原子、单 owner Skill 和运行时组合，增加 Package 数量与集成测试成本，换取清晰归因、可评测性和安全演进。
- 让确定性 Resolver 先限权、SleepCare 后选择，牺牲任意 Prompt 拼装自由，换取权限不可扩张和可重放。
- 把 Prompt 作为编译产物，避免 Skill/Prompt 双事实源，但要求 Compiler、Profile、Policy 和 Context 都进入版本链。
- 个性化留在 Memory/Context，不生成用户私有 Skill，牺牲极端定制速度，换取隐私、稳定性和抗投毒。
- 自进化独立为离线控制面且首版人工批准，牺牲“实时自我修改”的展示效果，换取医疗健康场景可审计和可回滚。
- 只实施 Foundation，不同时建设评测平台与线上进化，降低一次变更多变量导致的归因和安全风险。
- 新版不复用旧 Skill 语义；旧文件先逻辑停用、后独立删除，避免兼容层污染新架构，也避免本阶段混入破坏性清理。

## Risks / open questions

- 18 个 Skill 的具体 instructions、examples 和每项 deterministic Scorecard 仍需在 Foundation 编写时逐项接受领域审阅；本计划锁定职责与边界，不预写 Prompt 文案。
- 当前真实数据仍以 replay/模拟为主，Foundation 可证明工程可追溯性与安全回归，不能证明真实用户效果、医学有效性或未来统计晋级门槛。
- Package 签名的生产密钥托管、轮换和 CI identity 需随部署方案确定；无论具体设施如何，生产不得降级为只信任可编辑文件路径。
- Product Episode 的现有持久化覆盖范围需要实施前核对；若缺少跨进程 checkpoint，SkillLock 的持久化必须作为 Foundation 的 additive 变更完成。
- Skill 组合和 token 上限可能影响现有模型表现；必须用完整 Episode 回归校准，不能为压缩成本删除安全或证据指令。
- LLM 非确定性意味着同一 Prompt hash 不保证相同输出；在 Context 保留期内可重编译相同 Prompt，在 Context 删除后只能验证版本/哈希和因果链，不虚假承诺恢复已删除敏感内容或字节级模型输出。
- 医学/安全审批角色在团队中的具体人员安排尚未命名，但 Tier 1/2 的职责分离和审批数量不能因人员不足被绕过。

## Out of scope

- 重新设计产品定位、信息架构、Agent 名单、Episode、Tool、A2A 或权限模型。
- 实施 Evaluation、Failure Miner、Candidate Generator、审批后台、shadow/canary 或任何自动晋级。
- 创建第 19 个 Skill、新 Episode、用户专属 Skill 或新的模型训练流程。
- 选择具体 LLM provider/model，或优化模型权重。
- 把原始用户健康数据用于候选生成或建立真实生产评测集。
- 前端用户页面、语音、通知渠道或对外展示 Agent/Skill 过程。
- 物理删除旧 Skill/Prompt/legacy runtime 文件；仅确保新版实现不可达。
- 医疗诊断、药物建议、临床有效性声明或医疗器械合规结论。
