# RealSleepAgent v1 优化目标 Plan

## 1. 版本定位

RealSleepAgent v1 的目标是把当前 SleepAgent MVP 从“mock 驱动的产品原型”升级为“真实 SHHS 数据和真实后端任务驱动的睡眠健康 Agent 原型”。

当前 MVP 已经具备：

- FastAPI 后端接口：`/mock-analysis`、`/mock-report`、`/agent/orchestrate`、`/stage9/mock-context`。
- YASA 睡眠分期真实 SHHS 本地演示链路：EDF 读取、Wake / REM / NREM 映射、指标评估。
- 1D-CNN + BiLSTM 呼吸事件模型训练/评估/推理管线：但当前 20 条 SHHS demo checkpoint 性能不足，不能用于主结论。
- 报告生成边界：模板 + 本地 seed RAG，Chroma/DeepSeek 可选。
- Agent 编排边界：确定性 `SleepAnalysisAgent -> ReportAgent -> DialogueAgent`，LangGraph 可选但仍是线性同构图。
- Stage 9 本地 JSONL 数据管理、长期记忆压缩、本地告警、mock 外部上下文。
- Next.js 产品工作台：Task Thread、Agent Event、Artifact、角色模式、趋势、问答、关怀计划均已在前端 mock 中建模。

v1 不追求医疗级诊断能力，也不承诺呼吸模型达到临床可用。v1 的核心是把真实数据处理、真实 Agent 任务状态、真实 Artifact、持久化和安全评估闭环建立起来，并保留明确 caveat。

## 2. v1 总体目标

1. 用真实 `AnalysisService` 替换 `SleepAnalysisAgent` 的 mock 调用。
2. 用真实 LangGraph 状态图表达分析任务生命周期。
3. 提供任务 API 和事件流，前端从本地 mock 迁移到后端任务状态。
4. 将 Artifact 后端化，支持保存、修改、版本化和导出。
5. 将 Stage 9 JSONL 替换为 PostgreSQL，长期记忆成为 Agent 可检索上下文。
6. 建立安全、评估、报告审查和多轮一致性测试。
7. 呼吸模型先优化和门控，再允许进入 Agent 主结论。
8. 统一前端部署到 Next.js + FastAPI，移除 Stage 10 演示链路中的 Streamlit 主路径。

## 3. 架构目标

### 当前 MVP 架构

```text
FastAPI endpoint
  -> generate_mock_sleep_analysis()
  -> generate_mock_sleep_report()
  -> rule-based DialogueAgent
  -> local JSONL / mock external context

Next.js frontend
  -> frontend/lib/mock-data.ts
  -> local task state simulation
```

### RealSleepAgent v1 目标架构

```text
POST /tasks
  -> TaskService creates task
  -> LangGraph Agent graph
      -> LoadRecordNode
      -> QualityCheckNode
      -> SleepStagingNode
      -> RespiratoryDetectionNode
      -> RiskAssessmentNode
      -> MedicalRAGNode
      -> ReportGenerationNode
      -> CarePlanNode
      -> ChatContextNode
  -> ArtifactService persists outputs
  -> EventService streams AgentEvent
  -> PostgreSQL stores tasks, events, artifacts, reports, memory, alerts

Next.js frontend
  -> FastAPI task APIs
  -> SSE/WebSocket event stream
  -> backend-backed Artifact workspace
```

## 4. 具体优化计划

## 4.1 真实 AnalysisService 替换 mock SleepAnalysisAgent

### 当前状态

- `SleepAnalysisAgent.run()` 直接调用 `generate_mock_sleep_analysis()`。
- `/agent/orchestrate` 不读取 SHHS EDF/XML，不运行 YASA，不运行呼吸模型。
- 真实 YASA 和呼吸模型代码存在，但主要在 CLI/script 和本地 artifact 层。

### v1 目标

新增 `AnalysisService`，作为真实分析主入口。`SleepAnalysisAgent` 不再直接生成 mock，而是调用该服务。

目标节点：

