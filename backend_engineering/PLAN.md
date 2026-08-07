# Plan: SleepAgent FastAPI 后端工程基础与模拟闭环
_Locked via grill — by Codex + SleepAgent 项目负责人_

## Goal

在不重写现有产品定位、1+2+1 四角色 Agent、睡眠领域内核和模拟数据生成器的前提下，收敛仓库中分裂的后端入口，搭建一个生产形态的 FastAPI 模块化单体。首期使用模拟数据完成端到端验收，但真实使用 PostgreSQL 权威存储、独立 durable worker 进程、显式 Unit of Work、transactional outbox、可信鉴权、审计、角色投影和可恢复 Operation；模拟能力只能作为受隔离的输入与验证适配器。首期不接真实设备、真实用户或外部 IdP，也不承诺高可用生产上线。

## Success criteria

1. 仓库只有一个可组合的 FastAPI app factory 和一个 `SleepBackendRuntime`；旧入口只做薄装配，不再各自创建 store、runner 或 worker。
2. API 与 Worker 是同一代码库中的独立进程。API 请求不直接运行 LLM、Product Agent、摄入归一化或长期归纳任务。
3. 所有命令使用 `Idempotency-Key`，在一个数据库事务中创建或复用 durable Operation 与 outbox，并返回 `202 + operation_id`；查询只读取已提交投影。
4. PostgreSQL 是唯一验收权威库。SQLite、内存 store、fake live provider 只能存在于显式 unit-test 或 replay adapter 中，不能通过生产 preflight。
5. NightEpisode revision、确定性快路径、Product Agent attempt、三角色视图、确认目标、Commit Controller 结果和 publication intent 均有明确原子提交边界；Worker 崩溃恢复不会产生第二份语义结果。
6. 一晚睡眠的产品归属日期优先使用老人所在地时区的 observed/vendor wake date；无 wake 时使用显式标记为 estimated 的 deadline fallback。原始入睡日、醒来日、assignment basis、IANA timezone 和 boundary policy version 同时保留。
7. 所有新内部聚合使用服务端生成的全局唯一 UUIDv7；既有 legacy ID 和外部 provider/device ID 保持 opaque string。每次持久化与查询仍强制携带 namespace、data mode 和必要的 subject scope。
8. `/api/v1`、`/product/sleep`、`/demo/v1` 和内部运维面彼此隔离；生产 OpenAPI 不出现 demo 或 legacy Radar 路由。
9. 老人、家属、医生的对象级和字段级授权由 PostgreSQL 中的权威 binding 决定；请求不能自报角色、subject、scope 或 data mode，撤权会使排队任务、游标和未使用确认句柄失效。
10. 全量测试恢复为绿色，并新增真实 lifespan、PostgreSQL、并发租约、故障恢复、越权、schema 兼容及完整模拟旅程测试。

## Target architecture

```text
Browser / BFF / external clients / Perceptor adapter
                         |
                         v
              FastAPI application process
        authn/authz | validation | queries | commands
                         |
          PostgreSQL transaction: state + outbox
                         |
                         v
                  durable worker process
     ingestion -> lifecycle -> deterministic fast path
               -> Product Agent -> induction/delivery
                         |
                         v
       committed projections + events + audit receipts
```

进程与模块边界：

- `sleepagent/backend_runtime.py`：唯一 composition root，拥有配置、clock、pool 和 UoW/repository factory；按 `process_role=api|worker|migration` 构造 capability-scoped profile，构造时不启动后台线程。API profile 在类型和运行时均不可达 model/provider/Agent handler。
- `sleepagent/backend_app.py`：`create_sleep_backend_app(runtime, enabled_surfaces)`，只装配 middleware、异常映射、routers、lifespan 资源开关。
- `sleepagent/worker_runtime.py`：统一 Worker 可执行入口；按 queue/handler 配置运行 `ingestion`、`fast_path`、`product_agent`、`induction`、`delivery`、`retention`、`reconciliation`，开发环境可由一个进程处理全部队列。
- `sleepagent/sleep_api/*`：继续承载稳定 `/api/v1` 合同，逐步移除自己的全局 runtime/worker 所有权。
- 新的 Product API 模块：承载 `/product/sleep/*` DTO、router 和 application service adapter，不直接导入或返回内部 Agent run result。
- `backend/main.py`：变成生产 app 的薄入口；旧 `/product/radar/*` 仅在显式 development compatibility surface 下装载并记录弃用。

## Implementation batches and file map

本计划是后端 master plan，但实现必须按以下 batch 顺序进行；每个 batch 独立保持可运行和测试绿色，后续使用 `codex-build` 时应明确本次执行的 batch，不允许横跨未通过门禁的阶段：

