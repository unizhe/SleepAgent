# SleepAgent 整改优化与产品闭环实施方案

> 适用范围：当前 SleepAgent 已具备 Agent 架构、Habit/Memory、模拟数据、PostgreSQL 后端、终端 CLI 与真实毫米波雷达云云接入。本方案不重做已有系统，而是在现有可靠能力之上完成数据语义统一、报告主链收口、设备自动化运行、真实照护结果闭环和架构依赖治理。

---

## 1. 总体判断

当前最合理的方向不是继续增加 Agent、切换 Agent 框架、替换 PostgreSQL 队列或拆成微服务，而是把系统收敛为：

```text
真实设备数据
→ 统一的领域事实与来源治理
→ 夜晚最终化
→ 确定性质量/风险门控
→ 有界 Agent 推理
→ 单一 Shared Analysis
→ 分角色确定性报告
→ HITL 授权
→ 真实邮件送达与回执
→ 家属/医生确认
→ 照护行动完成
→ 下一夜结果评估
→ Habit/Memory 与评估指标更新
```

本轮改造应坚持四条总原则：

1. **先修事实语义，再扩展自动化和外部动作。** 体动、时区和来源校验不统一时，自动调度只会更稳定地生产错误结果。
2. **先建立唯一事实源，再退役兼容链。** SharedNightAnalysis 应成为所有角色报告和后续照护动作的唯一语义来源。
3. **所有外部副作用都由确定性工作流控制。** LLM 只能提出候选建议，不能自行选择联系人、发送邮件或确认动作完成。
4. **架构迁移必须增量完成。** 不进行一次性目录重写，不在同一个提交中同时修改行为、迁移数据库和大规模移动模块。

---

## 2. 本轮关键架构决策

| 编号 | 决策 |
|---|---|
| D1 | 将“体动指数”和“体动次数”建模为两种明确指标，禁止用一个通用 `movement` 数值混合聚合。 |
| D2 | Push、Pull、Replay 必须经过同一个 canonical observation 构造与语义校验边界。 |
| D3 | UTC 为权威时间；本地日期和展示时间由确定性代码根据 IANA 时区转换。 |
| D4 | `SharedNightAnalysis + RoleProjection` 成为唯一报告事实源；旧 per-role Agent 报告链只保留历史只读兼容。 |
| D5 | 报告语义和本地化展示分离；当前正式支持 `zh-CN`，不再直接把英文 claim statement 拼入中文报告。 |
| D6 | 夜晚“日期归属”和“数据是否最终化”分开建模，新增独立的 Night Finalization 状态机。 |
| D7 | 第一个真实渠道选择**邮件**。邮件比短信更易完成低成本演示、结构化内容和确认链接；但必须区分“服务商接受”和“真实送达”。 |
| D8 | 复用现有 PostgreSQL durable operation、lease/fence/retry、HITL 与 delivery intent 基础，不切换 Kafka、Celery、Temporal 或 LangGraph。 |
| D9 | 先拆关键循环依赖，再新增 Scheduler 和 Delivery 模块，避免新功能继续依赖错误层级。 |
| D10 | 个性化状态更新仍走现有 Habit/Memory 治理，不新增一个可任意写长期记忆的 MemoryAgent。 |

---

## 3. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│                    Perceptor Push / Pull                      │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ CanonicalObservationFactory                                  │
│ - schema/version validation                                  │
│ - ontology/metric validation                                 │
│ - provenance normalization                                   │
│ - UTC normalization                                           │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Durable Ingestion + DeviceBinding + Acquisition Scheduler    │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Night Finalization                                           │
│ OPEN → SOFT_FINALIZED → HARD_FINALIZED                        │
│ late data → new immutable revision → reanalysis              │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Deterministic Quality / Risk Gate                            │
│ urgent/unusable → zero-model fast path                       │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Shared Analysis Workflow                                     │
│ direct facts builder                                         │
│ + conditional EvidenceReasoning                              │
│ + policy-routed CareStrategy                                 │
│ + conditional SafetyReview                                   │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ SharedNightAnalysis.v2                                       │
│ → ElderProjection.zh-CN                                      │
│ → FamilyProjection.zh-CN                                     │
│ → DoctorProjection.zh-CN                                     │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Care Action + HITL                                           │
│ proposal → approval/rejection/revocation                     │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Email Delivery                                               │
│ intent → attempt → provider accepted → delivered/bounced/... │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Recipient Acknowledgement + Care Execution                   │
│ acknowledged/declined → in progress → completed/expired      │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Outcome Evaluation                                           │
│ next hard-finalized night + subjective feedback              │
│ → PersonalizationEffectReceipt                               │
│ → governed Habit/Memory update                               │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 改造优先级总表

| 阶段 | 优先级 | 核心结果 | 为什么必须按此顺序 |
|---|---:|---|---|
| P0 | 最高 | 基线、特征开关、回归夹具和架构约束 | 没有可比较基线就无法安全退役旧链，也无法证明没有行为回退。 |
| P1 | 最高 | 体动语义拆分；真实链/回放链统一校验 | 后续报告、最终化和结果评估都依赖正确且一致的事实。 |
| P2 | 最高 | UTC/本地时间统一；中文确定性渲染 | 在报告链收口前先确定新的报告合同，避免迁移两次。 |
| P3 | 高 | Shared Analysis 成为唯一报告主链；旧链停止新增 | 消除最大的架构冗余和潜在结果分叉。 |
| P4 | 高 | 拆除关键层级倒置与循环依赖 | 为 Scheduler、Delivery、Outcome 新模块建立正确依赖方向。 |
| P5 | 高 | DeviceBinding、持久化调度、夜晚最终化自动运行 | 将“设备已接入”升级为无需人工触发的稳定采集闭环。 |
| P6 | 高 | 邮件送达、回执、确认、行动完成、下一夜评估 | 补齐项目当前最重要的现实世界结果闭环。 |
| P7 | 收口 | 历史兼容退役、全链路故障测试、运行指标 | 证明新系统可长期运行，而不是只在 happy path 演示。 |

