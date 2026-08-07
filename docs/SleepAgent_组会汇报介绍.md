# SleepAgent 项目阶段性介绍

> 组会汇报版｜仓库核对日期：2026-07-28  
> 适用范围：产品定位、1+2+1 Agent 架构、睡眠习惯画像、HealthClaw 借鉴与当前完成度  
> 结论口径：核心方向、架构和工程闭环已经形成；当前仍是研究与工程验证阶段，不等同于生产发布或临床验证完成。

## 0. 一页摘要

### 一句话介绍

SleepAgent 的目标定位是成为一款以居家老年人为第一用户、连接子女与医生的睡眠健康观察与对话式照护助手。目标产品结合无感雷达长期数据和老人主观感受，把“昨晚发生了什么”转化为老人能理解的晨间解释、一个经本人确认的低负担行动，以及后续 3–7 天的持续观察；产品不替代医疗诊断、治疗或急救判断。当前已完成定位与核心工程链，老人前台尚未完全达到这一目标态，具体见第 6 节。

### 待验证的问题假设：不只是“再生成一份睡眠报告”

以下四点来自仓库中的产品设计假设，不代表已经完成用户研究或竞品验证：

1. 雷达持续产生客观记录，但老人很难直接读懂指标。
2. 通用大模型可以讲睡眠知识，却不了解这个人的长期变化、习惯和约束。
3. 一次性建议没有确认与跟进，难以形成真正的照护闭环。
4. 老人、子女、医生需要共享一致事实，但需要不同表达和不同权限。

因此，项目的核心主线是：

```text
无感观察
→ 晨间解释
→ 按需深聊
→ 老人确认一个行动
→ 3–7 天轻量跟进
→ 新数据与主观感受反馈
→ 新一轮解释
```

### 当前四项阶段成果

| 模块 | 已形成的成果 | 当前边界 |
| --- | --- | --- |
| 产品定位 | 第一用户、双主线、三身份协同、照护闭环和非诊断边界已经冻结 | 定位完成不等于老人端完整产品和市场验证完成 |
| 1+2+1 Agent | 四责任角色、中心编排、结构化协作、验收门、确认、持久化和安全降级已接入统一运行时 | 正式模型重复运行和外部发布证据仍为空 |
| 睡眠习惯 | 渐进问答、类型化画像、原子确认、持久化、API/UI 和 Agent 融入链路已打通 | 当前代码实现 10 个种子概念；冻结 v1 仍有 17 个概念待补，真实专业审核和用户测试待完成 |
| HealthClaw 启发 | 已实现审计轨迹与最小 EpisodeDigest 分离、确定性归纳、受控检索和撤权传播 | 只借鉴设计语义，不复用 HealthClaw 代码；Digest 生产读取仍受部署证明门禁控制 |

### 阶段判断

最准确的总体表述是：

> SleepAgent 已经从“睡眠数据展示 + 通用问答”的想法，推进到一个责任分离、来源可追溯、个人习惯可控写入、纵向记忆受治理的 Agent 工程原型；下一阶段重点是补齐产品化界面、完整习惯概念、真实设备与模型证据、专业审核和目标老人测试。

---

## 1. 产品定位：从睡眠监测走向连续照护

### 1.1 用户与场景

- 第一用户是居家老人。
- 子女是照护协同者，在老人需要帮助或趋势持续异常时获得适合家属理解的信息。
- 医生是专业协同者，在需要进一步评估时获得结构化、可追溯且保留不确定性的材料。
- 三个身份共享同一事实基础，但表达方式、可见范围和下一步行动不同。

这里的重点不是把家属 Dashboard 或医生工作台做成产品中心，而是让老人每天能快速回答三个问题：

1. 我昨晚睡得怎么样？
2. 为什么这样判断？
3. 我今天需要做什么？

### 1.2 两条产品主线

#### 主线 A：每日晨间解释

```text
一晚无感采集 → 自动分析 → 一句结论 → 一个原因 → 一个建议 → 按需追问
```

目标不是让老人阅读复杂 Dashboard。正常夜晚允许明确说“继续保持”或“今天无需特别处理”，不为了活跃度制造健康问题。

#### 主线 B：个性化对话照护

```text
自然提问
→ 读取本人长期数据
→ 区分事实、自述、知识、推断和未知
→ 必要时只追问当前决策所需的少量关键问题
→ 给出零个或一个低负担行动
→ 老人确认
→ 后续跟进
```

对话不是报告后的附属 Chat，而是承接“昨夜、近 7 天、近 30 天、睡眠习惯、最近失眠怎么办”等问题的主入口。

