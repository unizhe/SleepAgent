# Plan: SleepAgent 模拟数据后端闭环与终端演示
_Locked via grill — by Codex + user_

## Goal

在不接入真实毫米波雷达、不建设前端、不重复验收即插即用能力的前提下，
交付一条可从终端操作、由真实 HTTP 后端承载、以 provider-neutral canonical
睡眠数据为输入的纵向闭环：

```text
canonical replay recipe
→ SleepObservation
→ NightEpisode / revision
→ deterministic quality & risk fast path
→ cold-start readiness
→ FactSnapshot
→ NightEpisodeAgentBridge
→ 1+2+1 ProductEpisodeRunner
→ role views / user question / exact confirmation
→ governed persistence
→ terminal induction / Digest / canonical source revalidation
```

目标架构的唯一智能权威是现有四角色
`SleepCareAgent + EvidenceReasoningAgent + CareStrategyAgent +
SafetyReviewAgent`。旧 `legacy_fixed`、`dynamic_goal`、旧七角色 replay
运行时和旧 `radar-agent run-demo/run-goal` 只可作为历史回归或场景语义来源，
不得成为本闭环的实现或验收路径。

最终交付同时包含：

1. 可重复、离线的 deterministic 工程验收轨；
2. 使用 DeepSeek V4 Flash 的 live 智能演示轨；
3. 模块级最小场景；
4. 一条同一 synthetic 老人、跨 15 夜的有状态黄金旅程；
5. 面向终端的真实 HTTP 客户端，未来前端可复用同一应用服务和产品 API。

## Plan authority

1. `product_positioning/PLAN.md` 与 `product_information_architecture/PLAN.md`
   继续决定产品边界、老人第一用户及三角色协同。
2. `agent_architecture/PLAN.md` 决定四 Agent roster、职责、权限、Safety、
   Commit Controller 和最小运行路径。
3. `sleep_habit_profile/PLAN.md`、`DECISION-GAP-MAPPING.md`、
   `ONLINE-REASONING-FREEZE.md` 决定习惯提问、来源、确认和持久化规则。
4. `healthclaw_memory_governance/PLAN.md` 决定纵向 Memory、Digest、归纳、
   回源和生产发布门禁。
5. `cold_start/PLAN.md` 决定 readiness 的按指标、按 cohort、按 claim
   ceiling 语义。
6. `plug_and_play/PLAN.md` 继续决定真实 Adapter 与供应商接入，但本任务从
   canonical 边界开始，不重新验收其 transport/签名/厂商兼容能力。
7. 本计划只负责把上述已锁定设计组合为 simulation/replay 后端 vertical
   slice；发生冲突时，上述领域权威计划优先。

### Scoped replay-only amendment

用户明确批准了一个只为本任务服务的窄幅例外：在不伪造 benchmark attestation
的前提下，允许隔离的 `development/test + replay:*` composition 演示 Digest
查询机制。它不是对 production Memory 治理的修改：production repository 的
enablement state、`DeploymentControlAttestation`、release verifier 和默认
`LongitudinalMemoryService` 仍完全服从
`healthclaw_memory_governance/PLAN.md`。本计划只允许由
`sleepagent.simulation` 提供一个进程内、短期、不可持久化的 access-policy
实现，通过 production-owned policy protocol 注入；production package/factory
不得 import、构造、保存或反序列化该 simulation 类型。若这一隔离无法用静态
依赖和运行时负向测试证明，则取消 replay Digest read，只保留 fail-closed
演示，不能通过设置 `benchmark_gate_passed=True` 绕过。

## Current verified baseline and gap

仓库已经具备四角色 `ProductEpisodeRunner`、NightEpisode lifecycle、
`DeterministicFastPathService`、`NightEpisodeAgentBridge`、cold-start
纯策略、Habit Profile、持久化 Commit Controller、纵向 induction worker 和
Sleep API 的分段实现与测试。

当前缺口是应用级接线，而不是再设计一套 Agent：

1. `sleep_api` runtime 只装配 NightEpisodeService，尚未统一装配 fast path、
   readiness、Product Agent bridge 和相应 worker。
2. `NightEpisodeAgentBridge` 当前统一注入 unavailable readiness，canonical
   历史夜晚尚不能真实推进到 1/2/4/15 夜 ceiling。
3. 多个 Product Tool 仍依赖调用方传入 `tool_inputs`，没有统一从 canonical
   repository 按 FactSnapshot 取证。
4. feedback/reanalysis 能追加 revision，但没有稳定地继续提交 Agent analysis。
5. 当前通用 CLI 走旧运行时；四角色没有真实 HTTP、可持久化、可手动续跑的
   terminal demo。
6. 当前 `.env.deepseek.local` 使用旧
   `SLEEPAGENT_RADAR_AGENT_LLM_*` 键，而四角色默认读取
   `DEEPSEEK_API_KEY`/`SLEEPAGENT_PRODUCT_LLM_*`；必须显式解析并 fail fast。
7. 纵向 Digest 生产读取要求完整 deployment attestation，其中包含 benchmark
   gate；本任务明确不伪造该 gate，而使用严格隔离的 replay-only 功能模式。

## Definition of complete

只有以下七组均通过，才称为“模拟数据下后端闭环完成”：

1. `journey`：15 夜黄金旅程通过，live 模式由人手动推进。
2. `agents`：一般知识、晨报、Care、Safety、急症抢占均走正确最小路径。
3. `habit`：跳过、老人确认、家属来源和 episode-only 边界均正确。
4. `memory`：归纳、Digest 回源、遗忘失效和 urgent exclude 均正确。
5. `cold-start`：0/1/2/4/15 夜、按指标独立、降级与恢复均正确。
6. `backend`：正常、低质量、迟到报告均贯穿 canonical lifecycle、fast path、
   bridge 与 API。
7. `persistence`：后端重启后 Profile、Care、Episode、Digest、确认和审计可恢复。

此外必须同时满足：

- 全链没有调用 `legacy_fixed` 或 `dynamic_goal`；
- live Agent 成果全部来自 DeepSeek V4 Flash，且每次调用有 provider request ID；
- live 路径没有 scripted model、fallback 或预制 Agent work product；
- 输入、状态变化、Agent/Tool trace、Safety/确认、最终 Receipt 均可审计；
- 所有输出明确标记 `data_mode=replay` 和 synthetic/non-release 状态；
- 不宣称临床有效、生产就绪、真实老人获益或真实雷达性能。

## Simulation boundary and data design

### 1. Allowed simulated inputs

模拟对象只包括外部世界事实、身份关系和人类操作：

1. **冻结环境**
   - strict scenario schema/version；
   - 固定 seed、scenario-clock 起点、IANA timezone 和 sleep-day policy；
   - `DomainNamespace(namespace_id="replay:<workspace>", data_mode=replay)`；
   - Adapter/schema/producer/algorithm/policy 的精确版本与 hash。
2. **Synthetic identity and authorization**
   - 一个 synthetic subject；
   - elder、family、doctor 三个 actor；
   - device binding、role binding、authorization ID/scopes/epoch；
   - 所有姓名、ID、健康信息均为明显虚构值。
3. **Canonical sleep observations**
   - heart rate；
   - respiratory rate；
   - bed presence；
   - movement；
   - bed exit；
   - device connectivity；
   - missing interval；
   - sleep-stage interval；
   - vendor sleep-profile metric，仅以 canonical provider observation
     语义保存其 provenance、未知 calibration/confidence，不当作诊断真值。
4. **Human events**
   - 老人的一般问题和个性化问题；
   - Habit 回答、skip、unknown、do-not-ask；
   - family observer report；
   - Care/Habit/Memory 的精确确认或拒绝；
   - 主观反馈、reanalysis、forget/withdraw；
   - urgent boundary 文本。
5. **Scenario overlays**
   - 正常、低覆盖、缺失、离线、迟到 report、更正 revision；
   - 单指标缺失、质量回退和恢复；
   - 不通过 raw vendor payload、签名 envelope 或毫米波/IQ 波形表达。

### 2. Inputs that must never be simulated in live mode

下列内容只能由真实服务、确定性代码或 DeepSeek 产生，不能写进 world fixture：