```text
LoadRecord
QualityCheck
YASA Staging
Respiratory Inference
Risk Assessment
```

每个节点必须输出：

- 结构化 `payload`
- `status`
- `warnings`
- `caveats`
- `source_paths` 或 `source_artifacts`
- `metrics`
- `generated_at`

### 建议实现

新增模块：

```text
sleepagent/services/analysis_service.py
sleepagent/schemas/analysis.py
sleepagent/services/signal_quality.py
sleepagent/services/risk_assessment.py
```

建议核心接口：

```python
class AnalysisService:
    def run_analysis(self, request: AnalysisRequest) -> AnalysisRunResult:
        ...
```

`AnalysisRequest` 建议字段：

- `record_id`
- `subject_id`
- `shhs_root`
- `edf_path`
- `nsrr_xml_path`
- `profusion_xml_path`
- `eeg_channel`
- `eog_channel`
- `emg_channel`
- `respiratory_channels`
- `use_respiratory_model`
- `respiratory_checkpoint_path`
- `allow_demo_respiratory_model`

`AnalysisRunResult` 建议包含：

- `record_status`
- `quality_result`
- `sleep_staging_result`
- `respiratory_result`
- `risk_result`
- `sleep_analysis_result`
- `caveats`

### 关键 caveat

呼吸模型当前 demo checkpoint 不能作为“未发现异常”的证据。v1 必须在结果中明确：

- 如果使用 demo checkpoint，只能标记为 `pipeline_demo_only`。
- 呼吸风险主结论不得依赖当前低性能 checkpoint。
- 如果模型未通过门控，风险结论应使用 SHHS 标注摘要、YASA 睡眠时间和规则解释，或标记为 `respiratory_model_unvalidated`。

### 验收标准

- `SleepAnalysisAgent` 支持 `mode="real"` 和 `mode="mock"`，默认开发可保留 mock，但真实任务 API 使用 real。
- 给定本地 `shhs1-200001`，后端能完成 EDF/XML 路径解析、质量检查、YASA 分期、风险摘要输出。
- 每个步骤都有可序列化 payload 和 caveat。
- 真实分析失败时返回明确 error event，不吞掉异常。

## 4.2 LangGraph 改成真实状态图

### 当前状态

- LangGraph 是可选依赖。
- 当前图节点仍是 `sleep_analysis -> report -> dialogue -> build_result`。
- 它没有表达真实工具调用、任务状态、重试、错误分支或 Artifact 生成。

### v1 目标

将 Agent 图改为真实任务状态图：

```text
Start
  -> LoadRecordNode
  -> QualityCheckNode
  -> SleepStagingNode
  -> RespiratoryDetectionNode
  -> RiskAssessmentNode
  -> MedicalRagNode
  -> ReportGenerationNode
  -> CarePlanNode
  -> ChatContextNode
  -> End
```

### 节点职责

| Node | 职责 | 输出 |
| --- | --- | --- |
| `LoadRecordNode` | 解析 SHHS 本地路径，读取 EDF/XML metadata | record metadata, source paths |
| `QualityCheckNode` | 检查通道存在、采样率、时长、基础缺失 | quality status, warnings |
| `SleepStagingNode` | 调用 YASA adapter | epochs, sleep summary, metrics/caveat |
| `RespiratoryDetectionNode` | 调用呼吸模型或标注摘要 fallback | respiratory events, model caveat |
| `RiskAssessmentNode` | 汇总 AHI、血氧、事件、模型可信度 | risk level, evidence chain |
| `MedicalRagNode` | 检索医学知识与安全边界 | retrieved chunks, citations |
| `ReportGenerationNode` | 生成多角色报告 Artifact | report artifacts |
| `CarePlanNode` | 生成观察计划草稿 | care plan artifact, ask-before-act |
| `ChatContextNode` | 构建对话上下文和 memory links | dialogue context |

### 状态对象建议