1. **B0 — Baseline**：冻结工作树、修复/更新 3 个已知失败、锁定 Python/依赖；不改运行架构。
2. **B1 — Foundation**：typed settings、capability-scoped runtime、PostgreSQL pool/UoW、migration CLI、namespace registry、数据库身份权威；所有业务 route 暂时 fail closed。
3. **B2 — Durable work**：CommandReceipt、Operation、lease/fencing/heartbeat、invocation journal、outbox delivery/inbox 和独立 Worker；用无模型测试 handler 证明 crash recovery。
4. **B3 — Sleep vertical slice**：模拟 raw ingress、normalization、NightEpisode revision、醒来日 finalize、fast path、urgent zero-model 和 `/api/v1` committed views。
5. **B4 — Product Agent**：persistent Product operation、Runner prepare/commit、四角色 capability、Safety target、三角色投影、确认/回答事务。
6. **B5 — Product/demo surfaces**：`/product/sleep`、受隔离 `/demo/v1`、除 forget/retention 外的 journey、feedback/reanalysis、Care follow-up 和 verifier。
7. **B6 — Governance and hardening**：forget/retention/shred 完整旅程、observability、load/fault/security tests、runbook、legacy production detach 和最终门禁。

新增/收敛文件的所有权固定如下；实现中可以拆分同一模块内部文件，但不得改变责任或另建平行 runtime/domain contract：

| Path | Responsibility |
|---|---|
| `sleepagent/backend_settings.py` | typed settings、deployment/data-mode/process-role fail-closed 校验 |
| `sleepagent/backend_runtime.py` | capability-scoped composition root、dependency manifest、UoW factory wiring |
| `sleepagent/backend_app.py` | 唯一 FastAPI app factory 与 surface 装配 |
| `sleepagent/worker_runtime.py` | queue registry、claim/heartbeat/finalize loop、graceful shutdown |
| `sleepagent/radar_agent/persistence/uow.py` | connection provider、UoW factory、UoW-scoped repository factory |
| `sleepagent/radar_agent/persistence/migrate.py` | 独立 migration apply/check CLI；应用与 Worker 不执行 DDL |
| `sleepagent/product_api/contracts.py` | 版本化 Product API DTO |
| `sleepagent/product_api/router.py` | `/product/sleep/*` transport adapter |
| `sleepagent/product_api/service.py` | Product command/query application service |
| `sleepagent/demo_cli.py` | `sleepagent-demo` HTTP-only client 与外部 verifier 入口 |
| `backend/main.py` | production app 薄入口 |
| `sleepagent/sleep_api/app.py` | `/api/v1` standalone compatibility 薄入口，复用统一 factory/runtime |
| `pyproject.toml` | Python 3.11、prod/dev extras、console scripts、pytest markers |
| `requirements/production.lock`, `requirements/dev.lock` | 由固定版本 `pip-tools` 生成并提交的 hash-locked 依赖 |
| `compose.yaml`, `.env.test.example` | replay-only Postgres/migrate/api/worker 测试 profile 与 healthcheck |
| `docker/Dockerfile` | 先 hash-lock 安装依赖，再以 `--no-deps` 安装本项目 wheel；生产镜像排除 demo/oracle |
| `tests/conftest.py` | 保留 unit 轻客户端；另提供不被 monkeypatch 的 real-lifespan fixture |

新 migration 从现有 `020` 之后按 batch 追加，历史 `001–020` 不得改写：

- `021_migration_ledger_v2.sql`（B1）：新 checksum/status ledger；在 advisory lock 下从旧 `(version, applied_at)` ledger 自举，并把当前不可变 `001–020` 文件 hash 记为 `legacy_attested`；
- `022_backend_scope_identity.sql`（B1）：namespace/run/arm registry、service principal、actor/subject binding、grant、authorization/privacy/retrieval epochs；
- `023_backend_work_protocol.sql`（B2）：CommandReceipt、Operation origin/lease/fence、invocation journal、destination delivery、consumer inbox/checkpoint；
- `024_episode_contract_v2.sql`（B3）：UUID scheme、stable episode anchor、显式 bed/wake/episode dates、assignment basis、policy/schema version；
- `025_backend_encryption_retention.sql`（B6）：retention-domain DEK metadata、retention class/job 和 shred receipt。

Bootstrap runner 内置并校验 `021` 的 release checksum，成功创建 v2 ledger 后才允许应用 `022+`；发现同 version 历史 SQL hash 改变、旧 ledger 缺号或 `started` 未 `finished` 时 fail closed，按 runbook 人工恢复，不自动跳过。

## Approach

### 0. 冻结基线与执行前置条件

1. 对内层 Git 仓库的 modified/deleted/untracked 文件做只读清单，确认哪些是当前架构工作的权威版本；不得自动 reset、删除或覆盖用户改动。
2. 经用户确认后创建可复现基线 commit/tag；把当前 3 个失败测试分类为真实缺陷或过期 fixture，并在功能改造前恢复绿色。
3. 固定 Python 3.11，使用固定版本 `pip-tools` 生成并提交 `requirements/production.lock`、`requirements/dev.lock`（含 hashes）；将 pytest 等测试依赖移到 dev/test group。安装时先按 lock 安装第三方依赖，再用 `pip install --no-deps .` 安装本地项目，禁止第二次依赖解析。记录模型、schema、migration、policy、registry 和依赖哈希。
4. 把本计划作为本期实现权威。它明确取代 `simulated_backend_closure/PLAN.md` 中“file-backed SQLite 作为 closure authority”和“FastAPI lifespan 启动 Worker”的部分；继续复用该计划的领域生命周期、oracle 隔离、幂等、prepare/commit 和验证协议，并在实现日志中记录替代关系。