- data-quality/risk 结论；
- NightEpisode sufficiency 或 revision 结果；
- metric valid-night counts、baseline maturity、claim ceiling；
- FactSnapshot；
- EpisodePlan；
- EvidencePacket/claim；
- CareStrategy/CareActionCandidate；
- SafetyDecision；
- CommunicationDraft/final response；
- Memory query result、Digest 或 InductionReceipt；
- 预先决定的 Agent 路由和成功 verdict。

deterministic 模式可以另用严格、按 Agent/schema/call-index 绑定的 model script，
但该 script 必须物理上与 world fixture 分离，并且任何带 script 的运行都不能标为
live 或智能效果证据。

### 3. Fixture package

每个场景包使用同一组版本化合同：

```text
scenario.json       # 环境、身份、cohort、seed、夜晚 recipe 与 overlays
actions.jsonl       # deterministic 自动验收所需的人类动作，不供 live 自动执行
expected.json       # typed oracle，只存预期状态/不变量，不作为运行输入
```

deterministic model script 存放于单独的
`fixtures/deterministic_models/<scenario>.jsonl`。live loader 必须拒绝加载该目录。

生成器按 fast-path policy 的固定 cadence 产生恰好足够的 canonical observations，
而不是检入数千行手写 JSON。相同 seed + recipe + version 必须产生相同 ID、时间、
payload hash 和 observation sequence。旧七个 replay 场景只迁移“正常、质量、
离床、体征、趋势、升级、急症”的语义，不迁移旧 schema、golden 或 runtime。

### 4. Expected assertions are not inputs

`expected.json` 只能由 verifier 读取，业务 runtime、Agent context、Tool 和 Prompt
都不得读取。它验证：

- canonical observation/revision 数量与 lineage；
- fast-path quality/risk reason code；
- readiness ceiling/count/ref；
- Agent 调用集合和禁止调用集合；
- ToolReceipt、Safety、confirmation、state version、role view；
- terminal/induction/Digest lifecycle；
- provider/model/request ID；
- 禁止词义或越权行为采用结构化合同与 source refs 验证，不用脆弱全文比较。

oracle 的物理边界固定如下：

- `seed` client 只把由 `scenario.json` 解析出的 strict `ScenarioSeedRequest` 发给
  server；`actions.jsonl` 和 `expected.json` 留在 CLI/verifier 进程；
- `verify` 在 client 侧读取 oracle，再通过正式查询面和受限 developer trace
  收集实际值；server 不提供“读取 expected 并自行判分”的 endpoint；
- server-side `JourneyRun` 最多保存 opaque `verification_contract_hash`，不能用它
  解析文件、分支业务逻辑或构造 Prompt；
- deterministic model script 只可由 deterministic composition 的 model adapter
  在启动时显式加载，不能通过 seed payload 进入业务状态；live composition 遇到
  script path、script hash 或 scripted adapter 立即拒绝启动。

### 5. Dual-clock contract

系统显式区分两种不可互换的时间：

1. `ScenarioClock` 只决定 synthetic observation/report 的 `occurred_at`、sleep-day、
   night close/deadline、late-report 顺序和 cold-start/baseline 的事件窗口；
2. `ControlClock` 决定认证/JWS、authorization epoch 检查、confirmation/request
   expiry、worker lease、idempotency reservation、审计 `recorded_at`、publication
   reconciliation、Digest TTL/retention 和真实调用 latency。

deterministic 测试可以分别注入并显式推进两只 clock；live journey 只能推进
`ScenarioClock`，`ControlClock` 必须跟随 server 的真实 UTC，CLI 不得设置、倒退或
用 15-night fast-forward 改变它。需要测试 expiry/lease 的 deterministic case 使用
独立、审计过的 control-clock advance，不能冒充 live 证据。所有跨边界记录必须带
`time_basis=scenario|control` 或由 schema 明确字段语义；禁止拿 scenario timestamp
判断 token/lease/retention，也禁止拿 control timestamp 计算有效睡眠夜。`JourneyRun`
保存 scenario cursor/anchor 和 control recorded-at，restart 恢复前者、重新读取后者，
并拒绝 mixed-basis 比较。

### 6. Run and counterfactual-arm isolation

workspace generation 之下再建立 typed `RunScope(journey_run_id, arm_id, model_mode,
parent_snapshot_hash)`，并把它编码进 demo `DomainNamespace`。黄金旅程的所有 checkpoint
共用一个 RunScope；不同 suite、deterministic/live、以及 A/B 两臂绝不共享可变
Profile、Care、Memory、Digest、analysis 或 idempotency state。

counterfactual pair 从同一 frozen pre-intervention `RunSnapshotManifest` fork 两个新
arm namespace。fork 只复制白名单内已提交的 canonical observations、授权 binding、
Profile/Care/Memory 版本和 source refs；不复制 credential、pending handle、lease、
in-flight operation/outbox、provider attempt 或 oracle。server 签发 `ForkReceipt`，记录
parent/child namespace、各表 snapshot hash、model mode 和唯一允许的 intervention
delta；verifier 必须证明两臂其他 state hash 相同。每个 operation/result/receipt/view/
trace 绑定 exact JourneyRun + arm + model mode；跨臂或 deterministic→live source ref
立即 fail closed。live pair 仍只作非统计 smoke proof，不把 forked 旧 LLM output 计入
当前 live coverage。

## Target backend composition

### 1. One production-shaped composition root

新增一个可注入依赖的 `SleepBackendRuntime` composition root，统一拥有：

- one `RadarPersistenceStore`；
- `SleepDomainRepository`；
- `NightEpisodeService`；
- `DeterministicFastPathService`；
- canonical readiness service；
- `PersistentProductDataProvider`；
- canonical Product Tool handlers/source resolvers；
- one environment-built `ProductEpisodeRunner`；
- `NightEpisodeAgentBridge` and persistent Agent worker；
- Sleep API runtime/operation worker；
- longitudinal induction scheduler；
- Habit/Care/Memory Commit Controller stores；
- explicit `ScenarioClock` and `ControlClock` providers。

它是当前 replay vertical slice 和未来真实雷达接线共用的应用组合，不在
`sleepagent.simulation` 内复制业务逻辑。`sleepagent.simulation` 只拥有 recipe、
generator、dev control、CLI client 和 verifier。

demo/closure composition 启动时必须生成并持久化 `RuntimeDependencyManifest`：列出
每个 repository/store/worker/model/provider 的 concrete implementation、schema/
migration version、durability class、database storage UUID/path hash 和 config hash。
除明确标记的短期 synthetic Digest lease 外，JourneyRun、operations、outbox、
Product results、pending targets/handles、Commit journals、Profile/Care/Memory、
publication journal、terminal bundle/Job/Digest、auth replay nonce 和 audit cursor
全部必须落在同一个 file-backed SQLite authority。任一必需依赖是 `InMemory*`、
`:memory:`、fake live provider、不同 storage UUID 或未迁移 schema 时，preflight
fail closed；不能依赖“persistent class 继承 in-memory class”的名称推断持久性，
必须逐项重启验证。

跨上述表的原子协议使用同一个显式 SQLite Unit of Work/connection/cursor；参与方法
不能在内部提前 `commit()`。若现有 repository API 会各自提交，先提取 cursor-aware
方法或新增窄幅 Unit of Work，再承诺“同一事务”。不得用补偿写或进程锁冒充数据库
原子性。

### 2. Canonical lifecycle wiring

一个 night 的确定顺序为：

1. simulation control 通过应用服务提交 strict `SleepObservation`；
2. `NightEpisodeService.process_observation()` 绑定 subject/cohort 并更新 active
   NightEpisode；
3. close/deadline/late-report 只通过现有 lifecycle transition 产生 revision；
   revision 与 `night_revision_ready` domain outbox event 在同一数据库事务提交；
4. fast-path worker 以 lease + CAS 消费 committed event，不能依赖进程内 callback；
5. fast path 在一个事务中保存 quality/risk/source-scope receipt、对应 domain
   event，以及 non-urgent Agent operation intent；
6. Agent worker 以 persistent operation + bounded lease 执行 slow path；
7. bridge 只能读取 exact revision 的 canonical facts，构造最小 FactSnapshot；
8. feedback/reanalysis 产生新 revision 后，按 idempotency key 重新走 4–7；
9. urgent deterministic boundary 立即抢占，Agent/model invocation count 必须为 0。