### 1.3 目标产品承诺与边界

目标产品承诺：

- 帮助老人理解个人睡眠记录和长期变化；
- 说明结论来自哪里、还缺什么；
- 给出少量、低风险、可执行且可停止的照护建议；
- 在本人确认后持续跟进；
- 必要时支持家属和医生协同。

产品不承诺：

- 疾病诊断；
- 精确替代 PSG；
- 药物处方或调药；
- 确定治疗效果或因果；
- 替代急救和医生决策。

### 1.4 目标设计对比

下表用于说明 SleepAgent 的设计取舍，不是基于系统竞品调研得出的市场结论。

| 对照形态 | SleepAgent 的目标选择 |
| --- | --- |
| 单晚指标 Dashboard | 持续观察 + 晨间易懂解释 |
| 通用睡眠聊天机器人 | 使用个人长期证据，并显式保留未知 |
| 一次性建议清单 | 一个主要行动 + 本人确认 + 3–7 天跟进 |
| 家属或医生中心 | 老人优先、三身份协同 |
| 以 Agent 数量为卖点 | 前台统一 SleepAgent，Agent 只作为后台责任结构 |

---

## 2. 1+2+1 Agent 架构

### 2.1 “1+2+1”分别是什么

```text
老人 / 家属 / 医生
        │
        ▼
用户只感知一个 SleepAgent
        │
        ▼
  SleepCareAgent                      1 个主 Agent
  统一交互、规划、中心路由、最终发布
        │
        ├────────► EvidenceReasoningAgent   ┐
        │          事实、解释与不确定性      ├─ 2 个专业 Agent
        ├────────► CareStrategyAgent       ┘
        │          单一行动与跨天跟进
        │
        └─条件触发► SafetyReviewAgent         1 个独立安全 Agent
                   approve / revise / block
        │
        ▼
确定性运行与能力层
Episode Runtime / FactSnapshot / Tool / Policy / Gate
Confirmation / Commit Controller / Persistence / Receipt
```

“1+2+1”是当前唯一且封闭的责任拓扑。新能力必须由这四个 Agent 的 Skill、Tool、Service 或 Policy 承载，不增加第五身份，不保留旧 Agent alias。

### 2.2 四个 Agent 的职责

| Agent | 负责什么 | 不能做什么 |
| --- | --- | --- |
| `SleepCareAgent` | 唯一用户入口；理解目标；选择最少必要路径；协调补充信息、冲突和有界重规划，并向用户发起或解释确认；向不同角色发布最终表达 | 不能创造个人事实、因果解释或照护行动；不能改写 Evidence 数值和 Care 参数；不能绕过确认与安全规则 |
| `EvidenceReasoningAgent` | 选择证据范围；检查数据来源、覆盖、时效、冲突和缺失；形成 `EvidencePacket` | 不能制定行动、直接面向用户发布、写长期记忆或把一般知识冒充个人事实 |
| `CareStrategyAgent` | 判断是否需要行动；基于已验收 Evidence 选择零个或一个低风险行动；定义负担、周期、指标、停止条件；后续调整或结束 | 不能读取原始传感器数据、修改 Evidence、诊断、调药或直接写状态 |
| `SafetyReviewAgent` | 对低置信/冲突结论、目录外或有禁忌的行动、医生材料、受限措辞和外部动作做独立审查 | 不能重写事实或行动，不能授予权限，不能代替老人确认，也不能降低确定性硬风险 |

### 2.3 为什么需要责任分离

这套架构不是为了让多个模型“互相聊天”，而是为了把健康场景中不同责任拆开：

- 事实形成与行动选择分离，避免同一 Agent 一边下结论、一边据此给建议。
- 专业结论与最终表达分离，避免语言润色改变事实、数字和置信度。
- 内容生成与安全审查分离，避免生成者自我批准。
- Agent 判断与权限、确认、写入、外部执行分离，避免模型直接产生不可逆副作用。

过去的 Trend、Report、Memory、AlertCare 等角色没有简单删除能力，而是被重新归类：

| 原角色 | 当前归属 |
| --- | --- |
| Orchestrator + Dialogue | 合并为 `SleepCareAgent` |
| Trend | Trend Tool + Evidence Skill |
| AlertCare | Care Skill + Coordination Tool/Policy |
| Report | SleepCare Skill + Artifact Tool |
| Memory | Memory Service/Tool + SleepCare Skill |

### 2.4 不是所有场景都调用四个 Agent

系统按任务选择最小路径：

