# Plan: SleepAgent 轻量冷启动策略
_Locked via grill — by Codex + user_

## Goal

在不新增 Agent、通用状态平台或复杂统计系统的前提下，为 SleepAgent 建立一层确定性的轻量冷启动策略，同时处理画像信息不足、个人数据不足和新能力尚未取得生产资格三类情况。系统在信息不足时仍应提供当前证据允许的服务，但必须按当前请求限制结论强度，明确区分个人记录、已确认自述、一般知识与未知信息；冷启动不能阻塞绝对安全规则，也不能用少量数据、历史 Episode 或未经验证的能力制造虚假个性化。

## Inherited constraints

1. `product_positioning/PLAN.md` 继续决定居家老人第一用户、非诊断边界、个人数据/自述/一般知识/未知的来源区分，以及数据不足时的诚实表达。
2. `product_information_architecture/PLAN.md` 继续决定首次使用、无记录、数据不足、设备异常和对话入口的产品呈现。
3. `agent_architecture/PLAN.md` 继续决定四 Agent、确定性 urgent boundary、Evidence 语义、权限、确认、发布与降级边界；冷启动不增加第五个 Agent。
4. `sleep_habit_profile/PLAN.md` 继续决定可跳过的渐进画像、问题预算、当前使用与长期保存分离，以及 `ObjectiveBaselineArtifact` 不属于 Habit Profile。
5. `plug_and_play/PLAN.md` 与 `skills_design/PLAN.md` 分别继续决定 Adapter 和 Skill 的版本、验证、发布与回滚治理；本计划不建立一套覆盖 Adapter、Tool、Skill 的新控制面。
6. `healthclaw_memory_governance/PLAN.md` 继续决定历史 Episode/Digest 只能提供线索，支持当前结论前必须重新验证 canonical source、授权、时效和用途。

## Core model

### 1. Three orthogonal readiness dimensions

冷启动不是一个全局“用户等级”，而是三个相互独立的就绪维度：

1. **画像就绪度**：只记录当前请求所需字段的 `known | unknown | stale | disputed` 等既有状态，不生成画像完整度分。某字段未知只限制依赖该字段的判断，不降低无关功能。
2. **证据就绪度**：按 `subject × metric × measurement cohort` 计算。相同用户可以对作息时相已有稳定基线，而对夜间离床或呼吸指标仍无基线。
3. **能力就绪度**：按具体 Adapter capability、确定性 Tool 或 Skill 版本及运行环境判断。某一能力不可用只降级受影响的路径，不让整个 SleepAgent 进入统一“冷启动模式”。

每个 `ClaimRequirement` 必须来自一个有限、版本化的 runtime catalog。已注册的
Episode/plan step、请求意图和 Tool Schema 先确定合法候选 ID；SleepCare 可以
在候选集内选择 ID，但 runtime 展开其不可变的 claim kind、metric、SourceScope、
必要 Profile concept 和 capability。模型不能提交自由 metric、阈值或 requirement
正文。开放式问题无法形成合法候选时，只能先回答一般知识或走既有最小澄清。

每次请求由现有 runtime 对每个合法的
`claim_requirement_id + metric_id` 做一次确定性判断，最小输出为：

- `response_mode = supported | degraded | blocked`
- `claim_ceiling = general_knowledge | single_night_description | short_series_difference | provisional_pattern | established_baseline`
- 一个小型、有限的 `reason_codes` 集合，例如 `no_valid_night`、`metric_missing`、`insufficient_metric_nights`、`limited_quality`、`stale_evidence`、`incompatible_measurement_cohort`、`capability_not_production_eligible` 或 `authorization_missing`

一个回答涉及多个指标时必须携带多个独立 decision，不能用成熟指标的 ceiling
覆盖不成熟指标。`supported` 表示请求的目标结论全部在当前 ceiling 内；
`degraded` 表示只能返回更弱的结论或安全子集；`blocked` 表示当前操作没有可
授权执行的安全结果，不能用一般知识伪装成已经完成了个人查询或副作用操作。