```python
class SleepAgentGraphState(TypedDict, total=False):
    task_id: str
    request: TaskRequest
    record: RecordContext
    quality: QualityResult
    sleep_staging: SleepStagingNodeResult
    respiratory: RespiratoryNodeResult
    risk: RiskAssessmentResult
    rag: MedicalRagResult
    artifacts: list[Artifact]
    events: list[AgentEvent]
    caveats: list[str]
    errors: list[AgentError]
```

### 错误分支

v1 需要支持：

- EDF/XML 缺失：任务 `failed`，输出 `missing_local_data`。
- 通道缺失：任务可继续，但 sleep staging 或 respiratory 节点 `skipped_with_warning`。
- YASA/MNE 未安装：任务 `failed` 或回退到 XML-only summary。
- 呼吸 checkpoint 不合格：呼吸模型节点 `skipped_unvalidated_model`。
- RAG/LLM 失败：回退 deterministic report。

### 验收标准

- LangGraph graph 能在本地 mock-free task 中跑完。
- 每个节点都写入 `AgentEvent`。
- 每个节点都可单独单元测试。
- 失败节点能产生结构化错误，不导致前端空白。

## 4.3 增加任务 API 和事件流

### 当前状态

- 现有 `/agent/orchestrate` 是一次性同步接口。
- 前端已有 `TaskStatus`、`SleepAgentTask`、`AgentEvent`、`ToolCall` 类型，但数据来自 `frontend/lib/mock-data.ts`。
- 没有任务持久化，没有事件流。

### v1 目标

新增任务 API：

```text
POST /tasks
GET /tasks/{task_id}
GET /tasks/{task_id}/events
GET /tasks/{task_id}/artifacts
POST /tasks/{task_id}/confirm
POST /tasks/{task_id}/cancel
```

事件流优先支持 SSE：

```text
GET /tasks/{task_id}/events/stream
```

WebSocket 可作为后续增强：

```text
WS /tasks/{task_id}/ws
```

### API 行为

`POST /tasks`：

- 创建任务。
- 写入 `task_created` event。
- 生成 plan。
- 状态进入 `awaiting_confirmation`。
- 不直接执行重型分析。

`POST /tasks/{task_id}/confirm`：

- 状态进入 `running`。
- 后台执行 LangGraph。
- 每个节点持续写入 event。

`GET /tasks/{task_id}/events/stream`：

- 前端订阅 SSE。
- 返回与当前 `AgentEvent` 类型兼容的事件。

### AgentEvent 对齐

前端已有事件类型：

```ts
"task_created"
"plan_created"
"step_started"
"tool_called"
"finding_created"
"artifact_created"
"step_completed"
"task_completed"
"error"
```

后端 v1 应复用这些类型，避免前端重复建模。

### 验收标准

- Next.js 不再通过 `runMockAnalysis()` 模拟任务运行。
- 点击“开始任务”后调用 `POST /tasks`。
- 用户确认后调用 `POST /tasks/{task_id}/confirm`。
- Agent Run 页面通过 SSE 逐步收到事件。
- 刷新页面后能通过 `GET /tasks/{task_id}` 恢复任务状态。

## 4.4 Artifact 真正后端化

### 当前状态

- 前端已有 Artifact 类型和 Artifact workspace。
- 后端报告仍是 `MockSleepReport` 一次性响应。
- Artifact 修改只发生在前端 state 中，不持久化。

### v1 目标

新增 `ArtifactService`，所有报告、证据链、医生摘要、技术说明、趋势解释、关怀计划都成为后端 Artifact。

Artifact 类型沿用前端：

```text
risk_summary
evidence_chain
elder_report
family_report
doctor_report
technical_report
trend_interpretation
care_plan
```

### 后端能力

需要支持：

- 创建 Artifact
- 获取 Artifact
- 修改 Artifact
- 版本化 Artifact
- 导出 Artifact
- 记录 Artifact 操作事件

建议 API：

```text
GET /tasks/{task_id}/artifacts
GET /artifacts/{artifact_id}
POST /artifacts/{artifact_id}/revise
GET /artifacts/{artifact_id}/versions
POST /artifacts/{artifact_id}/export
```

### 数据模型建议