---

# 5. 分阶段实施方案

## P0：建立不可回退的改造基线

### 5.0.1 目标

在改变现有行为前，先固定当前主链、数据合同和 operation 行为，并建立后续迁移所需的观察点。

### 5.0.2 实施内容

1. 新增 ADR：
   - `ADR-Observation-Semantics-v2`
   - `ADR-Reporting-Pipeline-v2`
   - `ADR-Night-Finalization`
   - `ADR-Care-Delivery-and-Outcome`
   - `ADR-Layering-and-Composition-Roots`
2. 新增特征开关，默认仍保持当前行为：
   ```text
   observation_semantics_version = v1 | v2
   report_pipeline_mode = legacy | shadow | shared_only
   emit_legacy_report_compatibility = true | false
   acquisition_scheduler_enabled = true | false
   live_delivery_enabled = true | false
   ```
3. 建立可重复的测试夹具：
   - 同一 `body_shake` 历史样本和 SleepReport hourly count。
   - 同一 observation 分别经 Push、Pull、Replay。
   - UTC 事件在 `Asia/Shanghai` 和 `America/Los_Angeles` 下的本地日期。
   - 英文 Agent claim 进入中文角色报告的当前行为。
   - 当前 report operation DAG 与 compatibility operation 生成情况。
4. 记录旧报告链消费者：
   - API read model。
   - CLI。
   - Demo。
   - SQL/view/function。
   - 测试夹具。
   - 任何依赖 legacy operation/result ID 的代码。
5. 新增静态架构检查脚本，但 P0 只禁止继续新增以下反向依赖：
   ```text
   domain -> workers
   domain -> runtime.tools
   workers.kernel -> concrete handlers
   process -> app
   ```

### 5.0.3 验收标准

- 当前完整测试基线有固定结果。
- 能从数据库或 trace 中区分 legacy 与 shared report operation。
- 特征开关具备强类型配置和 fail-closed 校验。
- 新增代码不能再引入上述四种错误依赖。
- P0 不改变生产报告内容、调度方式或外部副作用。

### 5.0.4 禁止事项

- 不在 P0 删除旧链。
- 不在 P0 大规模移动文件。
- 不通过修改测试期望掩盖现有差异。
- 不修改已经应用的历史 migration。

---

## P1：统一 Observation 语义——拆分两种体动并统一三条输入链

这是整个整改的第一项实质性改造。

### 5.1.1 当前问题

当前 `MovementPayload` 同时允许 `count/index/event`，但默认单位为 `index`。Perceptor 历史数据中的 `body_shake` 与 SleepReport 中 `body_shake_data[].count` 都可能进入同一种 `movement` 类型，后续又可能在 `deterministic_night_summary` 中被统一求平均。

同时，Replay 明确调用 ontology 校验，真实 Push/Pull 的所有构造路径却没有统一经过同一个语义校验边界。

### 5.1.2 目标领域模型

建议引入 `movement_payload.v2`：

```python
class MovementMetricId(str, Enum):
    MOVEMENT_INDEX = "movement_index"
    MOVEMENT_EVENT_COUNT = "movement_event_count"
    LEGACY_AMBIGUOUS = "legacy_ambiguous_movement"

class MovementPayloadV2(...):
    schema_version: Literal["movement_payload.v2"]
    observation_type: Literal[ObservationType.MOVEMENT]
    metric_id: MovementMetricId
    value: float
    unit: Literal["vendor_index", "count"]
    aggregation_start_at: datetime | None
    aggregation_end_at: datetime | None
    vendor_semantic_code: str | None
```

强制不变量：

```text
movement_index
- unit 必须为 vendor_index
- value 可以是浮点数
- 不能与 count 做均值或趋势比较

movement_event_count
- unit 必须为 count
- value 必须是非负整数语义
- 必须有明确聚合窗口或明确事件时间语义

legacy_ambiguous_movement
- 只能用于旧数据读取和审计
- 不进入趋势、异常或照护建议计算
```

### 5.1.3 Perceptor 映射规则

| 厂商字段 | 目标指标 | 规则 |
|---|---|---|
| 实时/历史 `body_shake` | `movement_index` | 单位为 `vendor_index`，保留厂商定义和 normalizer version。 |
| SleepReport `body_shake_data[{hour,count}]` | `movement_event_count` | 单位 `count`；由 hour 与本地时区构造聚合窗口。 |
| SleepReport `body_shake_data[{time_long,value}]` | 仅在厂商文档能证明语义时映射 | 无法证明时标记为 `legacy/vendor_ambiguous`，禁止猜测。 |
| `sum_body_shake_times` | 睡眠报告摘要指标 | 保持为整夜 summary metric，不与逐点 index 混合。 |

### 5.1.4 建立唯一 canonical 构造边界

不要分别在 Push、Pull、Replay 中复制校验逻辑。新增：

```text
application/observations/canonical_factory.py
```

建议接口：

```python
class CanonicalObservationFactory:
    def build(
        self,
        *,
        raw_candidate: AdapterObservationCandidate,
        context: ObservationNormalizationContext,
    ) -> CanonicalObservationResult:
        ...
```

该边界统一完成：

1. payload schema/version 校验。
2. metric/ontology 校验。
3. source kind 与 metric 是否兼容。
4. provider/provenance 一致性校验。
5. UTC 时间归一化。
6. 缺失态和质量标记。
7. `ontology_version`、`normalizer_version` 写入处理步骤。
8. 生成 canonical identity/idempotency material。

Push、Pull、Replay 都只能把原始数据交给 Factory，不再直接写 canonical observation。