这只是薄策略结果，不建立通用规则引擎，不持久化全局用户冷启动状态。一般知识
请求可以在没有个人数据时得到 `supported + general_knowledge`；个人趋势请求
在只有一晚数据时得到 `degraded + single_night_description`。
这里的 `general_knowledge` 明确表示“不得形成个人 claim”，不是比个人证据更低
质量的知识。经过审核的一般知识可以伴随任何 ceiling，但永远不能被引用为个人
观察、个人规律或个人风险的证据。

首版 personal ClaimRequirement catalog 只包含以下五类，不创建任意表达式语言：

1. `describe_current_night`
2. `compare_explicit_nights`
3. `current_night_vs_established_baseline`
4. `short_window_pattern`
5. `longitudinal_trend`

metric 只能来自既有 reviewed metric allowlist。比较日期必须由用户明确指定，
或由 Episode 注册的完整固定窗口确定；模型不得根据数值选择一个子集来制造变化。
新增 claim kind 或 metric 属于普通代码/领域评审，不允许在线生成。

执行顺序保持最小且确定：

1. 验证身份、授权、目标 subject 和数据来源；Adapter/Tool/Skill 的 owning
   Registry 产生不可伪造的 capability eligibility receipt，薄策略只消费该
   结果，不复制各 Registry 的发布判断。
2. 对用户输入和当前合法来源运行既有 deterministic urgent boundary；未验证
   Adapter 的输出只能形成运维信号，不能形成用户健康红旗。
3. 将来源修订、Profile/Evidence refs、measurement cohort、策略版本和
   readiness decision refs/hash 固定进扩展后的既有 FactSnapshot/调用快照，
   再逐 claim 计算 decision。
4. 把 ceiling 和 reason codes 作为不可扩权约束交给现有 Agent/Tool 路径。
5. 发布前重新校验授权、revocation/source validity，并用确定性 postflight
   拒绝超过 ceiling 的 claim；不能只依赖 Prompt 要求模型自我约束。

每个 decision 使用严格的 `MetricReadinessDecision` 合同，并明确区分：

- `scope_valid_night_count`：当前查询/比较窗口中该指标的有效夜数；
- `baseline_valid_night_count`：当前 baseline Artifact 构建窗口中该指标的
  有效夜数；
- `baseline_artifact_ref`、当前 maturity/use eligibility，以及两组 source
  refs/hash。

它复用现有 Episode/Tool receipt 保存最小审计字段：策略版本、claim/metric、
cohort、上述两个计数、来源 refs/hash、基线成熟度、ceiling、response mode 和
reason codes。FactSnapshot 保存这些 decision receipt 的 refs/hash，使模型
调用与发布始终绑定同一判断。它不保存原始健康 payload，也不要求新增状态表。

capability eligibility receipt 至少绑定 owning Registry kind/snapshot hash、
能力或包的精确 ID/version、环境、配置/内容 hash、eligible verdict、reason、
解析时间和权威 verification/deployment refs；由 runtime 注入并纳入
FactSnapshot hash，不能由模型或普通 API body 提供。

ceiling 不依赖自由文本分类器执行。`EvidenceClaim`/accepted Evidence 以 additive
字段绑定 `claim_strength` 和 `readiness_decision_ref`；Evidence acceptance 在
模型内容进入发布前拒绝 strength 高于 ceiling、metric/cohort 不匹配或缺少
decision ref 的个人 claim。对于“请求个人趋势但证据只够单晚/一般知识”的
degraded 路径，runtime 使用审核过的确定性边界句，不让模型自行编造被拒绝的
个人趋势；其余一般知识仍按 reviewed Knowledge 路径回答。现有 publication
postflight 继续验证最终文本只能引用已接受 claim/Knowledge，并校验固定边界句，
不尝试用关键词猜测整段文本的医学语义。

### 2. User and profile cold start

1. 身份、角色、subject binding 和授权由可信入口提供；模型不能通过对话中的自我声明建立身份，也不能在首次对话取得覆盖未来所有用途的 blanket consent。
2. 首次进入只简短说明产品用途、非诊断边界、数据来源和问题可跳过，不设置前置问卷。
3. 默认先处理用户当前需求。只有一个回答确实可能改变当前 Evidence 或 Care 决策时，才通过既有 decision-gap 路径询问一个最小问题。
4. 用户可以主动进入可选轻建档；首次最多询问 2 个问题，整个 Episode 仍受既有最多 3 个画像问题的总预算约束。
5. 首次使用不固定询问一题笼统的“是否存在安全风险”。每个回答先经过既有确定性 urgent/risk scan；命中明确红旗或当前场景存在安全缺口时，立即停止普通画像采集并进入独立安全路径。
6. 当前回答可以按既有规则用于当前 Episode；长期画像仍需老人对精确 change set 单独确认。未知、跳过或拒绝不会被推断成画像事实。