| 场景 | 最小路径 |
| --- | --- |
| 晨间解释、个人数据问答 | SleepCare → Evidence → SleepCare |
| 趋势分析 | SleepCare → Evidence（Trend Tool）→ SleepCare |
| 一般睡眠知识 | SleepCare + reviewed Knowledge Tool |
| 制定照护行动 | SleepCare → Evidence → Care → SleepCare |
| 行动跟进 | SleepCare → Evidence → Care → SleepCare |
| 医生材料 | SleepCare → 必要 Evidence/Care → SleepCare 草稿 → Safety |
| 通知、分享、导出 | 已验收成果 → Safety → 确认 → Commit Controller |
| 急症线索 | 确定性规则立即抢占，不等待模型 |

Safety 是条件触发的语义审查，但急症、身份、权限、隐私、用户确认和外部执行检查始终是确定性硬规则。

### 2.5 一次 Product Episode 如何运行

1. 服务端绑定 actor、role、subject 和授权范围。
2. Runtime 冻结 `FactSnapshot`，记录时间范围、数据版本、Care/Memory 版本和来源。
3. Runtime 在首个模型调用前执行确定性急症抢占；身份绑定已在入口完成，数据与质量工具在计划形成后执行。
4. SleepCare 提出受 EpisodeDefinition 约束的计划。
5. Runtime 执行白名单 Tool，Evidence 形成结构化结果并通过 Evidence gate。
6. 需要行动时，Care 只消费已验收 Evidence，再通过 Care gate。
7. 命中规则时，Safety 对精确 target/hash 审查；退回后只允许责任 Agent 修订，最多两轮。
8. SleepCare 形成面向具体角色的表达；Communication gate 和发布前确定性终检再次核对事实、数字和行动。
9. Care、Habit Profile、外部动作及需要确认的 Memory 候选必须经精确确认，由 Commit Controller 唯一提交；老人当前顶层消息中的明确“记住、修改、忘记”可构成显式授权，但仍需通过主体、scope 和提交检查。
10. Episode 输出 `ProductEpisodeRunResult`：其中 `EpisodeReceipt` 记录执行模式、状态、调用、成果和 ToolReceipt 引用，结果对象另行保存精确待确认目标与提交状态。

### 2.6 已落地的关键工程约束

- 全仓库生产 Agent 名单在合同中只有四个 `AgentId`，并由 manifest、registry 和入口测试共同冻结。
- 仓库中的旧雷达 Agent 模块只作为迁移来源；收口验收后不存在可调用的生产、开发兼容或备用 Runtime 路径。
- Agent、跨 Agent 请求和 Tool 均为 deny-by-default allowlist。
- `FactSnapshot`、SourceScope、工作成果和 Safety 决定使用版本与 hash 绑定。
- 四个模型 Agent 均没有共享状态直写权或外部副作用权。
- 等待补充事实或确认的 Episode 可以从服务端冻结检查点恢复。
- Memory、Care、Habit、Episode result 和 commit journal 已有持久化边界。
- 对应模型、身份或外部动作路径缺少必要配置时明确 fail closed，不回退到旧单模型决策链或伪造执行成功；例如未配置外部 gateway 只阻断通知、分享或导出，不影响无外部动作的普通路径。

---

## 3. 睡眠习惯：把雷达看不到的个体差异融入照护

### 3.1 为什么需要 Habit Profile

雷达可以观察入床/离床、时相、连续性和长期趋势，却无法可靠知道：

- 老人是否必须早起；
- 是否需要夜间照护家人；
- 是否有午睡习惯；
- 睡前通常做什么；
- 偏好怎样的光线与声音；
- 老人最近主观上是否满意；
- 某个低风险行动是否可接受。

这些信息会改变解释和行动，但不能被设备自动推断成“事实”。因此，SleepAgent 没有做一个一次填完的“习惯测评”，而是做了按当前任务渐进补充、可跳过、可更正、可遗忘的结构化画像。

### 3.2 四类信息严格分开

| 上下文 | 例子 | 核心规则 |
| --- | --- | --- |
| `ConfirmedHabitProfile` | 午睡、睡前行为、环境偏好、作息约束 | 只有老人确认后才能长期保存 |
| `ObjectiveBaselineArtifact` | 雷达推导的入床时相、规律性、夜间离床分布 | 带窗口、覆盖、质量和算法版本；不能自动变成主观习惯 |
| `RecentReportedContext` | 近期满意度、困倦、家属近期观察 | 当前 Episode 可用；长期保存仍需确认 |
| `ClinicalSafetyContext` | 疾病、用药、剂量、跌倒、严重呼吸问题 | 不进入 Habit Profile，转独立临床/安全流程 |