Replay 可以保留入口处的早期报错，但最终接受/拒绝结果必须由同一个 Factory 决定。

### 5.1.5 包结构调整

为避免 `contracts.py ↔ ontology.py` 新循环，建议逐步拆为：

```text
sleepagent/domain/observations/
  types.py          # 枚举、基础值对象，不依赖上层
  contracts.py      # payload/candidate/canonical contracts
  semantics.py      # metric registry 和验证规则

sleepagent/application/observations/
  canonical_factory.py
  legacy_upcaster.py
```

### 5.1.6 数据迁移策略

新增 additive migration，例如：

```text
014_observation_semantics_v2.sql
```

建议为 observation/candidate 添加可查询字段：

```text
metric_id
canonical_unit
aggregation_start_at
aggregation_end_at
ontology_version
normalizer_version
semantic_status
```

迁移规则：

- 新字段先允许为空，不修改旧 JSON。
- 只对来源和路径足以证明语义的旧数据回填。
- 无法证明的旧 movement 标记为 `legacy_ambiguous_movement`。
- 新协议写入必须包含 v2 语义字段。
- 不要为了“数据看起来完整”而把未知旧数据默认转换为 index。

### 5.1.7 下游修改

`deterministic_night_summary` 等聚合逻辑改为按以下键分组：

```text
metric_id + canonical_unit + aggregation_window_kind
```

输出示例：

```text
movement_index_mean
movement_event_count_total
movement_event_count_hourly_max
```

严禁再输出一个含义不明的 `average_movement`。

### 5.1.8 测试矩阵

| 测试 | 期望 |
|---|---|
| 历史 `body_shake=39.9` | canonical metric 为 `movement_index/vendor_index`。 |
| hourly `{hour: 2, count: 12}` | canonical metric 为 `movement_event_count/count`，窗口明确。 |
| index 与 count 同夜出现 | 分别统计，绝不混合求平均。 |
| 同一合法样本经 Push/Pull/Replay | 接受结果、metric_id、unit 和时间一致。 |
| 同一非法样本经 Push/Pull/Replay | 三条路径均拒绝，并产生一致错误码。 |
| 无法证明语义的旧数据 | 可读取、可审计，但不进入推理和趋势。 |
| v1 历史报告读取 | 通过 upcaster 保持可读，不伪造 v2 精度。 |

### 5.1.9 完成定义

- 生产代码不存在把 count 和 index 放进同一个数值数组求均值的路径。
- 三条输入链只有一个 authoritative semantic validator。
- 每条新 observation 都能回答“这是什么指标、什么单位、什么窗口、由哪个版本规则解释”。
- 所有受影响的报告、趋势和照护规则只消费兼容指标。

---

## P2：统一时区与报告语言——从字符串拼接升级为结构化事实渲染

### 5.2.1 当前问题

系统已经保存 `timezone_name`，但部分报告字段直接对时间戳调用 `strftime`，没有先执行 `ZoneInfo(timezone_name)` 转换，存在将 UTC 时间标记成“本地时间”的风险。

报告侧则将 `EvidenceClaim.statement` 直接放入 `SharedNightAnalysis.summary_lines`，随后在中文标题下原样复制，导致中英文混杂。

### 5.2.2 时间模型

UTC 继续作为唯一权威时间。所有对外事实同时允许包含确定性派生字段：

```json
{
  "occurred_at_utc": "2026-08-25T17:35:00Z",
  "occurred_at_local": "2026-08-26T01:35:00+08:00",
  "local_date": "2026-08-26",
  "local_time": "01:35",
  "timezone_name": "Asia/Shanghai"
}
```

规则：

1. 所有本地转换统一使用 `zoneinfo.ZoneInfo`。
2. local date 必须由该事件在指定时区下计算，不能从 UTC date 截断得到。
3. NightEpisode 的 wake date、sleep window 和离床事件都绑定同一 timezone version。
4. 时区变更不回写历史事件；新 revision 使用新的 binding version。
5. LLM 永远不负责时间换算。

### 5.2.3 新增报告上下文

```python
class ReportingContextV1(...):
    timezone_name: str
    local_sleep_date: date
    output_locale: Literal["zh-CN"]
    audience_style_version: str
```

将其纳入：

- Shared analysis source。
- report desired hash。
- role projection hash。
- elder narrative 请求。
- report trace。

### 5.2.4 报告语义与展示分离

不再以 `summary_lines: tuple[str, ...]` 作为主要事实接口。Shared Analysis 保存结构化事实：

```json
{
  "claim_id": "...",
  "claim_kind": "direct_metric",
  "metric_id": "heart_rate_mean",
  "value": 63.2,
  "unit": "bpm",
  "window": "sleep_window",
  "authority": "device_measured",
  "source_refs": ["..."],
  "quality_qualifier": "partial_coverage"
}
```

新增确定性渲染器：

```text
sleepagent/presentation/zh_cn/elder.py
sleepagent/presentation/zh_cn/family.py
sleepagent/presentation/zh_cn/doctor.py
```

职责划分：

- Shared Analysis：保存语义和证据，不承担具体中文语气。
- Elder renderer：敬语、短句、低信息密度，不输出工程字段。
- Family renderer：睡眠总体、异常、可执行照护动作和确认入口。
- Doctor renderer：完整指标、数据质量、来源、趋势和不确定性。

### 5.2.5 LLM 推断文本处理

直接测量事实由确定性模板渲染。确需 LLM 生成的推断性文字：

1. 请求中必须显式包含 `output_locale=zh-CN`。
2. 输出采用结构化 claim，而不是自由报告全文。
3. 增加语言验证；不满足时进行有界重试或退化为确定性表达。
4. 英文单位缩写、协议名和医学缩写采用白名单，不做简单“禁止 ASCII”。
5. 语义 hash 与本地化 projection hash 分开，防止更换 renderer 后误认为分析事实发生变化。