### 3. Effective nights and measurement cohorts

“有效夜晚”采用两级判定，且只能由服务端根据 canonical data 计算：

1. **夜晚级资格**：NightEpisode 已唯一绑定到正确 subject、设备与时区，数据模式明确，事件时间可用于归属，当前修订可追溯，且数据未处于隔离、撤回或越权状态。
2. **指标级有效性**：目标指标所需数据存在，覆盖率与质量达到该指标在 `BaselinePolicy` 中的门槛，没有该指标的阻断 flag，并且数据属于当前允许比较的 measurement cohort。

一晚可以对在床状态有效、对呼吸率无效。当前 scope 和 baseline 窗口的有效夜数
都必须是目标指标的服务端派生计数，API、Agent、前端或调用方不得直接声明权威
计数。当前 replay、dashboard/chat 和 Trend 路径中不一致的计数逻辑必须收敛到
同一服务端函数。

现有 `SourceScope.valid_night_count` 仅保留为单指标兼容展示字段，不再作为
成熟度或 claim 权限的事实源。多指标请求不得把最小值、最大值或某一指标计数
冒充全局计数；权威值只存在于 FactSnapshot 绑定的各
`MetricReadinessDecision.scope_valid_night_count` 中。旧调用方完成迁移前，
服务端忽略其传入计数并用派生值覆盖；无法唯一对应一个指标时置为非权威的 `0`，
而不是猜测。baseline 构建夜数不得写回这个 scalar。

measurement cohort 默认使用精确来源代际键，至少绑定 subject、metric、
data mode、内部 device/binding version、设备测量域、Adapter 版本与配置指纹、
Observation Schema/data version、producer/algorithm 精确版本，以及来源提供时的
firmware/calibration 状态。对本地钟点、入床/起床时相等时间敏感指标，还必须
绑定 IANA timezone 和 sleep-day/boundary policy version。兼容规则只能合并
经过审核的精确代际，不能因为型号相同或 SemVer 看似兼容就自动合并。默认不
混合新旧设备或不兼容算法产生的数据：

- 经过审核的兼容矩阵或桥接证据明确允许时，数据才可继续进入同一 cohort，并保留 compatibility reference。
- 未通过桥接时，新设备独立积累数据；旧基线可作为历史记录查看，但不能直接支持新设备下的当前趋势或异常结论。
- 过渡期可以做带来源的描述性并排展示，不能把测量域变化称为用户睡眠突变。
- 设备换绑到其他 subject 时绝不继承个人基线。
- compatibility 不能跨 subject 或 `live/replay` 数据模式，也不能把未知
  calibration 当作已校准。

同一 sleep day、metric 和 cohort 只允许当前有效 NightEpisode revision 贡献
一次；迟到数据或更正报告产生新 revision 后，旧 revision 不再重复计数，但仍
保留审计引用。

### 4. Baseline maturity and claim ceilings

继续使用现有三级基线成熟度。下表描述的是**尚无可用 established baseline 的
当前 measurement cohort**：

| 当前指标证据 | `BaselineMaturity` | 最大个人化结论 |
| --- | --- | --- |
| 0 个有效夜晚 | `unavailable` | 一般知识、产品引导、设备状态；不形成个人睡眠结论 |
| 1–3 个有效夜晚 | `unavailable` | 单晚描述；至少两晚时可以说明精确夜晚之间的描述性差异，但不称趋势、规律或异常 |
| 达到该指标 provisional 门槛 | `provisional` | 短窗口初步规律和明确标注的低置信度比较 |
| 达到该指标 established 门槛 | `established` | 个人基线比较和较稳定规律，同时保留设备、算法与数据限制 |

