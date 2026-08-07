# SleepAgent

SleepAgent 是面向居家老人、家属和医生协同场景的睡眠健康观察与照护产品。产品提供持续观察、晨间解释、按需对话、经确认的低风险行动和后续跟进，不替代医疗诊断、治疗或急救判断。

## 架构权威

以下五组文档分别约束不同层面，避免把历史实现当成目标架构：

- [产品定位](product_positioning/PLAN.md)
- [产品信息架构](product_information_architecture/PLAN.md)
- [Agent 架构](agent_architecture/PLAN.md)
- [Skill 设计](skills_design/PLAN.md)
- [渐进式睡眠习惯画像](sleep_habit_profile/PLAN.md)

唯一生产 Agent roster 由 `SleepCareAgent`、`EvidenceReasoningAgent`、`CareStrategyAgent` 和条件触发的 `SafetyReviewAgent` 构成。它是封闭集合，不提供第五 Agent、旧身份 alias 或历史名称路由。Agent 是有独立目标、状态、权限、反馈闭环和审计身份的责任主体；计算、检索、模型推理、渲染、存储、权限检查与外部执行属于 Tool、Service 或确定性 Policy。

`product_agent` 已具备四角色 Contract、治理门和 Episode 主链；具体角色边界与旧运行时清理按架构收口阶段完成。验收完成前，旧固定/Dynamic 源码只属于待迁移残留，不是受支持的生产、开发 fallback 或备用架构：

- `sleepagent/radar_agent/` 提供雷达数据、证据、确认、持久化和 API 基础；其中旧 Agent/runtime 身份等待迁移后删除。
- `sleepagent/radar_agent/product_agent/` 是唯一 Agent namespace，承载四角色合同、Episode、治理、工具与 runner 内核，不应复制为平行实现。
- 渐进式 Habit Profile 作为 Questionnaire、类型化 Profile Store、Evidence adapter 和 Commit Controller 能力融入该内核，不新增 Agent；旧自由文本 Memory 和 family-only legacy writer 不是 Habit Profile 写入路径。
- `sleepagent/product_device/` 只提供设备数据与产品适配 API，不得拥有独立 Agent 身份或 alias。
- `sleepagent/integrations/perceptor/` 提供供应商 client、签名、统一 push/pull 规范化和真实验收合同；旧本地 webhook SQLite 仅用于显式诊断与一次性导入。
- `backend/main.py` 只挂载当前雷达、产品兼容、健康状态和 Perceptor webhook 入口。
- `frontend/` 是唯一保留的 Next.js 用户界面。

`sleepagent/radar_agent/` 中的旧角色只可作为能力迁移来源；禁止新增调用方，并将在收口阶段删除其可执行入口和身份。

## 快速启动

要求 Python 3.10+ 和 Node.js。创建环境文件并安装依赖：

```bash
cp .env.example .env
python -m pip install -e .
cd frontend
npm ci
cd ..
```

至少配置：

```dotenv
SLEEPAGENT_DEPLOYMENT_MODE=development
SLEEPAGENT_RADAR_AGENT_API_KEY=<strong-local-api-key>
SLEEPAGENT_PRODUCT_RADAR_API_KEY=<strong-local-product-key>
SLEEPAGENT_PRODUCT_ACTOR_ID=<authenticated-actor-id>
SLEEPAGENT_PRODUCT_ACTOR_ROLE=elder
SLEEPAGENT_PRODUCT_SUBJECT_ID=<authorized-subject-id>
SLEEPAGENT_PRODUCT_TIMEZONE=Asia/Shanghai
SLEEPAGENT_RADAR_AGENT_ACTOR_ID=<authenticated-actor-id>
SLEEPAGENT_RADAR_AGENT_ACTOR_ROLE=<elder-or-family-or-doctor>
SLEEPAGENT_RADAR_AGENT_SUBJECT_ID=<authorized-subject-id>
SLEEPAGENT_RADAR_AGENT_AUTHORIZATION_ID=<active-data-authorization-id>
SLEEPAGENT_RADAR_AGENT_ROLE_BINDING_IDS=<comma-separated-role-binding-ids>
SLEEPAGENT_RADAR_AGENT_SQLITE_PATH=/tmp/sleepagent_radar_agent.sqlite3
SLEEPAGENT_API_BASE_URL=http://127.0.0.1:18000
SLEEPAGENT_RADAR_AGENT_RUNTIME_MODE=product
SLEEPAGENT_RADAR_AGENT_DEV_MODE=true
SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE=fake
SLEEPAGENT_PRODUCT_RADAR_NAMESPACE=replay:local-product-demo
```