### 5.2.6 契约版本

建议新增：

```text
SharedAnalysisSourceV2
SharedNightAnalysisV2
RoleProjectionV2
```

`RoleProjectionV2` 至少显式包含：

```text
locale
timezone_name
renderer_version
shared_analysis_sha256
projection_sha256
```

### 5.2.7 测试矩阵

- `17:35Z` 在 `Asia/Shanghai` 显示为次日 `01:35`。
- 跨午夜的 sleep window 对应正确 local wake date。
- `America/Los_Angeles` 夏令时切换和重复小时测试。
- 角色报告不出现非预期英文自然语言句子。
- `bpm`、`REM` 等白名单单位/缩写可以保留。
- locale 或 renderer version 改变时 projection hash 改变，但 semantic analysis hash 不变。
- elder/family/doctor 三份报告引用同一 Shared Analysis hash。

### 5.2.8 完成定义

- 代码中不存在把 UTC `datetime` 直接命名为 `local_time` 的路径。
- 中文报告不再直接复制英文 claim statement。
- 同一语义分析可以稳定生成三种中文角色视图。
- 所有展示时间都能追溯到 UTC、时区和转换版本。

---

## P3：收口报告主链——退役新旧两套报告执行架构并存

### 5.3.1 当前问题

当前仓库同时保留：

```text
旧链：ProductAgentProcessor.prepare
      → elder/family/doctor 分角色 Agent Run

新链：ProductAgentProcessor.prepare_shared
      → 一次 SharedNightAnalysis
      → 三份确定性 RoleProjection
```

同时还存在 `product_agent_compatibility` operation、兼容完成逻辑和相关 read model。需要注意：兼容 operation 不一定在每个自动夜晚都真正触发三次模型调用，但**可执行旧路径、兼容 operation、旧结果合同和新结果合同同时存在**，已经形成长期维护和行为分叉风险。

### 5.3.2 唯一权威链

目标 operation DAG：

```text
quality/risk gate
→ product.report.run.v2
→ product.shared_analysis.v2
→ role.projection.v2 × 3（确定性）
→ optional elder.narrative.v2
```

约束：

- 每个 NightEpisode revision + personalization pin + runtime manifest + reporting context 只能有一个 desired report identity。
- 三个角色 projection 必须绑定同一个 shared analysis hash。
- elder narrative 只是可选展示增强，不是新的事实源。
- 后续 CareAction 只能引用 shared analysis/care candidate hash，不能引用 legacy role run。

### 5.3.3 迁移顺序

#### 第一步：消费者清点

建立 `LegacyReportConsumerInventory`，逐项确认：

- API `/today` 或其他 report API。
- Demo API。
- CLI `show/report/demo`。
- 数据库 view/function。
- 测试和 fixtures。
- compatibility operation/result ID 的调用者。

每个消费者标记：

```text
legacy_only / dual_read / shared_ready / retired
```

#### 第二步：Shadow 比较

`report_pipeline_mode=shadow` 时：

- shared v2 为候选链。
- legacy 可以运行用于结果差异审计。
- **shadow 结果不能触发邮件、HITL 或任何外部副作用。**
- 对比指标应为结构化事实、风险等级、care candidate 和来源集合，不做纯文本相似度决定。

#### 第三步：读路径迁移

依次迁移：

```text
内部 read model
→ CLI
→ Demo
→ Product API
```

使用新 RoleProjectionV2；历史 v1 报告通过只读 upcaster 展示。

#### 第四步：切为 shared-only

设置：

```text
report_pipeline_mode=shared_only
emit_legacy_report_compatibility=false
```

新 episode 不再创建 legacy compatibility operation。

#### 第五步：退役写路径

确认一段完整回归周期内新建 compatibility 数量为零、旧消费者为零后，删除：

- 旧 `prepare` 分角色路径。
- 只服务旧链的 `PreparedRoleRun` 分支。
- compatibility complete/failure 编排。
- 旧 queue 注册。
- 仅服务旧写路径的 SQL/index。

历史数据读取 adapter 可以继续保留，直到明确的数据保留期结束。

### 5.3.4 数据库与幂等性

建议新增 migration：

```text
015_reporting_pipeline_v2.sql
```

建立或强化：

- v2 desired report identity 唯一约束。
- shared analysis revision 与 role projection 的外键/哈希绑定。
- `superseded_by_revision_id`。
- `pipeline_version`。
- `authoritative` 标记只能由 shared v2 获得。

### 5.3.5 验收标准

- 一个报告请求只产生一个 authoritative SharedNightAnalysis。
- 不再按 elder/family/doctor 分别进行完整 Agent 分析。
- 三个 projection 的事实集合和 shared hash 一致。
- 新 episode 创建的 legacy compatibility operation 为 0。
- 历史 v1 报告仍可查看，但不能成为新 CareAction 来源。
- shadow 或历史链永远不能发送邮件或创建真实外部动作。
- CLI、Demo、API 全部读取同一 v2 read model。

### 5.3.6 回滚策略

- 切换前保留 `legacy` 与 `shadow` 开关。
- shared-only 发生阻断时，可以恢复 legacy 读路径，但不能让两个链同时执行外部副作用。
- 删除旧实现必须晚于写入停止和消费者归零，不能与读路径迁移放在同一个提交。

---

## P4：治理层级倒置和循环依赖

P4 放在报告主链收口之后，是为了先删除一批兼容代码，再移动仍然有效的核心模块；但在 P0 已通过静态规则阻止新增错误依赖。

### 5.4.1 目标分层

```text
sleepagent/domain/
  observations/
  episodes/
  quality/
  risk/
  care/

sleepagent/application/
  contracts/
  ports/
  observations/
  reporting/
  acquisition/
  finalization/
  care_actions/

sleepagent/infrastructure/
  postgres/
  perceptor/
  delivery/
  security_retention/

sleepagent/interfaces/
  api/
  cli/
  workers/

sleepagent/bootstrap/
  api.py
  worker.py
  scheduler.py
```