首版保留一个简单的安全下限：少于 4 个指标有效夜晚绝不进入
`provisional`；各指标可以要求更多。`15` 可作为首轮真实设备实验中
`established` 的候选门槛，但没有明确策略时不能因为到达 15 夜自动晋级。
最终门槛、覆盖率、允许缺失、观察窗口和新鲜度都由版本化
`BaselinePolicy` 按指标配置。`30` 夜和“长期运行”不新增永久成熟度枚举；
纵向检索和行动跟进按各自当前任务的证据要求决定，不自动继承某个全局长期等级。

`BaselinePolicy` 是静态、reviewed 配置，至少固定 metric、环境、scope claim
所需样本、provisional/established 门槛、rolling window、coverage/quality、
新鲜度、timezone/sleep-day 规则及 policy version。生产缺少当前 metric 的
reviewed policy 时必须返回 `baseline_policy_unavailable` 并保持
`BaselineMaturity.UNAVAILABLE`；不得使用代码默认值自动晋级。非生产逻辑测试
可以使用明确标记的 `4/15` fixture policy，真实设备实验通过后再发布精确生产
配置。

已有仍可用 established baseline 时，新的一晚可以支持
`current_night_vs_established_baseline` 这类明确的单晚对基线比较，但不能因为
这一晚生成 `longitudinal_trend`。同样，当前窗口只有两三晚时可以逐晚和稳定
基线比较，却不能把两三晚称为新的稳定模式。`ClaimRequirement.claim_kind`
必须区分单晚对基线、短窗口趋势和稳定规律，并同时检查 scope count 与 baseline
count/maturity。

历史 `ObjectiveBaselineArtifact` 保持不可变，记录它在原窗口、来源和策略版本下的成熟度。当前使用资格在读取时确定，可用一个不要求新增持久化表的临时 `BaselineReadinessProjection` 表达。以下情况可以使当前使用资格从 `established` 回退到 `provisional` 或 `unavailable`：

- 近期有效数据不足或数据超过新鲜度窗口；
- 持续离线、低覆盖或目标指标质量退化；
- 授权、来源或数据被撤回；
- 设备换绑，或当前数据进入不兼容 measurement cohort；
- 策略升级后，当前窗口不再满足新策略。

恢复足够的兼容有效夜晚后可以重新晋级。历史 Artifact 不被重写，但“曾经 established”不能单独授权当前结论。
如果新策略不能安全解释旧 Artifact 的构建语义，必须从仍可用的 canonical
source 重新构建；无法重建时降级，不能用新策略直接重标旧值。

当前 readiness projection 每次从 policy 限定的有界 canonical window 重新计算，
可使用非权威缓存。不可变 `ObjectiveBaselineArtifact` 与
`MetricReadinessDecision` 序列化进既有 ToolReceipt/ProductEpisodeResult，供
历史 Episode 审计与重放；重启后当前资格由 canonical data 重算，缓存丢失不改变
结果。缓存不得成为证据源，且授权/source epoch 或 policy version 变化时必须
失效。

### 5. Safety and historical evidence

1. 绝对红旗、权限越界和既有 deterministic urgent boundary 不依赖个人基线，并优先于冷启动策略执行。
2. 没有基线只能限制相对异常判断，不能把缺少证据解释成“未发现风险”。
3. 个人基线只能改变相对变化的解释，不能降低绝对红旗的风险等级。
4. 历史 Episode、Digest、旧 Profile 或旧 baseline 只能作为检索线索；当前结论必须重新验证原始来源仍存在、仍授权、仍在有效期内且与当前 measurement cohort 可比较。

### 6. Capability cold start without a universal platform

三类能力保留各自最小治理：

1. **Adapter**：部署状态和 capability verification 是两个正交门。生产路径
   必须同时满足 Adapter 精确版本的 `deployment_status=ENABLED`，以及所用
   capability 对精确配置指纹和环境达到人工审核的 `VERIFIED`；任一条件缺失
   都不得进入用户结果。生产环境中的影子 Adapter 最多产生未绑定 subject 的
   `AdapterObservationCandidate`、conformance/差异 comparison receipt 和运维
   统计，随后停止；需要完整端到端验证时使用隔离的非生产环境/测试 subject。
   影子输出不得进入 canonical 生产观测、个人基线、长期 Memory 或用户健康
   告警。首版只需关闭生产 resolver 默认接受未验证 capability 的缺口，不新增
   通用流量平台或新的数据模式。