验收：干净环境能安装锁定依赖并运行绿色基线；任何实现 diff 都能与冻结基线比较。

### 1. 统一配置、组合根和部署模式

1. 建立 typed settings，集中解析 deployment mode、database DSN、enabled surfaces、worker queues、签名密钥引用、model/provider mode、超时和预算；业务模块不得散落读取 `os.getenv()`。
2. `SleepBackendRuntime` 每进程只构造一次，但只持有 pool、client factory 和 UoW/repository factory，不持有 connection-bound singleton repository。它生成 `RuntimeDependencyManifest`，记录 process role、实际 store、database identity/role、migration/schema version、启用的 handler/model/provider、clock、data mode 和 config hash。
3. 配置至少区分 `test`、`development`、`production`，且一次 deployment 固定一个 `data_mode=live|replay`。Replay 与 live 使用不同 database/credential，不能只靠 namespace 字段隔离。Production 遇到 SQLite、in-memory authority、fake provider、demo router、默认密钥或 schema 未迁移时 fail closed。
4. FastAPI lifespan 只打开/关闭连接池和共享客户端，不启动 durable workers。API、Worker、migration 使用独立入口和退出码。
5. 开发 compose 收敛为 `postgres + migrate + api + worker`；migrate 成功后 API/Worker 才启动。允许同一 Worker 处理全部队列，但 API 容器内不运行后台任务；API、Worker 和 migration 使用不同最小权限数据库角色和独立 pool budget。
6. Runtime 是 deployment-scoped，可以处理同一 data mode 下多个已登记 namespace/run/arm。所有 scope 来自数据库 Namespace Registry，不接受请求自铸；Worker 通过窄幅 `SECURITY DEFINER` claim function 取得其 grant 允许的 namespace/job ID，再以精确 transaction-local namespace/subject context 读取业务 payload。跨 namespace claim 使用每 namespace 并发上限与 `priority, available_at, created_at` 排序，防止单一 replay run 饿死其他 run。

验收：同一进程不能安装两个 active runtime；多 API 进程不会复制 Worker；production OpenAPI 不含 demo/legacy surface。

### 2. PostgreSQL persistence、迁移与 Unit of Work

1. 首期沿用 psycopg 3 和现有 SQL migration 历史，不进行全量 ORM 重写。锁定 `psycopg[pool,binary]`/`psycopg_pool`；短查询使用同步 FastAPI handler，长任务只在 Worker 中执行，禁止在 `async def` 中直接调用同步 store、HTTP client 或 Runner。
2. 把当前单连接、进程锁和 helper 内部 `commit()` 改为显式 `ConnectionProvider -> UnitOfWorkFactory.begin(scope) -> UoW-scoped repositories`：每个 UoW 独占一个 pooled connection/transaction，同一 UoW 内的 repository 共享 cursor/transaction，领域服务和 repository 不能自行提交。Production dependency graph 禁止 connection-owning 或 auto-commit repository。
3. 将迁移从应用连接流程移出，形成独立 migration command；使用 session-level PostgreSQL advisory lock。Migration ledger 记录 version、不可变 SQL sha256、started/finished、status、applied_by；每个 migration 明确 transactional/non-transactional 策略和失败恢复。应用/Worker 只校验自身支持的 schema 版本区间，绝不执行 DDL。
4. 优先围绕现有表渐进拆分 repository，不大爆炸重写 `RadarPersistenceStore`。新的 application path 不得新增依赖旧 auto-commit 方法。
5. 数据库约束镜像关键领域约束：FK、唯一键、版本/CAS、data mode、namespace、subject scope、事件序号和幂等 body hash。

默认 transaction isolation 为 `READ COMMITTED`。所有短 UoW 使用统一锁顺序：authority/epoch → handle/CommandReceipt/Operation → domain aggregate/revision → projection/delivery intent → audit。配置 bounded lock/statement/idle-in-transaction timeout；仅对不含外部调用的完整短 UoW bounded retry `40P01/40001`。发生外部调用后只能重试 fenced checkpoint/finalize UoW，不能重跑 handler。

必须原子提交的 stage handoff：

- raw inbox record、ingress receipt 与唯一可 claim 的 `normalization_work`；
- normalization receipt、canonical observations、lifecycle state，以及需要时的 NightEpisode revision、`fast_path` Operation 与 committed domain event；
- fast-path quality/risk receipt、urgent result，以及 non-urgent 时唯一可 claim 的 `product_agent` Operation 与 committed domain event；
- Agent committed attempt、唯一 AnalysisRevision、三角色 views、pending handles、Commit Controller effects、terminal bundle、唯一可 claim 的 `induction` Operation、destination-specific delivery intents 与 committed domain events；
- delivery/reconciliation journal 的 fenced 状态推进。模型调用和外部 HTTP 调用永远不在数据库事务内。

`domain_outbox` 只作 append-only committed event log；现有 mutable status/lease 字段不再作为新路径调度权威。`normalization_work`、各类型 Operation 和 `delivery_intent` 分别是上述 handler 的唯一 claim row；不得再创建平行 induction job 或由 outbox event 隐式启动计算。

验收：在每个事务边界前后注入崩溃，恢复后只出现一个语义 revision/result/view/effect。