依赖规则：

```text
domain         → 仅 stdlib / Pydantic / domain 内部
application    → domain + application ports
infrastructure → 实现 application ports
interfaces     → 调用 application services
bootstrap      → 唯一允许组装 concrete implementations 的位置
```

### 5.4.2 具体拆解顺序

#### A. 消除 self-import

先做零行为变化提交，清理以下大文件中“模块导入自身”的机械合并痕迹：

- `runtime/agents.py`
- `workers/retention.py`
- `runtime/contracts.py`
- `runtime/knowledge.py`
- `runtime/registry.py`

#### B. 抽出 Worker Kernel

新增：

```text
workers/kernel.py
```

只保留：

```text
WorkContext
WorkResult
WorkHandler Protocol
Lease/Fence primitives
通用 retry/error types
```

具体 Handler 依赖 kernel；kernel 不得导入任何具体 Handler。

`workers/runtime.py` 只负责：

- claim loop。
- handler dispatch。
- lease/fence 生命周期。
- process shutdown。

具体 handler 注册移入：

```text
bootstrap/worker.py
```

#### C. 拆 composition root

消除：

```text
app.py → process.py → lazy import app.py
```

目标：

```text
bootstrap/api.py       # 组装 FastAPI dependencies
bootstrap/worker.py    # 组装 Worker handlers
bootstrap/scheduler.py # 组装 Scheduler jobs
```

`app.py` 只提供 FastAPI app factory，`process.py` 只保留进程生命周期和基础协议。

#### D. 移走 domain 对 workers 的依赖

`domain/postgres_slice.py` 实际承担大量基础设施职责，不应位于 domain，也不应导入 `workers.retention`。

逐步迁移为：

```text
infrastructure/postgres/sleep_slice.py
infrastructure/security_retention/
```

领域层仅保留：

- NightEpisode 状态规则。
- finalization transition 规则。
- 纯领域值对象。

加密、key coordinator、SQL repository 属于 infrastructure。

#### E. 消除 `domain.product_data ↔ runtime.tools`

将 `TrendRiskSignal`、`ProductRevisionFacts` 等共享事实合同移至：

```text
domain/facts.py
或 application/contracts/reporting.py
```

`runtime.tools` 只能消费合同，不应成为领域类型的所有者。

#### F. 消除 Worker 之间的横向导入

例如 `workers.effects` 不应导入 `workers.demo`。把共享的 Episode reconciliation 逻辑移入 application service 或中立 handler 模块。

### 5.4.3 迁移技巧

- 先复制/移动实现并在旧路径保留 re-export shim。
- 批量迁移 import。
- 通过静态检查确认新代码不再依赖旧路径。
- 最后删除 shim。
- 每个 PR 只完成一种依赖边界，不同时修改业务行为。

### 5.4.4 架构测试

新增 AST/import graph 检查，至少验证：

- domain 不导入 runtime/workers/infrastructure。
- worker kernel 不导入 handler。
- app/process 无 SCC。
- package import graph 不存在已知强连通分量。
- bootstrap 外禁止实例化具体 PostgreSQL/Perceptor/Email adapter。

### 5.4.5 完成定义

- 已知循环依赖全部消失。
- Worker runtime 与 concrete handler 单向依赖。
- domain 不再持有 SQL、key coordinator 或 worker 类型。
- 新增 Scheduler/Email 模块可以通过 application port 接入，而无需反向导入现有大文件。

---

## P5：完成真实设备自动化采集闭环

P5 分为 DeviceBinding、Durable Scheduler 和 Night Finalization 三个子项目。

## P5-A：DeviceBinding 配置与生命周期

### 5.5.1 目标

把目前依赖人工 SQL/配置的绑定过程变成正式 application service，并首先提供管理 CLI。

CLI 比直接构建管理前端更适合作为第一版：实现成本低、易于测试、可直接服务部署和演示，同时业务规则放在 service 中，未来 API 可以复用。

### 5.5.2 建议命令

```bash
sleepagent-device discover --provider perceptor
sleepagent-device bind --provider perceptor --vendor-device-id ... \
  --subject-id ... --timezone Asia/Shanghai --reason ...
sleepagent-device show --binding-id ...
sleepagent-device list --subject-id ...
sleepagent-device rebind --binding-id ... --subject-id ... --reason ...
sleepagent-device end --binding-id ... --effective-at ... --reason ...
sleepagent-device revoke --binding-id ... --reason ...
sleepagent-device validate --binding-id ...
```

### 5.5.3 领域不变量

- 同一 provider/device identity 在同一有效时间只能有一个 active binding。
- 同一内部 device identity 不允许重叠绑定。
- rebind 必须结束旧版本并创建新版本，不能原地覆盖历史。
- timezone 必须是有效 IANA timezone。
- subject 转移必须记录 actor、reason 和 authorization source。
- 所有写入使用 CAS/version。
- 所有变更生成 audit event。

CLI 只能调用 `DeviceBindingService`，不能直接执行 SQL 或实现业务规则。

---

## P5-B：持久化调度器

### 5.5.4 设计原则

不要把业务逻辑塞进 cron。持久化 scheduler 只负责：

```text
扫描 due schedule
→ 幂等创建 durable operation
→ 更新 next_run_at
```

真正的 Pull、finalization 和 report 仍由现有 Worker 执行。

### 5.5.5 建议数据结构

新增 migration，例如：

```text
016_acquisition_scheduler_and_finalization.sql
```

核心表：