2. **确定性 Tool**：继续通过静态 Registry、代码评审、Schema/权限/失败降级测试和重新部署发布。只读替换型 Tool 可在必要时做影子结果比较；有外部副作用的 Tool 在影子阶段不得真正执行，灰度也不能绕过确认、幂等和权限。
3. **行为 Skill**：沿用 `skills_design/PLAN.md` 的离线评测、影子、有限 canary、champion 和回滚边界。本计划不实现该控制面；控制面尚未具备时，新 Skill 或新版本只能离线/影子运行，且生产 resolver 必须保持不可达，不能仅凭代码内 `APPROVED/champion` 字段成为默认生产版本。

三类对象只共享“默认无生产权限、证据充分后人工放行、能够回滚”的原则，不共享一个新的生命周期 Schema。

### 7. User-facing presentation

用户界面不展示内部成熟度枚举、画像完整度或笼统置信分。每次降级说明应包含当前任务真正相关的事实：

- 实际可用的有效夜晚数量和日期范围；
- 当前回答使用的是个人记录、已确认自述还是一般知识；
- 缺少、过期或不可比较的内容；
- 因此当前可以说到哪一步，以及一个可行的下一步。

示例：“目前有 3 晚可用于比较的记录。我可以描述这些夜晚的差异，但还不足以判断这是你的稳定规律。”

内部 `reason_codes` 用于策略、测试、观测和审计；产品层将其映射为审核过的易懂文案，不直接暴露技术枚举。

## Approach

1. 在 `sleepagent/radar_agent/product_agent/cold_start.py` 建立唯一的小型、
   版本化纯策略模块，定义有限 `ClaimRequirement` catalog、
   `MetricReadinessDecision` 和 `ColdStartPolicy`；它消费已规范化的输入和
   owning Registry 产生的 capability eligibility receipt，不拥有数据库、后台
   worker 或发布控制，也不重新实现 Adapter/Tool/Skill 发布逻辑。其他入口不得
   复制 maturity/claim 规则。
2. 在该模块以静态表实现上述五类 ClaimRequirement 和 reviewed metric/policy
   lookup；未知 claim/metric/policy fail closed，不实现动态 DSL。
3. 在该模块建立唯一的夜晚资格与指标有效性纯函数，从 canonical
   NightEpisode/quality/provenance 输入派生指标级 `valid_night_count`，替换
   replay、API、dashboard/chat 和 Trend 中相互冲突的计数逻辑；现有
   `SourceScope.valid_night_count` 降为兼容展示字段。
4. 为现有 `ObjectiveBaselineArtifact` 增加由策略约束的构建/校验，在有界窗口
   上重算当前资格，并把 Artifact/typed decision 序列化进既有
   ToolReceipt/ProductEpisodeResult；缓存不作为权威，不新增状态表。
5. 为 measurement cohort 加入最小的设备/Adapter/Schema/算法/时间规则兼容键和可选 compatibility reference；默认隔离不兼容数据，修复当前跨设备混算。
6. 在个人趋势、晨间解释和 grounded dialogue 的现有入口调用薄策略；由
   runtime 产生合法 ClaimRequirements，将 decisions 绑定 FactSnapshot，并在
   Evidence acceptance 以 typed strength/ref 限制每个 claim。degraded 个人
   结论使用确定性边界句；发布前重验授权、来源与 semantic bindings，保留
   urgent boundary 的更高优先级。
7. 复用既有 Habit Profile API/Questionnaire Policy 实现“默认零问卷、按需一问、可选两题轻建档”；不建立新的 onboarding 问卷。
8. 收紧生产 Adapter capability 解析，使未对精确环境/配置/版本达到 `VERIFIED` 的能力不能进入用户结果；Tool 和 Skill 只增加边界测试与现有计划引用，不建设新发布控制面。
9. 添加边界、回退、跨设备、授权、安全、降级文案、重启重算和回归测试；用真实设备实验校准各指标门槛后再发布 reviewed 生产配置。

## Acceptance criteria