### 3. ID、namespace、时间与 schema 契约

1. 新内部主键由服务端生成 UUIDv7，并先以 canonical UUID string 写入现有 TEXT PK，避免本期全库换型；禁止从场景、路径、数组长度或外部 provider ID 派生可碰撞主键。既有非 UUID ID 保持可读且不回填，新写入必须标记 `id_scheme=uuidv7`；provider/device/message ID 原样作为 opaque string 保存并通过映射表解析，公共 ID 始终视为 opaque。
2. 所有核心记录显式带 `namespace_id` 与 `data_mode`；replay 记录再带 `run_id`、`arm_id` 和 generation。Repository 方法要求显式 scope，唯一键和幂等键包含相应 scope。首期锁定 PostgreSQL RLS：auth/grant、raw/canonical、NightEpisode/revision、Operation payload、handle、role projection、memory/profile、delivery/audit 等 subject/namespace 表均使用 `SET LOCAL` 驱动的 policy；API 与业务 Worker role 不得 `BYPASSRLS`，migration owner 是唯一 bypass role。跨 namespace work table 只可通过受审计的窄幅 claim function 取 ID；claim 后业务 UoW 必须设置精确 context。连接先 rollback/reset、验证 context 清空后才归还 pool。
3. 新 `night_episode.v2` 使用 UUIDv7 稳定身份和 `episode_anchor_key=(namespace generation, subject, opening trigger/source idempotency identity)`；日期不参与 identity。MonitoringSnapshot 的 active episode ID 与 transition receipt 用于重启/重复 start 恢复，旧 `night_key/local_sleep_date` 只保留在 v1 compatibility projection。
4. Episode 打开时冻结 subject 当时的 IANA timezone 与 boundary policy，v2 `episode_local_date` 允许 provisional/null；实际 wake 到达时按 wake local date CAS finalize。无 wake 时在 deterministic close deadline 使用 deadline local date，并写 `assignment_basis=deadline_fallback`、低 confidence/estimated status，不能表述为 observed wake date；observed/vendor wake 分别使用明确 basis。late wake/correction 通过新 revision 更正日期而不重建 Episode，旧/新日期投影在同一事务失效/重建。旅行后的 timezone 只影响下一 Episode，除非带权威来源的 correction 显式修订；DST fold/gap 必须携带 offset/fold 或拒绝。
5. 当前产品每 subject/date 只允许一个 finalized canonical main NightEpisode。Split-night segments 在同一 active Episode/allowed-lateness window 内聚合；naps/secondary sleep 不进入首期 NightEpisode。若 late correction 或独立 Episode 将造成同日冲突，不自动合并或覆盖：候选 revision 标记 `date_conflict/reconciliation_required`、不发布新投影，保留上一 committed current revision，等待确定性 reconciliation；migration 建 partial unique constraint 覆盖非冲突 finalized current rows。
6. 另存 `bed_local_date`、`wake_local_date`、UTC instants、原始 offset、timezone 和 `boundary_policy_version`。迁移模拟 generator、fixture、趋势查询和 Habit Profile。现有 `/api/v1` 的 `local_sleep_date` 不静默改义：保留原语义并标记 deprecated，新增 versioned `episode_local_date/assignment_basis/bed_local_date/wake_local_date`；Product API 只使用新 canonical 字段，后续 breaking schema 使用 overlap window。
7. 明确 `ScenarioClock` 只驱动观察事实和睡眠窗口，`ControlClock` 只驱动鉴权、重试、审计、保留和幂等过期；生产租约 eligibility/deadline 使用 PostgreSQL server time。Demo HTTP 只能推进 ScenarioClock，任何模式都不能修改 ControlClock。
8. 外部 DTO 使用严格解析：拒绝 naive datetime、NaN/Infinity、隐式 bool-number、未知请求字段和超限 body；epoch 单位必须显式。数据库统一 UTC `TIMESTAMPTZ`。
9. 建立 version dispatcher/upcaster；历史 JSON 永远按写入 schema version 读取，再投影到当前 read model。保留 raw source value/unit 与 normalized value/unit，建立版本化 metric/unit/source ontology。
10. 家属观察、老人自述、设备观测和推断使用唯一 canonical source enum，并为旧词汇建立显式映射；任何 family confirmation 不得升级为 elder self-report 或 objective fact。
11. 所有 API 响应、公共事件、导出和审计投影由服务端填充 `data_mode`；replay 强制 `synthetic_non_release=true`。旧误标 simulated artifact 只允许 quarantine/audit，不允许通用 JSON importer 自动导入。

验收：跨午夜、DST、等价时刻不同 offset、重复场景 ID、schema 升级、live/replay 混用和来源映射均有负向测试。

### 4. Durable Operation、Worker、Outbox 与租约