所以系统可以说“近一段有效记录中通常约在 22:45 入床”，但不能自动写成“您的习惯是 22:45 睡觉”。

### 3.3 当前工程链路

```text
明确询问习惯 / 可选轻建档 / 当前决策存在信息缺口
→ Questionnaire Tool 从审核目录确定性选题
→ 带 receipt 捕获回答
→ 回答先成为本 Episode 的带来源 Evidence
→ 对符合持久化资格且未命中安全边界的回答，构造 1–3 项原子 HabitProfileChangeSet
→ 老人查看、可删除其中任一候选
→ 对精确 manifest 二次确认
→ Commit Controller 唯一写入
→ 后续按 purpose 和 concept 读取最小画像切片
→ Evidence 解释
→ Care 只消费已验收 Evidence
```

主要约束包括：

- 首次使用没有强制前置问卷。
- 每个 Episode 最多 3 个习惯问题，模型重试不能重置预算。
- 不因“画像不完整”、提高活跃度或正常晨间结果主动盘问。
- 只有已下发的 concept、合法选项/范围和未过期 receipt 能进入结构化链路。
- `unknown`、跳过和“不愿回答”不形成长期事实。
- “以后不要再问”只保存最小 suppression，不保存答案或推测值。
- 当前回答可以服务本轮 Evidence，但不会默认永久保存。
- 任一候选、来源、时间窗或 manifest 变化都会使旧确认失效。
- 底层 Profile 合同与 Store 支持 create、replace、expire 和 forget，并通过 CAS 与幂等控制；当前 API/UI 已显式暴露 create、replace 和 forget，过期主要由读取策略确定性判为 stale。

### 3.4 三类来源不能互相冒充

- 老人自述保留为 `user_reported / elder_self_report`。
- 家属只可回答允许观察的概念，并保留为 `observer_reported / family_observation`。
- 家属不能代替老人回答满意度、焦虑、疲惫等主观体验。
- 家属说“没有看到打鼾”只有在确有观察机会时才是弱观察；否则只能记为未知。
- 老人确认长期保存家属观察，只代表同意保存，不会把来源改写成老人自述或客观事实。
- 雷达客观统计始终是带版本的 Artifact，不覆盖主观体验。

### 3.5 如何进入 1+2+1 架构

- SleepCare 决定何时适合问，并向老人解释目的、跳过权和长期保存规则。
- Questionnaire Tool 决定合法问题身份、答案结构、预算和 receipt。
- Evidence 解释回答、家属观察、雷达基线之间的关系与冲突。
- Care 只能读取已验收 Evidence 中与当前行动相关的习惯约束，不能直接读取原始问卷。
- 确定性规则对每个回答做风险扫描；命中危险线索后立即停止普通习惯问题并进入 Safety 路径。
- Commit Controller 是画像的唯一写入者。

因此，睡眠习惯只能作为 Tool、Service、Skill 与 Policy 的横切能力，绝不是独立 `HabitAgent`。

### 3.6 当前实现范围

当前 Python 默认目录有 10 个可执行概念：

1. 当前主要目标；
2. 作息约束；
3. 午睡模式；
4. 午睡时长；
5. 睡前行为；
6. 环境偏好；
7. 咖啡/浓茶时间；
8. 近期睡眠满意度；
9. 本人或家属观察到的打鼾/憋醒；
10. 昨晚设备位置是否变化。

前 9 项可按规则形成 Profile 候选；“昨晚设备位置”只用于本 Episode 的数据质量解释，不能成为长期习惯。

需要诚实说明：冻结的完整 v1 覆盖文档还列出 17 个待补概念，包括通常/偏好上床与起床窗口、规律性、午睡时段、酒精与大量饮水时间、温度偏好、非临床辅助物、可接受行动负担、日间影响和更多可观察夜间行为。因此，当前完成的是 10 个种子概念上的完整工程闭环，还不能称为“冻结 v1 概念范围全部交付”。

### 3.7 产品暴露与持久化

仓库目前已经包含：

- `/product/habit-profile/*` API；
- `/habit-profile` 老人页面；
- 可选轻建档；
- 查看、确认前删除候选、更正和遗忘；
- 类型化 Profile Store；
- Profile、suppression、问题预算/receipt/冷却的数据库迁移；
- 同一 ProductEpisodeRunner 中的 Habit selection、capture、Evidence 和 change set；
- 28 项自动化验收标准的测试映射。

未确认 change set 仍是短时进程状态，服务重启后需要重新汇总确认；已确认 Profile、问题预算、selection receipt、冷却和“以后不要问”状态可以持久化。