bridge、`/product/sleep/interactions/*` 和 feedback/reanalysis handler 都只能创建或
恢复 persistent `ProductInteractionOperation`，不得在 request thread 直接调用
`ProductEpisodeRunner.run()`。唯一 Runner caller 是 Agent worker。operation identity
分为两层：

1. canonical revision 分析只有 fast-path committed outbox 可以铸造 server-owned
   `AnalysisRequestKey(namespace/run/arm, subject, exact revision, trigger kind,
   model mode, policy/registry hash)`；它不包含 service principal、actor、路由或
   caller idempotency key，并在数据库有唯一约束；
2. HTTP/demo/bridge 只能引用或查询该 key。reanalysis 先产生新 canonical revision，
   再由 fast path 铸造新 key；用户主动 ask 使用独立的 authenticated interaction key，
   并以 parent AnalysisRequestKey/FactSnapshot 关联，不能竞争同一 revision analysis。

caller idempotency key 只去重命令并映射到 semantic key，不参与自动分析 identity。
相同 semantic material 返回同一 operation/result，命令 body hash 变化返回 conflict；
新 revision 创建带 parent lineage 的新 operation。旧 `/product/radar/chat` 只能留在
显式 legacy surface 做回归，新 CLI、bridge 和统一产品 API 的 dependency graph
均不得调用它。

一次 canonical analysis operation 还拥有一个 operation-derived `AnalysisAttempt`。
三角色所需的 prepared Product results、invocation receipts 和 communication drafts
先作为不可查询的 attempt members 持久化；所有必需成员/Safety/targets 准备完成后，
才在一个 Unit of Work 中提交 attempt、唯一 AnalysisRevision、三 role views、pending
handles、terminal group/induction job 和 outboxes。任何中途 crash 只留下可恢复但不可
发布的 prepared members，不留下 role-visible orphan。analysis/revision/member/view ID
全部从 AnalysisRequestKey + attempt generation + role 派生，禁止用 `len(history)+1`。
同一 analysis group 只创建一个 longitudinal induction job；role-specific drafts 是
该 group 的派生投影，不能各自重复归纳同一夜事实。

传递语义明确为 at-least-once delivery + idempotent effects，不声称分布式
exactly-once。每个 effect 的唯一键至少绑定 namespace generation、revision、
trigger、model mode 和相关 policy/version hash；重复、恢复或 worker 重试返回
原语义结果，不能产生竞争的 current revision、analysis、role view、confirmation、
terminal bundle 或 Digest。lease 丢失的 worker 无权提交，未知 LLM/外部结果不
自动盲重试。

### 3. Publication and terminal commit boundary

当前 Runner 的“先调用 publisher、后 append terminal bundle”顺序不能直接进入新
闭环。将 Runner 明确拆为 `prepare()` 与 caller-owned `commit_prepared(tx)`：前者
执行 Agent/Tool/Safety 并只写 invocation/attempt checkpoint，不发布、不调用 Commit
Controller、不生成可查询 role view/terminal bundle；后者只能由持有 Unit of Work
和有效 operation lease 的 Agent worker 调用。新 composition 执行以下 durable
protocol：

1. 所有 required prepared members 完成后，在同一事务 CAS 保存 committed result/
   AnalysisAttempt、exact pending targets、Commit Controller effects、terminal group
   bundle（若 terminal）、唯一 induction job 和 persistent publication intent/outbox；
   事务失败时不存在可见 state change 或 role view；
2. 只有 committed result 才能被 role-view query 或 delivery worker 读取；发起交互
   的 HTTP 请求返回 operation/receipt，客户端从已提交 resource 读取结果，连接中断
   后用同一 idempotency key/status endpoint 恢复；
3. delivery worker 按 intent id 调用幂等 sink，然后用 CAS 把 journal 单调推进为
   delivered/failed；sink 调用后、journal 更新前崩溃视为 `delivery_unknown`，必须
   reconciliation，绝不能重跑 LLM、Commit Controller 或创建第二个 terminal bundle；
4. public event 同样来自 committed outbox；任何 role view、Digest induction 或
   “已发送”状态都不能从未提交 draft 或进程内 callback 生成。

本任务不发送真实外部消息，但本地 public-event sink 仍按该协议实现。故障注入要
覆盖 prepared commit 前后、sink 返回前后、journal CAS 前后和 induction enqueue
前后，证明每个语义 effect 至多一个、传递仍诚实标为 at-least-once。

### 4. Canonical Product Tools

外部 HTTP body、CLI 或模型不得直接提供权威 `tool_inputs`、subject ID、夜晚计数
或 source refs。新增 canonical handler/provider：

- 根据 authenticated binding + FactSnapshot + exact revision/source scope 查询；
- 从 repository 读取 night/range evidence、device state、feedback、Care state；
- 计算 trend/quality，而不是读取 fixture 中的预制结论；
- 返回版本化 ToolReceipt 和 canonical source refs；
- enforce Agent allowlist、purpose、role、subject、data mode 和 budget；
- query/revalidation 失败时 fail closed，不退回旧 dashboard JSON 或 raw audit。

`ProductEpisodeRunRequest.tool_inputs` 可保留内部兼容字段，但 replay/public API
不得让调用方用它提供权威事实。旧 passthrough handler 不进入新 composition。

### 5. Cold-start readiness wiring

新增 canonical readiness service，把现有函数真正接入 runtime：

```text
canonical revisions
→ derive_metric_valid_nights
→ project_baseline_readiness
→ evaluate_readiness
→ MetricReadinessDecision + capability receipt
→ FactSnapshot refs/hash
→ Evidence acceptance/publication postflight
```

本任务增加显式 `simulation-baseline-policy.v1`：

| 有效夜晚 | simulation ceiling |
|---|---|
| 0 | general knowledge only |
| 1 | single-night description |
| 2 | explicit two-night difference |
| 4 | provisional pattern |
| 15 | established baseline |

该策略只在 `development/test + replay:*` 可解析；production loader 继续在缺少
reviewed policy 时 fail closed。每个 metric 独立计数；低质量、缺失、过期或
不兼容 cohort 只影响对应 metric。当前窗口与 baseline 窗口两个计数必须分开，
模型和 API 不能声明权威计数。

scenario body 不能自报“能力可用”或铸造 `CapabilityEligibilityReceipt`。server-owned
replay registry 固定登记 `canonical-replay-provider.v1`，通过现有 capability
resolution protocol 为本次 canonical generator 签发 synthetic/non-release receipt；
receipt 必须绑定 generator/schema/cohort/environment/configuration hash，并带明确的
resolution、verification、deployment authority refs。scenario 只能引用已登记的
provider ID/version。该 receipt 只在 `development/test + replay:*` 有效，production、
`live:*` namespace 和 release verifier 全部拒绝；它只证明本次 canonical replay
输入的版本身份，不构成即插即用、真实 Adapter 或硬件验收。

### 6. Longitudinal Memory functional mode

不伪造 `DeploymentControlAttestation.benchmark_gate_passed`。Digest access
由 production-owned protocol 决定，具有两个互不混用的实现来源：

- `release_attested`：production package 的唯一默认实现，保持现有完整 gate；
- `synthetic_nonrelease`：类型定义和构造器只存在于 `sleepagent.simulation`，
  只允许 `development/test + replay:*`，需要显式 CLI 开关、synthetic
  workspace 和短期不可持久化 lease/独立 Receipt。

`synthetic_nonrelease` 必须：

1. 永不写入或升级 production attestation/retrieval enablement state；server
   restart 后必须重新显式申请新的短期 lease；
2. 在每个 query/read/trace/view 上带 synthetic non-release label；
3. 仍执行 actor/subject/purpose/type/TTL/epoch/authorization 过滤；
4. Digest 仍只是 hint，Evidence 必须通过 canonical source resolver 重新取证；
5. forget/withdraw/kill switch 仍使旧 handle、slice 和 in-flight publication 失效；
6. production、`live:*` namespace、release verifier、production factory 和
   正式 acceptance manifest 一律拒绝；production dependency graph 不得 import
   simulation policy；
7. 后续完成正式 benchmark 后，production 仍只能启用 `release_attested`。