1. 分离调用方命令与业务语义身份。`CommandReceipt` 的唯一 reservation key 只包含 service principal、actor、route 和 caller idempotency key；body hash 作为比较值保存，因此同 reservation key 不同 body 必须 conflict，不能因把 hash 放进 unique key 而创建第二行。它映射到 Operation；`Operation.semantic_key` 由服务器按 namespace/run/arm、subject、exact revision/interaction revision、normalized intent/input hash、trigger、model mode、policy/registry/schema hash 决定。多个等价调用可指向同一 Operation；系统自动分析不绑定任意 HTTP actor。
2. 每类 durable work 只有一个调度权威：`normalization_work` 只供 ingestion claim；`operations` 供 fast_path、product_agent、induction 及用户命令 handler claim；`domain_outbox` 是 append-only committed event stream，不直接充当计算队列；外部/本地投递使用每 destination 独立 `delivery_intent`。同一事务显式创建下一阶段 Operation，而不是让 Operation 与 outbox 同时触发同一工作。
3. API command 在一个短事务内验证当前授权、预留/复用 CommandReceipt、保存 body hash、创建或引用 Operation，并保存 audit/outbox，然后返回 `202`。同 key 同 body 返回原 receipt/Operation；同 key 不同 body 返回 conflict。
4. Worker claim 使用短事务和 PostgreSQL server time，通过原子 update/returning 或 `FOR UPDATE SKIP LOCKED` 写入数据库单调递增的 lease generation、随机 fencing token、worker instance、deadline、attempt、next attempt 后立即 commit。业务/模型调用在事务和连接之外运行。
5. 长 handler 使用独立小型 heartbeat connection 按 `(job_id, lease_generation, fencing_token)` CAS 续租；checkpoint 和 final commit 在新的 UoW 中验证 Operation version、lease generation、token 和当前 epoch。过期或被重领的 Worker 无权提交。
6. 为模型/provider/external sink 建 append-only invocation journal：`reserved -> send_started/outcome_possible -> response_received|known_failed|outcome_unknown`。网络发送前先用短 UoW CAS/commit `send_started`、稳定 invocation key 和 request digest，再发请求；provider request id 在 headers/响应可得后补记，不能假设调用前存在。停在 `send_started` 或发送后失去 lease 只能 reconciliation；仅仍为 `reserved` 且从未取得 dispatch permit 的 attempt 可自动发送，除非 provider 明确支持同键幂等或结果查询，否则不得再次调用。
7. 错误分为 retryable、terminal、outcome_unknown。使用有界指数退避、最大尝试次数和 dead-letter/reconciliation；传递语义固定为 at-least-once delivery + idempotent effects，不宣称分布式 exactly-once。
8. 每个 delivery intent 绑定唯一 destination/handler 与 semantic effect key；fan-out 在源事务中生成独立 child delivery intents。消费者写 durable inbox/dedupe receipt；同 aggregate 使用 predecessor gate/per-aggregate sequence checkpoint，不能并发倒序发布相邻 revision。
9. 优先调度确定性 fast path；急症命中时模型调用计数必须为零，并生成可审计的确定性结果。Agent slow path 只在 non-urgent Operation 提交后运行。

验收：并发 Worker、lease 过期、stale worker、进程 kill、数据库短暂断开、outbox 重复投递和 unknown delivery 均有可重复故障测试。

### 5. 四角色 Product Agent 接线与提交协议

1. `sleepagent/radar_agent/product_agent` 是唯一 canonical 四角色实现；旧 LangGraph runtime 不进入新依赖图。
2. 保持职责：SleepCare 是唯一用户入口和最终发布者，Evidence 独占个人事实/不确定性，Care 只从受控 catalog 选择零或一个主要行动，Safety 条件触发并仅能 approve/revise/block。
3. Agent、Tool、Policy、Service 和 Commit Controller 分层。四个 Agent 无数据库写权限和外部副作用权限；能力 Registry deny-by-default，模型参数不能携带权威 subject、role、source refs 或确认状态。
4. 唯一 Runner caller 是 `product_agent` Worker。HTTP、bridge、feedback/reanalysis 只能创建或恢复 persistent operation。
5. 将 Runner 的发布路径收敛为 `prepare` 与 caller-owned commit：模型/工具阶段只保存不可对角色查询的 attempt checkpoint；全部必要成员和 Safety 结果齐备后，持有效 lease 的 Worker 使用一个 UoW 原子提交结果、三角色投影、确认目标和 outbox。
6. FactSnapshot 绑定 exact NightEpisode revision、source scope、quality/risk receipt、authorization/privacy/retrieval epochs、policy/registry/model/schema hash；恢复、回答和确认不能悄悄重算或替换目标。
7. Conversation、ProductEpisode、Operation、NightEpisode、CareAction、CareFollowup、HumanDecision/SafetyReview 保持独立状态机和关联键，不合并为通用 status。
8. Safety checkpoint 必须 target-specific：Evidence、Care candidate、最终 communication、doctor material 和每个 external action 分别绑定 target hash。FactSnapshot、source、policy、catalog、权限或确认版本变化即使旧批准失效；修订最多两轮，Safety 超时或缺少必需结果一律阻断。

验收：角色 capability 负向矩阵、Safety revision hash 失效、零/一个 Care action、急症零模型、准备阶段崩溃不可见、三角色视图原子可见均通过。

### 6. API surfaces 与公共合同