```text
artifacts
  id
  task_id
  subject_id
  record_id
  type
  title
  status
  current_version_id
  created_by_step_id
  created_at
  updated_at

artifact_versions
  id
  artifact_id
  version_number
  content
  revision_instruction
  created_by
  created_at
  safety_review_status
```

### 导出格式

v1 至少支持：

- Markdown
- JSON
- CSV summary

PDF 可作为后续版本。

### 验收标准

- 报告中心展示后端 Artifact。
- 修改报告后刷新页面内容仍保留。
- 每次修改生成新版本。
- 医生摘要和关怀计划需要用户确认后才能导出或启用。
- Artifact 内容包含来源、生成步骤和医学免责声明。

## 4.5 PostgreSQL 持久记忆替换 JSONL

### 当前状态

- Stage 9 使用 `LocalJsonlSleepDataRepository`。
- 长期记忆通过最近若干条 JSONL 记录压缩。
- DialogueAgent 的 `dialogue_context` 是 request 字段，不是持久检索上下文。

### v1 目标

引入 PostgreSQL，替换本地 JSONL 为默认存储；JSONL 可保留为本地 fallback 或测试 adapter。

需要持久化：

- users / subjects
- sleep records
- tasks
- task events
- analysis results
- reports / artifacts
- artifact versions
- memory summaries
- alert events
- external contexts

### Repository 边界

保留当前 `SleepDataRepository` 的思想，但新增：

```text
PostgresSleepDataRepository
TaskRepository
ArtifactRepository
MemoryRepository
AlertRepository
```

### 长期记忆升级

当前：

```text
request.dialogue_context.history_summary
```

v1：

```text
DialogueAgent
  -> MemoryRepository.get_subject_context(subject_id)
  -> latest memory summary
  -> relevant prior tasks
  -> recent artifacts
  -> safety notes
```

长期 memory summary 应该包含：

- 最近 N 次 AHI 趋势
- 睡眠效率趋势
- 呼吸事件趋势
- 风险等级分布
- 用户偏好
- 家属关注点
- 医生报告历史
- 最近问答主题

### 验收标准

- `/stage9/mock-context` 不再是主要集成入口。
- 创建任务后 task、events、artifacts 都写入 PostgreSQL。
- DialogueAgent 即使 request 不带 `dialogue_context`，也能读取 subject 历史摘要。
- 迁移脚本和测试数据库初始化可重复运行。

## 4.6 安全和评估强化

### 当前状态

- 报告模板已有免责声明。
- DialogueAgent 对胸痛、严重呼吸困难、意识异常等急症有规则边界。
- LLM draft 有 schema 校验和 unsafe pattern 拦截。
- 但缺少系统化评估、RAG 来源治理、报告审查流程和多轮一致性测试。

### v1 目标

建立安全与评估层，确保 Agent 输出始终符合“辅助分析，不替代诊断”的边界。

### 安全模块

新增：

```text
sleepagent/services/safety.py
sleepagent/evaluation/
```

安全检查包括：

- 禁止诊断性断言：如“确诊”“已经患有”“无需就医”。
- 急症边界：胸痛、严重呼吸困难、意识异常、晕厥等必须提示及时就医或急诊评估。
- 模型边界：未验证模型不得写成阴性结论。
- RAG 来源边界：seed knowledge 必须标注为内部种子知识，不能伪装成指南。
- 角色边界：老人版通俗，医生版专业，但都不得越过诊断边界。

### RAG 来源治理

v1 要求：

- 每个知识 chunk 有 `source`、`source_type`、`review_status`、`last_reviewed_at`。
- 区分 `internal_seed`、`clinical_guideline`、`paper`、`local_policy`。
- 未审核 chunk 只能用于开发，不用于用户可见医学依据。

### 报告版本评审

Artifact version 增加：

- `safety_review_status`
- `blocked_reasons`
- `reviewed_at`
- `reviewed_by`

未通过安全检查的报告不得导出给家属或医生。

### 多轮一致性测试

需要测试：