终端 `memory layers` 只展示 L0–L4 的职责、版本和 refs：L0 Policy，L1 reviewed
Knowledge/catalog，L2 typed Profile/governed Memory，L3 shared Skill，L4 audit +
EpisodeDigest。它不得伪装成五个都可由个人会话任意写入的 Store。

## DeepSeek live boundary

### 1. Configuration resolution

实现一个不输出 secret 的 Product LLM config resolver。解析优先级固定为：

1. canonical `DEEPSEEK_API_KEY` / `SLEEPAGENT_PRODUCT_LLM_*`；
2. 本地 demo 明确允许的旧别名
   `SLEEPAGENT_RADAR_AGENT_LLM_*`；
3. 非 secret 默认值仅限现有 DeepSeek base URL、timeout、retry。

若 canonical 与旧别名同时存在且值冲突，fail closed。`--env-file` 使用严格、
不执行代码的 dotenv parser，只接受已知 `KEY=VALUE`、注释和受支持的引号；拒绝
`export`、命令/变量替换、重复键、未知 live LLM 键和 multiline secret，绝不通过
shell `source`。文件必须由当前用户拥有且 group/world permissions 为 0，否则
preflight 拒绝并给出 `chmod 600 <path>` 的修复提示；工具不自行改权限。
加载只影响当前 server 进程，不复制、不回显、不写回 `.env.deepseek.local`。
日志、trace、error 和 JSON output 只能展示键名、model、provider host 和
redacted 状态。当前文件权限为 `664`，因此进入 live build proof 前必须由用户
显式收紧；deterministic 轨不读取该文件。

live preflight 必须确认：

- API key 已解析；
- exact model 为 `deepseek-v4-flash`；
- HTTPS base URL；
- timeout/retry 有界；
- provider 返回 request ID；
- 四个 Agent 的 strict structured schema 各完成一次最小非个人化 probe；
- probe 失败不启动 live journey。

### 2. Live provenance invariant

LLM adapter 的返回合同改为 atomic typed result，例如
`StructuredGenerationResult(output, call_metadata)`；`call_metadata` 至少包含 logical
invocation ID、attempt ID、provider/model、provider request ID、started/ended-at 和
outcome。Invoker 必须从同一个返回值同时取得 schema output 与 metadata，禁止再从
共享 model instance 的 mutable `last_provider_request_id` 侧信道读取，因为并发或
重试会把 request ID 绑定到错误 Agent。provider transport 关闭隐藏重试；有界重试
由 invocation 层逐 attempt 执行并持久化。每个收到 provider response 的 attempt 都
记录其 request ID（包括随后 schema-invalid 的 response），无 response 的 transport
failure 记录 local attempt ID/error class；最终 accepted work product 只绑定产生该
output 的 exact accepted attempt。live verifier 同时检查 accepted invocation 与全部
provider-response attempt，不能只检查 model 对象的最后一个 ID。

外部 LLM 调用还必须有 durable invocation journal：调用前先 reserve attempt；收到
response 后，把 schema-valid structured output、immutable metadata、context/output
hash 和 sanitized failure class 以 CAS 保存，再允许下游 acceptance/commit。raw API
key、完整 Prompt、provider reasoning 和未治理的 raw response 不落 journal。restart
遇到 `reserved` 且无 durable response 的 attempt 标为 `provider_outcome_unknown`，
不得自动再调模型；需要用户显式 retry，并创建可审计的新 attempt。自动 retry 只允许
明确的 pre-dispatch failure、provider 明示未处理的 retryable response，或已收到且
明确 schema-invalid 的 response；timeout-after-send/连接中断一律视为 unknown。
同一 logical invocation 通过 CAS 最多接受一个 attempt；迟到 response 只能记录为
discarded，不能覆盖 accepted output。这样 crash-resume 既不把脚本当结果，也不静默
制造第二次 DeepSeek 分析。

live 模式中，每个实际触发的 Agent invocation 都必须：

- provider=`openai-compatible`/DeepSeek；
- model=`deepseek-v4-flash`；
- 有非空 provider request ID；
- 有 invocation ID、prompt/skill/schema/profile hash；
- execution/generation mode 不为 scripted、fallback 或 simulated；
- strict schema validation 和 deterministic acceptance gate 均通过。

任一 Agent 缺失 request ID、调用失败、返回非法 JSON、超预算或发生 fallback，
该 live run 明确失败；不得切换 scripted model 继续并宣称 live 成功。

### 3. What live proves

live 不使用全文 golden。自动 verifier 证明结构、来源、边界、调用覆盖和状态；
它还证明 exact context packet/source refs 被真实发送给 DeepSeek，且 accepted output
通过了与这些 refs 绑定的 gate。单次随机 live A/B 的文本差异不能独自证明模型的
因果推理效果，因此不作该夸大声明。模块“功能产生了什么效果”的权威证据来自
deterministic 最小反事实：同一 seed、用户意图、target night、policy/model script，
每对只改变一个受测状态，并验证结构化 delta：

| 模块 | 唯一改变 | 必须出现的可审计 delta |
|---|---|---|
| Agents | trigger/claim requirement | invocation set、Safety target、final gate |
| Habit | answer 未确认 → 已确认 Profile | Profile version/ref、ToolReceipt、decision gap |
| Memory | valid Digest lease → forget/withdraw | hint/slice、回源 receipt、旧 handle 失效 |
| Cold start | compatible valid-night history | count/ceiling/decision ref、越界 claim 拒绝 |
| Backend | normal → missing/late overlay | revision lineage、quality/risk、operation/view |

live 使用对应 controlled pairs 做非统计 smoke proof：context packet hash/source refs
必须按预期不同，输出必须遵守相应 ceiling/provenance；不以“措辞不同”当作模块有效
或智能质量评测。受控 pair 包括：

- 单夜 vs provisional/established ceiling；
- Habit 未确认 vs 已确认且有 Profile ToolReceipt；
- Digest 可回源 vs forget 后无合法 slice；
- 普通晨报 vs Care vs Safety path。

live 黄金旅程中的每个 Agent Episode 都真实调用 DeepSeek；一般知识、Care 和
doctor/Safety 三类 live 路径合计覆盖全部四个 Agent。deterministic 轨只证明
工程控制流，不作为智能效果证据。

## Golden journey

使用一个 synthetic subject、一个 radar cohort、Asia/Shanghai timezone，以及
elder/family/doctor 三个授权 actor。15 夜由同一 generator 产生；只在关键节点
停留，非关键夜批量 advance 但仍逐夜写 canonical state。

### Checkpoint 0 — no personal night

- 老人提出一般睡眠知识问题；
- 只允许 SleepCare + reviewed Knowledge；
- Evidence/Care/Safety 不调用；
- 明确标注非个性化，不能假装已有个人数据。

### Checkpoint 1 — one valid night

- 充分覆盖的 canonical observations 形成第一个 revision；
- readiness ceiling 为 single-night description；
- `SleepCare → Evidence → SleepCare`；
- 不得称趋势、异常或个人基线；普通高质量晨报不固定调用 Safety。

### Checkpoint 2 — two explicit nights and Habit gap

- 只能形成两晚描述性差异；
- 设计 world facts 使一个注册的 decision gap 成为必要条件；
- live Agent 必须提出一个合法、最多一题、可跳过的 Habit request；
- 人工 `answer` 后回答可在当前 Episode 作为 provenance-bearing Evidence 使用；
- 未确认前 Profile version 不变；
- 人工查看 exact change set 并 `confirm` 后，Profile version +1。

黄金 deterministic case 固定验证 `habit.nap_pattern`；live case 允许从该 gap 的
合法 concept 集中选择一项，但必须证明该回答会改变当前 Evidence/Care 决策，
不能为了画像完整度提问。

### Checkpoint 4 — provisional and Care

- 第四个有效夜使目标 metric 进入 simulation-only provisional；
- Evidence 通过 Profile ToolReceipt 使用已确认习惯；
- Care 只消费 accepted Evidence，从 reviewed catalog 选择零或一个行动；
- deterministic 黄金 case 精确要求 `consistent-wake-time` 且参数在 catalog
  范围内；live case 要求 `action_required` disposition 和恰好一个来自预注册
  低风险 allowed set 的 catalog candidate。若 live 用户在当前顶层消息明确选择
  “稳定起床时间”为目标，则才精确要求 `consistent-wake-time`；