1. `/api/v1/*` 保持已有稳定 Sleep Domain API：service credential + actor JWS、bounded pagination、opaque cursor、schema version、Operation polling 和 at-least-once events。
2. `/product/sleep/*` 面向 BFF/产品用例，不暴露 Agent、prompt、tool 或 graph：
   - `GET /today`、`GET /trends`、`GET /care`、`GET /records` 读取已提交角色投影；
   - `/interactions/*` 支持 start、ask、status、answer、confirm、decline、feedback；所有修改命令异步返回 Operation。
3. `/demo/v1/*` 只支持 seed、advance、ScenarioClock、reset 和受限 trace；仅在 development/test、显式 flag、replay-only database/credential、`replay:*` namespace 四个条件同时满足时挂载，且使用独立 demo-controller principal。
4. `GET /livez` 只报告进程存活；`/internal/readyz`、`/internal/status`、迁移/依赖摘要和 reconciliation 管理面使用独立内部 surface、凭证和 OpenAPI，不挂到公网 app。
5. 旧 `/product/radar/*` 仅作为 development compatibility adapter，记录 deprecation telemetry；production 不挂载，新代码和新测试不得依赖它。
6. API DTO、application command/query、domain contract、persistence record、provider raw schema 和 Agent work product 分层；公共响应不直接序列化 ORM 或 `ProductEpisodeRunResult`。
7. 首期必须实现 ASGI receive/body limiter，在解析前限制 compressed bytes、decompressed bytes、JSON nesting/member 数和请求 deadline；生产阶段再由反向代理提供第二层限制。

验收：OpenAPI snapshot、v1 向后兼容、unknown request field、body limit、cursor resync、pending/data-insufficient 响应和生产 surface 隔离测试通过。

### 7. 身份、授权、确认与隐私

1. 本节的数据库 schema/resolver 属于 B1 foundation，必须先于任何 command route 启用。首期不接外部 IdP；PostgreSQL 保存 service principal、actor、subject、elder/family/doctor binding、grant、purpose、namespace/data-mode scope，以及 authorization/privacy/retrieval-policy epoch。`caregiver` 只作为外部 family alias，不形成第四种产品角色。
2. 有效权限固定为 `service-principal grant ∩ signed actor assertion ∩ authoritative actor/subject binding ∩ endpoint policy`，四层均绑定 namespace、data mode、purpose 和适用 epoch。保留 request-bound asymmetric JWS、nonce、method/path/body hash 和 replay protection；开发身份由 seed/migration 创建，不接受请求 header/body 自报权威角色。`/product/sleep` 由可信 BFF 生成 assertion，浏览器不持有签名私钥。
3. User-origin Operation 保存不可变 `AuthorizationSnapshot`：principal/binding ID、role、effective scopes、namespace/data mode、purpose、authorization/privacy/retrieval epochs 和 policy hash，但不保存 bearer/JWS。System-origin Operation 使用独立 `WorkloadAuthorizationSnapshot`：workload principal、namespace/data mode、subject、purpose、allowed handler、policy/各 epoch，不伪造 actor/binding/role；涉及角色发布或外部效果时再校验当前 recipient binding/confirmation。每次 claim、checkpoint、commit、external effect、事件轮询和下载都重新解析当前权威并比较相应 snapshot。
4. 确认/回答句柄是随机、不可枚举、单次使用的 opaque handle，服务端绑定 actor、subject、role、scope、target id/version/hash、FactSnapshot hash、Care/Profile state version、authorization/privacy/retrieval-policy epochs、authorization-policy hash、ControlClock expiry 和 consumed 状态。
5. 冻结唯一 action-to-role/scope/target 确认矩阵，legacy matrix 不得放宽。消费 handle 时在一个 UoW 内完成当前四层授权/epoch、target/version/hash、state CAS、single-use consume、CommandReceipt/Operation/outbox 和审计 receipt；失败整体回滚，重试返回原 receipt。
6. 采用对象级和字段级授权：老人、家属、医生读取独立投影，禁止先返回完整对象再由前端隐藏字段。
7. 首期实现 `namespace + subject + retention_domain + generation` 粒度的 DEK、wrapped KEK、key ID、有限旧 keyring、轮换兼容读和本地开发 key provider；至少区分 raw、conversation/checkpoint、personalized memory/profile、export/cache，审计使用独立假名化标识/密钥域。建立 raw、canonical、Agent checkpoint、conversation、role view、Memory/Profile、audit、cache/outbox/export 的 retention matrix；retention/shred Worker 传播删除或失效，receipt 精确列出 destroyed、expired 和 retained-with-reason。真实 KMS/HSM 与备份介质传播留到生产阶段。
8. 外部 effect 的撤权线性化点是短 UoW 中重新校验当前 epoch/recipient grant 并把 intent CAS 为 `dispatching` 的 dispatch permit。撤权保证阻止尚未取得 permit 的 effect；已经在飞的网络发送不能承诺瞬时取消，其结果进入 delivery journal/reconciliation 并在审计中明确。
9. 日志采用结构化字段 allowlist，只记录 opaque correlation ID；禁止 request/response body、健康自由文本、完整 prompt/response、认证头和真实 subject/device/provider ID。

验收：跨主体/跨角色/字段越权、撤权竞态、nonce replay、过期/重复确认、旧 target hash、日志泄露扫描和 retention/shred 流程测试通过。