```text
acquisition_schedule
- schedule_id
- binding_id / binding_version
- job_kind
- timezone_name
- next_run_at_utc
- cadence_policy_version
- enabled
- lease_generation
- last_success_at
- consecutive_failure_count

schedule_fire
- fire_id
- schedule_id
- due_at
- semantic_idempotency_key
- operation_id
- state

acquisition_checkpoint
- binding_id
- stream_kind
- watermark_at_utc
- provider_cursor
- version
```

### 5.5.6 Job 类型

```text
perceptor.history_overlap_pull
perceptor.sleep_report_pull
night.finalization_scan
```

可选后续增加：

```text
device.health_check
late_data.reconciliation_scan
```

### 5.5.7 幂等键

至少包含：

```text
binding_id
binding_version
job_kind
target_local_date 或 UTC window
cadence_policy_version
normalizer_version
```

### 5.5.8 运行要求

- 多实例运行安全，使用 `SKIP LOCKED`/lease/fence。
- PostgreSQL server time 为 due-time 权威。
- 支持 jitter、指数退避、pause/resume。
- 进程崩溃后能继续认领未完成 fire。
- 不依赖进程内 timer 作为唯一状态。
- Scheduler 不直接调用模型，也不直接生成报告。

---

## P5-C：夜晚最终化状态机

### 5.5.9 为什么单独建模

`NightEpisode` 的“日期归属/episode 生命周期”和“本夜数据是否完整”不是同一概念。建议新增独立的 `NightFinalization` aggregate，而不是继续向现有 episode state 塞入更多含义。

### 5.5.10 状态机

```text
OPEN
  ├─ observed wake / close deadline + grace
  ▼
SOFT_FINALIZED
  ├─ vendor SleepReport reconciled
  ├─ or maximum lateness deadline reached
  ▼
HARD_FINALIZED

任何阶段出现不可解释冲突
  → RECONCILIATION_REQUIRED

HARD_FINALIZED 后出现迟到数据
  → 原 revision 保持不可变并标记 SUPERSEDED
  → 创建新的 episode/finalization revision
  → 新 revision 从 OPEN/RECONCILING 重新计算
```

不要静默修改已经生成报告所绑定的旧 revision。

### 5.5.11 建议字段

```text
finalization_id
night_episode_id
revision
state
finalization_reason
soft_deadline_at
hard_deadline_at
push_watermark_at
pull_watermark_at
sleep_report_received_at
finalized_at
source_completeness
late_data_count
supersedes_finalization_id
version
```

### 5.5.12 报告资格

- SOFT_FINALIZED：可以生成带明确 `partial/provisional` caveat 的报告。
- HARD_FINALIZED：可作为正式趋势、CareOutcome baseline/follow-up 的首选数据。
- RECONCILIATION_REQUIRED：fail closed，不生成确定性结论；必要时只输出数据异常提示。
- 新 revision 生成后，旧 analysis/projection 标记 superseded，不能继续触发新 CareAction。

### 5.5.13 测试场景

- 正常入睡、起床、SleepReport 按时到达。
- 无 bed-out 事件，按 deadline soft finalize。
- SleepReport 晚于早晨报告到达。
- Push/Pull 乱序和重复。
- 设备离线，只有部分数据。
- 同一 SleepReport 重试多次。
- 跨时区午夜和时区变更。
- hard finalize 后迟到数据触发新 revision。
- 旧报告被 supersede，但仍可审计查看。

### 5.5.14 P5 完成定义

- 新设备可以通过 CLI 完成可审计绑定。
- 无需人工执行 Pull 或晨间 finalization。
- 任意一晚都能解释为何 soft/hard finalize、使用了哪些 watermark 和 deadline。
- 迟到数据通过 revision 重算，不覆盖历史结果。
- Scheduler 重启和多实例不会创建不可控重复 operation。

---

## P6：完成现实世界结果闭环——邮件、回执、确认、行动和下一夜评估

## P6-A：为什么先做邮件

第一版选择邮件而不是短信：

- 不需要处理复杂的手机号地域、短信签名、模板审批和高频计费。
- 适合承载结构化摘要和安全确认链接。
- 更容易在开发环境中使用 fake adapter 验证完整状态机。
- provider API 通常能返回 message ID，并可通过 webhook 提供 delivered/bounced 等事件。

但需要诚实区分：

```text
SMTP/API 接受请求 ≠ 邮件已送达 ≠ 收件人已阅读 ≠ 照护行动已完成
```

因此模型中必须保留独立状态。

MVP 先完整打通“家属邮件”链；医生使用相同 contract 和 recipient role，在联系人授权、报告粒度和专业责任规则验证后启用，不需要复制第二套实现。

### 5.6.1 目标 operation DAG

```text
product.shared_analysis.v2
→ care.action.propose.v1
→ hitl.decision
→ delivery.email.v1
→ delivery.receipt.v1
→ recipient.ack.v1
→ care.execution.v1
→ care.outcome.evaluate.v1
→ personalization.effect.v1
```

### 5.6.2 复用现有能力

应复用而不是重写：

- `ActionProposal`
- `HumanDecisionRequest`
- `ApprovalGrant`
- verified capability
- `CareActionCandidate`
- `CareDeliveryDecision`
- `ExternalActionTarget`
- delivery intent / reconciliation / outcome_unknown 处理范式
- lease/fence/retry/invocation journal

将当前 replay-specific delivery sink 抽象为通用 port：

```python
class DeliveryAdapter(Protocol):
    def dispatch(self, command: DeliveryCommand) -> ProviderAcceptedReceipt:
        ...

    def parse_webhook(self, request: RawWebhookRequest) -> DeliveryEvent:
        ...
```

实现：

```text
DeterministicReplayDeliveryAdapter
EmailDeliveryAdapter
```

### 5.6.3 核心状态模型

#### CareActionProposal

```text
PROPOSED
AWAITING_APPROVAL
APPROVED
REJECTED
REVOKED
EXPIRED
```

必须绑定：