---

## 4. HealthClaw 给 SleepAgent 的启发

### 4.1 借鉴的不是一个通用医疗 Agent 产品

HealthClaw 面向广域个人健康 Agent，覆盖多类医学任务、入口、工具与分层记忆。SleepAgent 没有把它当成可直接移植的睡眠产品，而是借它思考一个更具体的问题：

> 一个长期健康助手应该怎样选择性地记住过去，而不是把完整历史无差别塞给模型？

### 4.2 从 HealthClaw 概念到 SleepAgent 设计

| HealthClaw 概念 | SleepAgent 中的映射 |
| --- | --- |
| L0 行为规则 | GlobalPolicy、AgentProfile、EpisodeDefinition、确定性安全/权限/确认 |
| L1 领域知识 | reviewed Knowledge、Care catalog、Questionnaire Bank、Tool/Skill Registry |
| L2 个人画像 | typed Habit Profile + 受治理的通用长期 Memory |
| L3 可复用 SOP | 共享 Skill、Care catalog、确定性 EpisodeDefinition；不建立个人 SOP |
| L4 情景记忆 | 完整 `ProductEpisodeRunResult` 只供审计，另派生最小 `EpisodeDigest` |
| Episode 后归纳 | terminal result → Outbox/Job → deterministic induction |
| Tool 产生证据 | ToolReceipt → Evidence acceptance → Care/SleepCare |

### 4.3 SleepAgent 的纵向记忆链路

```text
终态 ProductEpisodeRunResult
        │
        ├─ 原始完整结果：受限审计，永不进入在线 Prompt/RAG
        │
        └─ 同一事务生成 allowlisted Manifest + InductionJob
                                   │
                                   ▼
                     DeterministicInductionWorker
                     不调用 LLM、不读取推理过程
                                   │
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                 ▼
          EpisodeDigest      待确认画像候选     SkillOutcome / exclude
                 │
                 ▼
       purpose/role/scope/TTL 硬过滤
                 │
                 ▼
       Evidence 重新解析当前仍有效、仍获授权的权威结构化来源
       （不读取原始 ProductEpisodeRunResult 审计内容）
                 │
                 ▼
              当前结论
```

这里有三个关键原则：

1. Digest 只是“不可信历史线索”，不是当前事实。
2. 形成当前个人结论前，Evidence 必须重新访问仍有权、仍有效的权威结构化来源，而不是读取原始 `ProductEpisodeRunResult` 审计内容。
3. 自动归纳只能生成待确认画像候选，不能自动写入老人真值画像。

### 4.4 相比直接复用更保守的治理

- 原始对话、完整 Tool 输出和 Agent 内部推理不进入在线 Memory/RAG。
- 归纳流程完全确定性，不用 LLM 从自由文本创造新事实。
- 查询先按身份、角色、purpose、类型、SourceScope、时效、授权和保留期硬过滤。
- 禁止空条件、通配符、“全部历史”和自动扩大时间范围。
- Care 和 Safety 没有直接纵向读取权；Evidence 才能读取 Digest，并必须重新取证。
- Digest 默认最多可检索 90 天，待确认候选 30 天，Manifest 明文生命周期最多 7 天，模型可见 handle/cursor 最多 15 分钟。
- forget、withdraw、delete、correction 和更严格授权会推进 epoch，使旧查询、缓存和在途结果失效。
- 提供 kill switch，关闭 Digest retrieval 并清空相关 handle；重新启用需要新的完整部署证明。

### 4.5 明确没有照搬的内容

SleepAgent 没有：

- 复制 HealthClaw 的 Markdown/TXT/JSONL 五层目录或源代码；
- 替换现有 1+2+1 架构；
- 新增 MemoryAgent 或 EvolutionAgent；
- 建立个人 SOP Store、个人 Skill 或个人 Prompt；
- 让会话末尾的 LLM 自动更新画像、规则或 Skill；
- 复用饮食、慢病、医学影像、生信和 benchmark 特化工具；
- 复用弱鉴权跨设备 Demo 服务；
- 把 HealthClaw 的合成结果当成 SleepAgent 临床或生产证据。

概括来说：

> SleepAgent 借鉴了 HealthClaw 的“纵向连续性和信息分层”，但拒绝了自由写回、个人 SOP 自动生长和广域医疗任务扩张。

---

## 5. 四项成果如何在一个真实问题中协同

以老人问“我最近总睡不好，有什么最容易做到的办法？”为例：