### 8. 模拟闭环与 oracle 隔离

1. Runtime 只读取 strict scenario/external fact 与模拟 human event；`actions.jsonl`、`expected.json` 和 verifier oracle 不能被 server package 导入、挂载或读取。
2. Demo seed 使用 allowlist artifact family、schema/hash/count 校验和 quarantine；`evidence_kind`、`data_mode`、release eligibility 只能由服务端 replay registry 赋值。
3. B5 旅程覆盖正常晨报、数据不足、设备异常、确定性紧急风险、Evidence 追问、Care 确认/拒绝、3–7 天 follow-up、反馈重分析、三角色投影、撤权和重启恢复；B6 再加入 forget/retention/shred，才称完整旅程。
4. 模拟生成和导入作为 durable Operation 分批写入；不得在一个 HTTP request 中传输或事务性插入整套观测数组。Server runtime 镜像/容器不包含 oracle 目录；verifier 使用独立进程与只读挂载。
5. Verifier 通过公共 API/事件和已提交资源验证结果；不得调用内部 Runner 或 repository 获得特殊通道。

验收：同一场景可重复运行且业务结果确定；不同 run/arm 不共享 state、idempotency 或 cursor；所有 replay 输出有不可移除的 non-release 水印。

### 9. 可观测性、运行安全与恢复

1. 建立贯穿 HTTP request、Operation、outbox、NightEpisode revision、ProductEpisode、FactSnapshot、Agent invocation、Tool receipt、provider/model call 和 delivery 的 correlation/causation 链。
2. 指标至少包含请求延迟/错误、队列深度与最老年龄、lease reclaim、retry/dead-letter/unknown、各 Agent/模型延迟与 token/cost、schema failure、Safety approve/revise/block、urgent fast-path、授权拒绝和 provider timeout；PHI/subject 不作为 metric label。
3. `/livez`、内部 `/readyz`、受保护 `/status` 分离。Readiness 校验数据库、schema、依赖 manifest、签名/加密 key provider，以及当前 process-role 必需的 model/provider 配置；API readiness 不要求或加载 Worker-only model/provider。Worker readiness 还校验 heartbeat/claim 能力。
4. API 和 Worker 支持 graceful shutdown：先停止 claim，再 cancel/drain 有界 in-flight。只有 handler 已确认结束并原子 bump fence 后才主动 release lease；仍在飞或取消结果未知的调用保留 lease 至自然过期，并由 invocation journal 进入 reconciliation。崩溃恢复不依赖内存 callback。
5. 审计记录与运营 telemetry 分离；审计 append-only，包含 actor/subject 的受控标识、authorization epoch、版本/hash、reason code 和 commit/delivery receipt。

验收：重启后内存指标丢失不影响业务恢复；通过 trace/correlation ID 能解释一份结果的事实、Agent、确认和提交链，同时常规日志不泄露原始健康内容。

### 10. 验证、文档与交付门禁

1. 单元测试：contracts、upcasters、日期/DST、source ontology、role capability、policy、idempotency key 和错误分类。
2. PostgreSQL 集成测试：全量 migration、升级路径、UoW rollback、FK/unique/CAS、并发 claim、fencing、outbox、authorization revocation。
3. ASGI 测试：使用真实 lifespan，不再依赖绕过 startup/shutdown 的全局 TestClient 替身；覆盖流式/轮询、并发和进程关闭。
4. 故障注入：在 prepare/commit、outbox claim、sink return、journal CAS、induction enqueue 前后 kill Worker，验证 at-least-once 与幂等效果。
5. 合同测试：OpenAPI snapshot、reference client、schema compatibility、错误 envelope、分页/cursor、role projection 和 demo/production surface 差异。
6. E2E：以 PostgreSQL、API 进程、Worker 进程运行完整模拟旅程；重启 API/Worker 后从 Operation 恢复，verifier 仅在服务端外读取 oracle。
7. 交付文档：模块所有权、进程入口、环境变量、migration/runbook、worker queue/retry/reconciliation、数据字典、权限矩阵、日期策略、开发 compose 和故障排查。

标准 proof commands 使用 committed `.env.test.example` 复制出的本地 `.env.test` 和固定 `COMPOSE_PROJECT_NAME=sleepagent_backend_test`；不得在命令行打印真实凭证。实现可增加门禁，但不可省略以下等价命令：

```text
python -m pip install --require-hashes -r requirements/dev.lock
python -m pip install --no-deps .
python -m pytest -q -p no:cacheprovider -m unit
COMPOSE_PROJECT_NAME=sleepagent_backend_test docker compose --env-file .env.test up -d --wait postgres
COMPOSE_PROJECT_NAME=sleepagent_backend_test docker compose --env-file .env.test run --rm migrate apply
COMPOSE_PROJECT_NAME=sleepagent_backend_test docker compose --env-file .env.test run --rm migrate check
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python -m pytest -q -p no:cacheprovider -m postgres
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python -m pytest -q -p no:cacheprovider -m asgi_lifespan
COMPOSE_PROJECT_NAME=sleepagent_backend_test docker compose --env-file .env.test up -d --wait api worker
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python -m pytest -q -p no:cacheprovider -m e2e
SLEEPAGENT_SETTINGS_PROFILE=test-postgres sleepagent-demo verify backend --model deterministic
python -m pytest -q -p no:cacheprovider
```