- runtime 进入 waiting_confirmation；
- 人工 `confirm` 后由 Commit Controller 激活，Care state version +1；
- 无确认时不得修改状态或声称执行成功。

### Follow-up and longitudinal memory

- 老人通过独立 `feedback` 提交主观体验；
- Evidence 保留 `user_reported`，Care 可保持/简化/停止，但不能把主观反馈改写成
  客观改善或因果疗效；
- terminal result 原子保存 audit result + sanitized Manifest + Job；
- induction worker 创建 Digest/Receipt；
- 后续 Evidence query 取得最小 Digest hint，并通过 exact canonical resolver
  取得新 ToolReceipt 后才能形成当前 claim；
- 在此 checkpoint 后重启后端：Profile、Care、Episode、Digest 本体仍可恢复，但
  synthetic Digest read 必须先 fail closed；人工重新执行显式短期 enable 命令并取得
  新 lease/Receipt 后，才可继续读取 Digest。`JourneyRun` 不得偷偷恢复旧 lease。

### Checkpoint 15 — established and doctor material

- 第十五个有效夜使目标 metric 进入 simulation-only established；
- Evidence 可以形成被 readiness receipt 支持的 15-night longitudinal summary
  和 established baseline artifact，仍保留局限；
- checkpoint 15 禁止形成“当前第 15 夜相对该 15 夜 baseline”的比较，因为目标夜
  不能进入自己的参考窗口。任何 current-vs-established-baseline claim 都必须使用
  更早、完整且与 target night 不相交的 established window；本黄金旅程不要求
  该额外第 16 夜比较；
- 老人请求 doctor material；
- runtime 强制 SafetyReview，批准只绑定 exact target/hash/revision/policy/expiry；
- SleepCare 只能在 accepted semantics 内渲染；
- elder/family/doctor 三个 role view 来自同一 NightEpisode revision，并服从各自
  authorization；本任务不发送、分享或导出给真实外部系统。

低质量、急症、forget/withdraw 等破坏性场景使用隔离 workspace/namespace 副本，
不修改主黄金旅程。

## Module acceptance suite

### 1. `journey`

- deterministic 自动全程回归；
- live 必须逐步手动 `answer/confirm/feedback`；
- live 不读取 `actions.jsonl` 自动代答；
- 覆盖全部黄金 checkpoint、三角色 view、restart 和 provider receipts。

### 2. `agents`

| Case | Required path |
|---|---|
| `knowledge_only` | SleepCare only |
| `normal_morning` | SleepCare → Evidence → SleepCare |
| `care_candidate` | SleepCare → Evidence → Care → waiting confirmation |
| `doctor_material` | necessary Evidence/Care → SleepCare draft → Safety → publish |
| `safety_revision` | deterministic target drift/revise → responsible Agent → re-review |
| `urgent_text` | deterministic preemption, zero model calls |

验证未触发角色确实没有 invocation，而不只是“最终输出里没显示”。

### 3. `habit`

1. skip/unknown 不阻塞晨间功能、不写 Profile；
2. elder 当前回答可当前使用，exact confirmation 后才持久化；
3. family observation 永远保留 observer provenance，不能变成 elder self-report；
4. `habit.device_position_last_night` 等 episode-only 值永不进入长期画像；
5. 自动合同测试覆盖当前实现的 22 concepts；完整冻结 v1 中另外 16 个未实现
   concepts 明确不在本任务补齐；旧 10-concept v18 证据不作为权威。

### 4. `memory`

1. complete terminal result → Manifest + Job → Digest + Receipt；
2. Manifest 不含 raw dialogue、完整 Tool payload、Agent reasoning 或强身份字段；
3. Evidence query → minimal hint → canonical source revalidation；
4. forget/withdraw/kill switch 后旧 handle/slice/publication 失败；
5. urgent/partial/blocked terminal revision 仍有 Job/Receipt，但无 retrievable Digest；
6. restart 后旧 synthetic lease 消失、read 先失败；显式重新 enable 后才恢复；
7. `memory layers` 展示 L0–L4 mapping，不声称生产 Digest gate 已通过。

### 5. `cold-start`

1. 0/1/2/4/15 exact boundary；
2. 同一夜 heart-rate 可用、respiratory 缺失，证明按 metric 独立；
3. 低质量或 stale current window 使当前资格回退；
4. 新的足量 canonical nights 使资格恢复，但不重写历史 baseline artifact；
5. Evidence 超过 ceiling、缺 decision ref 或 metric/cohort 不匹配时被 gate 拒绝。

不单独验证真实设备换绑/Adapter 发布；cohort hash 只作为 cold-start 正确性输入。

### 6. `backend`

1. `normal_night`：canonical observation → complete revision → fast path → Agent
   analysis → three role views → public event；
2. `poor_quality`：缺失/离线 → unknown/data insufficient，绝不 all-clear；
3. `late_report`：deadline 先形成 pending/insufficient revision，迟到 report 追加
   新 revision 和 exact reanalysis，不覆盖旧历史；
4. verifier 从最终 view/receipt 反向追到同一 namespace、subject、revision、
   observation refs 和 data mode。

### 7. `persistence`

- 每个 CLI mutating command 是独立 HTTP request/process；
- file-backed SQLite，不使用 `:memory:`；
- RuntimeDependencyManifest 证明所有 closure authority 共用 exact storage UUID 和
  transaction-capable connection，不接受默认 in-memory fallback；
- server restart 后恢复 Profile、Care、NightEpisode/revision、Product result、
  pending/consumed confirmation、induction Job/Digest 和 audit/event cursor；
- server restart 后 synthetic Digest read lease 不恢复，重新 enable 前 fail closed，
  重新 enable 后才可读取已持久化 Digest；
- 重复同一 idempotency key 返回原结果，改变 body 返回 conflict；
- bridge/HTTP duplicate trigger 恢复同一 ProductInteractionOperation，不重复 LLM；
- 三角色中任一点 hard crash 后，要么整个 AnalysisAttempt 尚不可见并可续跑，要么
  AnalysisRevision + three views + targets + terminal group 全部已提交；无 role orphan；
- restart 不切换 fake/legacy provider 或丢失 workspace generation。

## HTTP and CLI design

### 1. Application surfaces

新增唯一 `create_sleep_backend_app(runtime, enabled_surfaces)` factory。每个进程
只能持有一个 `SleepBackendRuntime`，且只有该 runtime 的幂等 lifespan
`start()/stop()` 可以启动/停止 migrations、Sleep API operation worker、fast-path
worker、Agent worker 和 induction scheduler。`backend.main:app`、standalone
`sleep_api.app:app` 和 demo server 都只是选择 surfaces/dependencies 的薄 entrypoint，
不得各自创建第二套 global store/runner/worker。`sleepagent-demo serve` 只启动
一次 development/replay composition app，并用 workspace lock 拒绝同一 workspace
的第二个 server owner。

app routers 不捕获永久 runtime 对象，而是每次 request 从单一 `RuntimeSlot` 获取
active generation。slot 提供共享 request lease 与 exclusive replacement fence；这
只是为 demo reset 服务，不允许一个进程同时 active 两个 runtime。正常启动、重启和
reset 后的 dependency manifest/storage UUID 都由 slot 校验。

该 app 复用正式 runtime 与 routers：

1. `/api/v1/*`：现有 versioned sleep-domain views、operations、feedback、
   reanalysis、events；
2. `/product/sleep/interactions/*`：统一 SleepAgent 的 start/ask/status/answer/
   confirm/feedback 产品交互，不以旧 radar task/Agent ID 作为公共合同；
3. `/demo/v1/*`：仅用于 seed、advance、scenario clock、workspace reset 和 developer
   trace/resource collection 的控制面；oracle 判定始终在 CLI/verifier 侧。

`/demo/v1` 只有在 `deployment_mode in {development,test}`、显式 demo flag、
`replay:*` namespace 三者同时满足时安装。production app 和 OpenAPI 均不得出现
该 router。

普通产品响应不暴露内部 Agent ID、Prompt 或 Tool payload。developer trace 是
受限 demo endpoint，仍过滤 secret、raw payload、完整报告 body 和持久 ID；
它可显示 Agent/Tool/Receipt 的安全摘要用于演示。