1. 产品定位决定回答不能停留在通用知识，而要结合本人近期变化，并只给低负担建议。
2. Runtime 冻结近 7 天或 30 天 `FactSnapshot`，先检查数据覆盖、身份、权限和急症线索。
3. SleepCare 把个人解释交给 Evidence。
4. Evidence 区分雷达客观变化、老人当前自述、已确认习惯、一般知识和未知项。
5. 如果“是否午睡”会改变解释或行动，系统只问这一项，而不是要求完成整份问卷。
6. 回答先进入当前 Evidence；老人愿意长期保存时，再确认精确 Habit change set。
7. Care 根据已验收 Evidence、作息约束和可接受负担，选择零个或一个审定行动。
8. 命中诊断、药物、禁忌、低置信冲突或医生材料等条件时，Safety 独立审查。
9. SleepCare 用老人能理解的语言发布结论、依据、未知和下一步，不能改变上游事实或行动参数。
10. 老人确认后由 Commit Controller 激活行动；3–7 天后结合新数据和感受决定保持、调整或停止。
11. Episode 结束后，完整轨迹只供审计；Episode 派生历史最多形成一个最小 Digest 线索，未来使用时仍需重新取证。

这个例子体现了项目四部分不是并列功能，而是一个连续系统：

```text
产品定位决定“为谁、解决什么”
        ↓
1+2+1 决定“谁对什么结论负责”
        ↓
Habit Profile 补足“雷达看不到的个体上下文”
        ↓
HealthClaw 启发的治理解决“过去信息怎样安全地进入未来”
```

---

## 6. 当前完成度与证据

### 6.1 可以说已经完成什么

- 产品定位、第一用户、双主线、三身份协同和非诊断边界已经形成锁定文档。
- Product Agent 已从历史多角色收口为严格四角色合同。
- 产品聊天、Agent run 和生产任务主路径复用同一 `ProductEpisodeRunner`。
- Agent/Tool 权限、结构化工作成果、四类 acceptance gate、Safety 修订和发布前确定性终检已经实现。
- Memory、Care、Habit、Product result 和 commit journal 已有持久化及幂等边界。
- Habit Profile 的选题、捕获、当前 Evidence、原子确认、提交、读取、更正、遗忘、API 和 UI 工程链路已经打通。
- HealthClaw 启发的 EpisodeDigest、Manifest、Job、确定性 worker、受控检索、来源重验证、撤权传播和 kill switch 已实现。
- 前端 TypeScript 检查在本次核对中通过。

### 6.2 当前本地验证结果

本次只读核对执行了完整 Python 回归：

```text
535 passed, 2 failed
```

两项失败都位于 `tests/test_product_agent_persistence.py`，原因是测试使用固定时间创建的确认令牌已经相对当前日期过期；业务代码按设计拒绝过期确认。本文没有修改代码或测试来规避该结果。

前端验证：

```text
npm run typecheck
通过
```

验收材料审计识别到：

- 3 个可用性观察槽位；
- 10 个概念审核槽位；
- 68 个 provider 场景观察槽位；
- `release_evidence_eligible=false`。

这些槽位仍是 simulated/template，不是实际参与者、真实专业签字或真实 provider execution。

### 6.3 不能说已经完成什么

| 事项 | 当前状态 |
| --- | --- |
| 正式 release | 未完成；manifest 为 `unreleased`，正式 observations 为空 |
| 真实模型重复运行证据 | 未完成 |
| 真实外部通知/分享/导出网关证据 | 未完成 |
| 完整冻结 v1 Habit concept | 未完成；当前 10 个，另有 17 个待补 |
| 具名领域/医学审核 | 未完成 |
| 3–5 名 60+ 目标用户可用性测试 | 未完成 |
| 真实设备数据质量与医学有效性 | 未证明；当前主数据仍以 replay/合成验证为主 |
| 老人第一的完整四页产品信息架构 | 尚未完全落地；当前主界面仍可见任务、Agent 过程和技术证据元素 |
| Digest 生产读取 | 默认保持关闭，待部署控制证明和纵向 benchmark gate |
| 完整 Care catalog | 未完成；默认目录目前只有固定起床时间和晨间光照两个种子行动，完整行动与禁忌仍待领域审核 |
| 临床级数据/风险引擎 | 未完成；部分雷达、设备和 Care feedback Tool 仍是薄适配层，急症关键词、风险和质量阈值是首版确定性基线 |
| 临床有效性或医疗器械合规 | 未开展或不在当前范围 |

### 6.4 当前最值得推进的下一步