- 同一指标多轮解释一致。
- 用户追问不会把 caveat 弱化。
- 急症问题始终触发安全边界。
- 报告修改不会删除免责声明。
- 医生模式不会变成诊断模式。

### 验收标准

- 每个 report artifact 保存前都经过 safety checker。
- 每个 dialogue answer 返回 `safety_flags`。
- 关键测试集覆盖急症、安全禁词、模型 caveat、RAG 来源。
- 安全失败时前端显示可解释错误或要求修改。

## 4.7 呼吸模型先优化再进入 Agent 主结论

### 当前状态

- 1D-CNN + BiLSTM 管线完整。
- 当前 20-record demo test 指标：abnormal recall 为 `0.0`，测试窗口全部预测 `normal_breathing`。
- 不能用于“呼吸异常不存在”的结论。

### v1 目标

呼吸模型必须先完成性能优化、校准和门控，再进入 Agent 主结论。

在此之前：

- 可以作为技术演示 Artifact。
- 可以作为模型管线 evidence。
- 不得作为临床风险阴性证据。
- 不得驱动低风险结论。

### 优化方向

1. 类别不平衡处理
   - class weights
   - focal loss
   - abnormal oversampling
   - subject-level balanced sampler
   - normal window downsampling

2. 阈值调优
   - 不只使用 argmax。
   - 为 `hypopnea` 和 `suspected_apnea` 单独调阈值。
   - 以 abnormal recall 为核心目标。

3. 更多 subject-level split
   - 不按窗口随机切分。
   - 以 subject/record 为 split 单位。
   - 固定 train/val/test manifest。
   - 增加 leakage check。

4. 校准与置信区间
   - temperature scaling
   - reliability curve
   - bootstrap confidence interval
   - per-record uncertainty summary

5. 模型基线扩展
   - 当前 1D-CNN + BiLSTM 保留。
   - 增加 CNN-only、TCN、Transformer encoder baseline。
   - 对比不同通道组合：`THOR RES`、`ABDO RES`、Airflow、SpO2。

### 模型进入主结论的门控

建议门控阈值：

- abnormal recall >= 0.80
- abnormal F1 有稳定提升
- AUC >= 0.85
- per-record recall 不出现系统性全 normal collapse
- 至少一个固定 test split 和一个外部 holdout split 通过
- 报告中展示置信区间和适用范围

未通过时：

```text
respiratory_model_status = "not_validated_for_risk_conclusion"
```

通过后：

```text
respiratory_model_status = "validated_demo_for_agent_risk"
```

### 验收标准

- Stage 6 experiment summary 明确记录模型状态。
- RiskAssessmentNode 检查模型门控状态。
- 未通过门控时，Agent 主结论必须保留 caveat。
- 模型优化实验有固定配置、固定 split、可复现实验报告。

## 4.8 统一前端部署到 Next.js + FastAPI

### 完成后状态

- Stage 10 文档推荐 Next.js 前端：`http://127.0.0.1:18510`。
- `scripts/run_stage10_shhs_demo.py` 打印 Next.js frontend 启动命令。
- `compose.yaml` 默认运行 FastAPI backend + Next.js frontend。
- Next.js 前端默认连接 FastAPI task API 和 SSE，mock mode 仅作为显式
  开发开关。

### v1 目标

统一部署架构：

```text
FastAPI backend: 127.0.0.1:18000
Next.js frontend: 127.0.0.1:18510
```

Streamlit 保留为 legacy debug 工具，但不再作为主 demo 或主 compose 服务。

### 已修改

1. `scripts/run_stage10_shhs_demo.py`
   - 将 “Start Streamlit frontend” 改为 “Start Next.js frontend”。
   - 默认前端端口改为 `18510`。
   - 输出 `cd frontend && npm run dev`。

2. `compose.yaml`
   - frontend 服务改为 Next.js。
   - Dockerfile 支持 Node/Next build，或拆分 backend/frontend Dockerfile。
   - 保留 FastAPI backend service。

3. `frontend/lib/api.ts`
   - 主路径使用真实 task API client，`runMockAnalysis()` 仅保留为 mock
     mode fallback。
   - 支持 SSE 订阅。
   - 保留 mock mode 开关用于无后端开发。