1. 一般知识请求在零个人数据时正常回答，且不会声称基于本人记录。
2. 零个指标有效夜晚时，个人趋势请求只能降级为一般知识、设备检查或数据准备说明。
3. 对尚无可用 established baseline 的新 cohort，一个有效夜晚只能支持带质量
   和日期的单晚描述；不能生成趋势、稳定规律或相对个人基线异常。
4. 对尚无可用 established baseline 的新 cohort，两到三个有效夜晚只能支持
   指明具体夜晚的描述性差异；输出中不出现“个人趋势”“通常如此”或“偏离个人
   基线”。
5. provisional/established 只能由服务端按目标指标、版本化策略和 canonical evidence 派生；伪造或陈旧的调用方 `valid_night_count` 不影响结果。
6. 同一 NightEpisode 可以对一个指标有效、对另一个指标无效；无效指标不能借用整晚或其他指标的有效状态。
7. 同一回答包含多个指标时，每个指标分别得到 claim ceiling；成熟指标不能提高不成熟指标的表达权限。
8. 模型伪造 ClaimRequirement、metric 或 readiness ref 会在 Evidence
   acceptance 前被拒绝；开放式意图无法确定映射时不会猜测个人指标。
9. `compare_explicit_nights` 只比较用户指定夜晚或注册的完整固定窗口；改变
   数值但保持日期/窗口不变不会改变入选夜晚，模型无法通过挑选子集制造趋势。
10. 现有 `SourceScope.valid_night_count` 不能授权成熟度或 claim；单指标时只
   展示 scope count，多指标时权威计数只从各 typed decision 读取；baseline
   count 不会覆盖 scope count。
11. 生产缺少 reviewed BaselinePolicy 时保持 unavailable 并给出稳定 reason；
   非生产 `4/15` fixture 不会被 production loader 接受。
12. 门槛边界、覆盖率边界、连续与稀疏夜晚、partial/unusable、过期和恢复均有确定性测试；少于 4 个有效夜晚不能形成 provisional baseline。
13. 不兼容的新旧设备或算法数据不会混入同一基线；没有桥接证据时换设备后目标指标从新 cohort 重新积累；同一夜的旧 revision 不会被重复计数；时间敏感指标跨不兼容 timezone/sleep-day policy 不会混算。
14. 设备换绑不会把上一 subject 的任何夜晚、基线或成熟度带给新 subject。
15. established 历史 Artifact 保持可审计，但当前数据过期、低质、撤权或不兼容时，当前使用资格会回退；重新积累后可以恢复；策略不兼容且无法重建时不会直接重标旧值。
16. 有可用 established baseline 且当前只有一晚时，可以形成经过审核的
   current-night-vs-baseline claim，但不能形成 longitudinal-trend claim；两个
   计数和两组来源在 receipt 中可区分。
17. 首次使用不出现强制问卷；跳过轻建档不影响一般知识和当前数据允许的功能。可选轻建档首次最多 2 题，整个 Episode 最多 3 题。
18. 任何首次回答命中红旗时停止普通画像采集并进入既有安全路径；没有个人基线不会压低绝对风险；未验证 Adapter 只产生运维信号。
19. 画像字段未知、过期或争议时，只限制依赖该字段的结论，不触发全局降级，也不会被模型补全或推断。
20. 生产 Adapter resolver 只有在 deployment enabled 和精确 capability/version/config/environment verified 同时满足时才返回能力；生产影子只到未绑定 subject 的 candidate/comparison receipt，完整 shadow 使用隔离非生产环境。
21. 新 Tool 或 Skill 的冷启动不能扩大 Agent allowlist、数据权限、确认范围或副作用权限；Skill 控制面缺失时新版本不能成为默认。
22. capability eligibility receipt 缺少 Registry snapshot/hash、精确版本/环境/
   配置或权威 refs 时不能进入 FactSnapshot；模型和请求 body 不能伪造它。
23. 每个冷启动 decision 都能从既有 receipt 追到策略版本、FactSnapshot/source refs、cohort、scope/baseline 两个有效夜数、ceiling 和原因；不记录原始 payload。
24. 当前 readiness 在服务重启和缓存清空后从相同 canonical window 得到相同
   decision；历史 Artifact/decision 仍可从冻结 ProductEpisodeResult/ToolReceipt
   审计。