```text
shared_analysis_sha256
care_candidate_sha256
subject_id
recipient_role
action_type
risk_level
approval_policy_version
```

#### DeliveryIntent / DeliveryAttempt / DeliveryReceipt

```text
PENDING
DISPATCHING
PROVIDER_ACCEPTED
DELIVERED
BOUNCED
FAILED
OUTCOME_UNKNOWN
```

每次发送保存：

```text
delivery_id
semantic_idempotency_key
provider_message_id
recipient_binding_id
requested_at
provider_accepted_at
delivered_at
failure_code
raw_event_ref
attempt_no
```

#### RecipientAcknowledgement

```text
UNSEEN
OPENED（可选，不作为强事实）
ACKNOWLEDGED
DECLINED
```

#### CareActionExecution

```text
NOT_STARTED
IN_PROGRESS
COMPLETED
CANCELLED
EXPIRED
OUTCOME_UNKNOWN
```

#### CareOutcome

记录：

```text
action_execution_id
baseline_night_revision_ids
followup_night_revision_ids
metric_definition_version
objective_deltas
subjective_feedback
exclusions
observed_association_only
```

不得宣称单次行动与下一晚改善存在因果关系，只能记录“行动后观察到的变化”。

#### PersonalizationEffectReceipt

```json
{
  "change_id": "...",
  "previous_analysis_sha256": "...",
  "next_analysis_sha256": "...",
  "changed_claim_ids": ["..."],
  "changed_advice_ids": ["..."],
  "user_accepted": true,
  "repeated_correction": false,
  "evaluation_window": "..."
}
```

### 5.6.4 联系人绑定

新增受治理的 contact binding，而不是让 LLM 或 CareStrategy 输出邮箱：

```text
ContactBinding
- subject_id
- recipient_role: family | doctor
- destination_type: email
- destination_ref/encrypted value
- consent_status
- effective_from/to
- verified_at
- status
- version
```

所有邮件目标都由确定性 application service 解析。

### 5.6.5 HITL 与发送边界

严格执行：

```text
LLM 产生 CareActionCandidate
→ policy 决定是否允许提出 proposal
→ 人工批准生成 verified capability
→ DeliveryCommandService 验证 capability
→ 创建唯一 DeliveryIntent
→ Worker 执行外部发送
```

禁止：

- CareStrategy 直接调用邮件工具。
- SafetyReview 直接发送。
- LLM 提供任意邮箱地址。
- shadow/legacy report 创建真实 DeliveryIntent。
- approval 被撤销后继续发送尚未开始的动作。

### 5.6.6 邮件内容与确认链接

邮件正文包含：

- 最小必要睡眠摘要。
- 建议动作。
- 数据质量 caveat。
- “确认收到 / 暂不执行 / 标记已完成”入口。
- 报告 revision 和生成时间的可读说明。

确认链接使用：

- 有效期有限。
- 单一用途。
- 绑定 recipient、subject、action 和允许动作。
- 服务端只存 token hash。
- 幂等消费。
- 不在 URL 中暴露原始 ApprovalGrant 或健康数据。

### 5.6.7 Provider 回执

- webhook 校验 provider 签名。
- 使用 provider message ID 映射内部 delivery。
- webhook event 去重。
- 状态单调前进；乱序事件通过 event timestamp/version 处理。
- provider accepted 不能自动升级成 delivered。
- 崩溃发生在“外部已接受、数据库未提交”时进入 `OUTCOME_UNKNOWN`，由 reconciliation 查询或 webhook 修复。

### 5.6.8 下一夜评估

当新的 `HARD_FINALIZED` 夜晚出现时：

1. 找到处于 evaluation window 的已完成 CareAction。
2. 读取 baseline 与 follow-up 的兼容指标。
3. 对 movement 只比较相同 `metric_id + unit + aggregation`。
4. 生成 objective delta 与数据质量说明。
5. 合并家属/医生主观反馈。
6. 生成 `CareOutcome`。
7. 通过现有治理流程提出 Habit/Memory 更新候选。
8. 生成 `PersonalizationEffectReceipt`，证明后续报告是否实际受该变化影响。

### 5.6.9 建议数据库迁移

```text
017_live_care_delivery_and_outcome.sql
```

优先复用已有 delivery intent 表；按缺口新增：

```text
contact_bindings
delivery_attempts
delivery_receipts
recipient_acknowledgements
care_action_executions
care_outcomes
personalization_effect_receipts
```

### 5.6.10 故障与幂等验收

必须覆盖：

- 发送前 Worker 崩溃。
- Provider 已接受后、本地 commit 前崩溃。
- 同一 operation 重试。
- webhook 重复、乱序和延迟。
- approval 在发送前撤销。
- stale report revision 尝试发送。
- recipient 重复确认或重复标记完成。
- 邮件 bounced。
- provider 无法判断结果。
- 下一夜数据不足，不错误生成效果结论。

### 5.6.11 P6 完成定义

- 没有有效 HITL approval 就不存在真实外部发送。
- 同一 semantic action 在崩溃和重试下最多产生一次业务发送意图。
- 系统能区分 provider accepted、delivered、bounced、failed 和 unknown。
- 家属可以确认、拒绝并标记行动完成。
- 下一硬最终化夜晚会触发可审计的结果评估。
- Habit/Memory 更新包含来源、权限、版本和对后续报告的实际影响证明。

---

## P7：最终收口与全链路验收

### 5.7.1 退役残留

在满足零新写入、零消费者和历史读取验证后：

- 删除旧 per-role report write path。
- 删除 compatibility queue 与 completion/failure orchestration。
- 移除过渡 re-export shim。
- 清理只服务旧链的 contracts、fixtures、SQL index/function。
- 历史表是否 drop 由数据保留策略决定，不为追求“仓库干净”而提前删除审计数据。