配置真实模型后，`/product/radar/chat`、`/product/radar/agent-runs`
及其追问都只调用同一个四角色 `ProductEpisodeRunner`。缺少上述
actor/role/subject 服务端绑定时会返回 503，不会退回旧单模型决策链。
固定绑定只适用于受控单对象部署；多用户环境必须由可信身份网关按会话提供。
Product 任务若需要补充事实或确认 Memory、Care、通知、分享、导出目标，
API 会保存不可对外返回的冻结请求检查点。续跑复用同一 FactSnapshot；
确认绑定候选 ID、实际 payload hash、actor、subject、scope 和有效期，
目标漂移或过期时确定性拒绝，不会把旧确认套到新目标。

如需运行受控的老人本人睡眠习惯可用性测试，还需在前端服务端配置：

```dotenv
SLEEPAGENT_HABIT_PROFILE_ACTOR_ID=<authenticated-elder-actor-id>
SLEEPAGENT_HABIT_PROFILE_ACTOR_ROLE=elder
SLEEPAGENT_HABIT_PROFILE_SUBJECT_ID=<elder-subject-id>
SLEEPAGENT_HABIT_PROFILE_AUTHORIZATION_SCOPES=
```

这组固定身份只适用于单对象、受控访问的测试部署；多用户生产环境必须由可信身份网关按会话绑定 actor、role 和 subject，不能允许浏览器自行声明身份。

启动后端：

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 18000
```

上述命令启动开发后端。独立外部睡眠域 API 使用：

```bash
uvicorn sleepagent.sleep_api.app:app --host 127.0.0.1 --port 18001
```

生产启动前必须按 `.env.example` 配置共享 PostgreSQL、canonical authority
cutover、raw encryption/retention、service credential、actor key 与权威角色绑定。

启动前端：

```bash
cd frontend
npm run dev
```

访问 `http://127.0.0.1:18510/`。前端通过同源 BFF 调用后端，不要把 API key 写入 `NEXT_PUBLIC_*`。

## 当前 API

- `/api/v1/*`：独立 Sleep API 应用中唯一受支持的外部睡眠域 API。
- `/radar-agent/*` 与 `/product/radar/*`：当前仅在非生产环境显式开启
  `SLEEPAGENT_RADAR_AGENT_DEV_MODE=true` 后可见的迁移/诊断面；架构收口后只允许
  Product Episode 诊断，不保留 `legacy_fixed`、`dynamic_goal` 或旧 Agent 执行入口。
  Fake provider 还必须显式配置 `SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE=fake`。
- `/product/habit-profile/*`：可选轻建档、当前回答、老人整体确认、查看、更正和遗忘；默认主动建档关闭。
- `/integrations/perceptor/webhook`：唯一正式供应商 webhook 写入入口。
- `/health`、`/status`：脱敏运行状态。