1. 补齐冻结 v1 中缺失的 17 个习惯概念，并完成措辞、选项、TTL、来源和安全路由审核。
2. 将老人前台从任务/Agent 工作台进一步收敛为“今天、趋势、问问 SleepAgent、记录”。
3. 接入并验证真实雷达数据质量，减少对 replay 的依赖。
4. 配置真实结构化模型与外部动作网关，采集当前 release identity 对应的真实 receipt。
5. 完成具名专业审核和 3–5 名 60+ 老人的可用性观察。
6. 完成纵向记忆部署证明、零 orphan 检查和隔离 benchmark，再决定是否启用 Digest read。
7. 修正当前两个依赖固定日期的过期令牌测试，并同步版本/证据说明文档。

---

## 7. 建议的 8–10 分钟汇报顺序

### 第 1 分钟：问题与定位

> 我们的产品假设是：单纯提供指标和报告，还不足以回答老人最关心的“昨晚怎么样、为什么、今天怎么做”。因此，SleepAgent 以居家老人为第一用户，目标是把无感雷达、个人长期证据、自然对话和经确认的轻量行动连成连续照护闭环。

### 第 2 分钟：产品主线

展示：

```text
无感观察 → 晨间解释 → 按需对话 → 确认一个行动
→ 3–7 天跟进 → 新数据与感受 → 新一轮解释
```

强调老人、家属、医生三身份事实一致、表达不同。

### 第 3–5 分钟：1+2+1 架构

重点讲：

- SleepCare 是统一入口和发布者；
- Evidence 对事实与不确定性负责；
- Care 对零个或一个行动及跨天跟进负责；
- Safety 与生成者分离且条件触发；
- 确定性 Policy 和 Commit Controller 高于 Agent 判断；
- 场景走最小路径，不固定四 Agent 全调用。

### 第 6–7 分钟：睡眠习惯

重点讲：

- 不是完整问卷和综合分；
- 主观习惯、客观基线、近期感受、安全信息四分；
- 最多三问、可跳过；
- 当前回答先用于本轮，长期保存需老人确认精确 change set；
- 家属观察不能冒充老人自述；
- Habit 是 Tool、Service、Skill 与 Policy 的横切能力，绝不是 Agent。

### 第 8–9 分钟：HealthClaw 启发

重点讲：

- 借鉴信息分层和 Episode 后选择性记忆；
- 完整轨迹只供审计；对 Episode 派生历史，只有最小 Digest 可以成为在线检索线索；
- Digest 不是事实，使用前必须重新取证；
- 不复制代码，不允许 LLM 自动写画像/SOP/Skill。

### 第 10 分钟：完成度与下一步

> 当前完成的是方向、责任架构和关键工程闭环；尚未完成的是完整习惯概念、老人端产品收口、真实设备/模型证据、专业审核、目标用户验证和正式发布门禁。

---

## 8. 组会可能追问

### 为什么不用一个大 Agent？

健康场景中，事实形成、行动选择、用户表达和安全审查的失败后果不同。拆分责任可以分别限制 Context、Tool 和输出合同，也能避免同一模型生成并自证结论。

### 为什么 Safety 不是每次都调用？

每次固定调用会增加延迟和成本，也会把 Safety 变成万能审核者。普通高质量晨间解释可以走最小路径；急症和权限等硬规则始终确定性执行，冲突、低置信、高风险、医生材料和外部动作再强制触发 Safety。

### 睡眠习惯能否由雷达自动生成？

不能。雷达得到的是带时间窗和质量信息的客观基线，不能自动改写成老人主观习惯。习惯长期保存需要带来源的回答和老人精确确认。

### 为什么不让 Agent 自动记住一切？

完整历史会扩大隐私暴露、上下文成本和旧错误自我强化。对 Episode 派生历史，SleepAgent 只允许最小 Digest 作为检索线索，并要求 Evidence 在当前任务中重新验证权威结构化来源；经确认的 Habit Profile、Governed Memory 和 Care State 仍按各自治理规则长期保存。

### 为什么不照搬 HealthClaw 的五层 Memory？

SleepAgent 已有 Policy、Knowledge、Habit Profile、Care State、Skill Registry 和 Episode audit 等不同权威边界。把它们合并成一组可被 Agent 自由读写的文件会削弱权限、确认和审计。

### 现在是否可以发布？

不可以表述为正式发布。核心代码和自动化机制已经形成，但真实模型/设备证据、专业审核、目标用户可用性测试和 deployment attestation 仍未完成，release verifier 按设计保持 fail closed。

### 当前最重要的研究价值是什么？