25. `EvidenceClaim.claim_strength` 高于绑定 decision ceiling、metric/cohort 不符或缺少 ref 时无法成为 accepted Evidence；degraded 路径不靠自由文本分类器执行边界。
26. 模型调用期间发生授权撤回或 source revocation 时不会发布陈旧结果。
27. 历史 Episode/Digest 单独不能支持当前异常、风险或行动；current-source revalidation 失败时输出明确降级。
28. 用户侧显示实际证据范围和可理解的能力边界，不显示内部成熟度枚举、画像完整度或无校准的综合置信分。
29. 新策略接入后，既有画像、Evidence、Safety、Memory、设备隔离、权限和产品 Agent 回归测试无退化。

## Key decisions & tradeoffs

1. 选择三个正交维度和按请求裁剪，而不是全局冷启动等级；增加少量运行时判断，换取局部降级和更诚实的能力边界。
2. 选择夜晚资格与指标有效性两级定义，而不是一个全局布尔“有效夜”；避免某个指标的低质量污染全部基线。
3. 选择设备/算法默认隔离、证据桥接后才延续；接受换设备后的短期保守体验，避免测量域变化被误报为个人变化。
4. 保留 `unavailable | provisional | established` 三级基线成熟度，把单晚和长期功能放入 claim ceiling；避免状态膨胀。
5. 历史成熟度不可变、当前使用资格可回退；同时满足审计真实性和当前证据诚实性。
6. 首次使用默认零问卷、按需一问、可选轻建档；接受画像长期不完整，换取较低负担。
7. Adapter、Tool、Skill 分别治理；牺牲表面统一性，避免建设不必要的通用发布平台。
8. 用户看到证据范围和自然语言限制，不看到内部枚举或综合分；优先可理解性而非技术可见性。

## Risks / open questions

1. 各指标 provisional/established 的有效夜数、覆盖率、新鲜度和允许缺失仍需真实设备实验校准；`4` 和 `15` 只是候选起点。
2. 当前 Trend 的 7/30/90 日窗口与 3/7/14 样本门槛需要和新的 `BaselinePolicy` 对齐，但不应为了统一而强迫所有指标使用同一门槛。
3. 不同设备型号、固件、Adapter 与算法版本的兼容矩阵需要由真实对照数据和领域审核确定；首版没有证据时一律隔离。
4. 当前 ObjectiveBaseline 主要是合同和内存 Store，生产构建、持久化及按策略重算的实际接入面需在实施前再次确认。
5. 当前 Adapter `enable` 与 resolver 对 `VERIFIED` 的约束不足；修复时需区分 Adapter 已启用和其中某项 capability 已验证，避免错误地把整台设备一次性全部放行。
6. 首次说明、降级文案和两题轻建档仍需目标年龄段可用性测试；这验证理解与负担，不证明医学效果。
7. 现有 `FactSnapshot` 和 `EvidenceClaim` 尚无 readiness refs/claim strength；
   实施需要 additive contract/version 变更和旧记录只读兼容，不能把字段藏进自由
   文本或 `active_constraint_codes`。
8. 现有 Trend 将当前窗口样本和 older baseline 分开计算，但 SourceScope 只有
   一个 scalar count；迁移必须防止旧消费者继续把 scope count 当成 baseline
   maturity。

## Out of scope

1. 新增 Agent、统一冷启动服务、通用规则引擎、工作流编排平台或新的用户状态数据库。
2. 画像完整度评分、统一睡眠分、用户等级、徽章或以补全资料为目标的主动追问。
3. Bayesian/机器学习基线、自动阈值学习、自动设备校准或无审核的 cohort 桥接。
4. 完整问卷、标准量表、正式风险筛查、诊断、治疗建议或医学有效性声明。
5. Adapter/Tool/Skill 共用的发布控制面、任意代码热加载、自动 Skill 生成、自动审批或自动晋级。
6. 多人共享设备归因、多设备自动融合、跨厂商临床等价性或医疗器械认证。
7. 重做产品信息架构、四 Agent 职责、Memory 治理、授权体系或完整数据保留制度。