`test-postgres` profile 从 `.env.test` 读取 compose 暴露的测试 DSN；测试输出必须脱敏。Pytest markers 在 `pyproject.toml` 注册为 `unit/postgres/asgi_lifespan/e2e`。PostgreSQL integration 使用每次测试独立 database/schema 或事务外清理 fixture，不共享 replay run；real-lifespan fixture 不经过当前全局轻量 TestClient monkeypatch；E2E 必须启动真实 API/Worker 进程并等待 healthcheck。B0 明确修复 governed-memory 旧 handle 未失效缺陷，并更新或版本化 10→22 concepts 的两个过期 acceptance artifact，不能用 skip/xfail 掩盖。

Docker build 同样执行“`production.lock --require-hashes` 安装第三方依赖 → 构建/安装本项目 wheel `--no-deps`”，不得用普通 `pip install .` 重新解析宽范围依赖。`sleepagent-demo` 必须在 `pyproject.toml` 注册到 `sleepagent.demo_cli:main`，并在 clean environment proof 中验证 `sleepagent-demo --help`。

最终门禁：全量测试绿色；无新 legacy 依赖；PostgreSQL restart/Worker crash 后旅程可恢复；生产配置 fail-closed；依赖 manifest、OpenAPI、migration 和审计链可复现。

## Key decisions & tradeoffs

1. **首期范围**：模拟数据、生产形态闭环；真实设备、真实用户上线随后进行。代价是本期不证明真实 provider 稳定性，但避免把接入不确定性混入基础架构。
2. **模块化单体**：一个仓库和领域模型，API/Worker 分进程；暂不拆微服务。牺牲独立服务自治，换取事务一致性和更低运维复杂度。
3. **PostgreSQL DB-backed queue/outbox**：首期不引入 Redis/Celery/Kafka。吞吐上限较低，但可直接复用现有 Operation/outbox，并减少双重权威。
4. **同步 psycopg + 显式 UoW**：不做全量 async/ORM 重写。短期最贴合现有同步领域代码；代价是 API 必须严格隔离长任务，未来高并发时再评估 async read path。
5. **一个 Worker 程序、多个 handler/queue**：开发可单进程，后续按队列扩容。避免首期制造多个服务，同时保留隔离慢 Agent 和高优先 fast path 的能力。
6. **醒来日归属**：产品 canonical date 使用本地醒来日；另存 bed/wake date 与 policy version。需要迁移现有模拟 fixture，但消除产品与 Habit Profile 的长期歧义。
7. **全局 UUIDv7 + 显式 scope**：新 ID 以 UUIDv7 string 写入现有 TEXT PK，旧 ID 保持 opaque/readable，不做危险全库回填；namespace/data mode 仍参与授权、唯一约束和幂等，不能仅凭 UUID 读取对象。
8. **数据库身份权威**：首期不接 IdP，但不使用可伪造的 Header/env 固定身份。后续 IdP 只替换 adapter，不改变 actor/subject/role 领域合同。
9. **渐进收敛现有持久层**：先建立新 UoW/application path，再迁移旧调用，不一次性重写 3000+ 行 store/runner。兼容期会短暂存在旧路径，但生产依赖图禁止使用它。
10. **真实 KMS 延后、治理接口先行**：本期实现 key provider、envelope metadata、轮换与 shred 语义，使用本地非生产 key；正式托管密钥是下一阶段上线门禁。

## Risks / open questions

1. 当前工作树未冻结且基线测试非绿色；在用户确认基线前不得开始结构性迁移。
2. 现有迁移与新 UUID/date/schema 字段的升级成本需在 Phase 2 做数据库演练；模拟数据允许再生成，但不可默认删除任何已有真实或人工标注材料。
3. PostgreSQL 队列的容量目标尚无真实负载数据；首期通过可配置 batch/concurrency、队列最老年龄和压测建立升级阈值，达到阈值后才评估专用 broker。
4. 真实 KMS、外部 IdP、provider SLA、备份 RPO/RTO 和多区域策略仍是生产上线前的独立决策，不影响首期接口边界。
5. UUIDv7 生成实现必须固定并做跨进程/时钟回拨测试；算法语义、TEXT 存储和仅约束新写入已锁定，具体依赖在依赖锁定步骤选择并记录。

## Out of scope

- 真实 Perceptor/雷达设备接入、真实用户迁移和临床验证。
- 外部 IdP、托管 KMS/HSM、生产域名/TLS/WAF、正式消息或通知渠道。
- 多区域、高可用、自动故障转移、完整备份/PITR 与生产 RPO/RTO 承诺。
- Kafka、Redis、Celery 或服务拆分；除非首期压测证明确有需要，不提前引入。
- 移动端/前端重构，以及产品信息架构之外的新功能。
- 诊断、用药建议、PSG 替代能力或超出审定 Care catalog 的行动。
- 高频波形/大对象存储架构；首期只保存当前模拟闭环需要的规范化观测与受控 raw payload。
- 清理或删除旧代码、fixture、归档和用户未确认的工作树变更；legacy 删除需另行批准。