`/demo/v1` 使用与 elder/family/doctor 完全不同的 demo-controller service principal
和 `demo:seed|advance|trace|reset|clock` scopes，所有调用写 control-time audit。actor
credential 不能访问 control/trace，controller credential 也不能冒充 actor 调用
answer/confirm 或读取未授权 role view。demo server 默认只绑定 loopback；显式非
loopback 绑定在本任务 fail closed。trace 查询还必须绑定 exact workspace generation/
JourneyRun，并继续通过 subject/role redaction，不提供跨 workspace 列表。

### 2. Authentication

CLI 是真实 HTTP client，不在进程内调用 Runner。workspace 初始化创建 synthetic
service credential、actor signing key 和 elder/family/doctor authoritative binding；
CLI 按现有 Sleep API 合同签名并带 idempotency key。浏览器/body 不能自报 role、
subject 或 scope。local credential 文件权限限制为 owner-only，reset 不输出 secret。

`JourneyRun` 只负责演示导航，绝不构成 answer/confirmation 授权。waiting target
持久化后，server 为当前 authenticated actor 签发 opaque、随机、不可枚举的单次
handle；server-side 记录固定绑定 namespace generation、actor/role、subject、purpose/
action scope、target kind/ID/version/hash、FactSnapshot hash、相关 Profile/Care state
version、authorization epoch、control-clock expiry 和 consumed/declined 状态。CLI 只把
opaque handle 与用户选择发回，不能提交 model-supplied candidate、subject、scope、
内部 `confirmation_id` 或 token 字段。resume 时重新验证当前 binding/epoch、expiry、
exact frozen target/state version 和 single-use CAS；任一漂移 fail closed，要求重新
plan，不把旧确认套到新输出。decline 与 answer handle 服从相同的 actor、expiry、
single-use 和审计规则。日志/pretty output 可显示短 label，不能泄露 handle 全值。

### 3. CLI commands

新增 console script `sleepagent-demo`，至少支持：

```text
sleepagent-demo serve --workspace <id> --env-file <path>
sleepagent-demo preflight --model deterministic|live [--env-file <path>]
sleepagent-demo list
sleepagent-demo reset <workspace> --yes
sleepagent-demo seed <scenario>
sleepagent-demo journey start <scenario> --model deterministic|live
sleepagent-demo journey advance <journey-run-id> --to <checkpoint>
sleepagent-demo ask <journey-run-id> --actor elder "..."
sleepagent-demo answer <journey-run-id> <request-id> --value <json-or-option>
sleepagent-demo confirm <journey-run-id> <confirmation-id>
sleepagent-demo decline <journey-run-id> <confirmation-id>
sleepagent-demo feedback <night-or-care-id> --text "..."
sleepagent-demo forget <memory-or-profile-id> --yes
sleepagent-demo show <resource-id> --stage canonical|readiness|agents|memory|all
sleepagent-demo memory enable-synthetic <journey-run-id> --ttl-seconds <bounded>
sleepagent-demo memory layers
sleepagent-demo verify <journey-run-id>
sleepagent-demo verify agents|habit|memory|cold-start|backend|persistence|all \
  --model deterministic|live
```

上表中的 `<request-id>`/`<confirmation-id>` 是 CLI 展示 label；HTTP client 实际提交
server-issued opaque handle，用户或 scenario 文件不能自行构造内部 target ID。为
满足“每条命令是独立进程”且不把 secret 打印到终端，CLI 用 actor credential 和
label 即时领取/轮换当前 target 的 handle，在同一进程完成 POST，handle 不进入本地
session cache、shell history 或 pretty/JSON 输出。

`journey start` 持久化 strict `JourneyRun` aggregate：journey-run ID、workspace
generation、scenario ID/version/hash、opaque verification-contract hash、model mode、
current checkpoint、pending request/confirmation handle refs、step receipts、scenario
clock cursor/control recorded-at、status 和 timestamps。每个后续命令
必须绑定该 ID、当前 revision 和 expected next step；跨进程或 restart 从数据库
恢复，不能从本地 CLI 内存猜测。live journey 在 waiting 状态停止并打印下一条
所需命令，不自动消费 answer/confirmation；`verify <journey-run-id>` 只审计已提交
steps，并在尚未完成时返回 typed incomplete/nonzero，不自行推进。deterministic
`verify --all` 可读取 actions fixture 自动完成回归。

所有命令支持 `--format pretty|json|jsonl`，失败返回稳定非零退出码。

### 4. Demo persistence and reset

默认 workspace 位于 gitignored `.sleepagent-demo/<workspace>/`，SQLite、synthetic
credentials、clock 和 non-secret metadata 分开保存。所有数据库记录继续带
`replay:<workspace>:<generation>` namespace，不能与其他 workspace 或 live 数据混合。

`reset` 必须：

- 拒绝空 workspace、`live:*`、路径穿越、symlink 和 workspace root 之外的目标；
- 要求 exact workspace + `--yes` 和有 `demo:reset` 的 controller；
- 通过 `RuntimeSlot` 进入 durable `resetting` exclusive fence，拒绝新 request、等待
  已进入的 HTTP request drain、停止并 join 全部 worker、失效 active leases、flush
  audit，然后关闭旧 SQLite connection；
- 在旧 authority 中先原子递增/revoke generation、authorization/privacy epoch 并
  写 reset receipt，才关闭/归档；旧 runtime 对象永久 invalid，不能再次 start；
- 使旧 actor/service credentials、cursor、idempotency reservation、pending handle、
  synthetic Digest lease 和 in-flight publication intent 全部不能作用于新 generation；
- 将旧 workspace generation 移入可恢复 archive，而不是 broad recursive delete；
- 为新 generation 轮换 signing key/credentials 并保持 owner-only；archive 默认
  offline，显式 restore 也必须签发新 generation/credentials，不能复活旧 handle；
- 构造、preflight 并原子安装全新的 runtime/connection/dependency manifest 后才
  解除 fence；任一步失败则保持 fenced recovery state，不回退到半归档旧 runtime；
- 输出移除范围和恢复路径；
- 绝不读取或删除 `.env.deepseek.local`。

## Target source mapping

实现时优先复用现有代码，目标映射为：

1. 新 `sleepagent/backend_runtime.py`
   - production-shaped composition root、worker lifecycle、dependency injection、
     dual-clock providers、RuntimeDependencyManifest、single SQLite Unit of Work、
     durable ProductInteractionOperation/publication/outbox ownership。
2. 新 `sleepagent/sleep_domain/readiness.py`
   - canonical history → existing cold-start functions → frozen decisions。
3. 新 `sleepagent/sleep_domain/product_tools.py`
   - repository-backed Product Tool handlers 和 canonical source resolvers。
4. 修改 `sleepagent/sleep_domain/agent_bridge.py`
   - 只消费 server-owned AnalysisRequestKey；使用 operation-derived AnalysisAttempt，
     不按 history 长度编号，不逐角色提交可见结果。
5. 修改 `sleepagent/sleep_domain/fast_path.py`、`episodes.py`
   - 只补 committed domain event/outbox 接线，不允许 in-memory callback 作为
     lifecycle→fast-path→Agent 的权威触发；fast path 是 AnalysisRequestKey 唯一
     producer，不复制质量/生命周期算法。
6. 修改 `sleepagent/sleep_api/runtime.py`、`service.py`、`app.py`
   - 使用统一 backend runtime，feedback/reanalysis 后继续 fast/slow path。
7. 修改 `backend/main.py`
   - 复用同一 composition；统一产品路由只 enqueue operation，不在 handler 直接
     `runner.run()`；旧 radar chat 明确隔离为 legacy surface。
8. 新 `sleepagent/simulation/`
   - strict recipe contracts、generator、catalog、dev router、CLI HTTP client、
     client-side verifier、deterministic model adapter、replay capability registry；
     不得含业务判断，server 不得读取 action/oracle。
9. 修改 Product LLM config/runtime factory
   - canonical/legacy env alias resolution、DeepSeek live preflight、atomic
     generation-result metadata、durable invocation journal、per-attempt request-ID
     gate；移除 mutable last-ID provenance side channel 和 provider 内部隐藏重试。
10. 修改 `sleepagent/radar_agent/product_agent/runner.py`、
    `product_persistence.py`、`governance.py` 与
    `sleepagent/radar_agent/persistence/store.py`
    - 把 Runner 拆为 side-effect-controlled prepare/commit；增加 cursor-aware Unit of
      Work、AnalysisAttempt/terminal group、publication journal/outbox 和唯一 group
      induction job；删除新 composition 中 publish-before-store 的 crash window。