### 5.7.2 端到端验收场景

至少固化以下故事：

1. **正常完整夜晚**：Push/Pull → hard finalize → shared report → 三角色 projection。
2. **部分数据夜晚**：deadline soft finalize → partial report → SleepReport 晚到 → 新 revision → supersede。
3. **体动双指标夜晚**：index 与 hourly count 同时存在，报告分别解释。
4. **跨日时区夜晚**：UTC 数据正确映射 local wake date。
5. **Urgent Safety**：继续保持 authoritative zero-model fast path，不等待普通报告或邮件链。
6. **家属邮件闭环**：proposal → HITL → delivered → acknowledged → completed → next-night evaluation。
7. **撤销与故障**：approval 撤销、Worker crash、重复 webhook 下无重复副作用。
8. **个性化效果**：Habit/Memory revision 被下一份报告读取，并生成 effect receipt。

### 5.7.3 运行指标

最低需要：

```text
active_device_binding_count
acquisition_schedule_lag
push/pull freshness
soft/hard finalization latency
late_data_revision_count
report_ready_latency
shared_analysis_provider_call_count
legacy_report_operation_created_count（目标 0）
delivery_provider_accepted_rate
delivery_delivered_rate
delivery_outcome_unknown_count
acknowledgement_latency
care_action_completion_rate
outcome_evaluation_eligible/completed count
personalization_effect_receipt_count
```

### 5.7.4 最终 Definition of Done

只有同时满足下列条件，才算本轮整改完成：

- 两种体动在合同、数据库、聚合和报告中完全分离。
- Push、Pull、Replay 使用同一个 semantic validation boundary。
- UTC、本地时间、local date 和 timezone 可追溯且测试覆盖跨日/DST。
- 所有正式角色报告均来自一个 SharedNightAnalysis。
- 新数据不再产生 legacy compatibility report operation。
- 关键依赖方向符合分层规则且不存在已知 SCC。
- DeviceBinding 可通过正式 CLI 管理。
- Pull、finalization 和报告触发无需人工操作。
- 迟到数据通过 immutable revision 重新分析。
- 家属邮件实现真实发送、回执、确认和行动完成。
- 下一夜数据触发结果评估，并能影响受治理的个性化状态。
- urgent zero-model fast path 未被普通闭环削弱。
- 所有外部副作用在进程崩溃、重试和重复 webhook 下保持幂等。

---

# 6. 建议的 Codex 提交序列

为降低一次性改动风险，建议 Codex 按以下独立目标执行；每个目标都必须独立测试和提交：

```text
M0  Baseline, ADRs, feature flags, characterization fixtures
M1  Movement v2 contracts and semantic registry
M2  CanonicalObservationFactory and Push/Pull/Replay parity
M3  Movement aggregation migration and legacy upcaster
M4  ReportingContext, timezone conversion and local-date tests
M5  Structured facts and zh-CN role renderers
M6  Report pipeline shadow mode and consumer inventory
M7  API/CLI/Demo readers migrate to RoleProjectionV2
M8  Shared-only cutover; stop new compatibility operations
M9  Remove legacy report write path
M10 Extract worker kernel and composition roots
M11 Remove domain→workers/runtime inversions and import cycles
M12 DeviceBindingService and admin CLI
M13 Durable acquisition scheduler
M14 Night Finalization state machine and late-data revisions
M15 Generic DeliveryAdapter and email provider adapter
M16 HITL→delivery→receipt→acknowledgement→execution
M17 CareOutcome and PersonalizationEffectReceipt
M18 Full PostgreSQL/process/fault E2E and final legacy cleanup
```

每个目标的 Codex 最终回复必须包含：

1. 变更文件清单。
2. 新增或修改的领域不变量。
3. migration 名称及可回滚/向前兼容说明。
4. 新增测试及实际运行结果。
5. provider call、operation 数量或外部副作用是否发生变化。
6. 是否保留兼容 shim/feature flag，以及何时删除。
7. 未完成项和下一目标入口。

---

# 7. Codex 全程约束

1. 不修改既有 migration；数据库变化只新增 migration。
2. 不在同一个提交中同时进行“大规模文件移动”和“业务行为修改”。
3. 不新增第二套 Observation validator、Scheduler 业务规则或 Delivery 状态机。
4. 不让 LLM 执行时间换算、选择联系人或直接产生外部副作用。
5. 不把 ambiguous legacy movement 当作 index 或 count 猜测性回填。
6. 不让 legacy/shadow report 触发 HITL、邮件或 CareOutcome。
7. 不以删除历史数据来掩盖旧链兼容问题。
8. 不替换现有 PostgreSQL durable queue、lease/fence 或 HITL 核心，除非先证明现有不变量无法满足。
9. 每个跨层接口先定义 port/contract，再接 concrete adapter。
10. 每个新 operation 都必须有 semantic idempotency key、可追踪状态和 crash/retry 测试。
11. 任何 hard-finalized 数据的迟到修正必须创建新 revision，不能原地覆盖报告依据。
12. 所有结果评估只能描述观察关联，不做未经验证的医疗因果结论。

---

# 8. 推荐立即开始的第一批工作

第一批不要同时启动设备调度和邮件闭环。先连续完成：

```text
M0 → M1 → M2 → M3
```

即：

```text
基线与开关
→ Movement v2
→ 三条输入链统一 Factory
→ 下游聚合和旧数据 upcaster
```

完成后再执行：

```text
M4 → M5 → M6 → M7 → M8 → M9
```

即：

```text
时区/语言合同
→ 结构化中文渲染
→ 报告 shadow 对比
→ 所有读取迁移
→ shared-only 切换
→ 旧写链退役
```

这两组完成，SleepAgent 才拥有稳定、唯一、语义正确的报告主干。之后再进行依赖治理、设备自动调度和现实照护闭环，返工风险最低。