4. 文档
   - `README.md`
   - `docs/STAGE10_SHHS_DEMO.md`
   - `TASK_LOG.md`

### 验收标准

- `npm run dev` 打开 Next.js。
- 前端点击“开始任务”会创建后端 task。
- Agent Run Console 显示后端事件，而不是本地定时器模拟。
- Docker compose 默认启动 FastAPI + Next.js。
- Streamlit 只在 legacy/debug 文档中出现。

## 5. 推荐阶段拆分

### Phase 1：真实分析服务最小闭环

状态：已完成最小闭环。

目标：

- 新增 `AnalysisService`。
- 接入真实 SHHS path resolver、quality check、YASA staging。
- `SleepAnalysisAgent` 支持 real mode。
- 输出结构化 caveat。

验收：

- 本地 `shhs1-200001` 可通过后端任务完成真实睡眠分期。
- 呼吸模型仍可跳过或标记为未验证。

### Phase 2：任务 API + LangGraph 状态图

目标：

- 新增 task repository。
- 新增真实 LangGraph 节点。
- 新增 `POST /tasks`、`GET /tasks/{id}`。
- 事件持久化。

验收：

- 任务可创建、确认、运行、失败、恢复。
- Agent Run 每个节点都有 event。

### Phase 3：SSE + Next.js 对接

目标：

- 新增 `/tasks/{task_id}/events/stream`。
- 前端从 mock event 切到后端 event。
- 保留 mock mode 作为开发开关。

验收：

- 前端刷新后能恢复 task。
- 事件流断开后可重新拉取历史 events。

### Phase 4：Artifact 和 PostgreSQL

目标：

- Artifact 后端化。
- PostgreSQL 替换 JSONL。
- memory summary 持久化。

验收：

- 报告修改有版本。
- DialogueAgent 能读取持久历史。

### Phase 5：安全评估和呼吸模型门控

目标：

- safety checker。
- RAG source governance。
- 多轮对话评估。
- 呼吸模型优化实验和门控策略。

验收：

- 未验证呼吸模型不能进入主结论。
- 报告和对话均通过安全测试。

### Phase 6：部署统一

状态：已完成。

目标：

- Next.js + FastAPI compose。
- demo script 更新。
- README 和 Stage 10 文档统一。

验收：

- 一条文档路径即可启动真实 v1 demo。

## 6. v1 非目标

RealSleepAgent v1 明确不做：

- 医疗诊断结论。
- 自动治疗建议。
- 真实短信、邮件、App 推送。
- 未经审核医学知识库对用户开放。
- 未通过门控的呼吸模型驱动低风险结论。
- 直接上传或提交 SHHS 原始数据、EDF/XML、NPZ、checkpoint 到代码仓库。

## 7. v1 成功标准

v1 可以认为完成，当且仅当：

- 后端可基于本地授权 SHHS 样本创建真实分析任务。
- YASA 睡眠分期进入 Agent 真实执行图。
- 呼吸模型状态被严格门控并在报告中透明说明。
- 前端 Task Thread 和 Agent Run Console 使用后端 task/event/artifact 数据。
- Artifact 可保存、修改、版本化、导出。
- PostgreSQL 持久化 task、event、artifact、analysis、memory。
- DialogueAgent 使用持久 memory，而不是只依赖一次性 request 字段。
- 安全检查覆盖报告和对话。
- Docker/demo 文档统一到 Next.js + FastAPI。

## 8. 最关键的工程原则

- 先建立真实数据与任务状态闭环，再追求模型性能。
- 任何模型 caveat 都必须进入 Agent 状态、报告和前端。
- Agent 输出必须可审计：事件、工具调用、Artifact、版本都可追踪。
- 前端已有的 Task Thread / Artifact 类型应作为 v1 API 对齐目标。
- 呼吸模型未通过门控前，不能被包装成产品能力。
- 所有医学表达都必须保持辅助分析定位，不替代医生诊断。