11. 修改 longitudinal memory governance
    - 增加 production-owned access-policy protocol/default release implementation；
      `synthetic_nonrelease` 类型与构造器只放 simulation package，production gate
      和持久化 enablement state 不变。
12. 修改 `pyproject.toml`
    - 注册 `sleepagent-demo` 及 fixture package data。
13. 新增 focused tests 和 scenario fixtures；旧 replay tests 保留为 legacy regression，
    不改 golden 以伪装新闭环。

具体文件名可在不改变上述责任边界的前提下微调；不得把 composition、readiness、
canonical tools 或 Memory gate 放进 CLI 形成第二套实现。

## Approach

1. **Freeze contracts and failure-first tests**
   - 新增 strict simulation recipe/action/oracle contracts；
   - 固定 seven-suite IDs、workspace/data-mode/model-mode、RunScope/ForkReceipt、
     AnalysisRequestKey/AnalysisAttempt 合同；
   - 先写失败测试证明当前 normal canonical observation 无法自动到 role view、
     readiness 永远 unavailable、CLI 不存在、Digest production gate 冲突；
   - 捕获 RuntimeDependencyManifest，证明当前默认 Runner/in-memory store 和直接
     HTTP invocation 不满足 closure；
   - 检查 dirty worktree，只改本计划授权文件，保留用户现有修改。
2. **Build one thin deterministic normal-night vertical slice**
   - 建 composition root；
   - 注入并区分 ScenarioClock/ControlClock；
   - canonical generator 产生一个 good night；
   - 接 lifecycle → fast path-owned AnalysisRequestKey → bridge/worker → deterministic
     Product runner prepare → atomic AnalysisAttempt/three role views；
   - prepared result/terminal bundle/publication intent 先原子提交，再由 outbox
     delivery；用故障注入证明 crash recovery；
   - 通过 HTTP 查询并反向验证 lineage；
   - 任何 shortcut/passthrough 先移除再扩场景。
3. **Wire canonical evidence and cold start**
   - repository-backed Product tools/source resolver；
   - canonical metric-night derivation；
   - simulation policy 0/1/2/4/15；
   - decisions 固定进 FactSnapshot，Evidence/publication gate 拒绝超 ceiling。
4. **Add durable interaction/HITL path**
   - versioned product interaction API；
   - waiting_user/waiting_confirmation checkpoint 持久化；
   - server-issued opaque single-use handle 与 exact answer/confirmation/decline/
     feedback resume；
   - Habit/Profile/Care version、CAS 和 idempotency 通过独立 HTTP command 验证。
5. **Add induction and replay-only Digest functional mode**
   - terminal bundle → scheduler → Receipt；
   - synthetic non-release read enablement；
   - canonical source revalidation；
   - forget/withdraw/kill-switch invalidation；
   - urgent/partial/blocked exclude；
   - restart 后旧 lease fail closed，再显式 re-enable。
6. **Add CLI and workspace lifecycle**
   - HTTP-only client、actor/controller credential separation、pretty/json/jsonl；
   - serve/preflight/seed/advance/ask/answer/confirm/show/verify/reset；
   - file-backed SQLite、restart、RuntimeSlot drain/close/rebuild、generation-fenced/
     credential-rotating recoverable scoped reset。
7. **Implement the seven deterministic suites**
   - 共用 one generator + overlays；
   - expected/actions 留在 CLI verifier，server seed 只接收 sanitized world input；
   - `verify --all` 非零失败；
   - 每个核心模块至少一对从同一 RunSnapshotManifest fork 的 isolated
     single-variable counterfactual，输出 ForkReceipt + typed delta receipt；
   - deterministic scripts 不得进入 live loader/package result。
8. **Enable and prove DeepSeek live mode**
   - 解析 `.env.deepseek.local` 的旧键但不显示/修改 secret；
   - 四 schema probes；
   - 手动运行黄金旅程；
   - 普通、Care、doctor/Safety 覆盖四角色；
   - 审计 atomic accepted invocation 与每个 provider-response attempt 的 request ID，
     确认零 scripted/fallback/metadata race。
9. **Regression, self-review and handoff**
   - focused tests、full backend pytest、CLI deterministic all、live evidence audit；
   - 检查旧 runtime 未被新 CLI 调用；
   - 检查 production OpenAPI 无 demo routes、production Digest gate 未弱化；
   - 更新 README/demo runbook、scenario catalog/version 和 synthetic evidence
     disclaimer；
   - 不提交、不 push，除非用户之后明确授权。

## Verification and acceptance

### Automated deterministic proof

至少运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_simulation_contracts.py \
  tests/test_backend_runtime_vertical_slice.py \
  tests/test_simulation_api.py \
  tests/test_simulation_cli.py
```

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q \
  tests/test_product_agent_architecture.py \
  tests/test_product_agent_runner.py \
  tests/test_cold_start_policy.py \
  tests/test_sleep_habit_profile.py \
  tests/test_healthclaw_memory_governance.py \
  tests/test_night_episode_lifecycle.py \
  tests/test_sleep_domain_fast_path.py \
  tests/test_sleep_domain_agent_bridge.py \
  tests/test_sleep_api_v1.py
```

```bash
sleepagent-demo verify all --model deterministic --format json
```

最后运行完整 `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q`。若仓库现有用户
修改导致无关失败，必须区分 baseline failure 与本任务回归，不得删除或 reset
用户改动。

### Live DeepSeek proof

live proof 由用户授权网络调用后执行：

```bash
sleepagent-demo preflight --model live --env-file .env.deepseek.local
sleepagent-demo verify agents --model live --format json
sleepagent-demo journey start golden-15-night --model live --format json
# 人工执行 CLI 返回的 advance/ask/answer/confirm/feedback 命令
sleepagent-demo verify <journey-run-id> --format json
```

live verifier 必须输出：

- 每个 Agent 的 invocation count；
- provider/model/request IDs 的 sanitized list，accepted invocation 与每个
  provider-response attempt 均能一一关联；
- scripted/fallback count = 0；
- accepted work products、Safety targets、confirmation/state transitions；
- readiness refs、Profile/Digest source refs；
- final run verdict。

不得把 preflight probe、deterministic run 或历史 v18 simulated acceptance
material 计入 live Agent coverage。

### Static and negative proof

1. 新 CLI dependency graph 不引用旧 `legacy_fixed/dynamic_goal` executor。
2. production app/OpenAPI 没有 `/demo/v1`。
3. production Digest read 仍要求完整 release attestation；synthetic mode 在
   production/live namespace 验证失败。
4. live loader 遇到 deterministic model script 立即失败。
5. expected/actions 无法被 server/runtime/Prompt/Tool import 或读取；server 收不到
   oracle 内容，只有 CLI verifier 判分。
6. logs/traces 不含 API key、raw `.env` value、raw health payload、完整 report body。
7. urgent scenario 的 provider/model invocation count 为零。
8. poor-quality scenario 不产生 normal/all-clear claim。
9. reset 不能作用于 workspace root、live namespace、symlink 或其他 workspace。
10. live 15-night advance 只改变 ScenarioClock，不延长 token、lease、authorization、
    Digest TTL 或 retention；mixed-clock 输入被拒绝。
11. 并发/重试测试不能把 Agent A 的 provider request ID 记录到 Agent B；accepted
    output 与 atomic metadata hash 一致。
12. scenario 不能提交 capability receipt；未知/篡改 replay provider 或任一绑定
    hash 使 readiness/Agent gate fail closed。
13. raw internal confirmation ID、过期/已消费 handle、actor/epoch/state-version 漂移
    均不能 commit；`JourneyRun` ID 单独出现没有授权效力。
14. publication/terminal 故障矩阵证明未提交 view 不可见、unknown delivery 不重跑
    LLM/commit、terminal bundle/induction job 不重复。
15. closure preflight/runtime manifest 中没有必需的 `InMemory*`/`:memory:`/不同
    storage UUID；真实 subprocess hard-kill/restart 后状态与 operation 可恢复。