Perceptor 边界详见 [接入说明](docs/PERCEPTOR_INTEGRATION.md)。
睡眠习惯页面位于 `/habit-profile`。它通过同源 BFF 注入服务端身份，未配置时返回 503；发布仍要求 [3–5 人可用性测试](sleep_habit_profile/USABILITY-TEST-PROTOCOL.md)，自动化测试不能替代该门禁。
验收材料的严格 JSON 格式、catalog/identity 绑定和审计命令见
[证据格式](sleep_habit_profile/ACCEPTANCE-EVIDENCE-FORMAT.md)。
可先用 `acceptance_materials --initialize-templates` 生成绑定当前版本且
拒绝覆写的采集骨架；骨架始终不可发布，必须用真实会话、签字和执行
receipt 逐项替换。审计器也可直接接收受限 ZIP；当前
Product Agent v23 的采集骨架位于
[`sleep_habit_profile/real-evidence-v23-collection`](sleep_habit_profile/real-evidence-v23-collection)，
并绑定 release identity `e2d721ca…c689d2`；其中仍是 template，
审计结果必须保持不可发布，直至真实材料完成。旧
`sleepagent-v15-complete-simulated-evidence.zip` 保留为绑定旧 v15
identity 的完整模拟 fixture，不会进入当前 v23 正式 manifest。
历史习惯画像 v18 的完整模拟 fixture 位于
[`sleep_habit_profile/simulated-evidence-v18-complete`](sleep_habit_profile/simulated-evidence-v18-complete)，
并归档为 `sleepagent-v18-complete-simulated-evidence.zip`（SHA-256
`8dc3e39f55c76cfb1eb72053e68784a24fb6f0b29eb4ab5487e790b83c7ebea9`）。
它包含 5 条模拟可用性观察、10 条模拟 catalog 审核和 68 条合成
provider receipt 形状，只用于开发与审计器验证，不能作为真实发布证据。
机器可读的完整覆盖与文件哈希清单位于
[`SIMULATION-COVERAGE-v18.json`](sleep_habit_profile/SIMULATION-COVERAGE-v18.json)，
固定证明 5 条观察、1 份 synthetic attestation、2 名虚构审核角色、
10 个 concept、68 条场景观测、66 份 synthetic provider receipt/request
ID 和 2 条无 receipt 的确定性观测。
旧 `real-evidence-v18-collection` 与 `real-evidence-v20-collection`
只保留为历史模板，不能继续采集或升级到当前 identity。v23 骨架中的所有
`template` 槽位都必须用实际会话、真实签字或真实 provider receipt
完整替换，不能直接改成 `evidence_kind=real`。三条真实采集工作流和
签字/receipt 交接规则见
[`REAL-EVIDENCE-COLLECTION-RUNBOOK.md`](sleep_habit_profile/REAL-EVIDENCE-COLLECTION-RUNBOOK.md)。

安装 editable package 后可使用：

```bash
radar-agent run-demo --scenario normal_night --format pretty
radar-agent run-goal --goal-type night_review --date 2026-07-09 --format json
radar-agent inspect-task <task_id> --decision-trace --format pretty
```

本地 demo 可显式使用 SQLite；生产必须配置 PostgreSQL、raw encryption key
与 retention，且不得使用 `/tmp`/SQLite 作为 live authority。迁移位于
`sleepagent/radar_agent/persistence/migrations/`。动态任务在模型未配置时必须明确标记安全降级，不能把模板结果冒充智能调用。
已确认的 Habit Profile 复用同一个
`SLEEPAGENT_RADAR_AGENT_DATABASE_URL`/`SLEEPAGENT_RADAR_AGENT_SQLITE_PATH`
数据库，通过 `003_habit_profile` 迁移持久化；未确认 change set 仍只在
当前进程短时保留，重启后要求重新汇总确认。经单独确认的“以后不要再问”
偏好通过 `004_habit_question_suppression` 以最小字段持久化；普通跳过和
未确认回答仍不形成长期画像。`005_habit_questionnaire_state` 持久化
Episode 三题预算、短期 selection receipt/消费状态和 actor 级跨 Episode
冷却，使重启或并发请求不能重置采集边界。
四 Agent `product_agent` 的真实结构化调用使用
`OpenAICompatibleStructuredAgentModel`；它复用服务端
`SLEEPAGENT_PRODUCT_LLM_*`/`DEEPSEEK_API_KEY` 配置并保留 provider request
ID。未配置密钥或 provider 不返回请求 ID 时，不能生成真实发布证据。
Product Memory、跨天 Care、提交幂等 journal 和 Episode receipt 使用同一
`SLEEPAGENT_RADAR_AGENT_DATABASE_URL`（本地演示可用 SQLite）持久化，
服务重启后仍按版本 CAS。通知、分享和导出默认 fail-closed；只有配置
对应 `SLEEPAGENT_EXTERNAL_*_URL` 的 HTTPS 网关并收到真实 request ID
和 `pending | delivered` 状态后，才记录外部执行成功。未知结果持久化后
不会自动重试。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
cd frontend
npm run typecheck
npm run build
```

测试目录只保留当前雷达运行时、产品设备、Perceptor、可观测性、前端契约和 `product_agent` 迁移基础的回归测试。

## 安全边界

急症、身份、权限、隐私、用户确认和外部执行由确定性规则控制，优先于任何 Agent 判断。`SafetyReviewAgent` 即使批准，也不能授予权限、代替用户确认或绕过执行检查。

出现胸痛、严重呼吸困难、意识异常、跌倒等急症线索时，系统应优先提示及时寻求线下医疗或急救协助。