不是 Agent 数量，而是探索如何把长期感知、主观习惯、专业责任分离和受治理记忆组合成一个可追溯、可确认、能持续跟进的睡眠照护系统。

---

## 9. 仓库依据索引

### 产品与信息架构

- [项目 README](../README.md)：当前总体定位、架构权威、API、运行与发布边界。
- [产品定位计划](../product_positioning/PLAN.md)：第一用户、双主线、照护循环、验收与非诊断边界。
- [产品定位审查记录](../product_positioning/PLAN-REVIEW-LOG.md)：定位锁定过程。
- [产品信息架构](../product_information_architecture/PLAN.md)：目标“今天、趋势、问问 SleepAgent、记录”四页主线。

### 1+2+1 Agent

- [Agent 架构计划](../agent_architecture/PLAN.md)：四角色责任、最小路径、状态、权限、Safety 和 Skill 边界。
- [Agent 合同](../sleepagent/radar_agent/product_agent/contracts.py)：四个 `AgentId`、SourceScope、FactSnapshot、工作成果和 Receipt。
- [Agent 与 Tool 注册表](../sleepagent/radar_agent/product_agent/registry.py)：角色定义、调用关系和 deny-by-default allowlist。
- [ProductEpisodeRunner](../sleepagent/radar_agent/product_agent/runner.py)：统一运行、Habit 融入、Safety、确认、提交与归纳。
- [Agent 构建与验证日志](../agent_architecture/PLAN-REVIEW-LOG.md)：从设计收口到生产接线和持久化的阶段记录。
- [当前验收 Manifest](../agent_architecture/ACCEPTANCE-MANIFEST.json)：当前版本身份与正式 evidence 空缺。

### 睡眠习惯

- [睡眠习惯画像计划](../sleep_habit_profile/PLAN.md)：四类上下文、渐进采集、确认、冲突和角色边界。
- [v1 概念覆盖](../sleep_habit_profile/V1-CONCEPT-COVERAGE.md)：完整冻结范围、ObjectiveBaseline 和 17 个待补概念。
- [Habit Skill/Tool 路由](../sleep_habit_profile/HABIT-SKILL-TOOL-ROUTING.md)：Habit 如何融入四角色。
- [Decision Gap 映射](../sleep_habit_profile/DECISION-GAP-MAPPING.md)：何时允许提问，以及回答能改变什么决策。
- [默认 Habit 目录](../sleepagent/radar_agent/questionnaire/defaults.py)：当前 10 个可执行概念。
- [Habit Profile 合同](../sleepagent/radar_agent/product_agent/habit_profile.py)：ObjectiveBaseline、Fact、ChangeSet、Confirmation 和 Store。
- [Habit 产品应用](../sleepagent/radar_agent/product_agent/habit_application.py)：可选建档、回答、候选、确认、更正与遗忘。
- [Habit 验收矩阵](../sleep_habit_profile/ACCEPTANCE-MATRIX.md)：28 项自动化标准及正式 release gate。

### HealthClaw 借鉴

- [HealthClaw 启发的纵向记忆计划](../healthclaw_memory_governance/PLAN.md)：语义映射、拒绝项、归纳、检索、保留与 benchmark。
- [纵向记忆实现说明](../healthclaw_memory_governance/IMPLEMENTATION.md)：运行边界、部署、隐私和 kill switch。
- [纵向记忆代码](../sleepagent/radar_agent/product_agent/longitudinal_memory.py)：GovernedMemory、EpisodeDigest、Manifest、Worker 和查询服务。
- [隔离纵向 benchmark](../benchmarks/healthclaw_memory_governance/README.md)：暴露减少和非劣验证门槛。
- [HealthClaw 论文仓库副本](../HealthClaw_paper/full.md)：仅作为设计输入，不作为 SleepAgent 临床或发布证据。

## 10. 汇报时建议避免的表述

不要说：

- “SleepAgent 已经可以诊断失眠或睡眠呼吸暂停。”
- “1+2+1 已经被证明一定优于单 Agent。”
- “HealthClaw 代码已经集成进 SleepAgent。”
- “睡眠习惯完整 v1 已全部实现。”
- “模拟老人、模拟 reviewer 和 synthetic provider receipt 是真实发布证据。”
- “当前 UI 已完全实现老人第一的四页信息架构。”
- “完整 Python 测试当前全绿。”
- “项目已经具备生产发布或临床有效性。”

建议说：

> 产品方向、四责任 Agent 内核、10 个种子习惯概念的工程闭环，以及 HealthClaw 启发的纵向记忆治理已经形成；当前正在从架构实现走向完整产品化和真实证据验证。