16. bridge 与统一 product HTTP route 对同一 material 并发提交只产生一个
    ProductInteractionOperation/Episode/Agent invocation set；旧 radar chat 不在依赖图。
17. LLM response 前后故障注入证明 durable accepted attempt 可 resume；indeterminate
    attempt 不自动重调，显式 retry 也最多 CAS 接受一个 output。
18. deterministic module pairs 每对只有一个 state delta；live 报告只声称 context/
    provenance/gate smoke proof，不把随机措辞差异计为因果效果。
19. actor credential 访问 demo control/trace、controller 冒充 actor、非 loopback
    demo bind 均失败；trace 不能跨 workspace/generation。
20. reset 后旧 credential/handle/cursor/lease/idempotency key 全部被新 generation
    拒绝，archive restore 也不能复活旧授权。
21. client idempotency/service principal/HTTP route 不改变同一 revision 的
    AnalysisRequestKey；数据库竞争测试只有一个 operation，reanalysis 的新 revision
    则产生有 parent lineage 的新 key。
22. 在第 1/2/3 个 role member 后 hard-kill，恢复后均无可见 orphan、history-based
    ID drift 或重复 Digest Job，最终三 view 同属一个 committed AnalysisAttempt。
23. reset fault injection 覆盖 drain、worker join、DB close、archive、new-runtime
    preflight/slot swap；失败时保持 fenced，旧 connection/runtime 不可重新服务。
24. A/B 与 deterministic/live 使用不同 RunScope；ForkReceipt 证明 parent snapshot
    一致且只有声明的 intervention delta，跨 arm source/handle/idempotency 全部失败。

## Key decisions & tradeoffs

1. **ProductEpisodeRunner 是唯一 Agent 权威。** 放弃包装旧 replay CLI 的捷径，
   换取与当前 1+2+1 设计一致的证据。
2. **模块微场景 + 同一老人黄金旅程。** 增加少量 fixture 编排，换取模块可定位
   与整体可讲述性。
3. **canonical boundary，不做 raw radar/vendor transport。** 能证明后端域闭环，
   但不宣称即插即用或真实硬件接入。
4. **deterministic + live 双轨。** deterministic 保证回归，DeepSeek live 保证
   Agent 分析不是脚本；两者证据和 verdict 严格分离。
5. **所有 live Agent work product 必须真实调用 DeepSeek。** 接受网络、成本和
   输出波动，禁止用 fallback 掩盖失败。
6. **Habit 锁定当前实现的 22 concepts。** 不把本任务扩张为补齐 38-concept v1，
   也不沿用过期 10-concept 材料。
7. **4/15 仅是 simulation policy。** 展示冷启动演进，但不冒充生产/临床门槛。
8. **Memory 本轮证明功能，不做统计 benchmark。** replay-only Digest mode
   明确不可发布，production gate 不降级。
9. **CLI 是 HTTP client。** 实现成本高于直接 import Runner，但未来前端复用
   同一应用服务，不再重打一条链。
10. **file-backed SQLite 证明跨进程状态。** 不提前承担 PostgreSQL、高并发和
    高可用，却拒绝用 in-memory 状态冒充跨天记忆。
11. **live 手动、deterministic 自动。** 人工 answer/confirm 在演示中可见，CI
    仍能一条命令稳定回归。
12. **七组 suite 是本阶段固定完成口径。** 新想法进入后续 backlog，不在实现
    中无界扩场景。

## Risks / open questions

当前没有需要用户继续决策的产品分叉；以下是实现风险及固定处理方式：

1. **DeepSeek strict JSON Schema 兼容或 request ID 缺失。** 四角色 probe 先行；
   不兼容则 live gate 失败并保留 deterministic 结果，不伪造 live 成功。
2. **LLM 输出波动导致固定 Care/Safety 路径不稳定。** world facts、Prompt、
   catalog 和 Agent schema 固定；自动断言结构与 source refs，不比较全文；仅按
   invocation-journal 规则重试明确安全/已知失败的 attempt，unknown 必须人工决定，
   且禁止切 scripted/fallback。
3. **SQLite worker 并发锁。** 本任务限定单机 demo，使用现有事务、WAL/timeout、
   persistent leases 和 idempotency；压力与多进程稳定性留到下一阶段。
4. **现有全局 runtime/factory 难以注入。** 先提取 composition root，不在测试中
   monkeypatch 第二套生产路径。
5. **canonical history 与旧 dashboard/replay 数据重复。** 新路径只信任
   SleepDomainRepository；旧 JSON 仅用于迁移场景语义。
6. **synthetic Digest mode 被误当成 release 证据。** 类型、namespace、API、
   Receipt、CLI 水印和 release verifier 五层隔离。
7. **15 夜 observation 数量膨胀。** 通过 seed/recipe 生成，不检入展开数据；
   cadence 和 count 仍由 fast-path policy 验证。
8. **仓库已有大量未提交修改和历史 root review log。** 本计划使用任务专属目录；
   实现时不得覆盖、reset 或清理用户现有改动。
9. **现有 provider provenance 使用 mutable last request ID。** 改为 atomic typed
   generation result，并把 retry 提升到 invocation 层；在此完成前 live proof 必须
   fail closed。
10. **15-night fast-forward 与安全 TTL 混淆。** dual-clock schema、dependency
    injection 和负向测试固定时间归属；live 永不允许控制 ControlClock。
11. **publisher 与 terminal store 的现有顺序存在 crash window。** 新 composition
    先 durable commit 再 deliver，保留 legacy 仅作回归，不能作为新闭环证据。
12. **默认 Product runner/store 和部分 authority 是进程内状态。** closure startup
    通过 dependency manifest 拒绝 in-memory authority，并用同一 SQLite Unit of Work
    和 hard-restart proof 验证真正持久化。
13. **旧 HTTP chat 可绕过 persistent operation worker。** 新产品面只能 enqueue，
    Runner 只由 Agent worker 调用；旧入口不删除但在 legacy surface 隔离。
14. **live 单次 A/B 不能证明因果智能效果。** deterministic single-variable pairs
    证明功能 delta；live 只证明真实 context、真实调用、provenance 与 gate。
15. **reset/archive 可能保留仍有效的旧凭据。** generation fence、epoch revoke、
    credential rotation 和 restore-time rekey 防止跨 generation 重放。
16. **现有 publish/store API 内部自行 commit，无法满足计划的原子边界。** 明确将
    Runner、Product persistence/governance 和 RadarPersistenceStore 纳入重构范围，
    以 caller-owned cursor 和 prepare/commit 协议实现；做不到则 closure 不通过。
17. **同一 revision 可由多入口生成不同 operation ID。** 只有 fast-path outbox
    铸造不含 caller idempotency 的 AnalysisRequestKey，其余入口只引用。
18. **三角色顺序提交会留下 orphan result。** operation-derived AnalysisAttempt
    聚合 prepared members，全部完成后一次提交三 view 和唯一 induction job。
19. **hot reset 时 router 仍可能持有旧 runtime/connection。** RuntimeSlot exclusive
    fence 必须 drain、join、close、rebuild、swap；失败保持 fenced。
20. **A/B 或 live/deterministic 串用同一 subject state。** RunScope namespace 与
    ForkReceipt 隔离 Profile/Care/Memory/operation，verifier 拒绝跨臂 lineage。

## Out of scope

1. 毫米波原始波形、IQ 数据、硬件信号处理和传感器精度评测。
2. Perceptor/其他厂商的真实签名、push/pull、重放防护和 Adapter 验证。
3. 即插即用能力的独立验收；只预置一个 synthetic canonical binding/cohort。
4. 真实老人、真实家属、真实医生数据或临床有效性。
5. 38-concept 完整 Habit v1 中尚未实现的 16 concepts。
6. HealthClaw 纵向 memory 的 30+ paired Episode 统计 benchmark、质量非劣效
   结论和 production Digest enablement。
7. PostgreSQL、负载/并发/长稳测试、高可用、备份恢复、生产迁移和运维告警完善。
8. 真实通知、分享、导出、医生发送或其他外部副作用。
9. 前端页面、视觉交互、适老可用性测试和最终完整视频演示。
10. 项目级量化评测、论文结论、发布清单与最终收尾。
11. 重写旧 Radar runtime、删除 legacy 代码或迁移历史数据。
12. commit、push、PR、部署或 release；均需用户后续单独授权。
