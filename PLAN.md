# SleepAgent Repository Simplification & Dead Code Removal PLAN

状态：待批准、尚未实施

审计基线：分支 agent/repository-structure-cleanup，提交 2cbf5b7

审计日期：2026-08-12

计划性质：删除优先的冻结执行规格，不是新的架构愿景

## 0. 本轮边界与执行决定

本轮只新增这份计划。没有修改生产源码、测试、文档、配置或迁移，没有删除文件，也没有提交。审计开始前工作树已有一项用户改动：

- D .agents/skills/sleepagent-codebase-curator/SKILL.md

实施时必须把它视为用户所有的既有删除，不得恢复或覆盖；本计划会删除同一 .agents 技能目录中剩余的 openai.yaml，使仓库最终不再携带这套本地施工技能。

计划只优化当前真实运行链，不保留“也许以后会用”的第二实现。已经作出以下决定：

1. PostgreSQL 是唯一服务器持久化路径；RadarPersistenceStore、SQLite schema、旧 SleepApiRuntime 和进程内旧 Worker 全部退役。
2. backend.main:app 当前是唯一 ASGI composition root；最终移动为 sleepagent.app:app，不保留旧 import shim。
3. Product Agent 当前真实外部契约冻结为 PostgreSQL product_agent 队列产生的 MORNING_REVIEW 三角色投影；四个 Agent、确定性安全门、当前 Tool dispatch 和 durable commit 语义必须保留。
4. 仅因 runtime_factory eager composition 而可达、但 canonical Product worker 从不构造输入的 Habit questionnaire/profile write、Runner continuation、Runner external action 和旧 in-memory induction sidecar，不算当前运行功能；先做 characterization，再删除这些分支及其专用测试。
5. real-perceptor-acceptance console script 在仓内没有部署 consumer，且本身不调用 Perceptor，只读取旧仓储；本计划默认删除它。若实施前出现新的仓外 owner 证据，只允许把审计器重写为 canonical PostgreSQL/UoW 只读查询，仍不得保留旧 Runtime/SQLite 链。
6. Perceptor push/pull/webhook 未装配到 canonical API 或 Worker；整套删除。未来 live ingestion 若需要，应作为独立产品功能重新设计，不以兼容 shim 复活旧链。
7. 当前 frontend 只调用 diagnostic backend 路由，而 canonical backend 没有这些路由；整套删除。未来 UI 是新功能，不在本次简化中伪造适配层。
8. migrations 001–007、migration_manifest.json、ledger identity 和 checksum 原样保留，不 squash、不改名。历史 SQL 文件名、已安装数据库函数和已持久化 version/event identifier 中的 Stage 字样是不可变数据合同的明确例外；最终生产 Python 模块路径、Python 符号、测试文件和脚本不得保留 Stage2/3/4/5 施工命名。不可变字符串集中到 migration/release metadata，业务代码不得继续散落施工词。
9. 不新增 ports、facade、adapter、factory、contracts 微模块。所有目标文件由现有文件 git mv 后合并形成；除本 PLAN.md 外，实施的语义新增文件预算为 0。

## 1. 审计方法与可复算基线

### 1.1 使用的证据

审计交叉使用了：

- git ls-files、git status、git grep 与 rg；
- Python AST import、测试函数和 consumer 分析；
- pyproject.toml console scripts 与 package data；
- compose.yaml 的实际 command、entrypoint、profile、queue 环境；
- FastAPI app factory 和真实 import probe；
- Worker 显式 queue registry、lazy imports 与 handler builder；
- ProductAgentProcessor 到 ProductEpisodeRunner 的实际调用；
- migration manifest、SQL 文件、CLI 与 runtime attestation；
- demo seed registry、packaged fixtures 和 CLI allowlist；
- 前端入口、BFF proxy allowlist 与 canonical OpenAPI 路径；
- pytest collection、全量测试基线、Compose 配置与前端 typecheck。

这里的“可达”不等于“被 import”。Python 会先执行 package 的 __init__.py；当前大量 eager re-export 把 legacy 模块拉入 sys.modules。分类以实际构造和方法调用为准，并单独记录 import-time 污染。

### 1.2 当前统计

统计口径固定为 HEAD 2cbf5b7 的 tracked 文件；当前未跟踪的 PLAN.md 不计入基线。

| 指标 | 当前值 | 可复算口径 |
| --- | ---: | --- |
| tracked files | 499 | git ls-files |
| production Python files | 173 | backend/**/*.py + sleepagent/**/*.py |
| production Python LOC | 111,048 | 上述文件 wc -l |
| test Python files | 145 | tests/**/*.py |
| tests tracked files | 169 | tests/** |
| test Python LOC | 59,015 | tests/**/*.py |
| collected test cases | 1,257 | pytest --collect-only |
| all tracked Python files | 322 | git ls-files '*.py' |
| all tracked Python LOC | 170,906 | 全部 tracked Python |
| docs files | 80 | docs/** |
| docs lines | 39,440 | docs/** |
| frontend tracked files | 31 | frontend/** |
| root tracked directories | 10 | 含 .agents；可见目录 9 |
| sleepagent first-level package directories | 8 | integrations、persistence、product_api、product_device、product_runtime、simulation、sleep_api、sleep_domain |

测试基线：

- pytest 收集 1,257 cases。
- sandbox 内全量结果为 1,234 passed、15 skipped、8 failed；8 个失败均是测试创建 loopback socket 时的 PermissionError。
- 将相关 socket cases 在允许网络命名空间的环境重跑后为 9 passed。
- 当前 suite 大量 integration/e2e 文件没有 postgres/e2e marker；tests/conftest.py 会把未显式标记的测试统一当 unit。因此“文件夹叫 integration”不能作为真实 PostgreSQL 证明。
- 尚未把所有 PostgreSQL、进程 kill/reclaim 和迁移升级路径在本轮环境完整跑通；这些是实施验收，不可用当前 skipped 结果代替。

## 2. 当前真实运行图

### 2.1 进程入口

    compose postgres
      ├─ migrate
      │    └─ python -m sleepagent.persistence.migrate apply
      ├─ test-bootstrap
      │    └─ python -m sleepagent.persistence.test_bootstrap
      ├─ api / demo-api / internal-api
      │    └─ uvicorn backend.main:app
      └─ worker
           └─ python -m sleepagent.worker_runtime run

    pyproject console scripts
      ├─ sleepagent-migrate → sleepagent.persistence.migrate:main
      ├─ sleepagent-demo → sleepagent.demo_cli:main
      └─ real-perceptor-acceptance → legacy audit-only chain；计划删除

compose.yaml 使用 test-postgres、replay data mode 和 development image。它是当前 replay/PostgreSQL 集成 harness，不是已经证明可用于 live production 的部署描述。最终文档必须明确这一点。

docker/Dockerfile 的 development 与 production targets 当前也默认 backend.main:app；因此 app path 的移动必须同时更新两个 target，而不只改 compose。

### 2.2 Canonical API graph

    backend.main
      → SleepBackendSettings.from_environment
      → build_backend_runtime
          → psycopg pool
          → UnitOfWorkFactory
          → migration release attestation
      → build_api_runtime_services
          ├─ public_v1
          │    → build_postgres_sleep_api_runtime
          │    → PostgresSleepApiRuntime
          ├─ product
          │    → ProductApiService
          │    → PostgresProductBackend
          │    → PostgreSQL UoW
          ├─ demo
          │    → DurableDemoController
          │    → PostgresDemoStore
          └─ internal
               → PostgresInternalStatus
      → create_sleep_backend_app
          → /api/v1/*
          → /product/sleep/*
          → /demo/v1/*
          → /internal/*

canonical app 没有挂载 product_api/diagnostics、Radar task API、Perceptor webhook 或 tests/support/diagnostic_app。

### 2.3 Canonical Worker graph

Worker 不使用动态插件发现。worker_runtime._cli_handlers 以显式 dict lazy import 六组 builder，检查 queue 重复和 unsupported queue 后才启动：

| Queue | 当前 handler 来源 | 实际状态 |
| --- | --- | --- |
| ingestion | sleep_domain.worker_adapters + postgres_slice | replay normalization，current |
| fast_path | sleep_domain.worker_adapters + postgres_slice | deterministic fast path，current |
| product_agent | product_runtime.postgres_worker | 四 Agent 三角色投影，current |
| sleep_command | stage2_worker | 当前 replay command |
| product_interaction | stage2_worker | 当前 replay interaction |
| demo_advance | stage3_worker | ScenarioClock/demo，current |
| replay_journey | simulation.journey_worker | canonical replay root，current |
| induction | stage4_worker | replay proof/current optional queue |
| delivery | stage4_worker | deterministic replay sink；不是 live delivery |
| reconciliation | stage4_worker + stage3 date handler | current |
| retention | stage5_worker | replay bounded retention |
| demo_reset | stage5_worker | replay demo reset |

.env.test.example 的默认队列是 ingestion、fast_path、product_agent、sleep_command、product_interaction、demo_advance、replay_journey、reconciliation；proof scripts 额外打开 induction、delivery、retention、demo_reset。Stage 文件不能因默认 compose 没启用全部 queue 而删除；其当前逻辑要按业务职责改名和归位。

### 2.4 Canonical 四 Agent graph

    worker_runtime
      → build_product_agent_worker_handlers
      → ProductAgentWorkHandlerAdapter
      → ProductAgentProcessor
          ├─ load exact PostgreSQL source/fence
          ├─ for elder/family/doctor
          │    → ProductEpisodeRunner.run(MORNING_REVIEW)
          │         ├─ SleepCareAgent
          │         ├─ EvidenceReasoningAgent
          │         ├─ CareStrategyAgent
          │         └─ SafetyReviewAgent，按风险条件调用
          ├─ persist prepared artifact
          └─ fenced PostgreSQL commit

ProductAgentProcessor.prepare 是 canonical PostgreSQL worker 对 ProductEpisodeRunner 的真实调用点。runtime_factory 的 live builder明确使用 persistence_store=None；durable authority 是 PostgresProductAgentRepository，不是 product_persistence.py、habit_persistence.py 或 RadarPersistenceStore。

当前 product Agent data mode 仍被 builder 强制为 replay；live 仅表示 LLM provider 可为 live。backend settings 对 production profile 又要求 live data。最终文档不得宣称仓库已有可工作的 live ingestion/product slow path。

### 2.5 Registry/factory 审计

- Worker registry 是显式 queue→handler dict，无 importlib、entry_points 或插件扫描。
- 四 Agent roster、Agent/tool allowlist 和 skill manifest 是静态 MappingProxy/registry；ToolRequest 会动态按注册名 dispatch，因此已注册且有 handler 的当前 tool 不能只凭文本 import 次数删除。
- simulation.__getattr__ 是包级 lazy re-export，不是插件边界；最终改为 consumer 直接 import 并最小化 package init。
- product_runtime、product_device、sleep_api、sleep_domain、persistence 的 package init 当前 eager re-export，是 import 污染源，不是公共兼容需求。

### 2.6 当前前端与 reference client

当前浏览器入口是：

    frontend/app/page.tsx
      → RadarWorkspace
      → DynamicRadarWorkspace

frontend/app/radar/page.tsx 是未链接的重复入口；RadarWorkspace 中除三行转发外还保留一套不可达的 LegacyFixedRadarWorkspace。BFF route 只转发 /product/radar/*、/radar-agent/*、/product/habit-profile/*，而 canonical OpenAPI 的 Product surface 只有 /product/sleep/*。这些旧路由只存在于 tests/support/diagnostic_app.py，因此 frontend 对真实 backend 会得到 404。

reference_client/sleep_api_v1_client.py 则只使用 canonical /api/v1 contract，不 import server package；它有独立 wheel/package consumer 和 contract tests，是应保留的真实外部边界。

## 3. Reachability 分类定义

| 类型 | 判定 |
| --- | --- |
| production reachable | compose/console 的 canonical API、Worker、migration 在正常配置下会构造并执行 |
| current demo/replay reachable | demo API/CLI、replay journey、registered fixtures 或 proof queues 会执行 |
| test-only | 只有 tests、tests/support 或断链 frontend 消费 |
| migration/compatibility only | 只为旧数据路径、旧 console 或历史迁移服务 |
| dead/unreachable | 仓内无有效 consumer，或唯一 consumer 本身也被退役 |
| uncertain | 静态证据无法决定；必须用外部契约和 characterization 关门，不能默认保留 |

本计划中的 uncertain 只有两类，并已给出执行决策：

- 仓外是否有人调用 real-perceptor-acceptance：仓内判定删除；若出现 owner 证据则重写为 canonical UoW，旧链仍删除。
- Habit/continuation 是否是当前产品契约：canonical API/worker 证据表明不是；冻结 MORNING_REVIEW 输出后删除 dormant 分支。

## 4. KEEP / MERGE / MOVE / DELETE 总矩阵

| 路径 | 当前职责 | 当前真实 consumer | 类型 | 动作 | 原因与目标 |
| --- | --- | --- | --- | --- | --- |
| backend/ | 17 行 ASGI root 与 package marker | compose、Docker、OpenAPI generator | production reachable | MOVE+MERGE | 移入 sleepagent/app.py；更新全部入口，不留 backend shim |
| sleepagent/backend_app.py | FastAPI factory、middleware、surface mount | backend.main | production reachable | MERGE | 与 backend_services 和 main 合为 app.py，保留一处 ASGI composition |
| sleepagent/backend_runtime.py | pool/UoW/schema attestation/process lifecycle | API、Worker | production reachable | MOVE+REDUCE | git mv 为 sleepagent/process.py；共享进程层不得 import FastAPI/app |
| sleepagent/backend_services.py | API service composition | backend_runtime API lazy path | production reachable | MERGE | 合入 app.py，避免第二 composition 层 |
| sleepagent/backend_settings.py | profile/process/data/queue config | 所有进程 | production reachable | MOVE+MERGE | 与 backend_keys.py 合为 sleepagent/config.py |
| sleepagent/backend_keys.py | key reference 与 test key policy | API/Worker/retention | production reachable | MERGE | 配置和 key resolution 同属进程配置边界 |
| sleepagent/observability.py | log/metrics/correlation helpers | app、worker | production reachable | KEEP+MOVE | 保留为 sleepagent/observability.py，删除无 consumer helper |
| sleepagent/backend_persistence.py | authority、replay guard、Product PostgreSQL reads/commands | product/public API | production reachable | MOVE+REDUCE | 移到 sleepagent/api/postgres.py；不是 dead，去掉 legacy import |
| sleepagent/demo_api.py + demo_persistence.py | demo transport/controller/store | demo surface | current demo/replay | MERGE+MOVE | 合为 api/demo.py，API 与唯一 controller 紧密耦合 |
| sleepagent/demo_cli.py | HTTP-only demo 与 backend verifier | console、README、proof scripts | current demo/replay | MOVE+REDUCE | 移到 simulation/cli.py；保留 show/seed/advance/trace/reset 与 business-named verify suites，删 Stage flags/重复 orchestration |
| sleepagent/worker_runtime.py | durable lease/fence/heartbeat/dispatcher/CLI | compose worker | production reachable | MOVE | workers/runtime.py；不与业务 handler 巨型合并 |
| sleepagent/stage2_worker.py | sleep command、product interaction | 默认 worker queues | production reachable | MOVE+RENAME | workers/commands.py；Python类/错误/局部变量改业务名；已持久化 schema/event/DB identifiers原值不变 |
| sleepagent/stage3_worker.py | demo advance、episode-date reconciliation | demo queue；date handler 被 stage4 调用 | demo + production reachable | MERGE+MOVE | demo advance 进 workers/demo.py；date reconciliation 进 workers/effects.py |
| sleepagent/stage4_worker.py | induction、replay delivery、reconciliation | optional proof/default reconciliation | current replay reachable | MOVE+RENAME | workers/effects.py；明确 replay sink，不宣称 live effect |
| sleepagent/stage5_worker.py | retention、subject forget、demo reset | proof queues、demo reset reservations | current replay reachable | MERGE+MOVE | retention/forget 进 workers/retention.py；reset 进 workers/demo.py |
| sleepagent/retention.py | retention key/policy coordination | ingestion、stage3/5、journey | current replay reachable | MERGE+MOVE | 与 stage5 retention 合为 workers/retention.py |
| sleepagent/persistence/migrate.py | manifest-pinned PostgreSQL CLI/bootstrap | compose、console | production reachable | KEEP+REDUCE | 保留 migration runner；删除旧 store convenience imports |
| persistence/migrations.py + manifest + migrations/001–007 | release metadata、checksums、历史数据库/payload identifiers、SQL history | settings/runtime/migrate/worker | production + migration | KEEP+REDUCE | 只删 SQLite converter；SQL、persisted identifiers 与 manifest identity 原样保留并集中 |
| persistence/uow.py | scoped PostgreSQL UoW | API、Worker、demo | production reachable | KEEP | 真正跨进程数据库边界 |
| persistence/test_bootstrap.py | replay test roles/bootstrap | compose test-bootstrap | current replay | KEEP | 名称明确是测试 harness，不伪装 production |
| persistence/store.py、models.py、vector_store.py、sqlite_schema.sql | Radar store/SQLite legacy substrate | 旧 runtime、diagnostics、旧 tests | compatibility/test-only | DELETE | PostgreSQL UoW/worker 是替代；同步删 package data/tests |
| sleep_domain/postgres_slice.py | domain policy、ingress handlers、PostgreSQL repository 混合 | canonical ingestion/fast_path | production reachable | SPLIT BY MOVE+MERGE | pure rules 进 domain/episodes.py/fast_path.py；SQL/handlers 与 worker_adapters 合入 workers/ingestion.py |
| sleep_domain/worker_adapters.py | queue adapters/UoW bridge | worker registry | production reachable | MERGE | 合入 workers/ingestion.py，避免 374 行薄桥 |
| sleep_domain/contracts.py、episode_v2.py、product_data.py、ontology.py、schema_versions.py | current canonical DTO/domain rules | ingress、Product worker、API | production reachable | KEEP+MERGE+MOVE | 收口为 domain/contracts.py、episodes.py、product_data.py；小 schema helpers 合入相邻文件 |
| sleep_domain/fast_path.py | 纯计算与 SleepDomainRepository 服务混合 | postgres_slice 调用 | production + legacy mixed | REDUCE+MOVE | 保留纯质量/风险/alert 决策，改为显式输入输出；删除旧 repository service/lease orchestration |
| SleepDomainRepository 及 repository.py | 6,998 LOC 旧 DB-API facade | 旧 Perceptor/Sleep API/tests；current 仅借 3 个 record 类型 | compatibility only | DELETE | 先把 DomainNamespace、CurrentRevisionPointer、SubjectLifecycleLease 移到 current contracts |
| sleep_domain/agent_bridge.py、authority_migration.py、care_followup.py、crypto.py、device_binding.py、episodes.py、ingestion.py、legacy_migration.py、lifecycle.py、radar_compat.py、registry.py | 旧 Radar/SQLite/adapter/runtime domain | 旧链、package re-export、tests | compatibility/test-only | DELETE | current postgres slice/worker/domain contracts 替代；纯规则若仍用则先内联到 retained domain 文件 |
| sleep_api/router.py + postgres_runtime.py + auth.py + contracts.py | canonical /api/v1 transport/Postgres adapter | backend app | production reachable | MOVE+MERGE | 收口为 api/public.py + api/public_contracts.py |
| sleep_api/service.py | Error/Cursor + 旧 SleepApiRuntime + thread worker | current 只需前两个小符号 | mixed | MERGE SMALL CORE THEN DELETE | Error 合入 public transport/auth，cursor 合入 Postgres runtime；其余删除 |
| sleep_api/runtime.py、persistence.py、events.py、compatibility.py | 旧 env composition/SQLite/API event store/小 shim | acceptance CLI、legacy tests | compatibility/test-only | DELETE/MERGE | prefix/header 小常量合入 public.py；其余无 canonical consumer |
| product_api/contracts.py、router.py、service.py | canonical /product/sleep DTO/transport/application | backend app | production reachable | MERGE+MOVE | 合为 api/product.py；只有一个实现，无需 Protocol 文件分层 |
| product_api/diagnostics/** | 3,102 LOC 第二套 diagnostic routes | tests/support/diagnostic_app | test-only | DELETE | canonical app 不挂载；删除完整诊断测试簇 |
| product_runtime/postgres_worker.py | Product Agent processor/repository/handler | worker product_agent | production reachable | MOVE+REDUCE | workers/product.py；保留 exact source、三角色 run、fenced commit |
| product_runtime/runner.py、episode.py | 四 Agent orchestration/lifecycle | Product processor | production reachable | KEEP+MOVE+REDUCE | 移入 runtime/；去 dormant Habit/continuation/external branches |
| product_runtime/runtime_factory.py | canonical graph + Radar/SQLite/persistent compatibility | Product worker；旧 diagnostics/tests | mixed | MOVE+REDUCE | runtime/factory.py 只保留 deterministic/live worker builders；删 env legacy facade |
| product_runtime/contracts.py、runtime_contracts.py、runtime_ports.py、schemas/canonical.py | current contracts + 微型 ports/schema | runtime/worker/tools | production reachable | MERGE | 合成 runtime/contracts.py；只有真实多实现边界才保留 Protocol |
| product_runtime/agents/** | 四 Agent、roster、manifest/ports | runner/factory | production reachable | MERGE | 合为 runtime/agents.py，保留 exactly-four invariant |
| invocation.py + agent_invocation_coordinator.py | model invocation/context/budget | runner | production reachable | MERGE | runtime/invocation.py |
| tooling.py + tool_execution_coordinator.py + services/runtime_capabilities.py | tool registry execution/read capability | runner/factory/dynamic ToolRequest | production reachable | MERGE | runtime/tooling.py |
| registry.py + skills.py | tool/skill/version registry | factory/model/tool dispatch | production reachable | MERGE | runtime/registry.py；先用 registered-handler coverage 防误删 |
| tools/**、policies/** | 当前 tool handlers 与 deterministic policies | dynamic registry/runner | production reachable | MERGE BY COHESION | 分别合为 runtime/tools.py、runtime/policies.py，删 re-export init |
| knowledge/** + services/reviewed_knowledge.py | reviewed knowledge/seed/grounding | tool registry | production reachable | MERGE | runtime/knowledge.py |
| reports/** | artifact rendering + 48 行 roles | rendering tool/tests | production reachable | MERGE | runtime/reports.py |
| governance.py、hitl.py、confirmed_action_coordinator.py、external_actions.py | safety/HDS/confirmation/external effect 混合 | runner；部分仅 dormant branches | mixed | KEEP CORE+MERGE+DELETE BRANCHES | current deterministic safety/confirmation 合为 governance.py + actions.py；删无 external consumer 的 executor/continuation |
| publication_service.py + episode_result_finalizer.py | terminal result/publication | runner | production reachable | MERGE | runtime/results.py，不塞回 2K runner |
| longitudinal_memory.py、cold_start.py | current model-input guards + 大量 sidecar/scheduler/diagnostic | factory/runner eager path | mixed | REDUCE+MERGE | characterization 后仅保留 prepublication/model-input guards 到 runtime/memory.py/contracts/governance，删 scheduler/storage sidecar |
| acceptance.py、acceptance_materials.py | release evidence/material generator | tests/docs only | test-only | DELETE | canonical worker/runner tests 替代 |
| task_runtime/** | 旧 Radar task runtime/trace | old store/diagnostics/tests | dead after diagnostic removal | DELETE | durable worker runtime + ProductAgentProcessor 替代 |
| product_persistence.py、habit_persistence.py | Radar store persistent runtime authority | legacy runtime_factory/tests | compatibility only | DELETE | canonical durable authority为 PostgreSQL worker |
| habit_api.py、confirmation/** | compatibility facade/matrix | diagnostic/tests | test-only | DELETE | canonical product commands + deterministic gates 替代 |
| Habit application/profile/runtime/online_reasoning/questionnaire/** | dormant Habit execution platform | eager factory/runner；canonical worker input从不设置 Habit fields | uncertain resolved to non-current | DELETE AFTER CHARACTERIZATION | 冻结 MORNING_REVIEW 外部输出后删除；数据库历史不删 |
| product_device/** | diagnostic device/provider/schema/quality + LLM wrapper | diagnostics/Perceptor/tests；LLM wrapper被 current worker用 | mixed | MOVE LLM CORE THEN DELETE DIRECTORY | LLM transport 合入 runtime/provider.py；current Product facts来自 sleep_domain.product_data |
| integrations/llm/** | Cloud client + ModelRouter/task audit/test faults | product_device LLM、tests | mixed | MERGE CURRENT TRANSPORT THEN DELETE | HTTP retry/schema/env逻辑合入 runtime/provider.py；删旧 ModelRouter/audit/fault injector |
| integrations/perceptor/** | push/pull/webhook/client + legacy acceptance | 无 canonical API/Worker consumer | compatibility/test-only | DELETE | 同步删 console、tests、docs；未来 live connector另立项目需求 |
| simulation/contracts.py、generator.py、replay_ingress.py、seed_registry.py、journey_worker.py、registry JSON | canonical 8-seed replay | demo/migrate/worker | current demo/replay | KEEP+MOVE/REDUCE | 保留 registry-backed replay；direct imports，最小 init |
| simulation/replay/** | 旧 snake_case diagnostic catalog | diagnostic provider/frontend tests | test-only | DELETE | canonical seed registry 替代 |
| 8 个 registry-backed scenario.json | canonical seed artifacts | migrate allowlist/demo/journey | current demo/replay | KEEP | registry 校验并写入全部 8 个，不能只保留 CLI 展示的 3 个 |
| golden-15-night、memory-forget、urgent-human-text fixtures | 未注册旧 verifier scenarios | tests/support verifier | test-only | DELETE | 无 canonical seed entry |
| frontend/** + docker/frontend.Dockerfile | Next diagnostic UI/BFF | 只调用旧 diagnostic routes | test-only/disconnected | DELETE | canonical API 无对应路径；没有当前 UI 替代，未来重写另开范围 |
| docker/Dockerfile | backend image | compose | current replay | MOVE | git mv 到根 Dockerfile，更新 compose |
| compose.yaml | PostgreSQL/replay API/demo/internal/worker harness | 开发与集成验证 | current demo/replay | KEEP+REDUCE | 更新 app/worker/Dockerfile 路径；删除退役 env；明确不是 live production manifest |
| pyproject.toml | build、依赖、console、package data、pytest/mypy | wheel/CLI/tests | production tooling | KEEP+REDUCE | 移除 backend package、acceptance console、SQLite/旧 replay package-data；更新 demo入口 |
| .env.example | 主要是 Radar/SQLite/frontend/Perceptor/Habit/external-action旧变量；夹有 current LLM变量 | README legacy diagnostics | compatibility-heavy | MIGRATE CURRENT KEYS THEN DELETE | 将 DEEPSEEK_API_KEY、SLEEPAGENT_PRODUCT_LLM_MODEL、BASE_URL、TIMEOUT_SECONDS、MAX_TOKENS 的说明迁入 operations.md 和 .env.test.example 注释；其余旧键删除 |
| .env.test.example | canonical PostgreSQL replay profile | compose/tests | current demo/replay | KEEP+PRUNE | 删除重复/失效键，保留真实 settings和明确 queue |
| .gitignore + .dockerignore | 本地/镜像忽略规则 | Git/Docker tooling | tooling | KEEP+MERGE DUPLICATES | 删除 frontend/Radar/旧 env规则，保留 secrets、Python build/cache与实际数据产物 |
| README.md | 当前产品/开发入口，但混有 frontend/legacy说法 | 开发者/package metadata | mixed documentation | KEEP+REWRITE | 只描述 final tree、canonical replay/backend和真实验证 |
| requirements/** | hash-locked production/dev依赖 | Docker/dev install | production tooling | KEEP | 若依赖因删除代码可减少则用现有 pip-tools流程重锁；不得手改 hash |
| tests/** | 旧架构和当前行为混合 | pytest | mixed | KEEP/MERGE/DELETE | 收口到不超过 65 个 Python 文件，详见第 9 节 |
| docs/** | current说明 + 大量历史 audit/evidence | 开发者 | mixed | MOVE/MERGE/DELETE | 最终不超过 8 个文件，详见第 10 节 |
| scripts/** | OpenAPI generator、4 个 Stage verifier、fault probe | dev/proof/tests | mixed | KEEP/MERGE/MOVE | 4 shell 合为 scripts/verify_backend.sh；probe 移 tests/support；保留 generator |
| reference_client/** | server-independent /api/v1 client | package、contract tests、外部用户 | production companion | KEEP | 独立边界真实，不导入 server；只更新文档/contract snapshot |
| .agents/** | 本地施工 skill 元数据 | 无 runtime consumer | dead repository metadata | DELETE | Git 已保存；尊重现有用户删除 |

## 5. 高风险 class/function 逐项裁决

| 符号/区域 | 属于哪一层 | Consumer 结论 | 执行动作 |
| --- | --- | --- | --- |
| WorkDisposition、LeaseClaim、WorkContext、InvocationDispatcher | Worker contract/runtime | 所有 queue handler current | 移 workers/runtime.py，保留 |
| PostgresDurableWorkStore | PostgreSQL Worker adapter | compose worker current | 保留；删除与旧 store 无关的兼容入口 |
| DurableWorkerRuntime、_Heartbeat、run_worker_command | Worker runtime/CLI | compose current | 保留；中文注释解释 lease/fence/heartbeat 原因 |
| _cli_handlers | Worker composition | 唯一显式 queue registry | 改 direct imports 到业务命名文件；保留 overlap/unsupported fail-closed |
| Stage2CommandProcessor、Stage2WorkHandler | Application/Worker/PostgreSQL mixed | 默认 queues current | 改名 CommandProcessor/CommandWorkHandler；移 commands.py |
| Stage3AdvanceHandler | Demo/Worker | demo_advance current | 改名 DemoAdvanceHandler；移 demo.py |
| EpisodeDateReconciliationHandler | Reconciliation Worker | 被 stage4 router current | 移 effects.py；消除跨 Stage import |
| InductionWorkHandler、ReplayDeliveryWorkHandler、DeliveryReconciliationHandler | replay effect Worker | proof/current queues | 移 effects.py；ReplayDelivery 名称保留 replay 限定 |
| RawRetentionWorkHandler、SubjectForgetRetentionHandler、DemoResetWorkHandler | retention/demo Worker | proof/demo current | 前两者进 retention.py，reset 进 demo.py |
| PostgresAuthorityStore、PostgresAssertionReplayStore、PostgresProductIdentityResolver | PostgreSQL/API auth | public/product API current | 移 api/postgres.py，保留 |
| PostgresProductBackend | PostgreSQL/API read+command adapter | ProductApiService current | 保留；cursor 与 SQL helper不另拆微模块 |
| ReplayIngressHandler、NormalizationHandler、FastPathHandler | Application/Worker | canonical worker current | 移 workers/ingestion.py |
| PostgresSleepSliceRepository | PostgreSQL adapter | canonical ingress/fast path | 移 workers/ingestion.py；与 pure domain projector分开 |
| SleepSlicePolicy、EpisodeLifecycleProjector、decide_fast_path_followup | Domain rule | current | 移 domain/episodes.py/fast_path.py |
| DeterministicFastPathService | Domain rule + legacy repository orchestration 混合 | current 通过 postgres adapter鸭子类型调用 | 改成纯 evaluate 函数/值对象；移除 SleepDomainRepository 类型和内部 lease/persist |
| SleepDomainRepository | legacy persistence facade | current 只借 3 个 dataclass；其余旧链 | 迁 3 类型后整文件删除 |
| SleepApiApplicationError | API transport error | public router/Postgres runtime current | 合入 api/public.py 或 auth 区域 |
| OpaquePageCursorCodec | API/PostgreSQL transport | PostgresSleepApiRuntime current | 合入 api/public.py，不建 cursor.py |
| SleepApiRuntime、AuthorizationEpochRoleViewCache | legacy application runtime | 非 canonical | 删除 |
| SleepApiOperationWorker | legacy in-process Worker | 非 canonical | 删除；durable Worker替代 |
| PostgresSleepApiRuntime | API application/PostgreSQL adapter | canonical /api/v1 | 保留并移 public.py |
| ProductAgentProcessor | Product application Worker | canonical product_agent | 保留；characterization 锁定三角色 exact output |
| PostgresProductAgentRepository | Product PostgreSQL adapter | canonical durable authority | 保留并移 workers/product.py |
| ProductAgentWorkHandlerAdapter/build_product_agent_worker_handlers | Worker composition | canonical | 保留，去 replay/live 误导文案 |
| ProductEpisodeRunner.run | 四 Agent application runtime | canonical processor | 保留 MORNING_REVIEW 分支 |
| ProductEpisodeRunner.reexecute_with_added_fact、commit_frozen_confirmations | dormant continuation | 无 canonical API/Worker caller | characterization 后删除 |
| build_deterministic_product_runtime_bundle | Product composition | canonical replay Worker | 保留、改为 worker-specific公开 builder |
| _build_postgres_worker_product_runtime_bundle_from_env | Product composition | canonical live-model Worker | 保留并改非私有业务名 |
| build_product_runtime_bundle_from_env、build_product_episode_runner_from_env、_persistence_store_from_env | legacy compatibility composition | diagnostics/tests | 删除 |
| ProductAgent roster/manifest/tool registry | runtime/domain registry | dynamic dispatch current | 保留语义，合并文件 |
| diagnostic http runtime/create routes | 第二 API transport/runtime | tests/support only | 删除 |
| real acceptance auditor/CLI | migration audit compatibility | console only，读取旧 repository | 默认删除；有外部 owner 时改写 UoW，不保留旧依赖 |

## 6. 删除闭包：删除什么、连带改什么、由谁替代

### 6.1 Legacy Sleep/SQLite/Perceptor 闭包

DELETE：

- sleepagent/persistence/store.py
- sleepagent/persistence/models.py
- sleepagent/persistence/vector_store.py
- sleepagent/persistence/sqlite_schema.sql
- sleepagent/sleep_api/runtime.py
- sleepagent/sleep_api/persistence.py
- sleepagent/sleep_api/events.py
- sleepagent/sleep_api/service.py 在迁走 Error/Cursor 后的全部旧 runtime 内容
- 第 4 节列出的 legacy sleep_domain 文件及 repository.py
- sleepagent/integrations/perceptor/**
- pyproject 的 real-perceptor-acceptance entry 和 SQLite package-data

只有旧测试引用的原因：

- Perceptor builders、webhook 和 push/pull service 只被专用 tests 调用；
- acceptance CLI 只审计 committed rows，不调用 Perceptor；
- legacy Sleep API composition 才会选择 SQLite fallback；
- canonical backend service composition 直接构造 PostgresSleepApiRuntime 和 UoW。

同步 DELETE/修改：

- tests/integration/test_perceptor_*.py
- tests/unit/test_perceptor_*.py
- tests/integration/test_sleep_domain_persistence.py 中 SQLite cases
- tests/unit/test_authority_cutover.py、test_sleep_domain_adapter_registry.py、test_sleep_domain_binding_promotion.py 等旧 registry/repository cases
- docs/development/perceptor-integration.md
- docs/audits/plug-and-play/** 和所有相关历史 audit
- README 中 .env legacy diagnostics 与 Perceptor/SQLite 叙述

替代：

- PostgreSQL UoW、PostgresSleepSliceRepository、PostgresSleepApiRuntime；
- canonical worker lease/fence/queue；
- migration ledger 与 current contract tests。

### 6.2 Diagnostic backend/frontend 闭包

DELETE：

- sleepagent/product_api/diagnostics/**
- tests/support/diagnostic_app.py
- tests/support/runtime_fixtures.py
- tests/support/radar_cli.py
- frontend/**
- docker/frontend.Dockerfile

只有旧测试引用的原因：

- tests/support/diagnostic_app.py 自己创建完整 FastAPI app、lifespan、auth、health、Radar、webhook routes；
- canonical backend_app 从未 mount diagnostics；
- frontend BFF 只允许 /product/radar/*、/radar-agent/*、/product/habit-profile/*，canonical OpenAPI 只有 /product/sleep/*；
- compose、Docker 和 README 没有启动 diagnostic app，compose 也没有 frontend service。

同步 DELETE/修改：

- architecture/test_authority_runtime_boundaries.py 中仅诊断边界的源码断言；
- integration/test_habit_profile_api.py、test_perceptor_webhook.py、test_product_api_endpoints.py、test_radar_task_api.py；
- unit/test_health.py、test_product_agents.py、test_product_runner_contraction.py 中只覆盖第二 app 的 cases；
- 5 个 frontend source-string tests 和旧 frontend e2e；
- frontend env、Node 开发说明和 npm 验证命令。

替代：

- canonical sleepagent.app app fixture；
- /api/v1 与 /product/sleep OpenAPI snapshots；
- sleepagent-demo HTTP client 和 reference_client；
- 无 UI 替代，未来 UI 必须直接基于 canonical contract 重写。

### 6.3 旧 Product task/device/persistence 闭包

DELETE：

- product_runtime/task_runtime/**
- product_runtime/product_persistence.py
- product_runtime/habit_persistence.py
- product_runtime/acceptance.py
- product_runtime/acceptance_materials.py
- product_runtime/habit_api.py
- product_runtime/confirmation/**
- product_device/** 在把 current LLM transport/schema 必需定义移入 runtime 后全部删除
- integrations/llm/** 在把 current Cloud HTTP transport 合入 runtime/provider.py 后删除

符号级迁移/删除表：

| 来源符号 | 目标/动作 | 证明 |
| --- | --- | --- |
| ProductLLM env常量、OpenAICompatibleProviderConfig、ProductLLM* errors、ProductChatProvider、OpenAICompatibleChatProvider、env/base-URL/secret校验 | MERGE → runtime/provider.py | live Product worker/runtime_factory直接消费 |
| CloudLLMClient 的 timeout/retry/request-id/JSON HTTP行为及必要 error mapping | MERGE实现 → runtime/provider.py，不保留第二层 router | OpenAICompatibleChatProvider当前委托它；provider HTTP tests逐项锁定 |
| ModelRouter、task_service_llm_audit_sink、LLMFaultInjector/faults.py、旧 context envelope | DELETE | 只有旧 task runtime/tests消费 |
| runtime/schemas/canonical.py 中 RadarDevice、RadarNightSummary、risk/quality/work-product canonical DTO | MERGE → runtime/contracts.py | current tooling、reports、trend handlers消费 |
| product_device/schemas.py 的 vendor DTO、converter/build helpers | DELETE | 只被 Perceptor/diagnostic/provider path消费；canonical Product worker使用 ProductRevisionFacts |
| CanonicalRadarEvidenceTool、CanonicalRadarDeviceStatus 及 ProductRevisionFacts验证分支 | MERGE → runtime/tools.py | current registry handler |
| RadarDataAdapter、RadarNightEvidence provider-read分支、RadarProviderLike、DataQualityGate/night_quality.py | DELETE | canonical worker注入已提交 ProductRevisionFacts，不调用 provider pull |

迁移后必须以 registry snapshot 做集合等价：每个 retained TOOL_DEFINITIONS 名恰有一个 handler，owner/permission/version不变；任何只在删除列表中的 vendor/provider handler必须先从 registry和Agent allowlist同时移除并证明 canonical 3×3 characterization不请求它。

同步 DELETE/修改：

- test_canonical_task_runtime.py、test_radar_agent_persistence.py、test_radar_cli_trace.py；
- test_product_agent_acceptance.py 与 tests/fixtures/acceptance-materials/**；
- test_radar_human_confirmation_matrix.py；
- product/device/provider/quality/replay 专用 tests；
- product_capability_goldens.json 和 support/golden_fixtures.py；
- docs 中 acceptance material、simulated evidence 和旧 Product architecture。

替代：

- workers/product.py 的 ProductAgentProcessor + PostgresProductAgentRepository；
- runtime/provider.py 的一套 OpenAI-compatible transport；
- runtime/contracts.py 的 canonical model DTO，而不是迁移旧 vendor DTO；
- current four-Agent runner、tooling 和 PostgreSQL integration tests。

### 6.4 Dormant Habit/continuation/memory 闭包

前置 characterization：

1. 固定 normal、degraded、urgent 三类 canonical Product worker source；
2. 保存 elder/family/doctor 的 public projection、Agent调用顺序、Safety条件、tool receipt allowlist、failure codes、prepared artifact canonical JSON与 attempt SHA；
3. 证明 canonical processor 请求没有 Habit fields、continuation、external_action；
4. 删除分支后重跑并逐字段比较；public projection、durable artifact schema/canonical JSON/hash必须相同。若当前 durable DTO会序列化 Habit optional-null 字段，保留这些少量字段并固定为当前 null语义，但不得为它们保留整套执行 subsystem。

characterization 通过后 DELETE：

- habit_application.py、habit_profile.py、habit_runtime.py、online_reasoning.py；
- questionnaire/**；
- runner/runtime contracts/factory 中 Habit request、change set、questionnaire handlers和store graph；
- reexecute_with_added_fact、commit_frozen_confirmations；
- external executor 与无 caller 的 HDS continuation；
- longitudinal_memory.py 中 in-memory persistence/scheduler/sidecar，cold_start.py 中 diagnostic evaluator；
- 对应 Habit、online reasoning、memory benchmark、continuation/confirmation tests；
- docs/product/habit-profile/**。

保留：

- 当前 model input 需要的 source/evidence guard；
- deterministic safety、HITL fail-closed 和 care confirmation核心；
- PostgreSQL product projection/operation/delivery事实；
- migration 中已经存在的 Habit/history tables，不改历史 SQL。

### 6.5 Replay 闭包

KEEP registry 中 8 个 scenario：

- normal-one-night
- elevated-vitals
- overlay-matrix
- worsening-vital-trend
- urgent-zero-model
- device-abnormal
- care-escalation
- habit-family-report

DELETE：

- sleepagent/simulation/replay/**
- 未注册 fixtures golden-15-night、memory-forget、urgent-human-text
- tests/support/simulation_verifier/**
- retired workflow golden e2e

migrate._bootstrap_replay_seed_allowlist 会校验并写入全部 8 个 registry seed，所以不能只因 sleepagent-demo show 当前只展示 3 个就删除其余 5 个。

## 7. 最终物理结构

目标结构以少目录、少文件为先。下列目标文件必须由现有文件 rename/merge 形成，不为目录模板新增占位模块。

    Dockerfile
    .dockerignore
    .env.test.example
    .gitignore
    PLAN.md
    README.md
    compose.yaml
    pyproject.toml
    requirements/
    reference_client/
    scripts/
      generate_openapi_snapshots.py
      verify_backend.sh
    docs/
      README.md
      architecture.md
      operations.md
      contracts/
        README.md
        openapi/
          backend-bff-v1.json
          demo-v1.json
    sleepagent/
      __init__.py
      app.py
      config.py
      observability.py
      process.py
      api/
        __init__.py
        public.py
        public_contracts.py
        product.py
        postgres.py
        demo.py
      domain/
        __init__.py
        contracts.py
        episodes.py
        fast_path.py
        product_data.py
      persistence/
        __init__.py
        migrate.py
        migrations.py
        test_bootstrap.py
        uow.py
        migration_manifest.json
        migrations/001...007.sql
      runtime/
        __init__.py
        contracts.py
        agents.py
        episode.py
        runner.py
        factory.py
        provider.py
        deterministic_model.py
        registry.py
        invocation.py
        tooling.py
        tools.py
        policies.py
        knowledge.py
        reports.py
        governance.py
        actions.py
        results.py
        memory.py
      simulation/
        __init__.py
        contracts.py
        generator.py
        replay_ingress.py
        seed_registry.py
        journey.py
        cli.py
        replay_seed_registry.json
        fixtures/
      workers/
        __init__.py
        runtime.py
        ingestion.py
        product.py
        commands.py
        effects.py
        demo.py
        retention.py
    tests/
      conftest.py
      unit/
      integration/
      e2e/
      support/

一级目录职责：

- api：唯一 FastAPI transport、public/product/demo DTO 与 PostgreSQL API query adapter。
- domain：无 FastAPI、无 Worker loop、无数据库连接的 sleep episode/quality/risk/Product fact 规则。
- persistence：PostgreSQL release manifest、迁移、scoped UoW 与 test bootstrap。
- runtime：四 Agent roster、orchestration、provider、tool、policy、安全和结果语义。
- simulation：registry-backed replay generation、journey 与 HTTP demo CLI。
- workers：durable worker core 及按 queue 业务责任分组的 PostgreSQL handlers。

根目录职责：

- docs：仅当前权威架构、运维与 API contract。
- reference_client：不导入 server 的外部 /api/v1 参考客户端。
- requirements：hash-locked 环境。
- scripts：OpenAPI drift 和一个 PostgreSQL backend verifier。
- sleepagent：唯一生产 Python package。
- tests：当前 unit、PostgreSQL integration 和少量 process e2e。

## 8. 预计收缩统计与硬预算

这些是验收上限，不是为了凑数字误删 consumer。若某项未达标，必须列出具体 retained consumer 和批准理由，不能新建 shim 抵消。

| 指标 | 当前 | 计划后上限/目标 | 变化 |
| --- | ---: | ---: | ---: |
| tracked files | 499 | 261 | 至少 -238；261 包含新 PLAN.md |
| production Python files | 173 | 95 | 至少 -78 |
| test Python files | 145 | 上限 65；按当前 manifest 预计约 40 | 至少 -80 |
| tests tracked files | 169 | 85 | 至少 -84 |
| docs files | 80 | 8；目标树为 6 | 至少 -72 |
| production Python LOC | 111,048 | 70,000 | 至少 -41,048 |
| test Python LOC | 59,015 | 36,000 | 至少 -23,015 |
| all Python files | 322 | 165 | 至少 -157 |
| all Python LOC | 170,906 | 108,000 | 至少 -62,906 |
| root tracked directories | 10 | 6 | -4 |
| sleepagent package directories | 8 | 6 | -2，且删除语义前缀重复 |
| true DELETE paths | 0 | 至少 215 | 不含 merge-away |
| MERGE-away source files | 0 | 至少 24 | 内容进入 retained destination |
| MOVE/rename paths | 0 | 20–30 | 净文件变化 0 |
| NEW tracked paths | 0 | 1 | 仅本 PLAN.md；业务源码/测试/文档新增 0 |

文件数等式：499 + 1 个 PLAN − 至少 215 个真实删除 − 至少 24 个 merge-away = 至多 261。

已识别的纯 legacy/diagnostic production 删除集本身约 41K Python LOC；后续 dormant Product 分支与 retained 巨型模块内 legacy branch 的删除给 70K 上限留出余量。

## 9. 测试收缩计划

### 9.1 最终测试分层

| 类别 | 保留目标 |
| --- | --- |
| 核心必须保留 | 四 Agent roster/run/safety/tool/governance；canonical API contract/auth；episode/fast path domain规则；migration manifest/RLS/UoW/lease/fence/idempotency/outbox；provider schema/timeout；replay registry |
| 可以合并 | 按同一 public behavior 或同一 PostgreSQL causal chain 合并，不再按施工 Stage/每个微模块一文件 |
| 旧架构专用 | SQLite、RadarPersistenceStore、Perceptor、diagnostic app、旧 task runtime、Habit dormant API、source-string frontend；删除 |
| 重复覆盖 | 同一 schema/role/tool 在多个 test 文件重复；保留一份 semantic table-driven case |
| 施工阶段临时验证 | 源码路径、固定 __all__、Stage token、目录不存在、旧 PLAN evidence；删除 |
| 主运行链 e2e | 一个 clean PostgreSQL pipeline，一个 process kill/reclaim/fence，一个 canonical ASGI lifespan |

### 9.2 KEEP 集

保留并允许内部 prune/rename 的核心文件约 39 个：

- architecture：tests/architecture/test_runtime_dependency_boundaries.py，重写为唯一小型 AST/import boundary test。
- e2e：tests/e2e/test_backend_process_boundaries.py。
- integration：tests/integration/test_backend_app.py、test_backend_first_slice_postgres.py 改业务名、test_backend_postgres_foundation.py、test_backend_postgres_integration.py、test_product_live_http_provider.py、test_product_live_postgres_worker.py、test_product_postgres_integration.py、test_sleep_postgres_vertical_slice.py、test_worker_postgres_integration.py。
- unit：tests/unit/test_artifact_rendering.py、test_backend_runtime.py、test_care_coordination_policy.py、test_cold_start_policy.py、test_demo_cli.py、test_episode_contract_v2.py、test_healthclaw_memory_governance.py、test_product_agent_factory.py、test_product_agent_governance.py、test_product_agent_provider.py、test_product_agent_runner.py、test_product_agent_tooling.py、test_product_device_schemas.py、test_product_human_decision.py、test_product_publication_service.py、test_product_runtime_coordinators.py、test_product_sleep_api.py、test_radar_data_tool.py、test_replay_ingress_adapter.py、test_retention.py、test_reviewed_knowledge.py、test_risk_policy.py、test_sleep_api_reference_client.py、test_sleep_domain_contracts.py、test_sleep_domain_fast_path.py、test_sleep_habit_profile.py、test_trend_analysis.py、test_worker_runtime.py。

文件改名后以最终业务名为准，不保留 radar、stage、healthclaw 等历史前缀。

上面的 test_cold_start_policy.py、test_healthclaw_memory_governance.py、test_product_device_schemas.py、test_sleep_habit_profile.py 是“迁出仍属 current 的断言后作为 merge source 保留到 Phase D”的临时裁决，不代表最终继续保留 cold_start、healthclaw、device、habit 文件名。Phase D 结束时，它们必须改业务名并合入 contracts/governance/runner tests，或在无剩余 current assertion 时删除；最终文件上限按 39 个 test_*.py 加少量 support 计算。

### 9.3 MERGE 集

45 个 merge-away test sources 与目标如下；目标均为第 9.2 节 retained 文件或由 retained 文件 rename 得到，不新增 test 文件：

Architecture 4：

- test_authority_runtime_boundaries.py → backend app/PostgreSQL integration，只迁 production fail-closed/no SQLite fallback。
- test_backend_entrypoints.py → backend app + backend runtime，加入精确 negative import。
- test_product_agent_architecture.py → product agent factory/governance/tooling semantic tests。
- test_tool_handler_ownership.py → tooling、artifact rendering、reviewed knowledge、risk policy。

E2E 2：

- test_real_lifespan_harness.py → test_backend_process_boundaries.py。
- test_simulation_contracts.py → test_replay_ingress_adapter.py，只迁 generator/ingress contracts。

Integration 9：

- test_backend_stage2_postgres.py、test_backend_stage3_postgres.py、test_backend_stage4_postgres.py、test_backend_stage5_postgres.py → 由 test_backend_first_slice_postgres.py rename 得到的 business-named pipeline test。
- test_habit_profile_persistence.py → test_product_postgres_integration.py，仅迁仍属 current PostgreSQL schema/authority 的断言。
- test_product_postgres_worker.py → test_product_live_postgres_worker.py + test_product_postgres_integration.py。
- test_sleep_api_v1.py → test_backend_app.py + unit reference-client/Product API contract。
- test_sleep_domain_persistence.py → test_sleep_postgres_vertical_slice.py，SQLite cases不迁。
- test_worker_postgres_store.py → test_worker_postgres_integration.py + unit worker runtime。

Unit 30：

- test_backend_keys.py、test_backend_persistence.py、test_backend_settings.py、test_observability.py → backend runtime/app/PostgreSQL tests。
- test_demo_persistence.py → demo CLI + business-named PostgreSQL pipeline。
- test_live_worker_stage_gates.py、test_stage2_worker.py、test_stage3_worker.py、test_stage4_worker.py、test_stage5_worker.py → worker runtime + business handler/pipeline tests。
- test_night_episode_lifecycle.py → episode contract + fast path。
- test_product_agent_deterministic_model.py、test_product_agent_live_worker.py → retained test_product_agent_provider.py。
- test_product_agent_episode_runtime.py、test_product_agent_persistence.py → runner + Product PostgreSQL integration。
- test_product_agent_invocation.py、test_product_agent_roles.py → Product factory/runner。
- test_product_agents.py、test_radar_agent_canonical_schemas.py → retained runtime contract/agent tests，不保留 device/radar文件名。
- test_radar_cloud_llm.py、test_radar_provider_contract.py → retained Product provider HTTP/schema suite。
- test_radar_data_quality_gate.py、test_radar_replay_provider_anomalies.py → current Product facts/tool + replay ingress tests。
- test_radar_reports_packaging.py → artifact rendering。
- test_radar_reviewed_seed_rag.py → reviewed knowledge。
- test_replay_journey_worker.py → worker runtime + replay ingress/pipeline。
- test_schema_versions_and_ontology.py → domain contracts + episode。
- test_sleep_api_events_v1.py → reference client + backend app。
- test_sleep_habit_online_reasoning.py → 只迁与 current risk policy无关的通用规则；Habit execution cases删除。
- test_sleep_worker_adapters.py → worker runtime + PostgreSQL vertical slice。

第 9.4 节中的 test_sleep_domain_adapter_registry.py 和 test_sleep_domain_binding_promotion.py 最终是 DELETE，不属于 MERGE；canonical 所需小类型和 PostgreSQL行为由 retained contracts/vertical-slice tests直接覆盖。

分类对账：13 个 architecture = 1 KEEP + 4 MERGE + 8 DELETE；5 个 e2e = 1 + 2 + 2；28 个 integration = 9 + 9 + 10；80 个 unit = 28 + 30 + 22。合计 126 个 test_*.py = 39 KEEP + 45 MERGE + 42 DELETE，无遗漏、无重叠。

剩余 19 个非 test_*.py Python 路径也逐项闭合：

- KEEP：tests/conftest.py。
- MERGE→retained provider test/conftest 后删除 source：tests/support/openai_compatible_server.py。
- DELETE：tests/__init__.py、architecture/__init__.py、e2e/__init__.py、integration/__init__.py、support/__init__.py、unit/__init__.py；tests/benchmarks 下两个 __init__.py 与 harness.py；tests/support/diagnostic_app.py、golden_fixtures.py、radar_cli.py、runtime_fixtures.py；tests/support/simulation_verifier 下 __init__.py、contracts.py、loader.py、workflow_goldens.py。

因此 145 个 test Python 全部分类为：39 retained test files + 1 conftest + 45 test merge-away + 42 test delete + 1 support merge-away + 17 support delete。若不需要额外 retained support，预计最终为40个 Python测试文件；65只是硬上限，不是要填满的配额。

### 9.4 DELETE test_*.py 集

以下 42 个 test 文件删除；若其中有一条 current invariant，先迁一条 semantic assertion 到上述 KEEP 目标，不保留墓碑文件。

Architecture：

- test_backend_release_artifacts.py
- test_clean_checkout_reproducibility.py
- test_product_agent_public_api.py
- test_product_runtime_architecture.py
- test_repository_namespaces.py
- test_retired_runtime_absence.py
- test_runtime_physical_boundaries.py
- test_tool_service_boundaries.py

E2E：

- test_frontend_radar_replay_scenarios.py
- test_radar_replay_workflow_goldens.py

Integration：

- test_backend_runtime_vertical_slice.py
- test_habit_profile_api.py
- test_perceptor_pull_reconciliation.py
- test_perceptor_push_ingestion.py
- test_perceptor_push_runtime.py
- test_perceptor_webhook.py
- test_perceptor_real_acceptance.py
- test_perceptor_real_acceptance_auditor.py
- test_product_api_endpoints.py
- test_radar_task_api.py

Unit：

- test_authority_cutover.py
- test_backend_process_fault_probe.py，在 probe 与 process e2e 合并后删除
- test_canonical_task_runtime.py
- test_frontend_radar_safety_ui.py
- test_frontend_radar_security.py
- test_habit_profile_frontend_contract.py
- test_health.py，仅 diagnostic app 部分；canonical health迁 app test
- test_longitudinal_memory_benchmark.py
- test_memory_capability.py
- test_perceptor_adapters.py
- test_perceptor_client.py
- test_perceptor_signing.py
- test_product_agent_acceptance.py
- test_product_frontend_launcher.py
- test_product_runner_contraction.py
- test_radar_agent_persistence.py
- test_radar_cli_trace.py
- test_radar_human_confirmation_matrix.py
- test_radar_privacy_authorization.py
- test_sleep_domain_adapter_registry.py
- test_sleep_domain_agent_bridge.py
- test_sleep_domain_binding_promotion.py

额外 support/fixture DELETE：

- tests/support/diagnostic_app.py
- tests/support/runtime_fixtures.py
- tests/support/radar_cli.py
- tests/support/golden_fixtures.py
- tests/support/simulation_verifier/**
- tests/benchmarks/**
- tests/fixtures/acceptance-materials/**
- tests/fixtures/product_capability_goldens.json
- 失去内容后的 tests package __init__.py

openai_compatible_server.py 的小型 provider fixture 合入 retained provider test 或 conftest 后删除。backend_process_fault_probe.py 从 scripts 移入 retained process-boundary support，再与唯一 consumer 同文件或保留一个 support 文件，禁止 production import tests。

### 9.5 conftest 清理

- 删除 diagnostic env 默认值、FastAPI TestClient 全局替换和 legacy app fixture。
- 锁定 CPython 3.11 后使用 canonical app lifespan fixture/native TestClient。
- 不再把所有未标记测试偷偷标 unit；每个 PostgreSQL、lifespan、process e2e 显式 marker。
- 禁止 cross-test imports；共享 fixture 只放 conftest 或一个有两个以上真实 consumer 的 support 文件。

## 10. 文档与脚本收缩

### 10.1 文档 KEEP/MOVE/MERGE

- KEEP docs/README.md，重写成唯一索引和开发入口。
- MOVE docs/architecture/overview.md → docs/architecture.md，并吸收 positioning、仍有效 technical debt。
- MOVE docs/runbooks/backend-operations.md → docs/operations.md，去 Stage 命令，明确 compose 是 replay/test harness。
- KEEP docs/contracts/README.md。
- KEEP 两个 OpenAPI JSON，并由 current app 更新。

### 10.2 文档 DELETE

- docs/audits/** 全部 33 个历史计划、review log、completion audit、manifest/evidence。
- docs/product/habit-profile/** 全部；当前 Habit execution 不在 external contract，证据档案可由 Git 恢复。
- docs/development/perceptor-integration.md。
- docs/product/positioning.md，在有效内容合入 README/architecture 后。
- docs/references/healthclaw.md、team-presentation.md。
- docs/technical-debt.md，在每条仍有效事项落实或合入 architecture/operations 后。
- 失去内容的 docs/architecture、runbooks、development、product、references 目录。

文档最终 6 个文件；允许上限 8 仅给执行中确有当前 consumer 的额外运维材料，不允许保留历史 PLAN 填满额度。

分类对账：80 = 33 个 audits + 36 个 habit-profile + 1 个 Perceptor development + 1 个 positioning + 2 个 references + 1 个 technical-debt + 6 个最终 retained/moved 文件。

### 10.3 脚本

- git mv scripts/verify_backend_first_slice.sh scripts/verify_backend.sh。
- 将 verify_backend_stage3.sh、stage4.sh、stage5.sh 合并为 verify_backend.sh 的 core、read-models、delivery-recovery、retention、all suites 后删除三个源文件。
- 将 backend_process_fault_probe.py 移到 tests/support，并在只有一个 consumer 时继续合入对应 e2e test。
- 保留 generate_openapi_snapshots.py，更新 app import。
- Dockerfile 移至根；compose 更新 dockerfile 和 Python module paths。

## 11. 分阶段执行规格

每个 Phase 单独形成可审查 diff；不得把全仓 rename、逻辑删除和中文注释混在同一提交。计划不授权自动 commit，实施者在每个 Phase 验证后再请求用户决定是否提交。

### 11.1 唯一 action owner 与净变化总账

第 6、9、10 节是逻辑闭包；实际删除只能由下表中的一个 Phase 拥有，禁止在两个 Phase 重复计数。实施前先从这些列表生成 path/action/destination 清单并核对文件存在性；清单留在 PR/命令输出，不新增 tracked inventory。

| Phase | 唯一拥有的 source paths | 预计净文件变化 |
| --- | --- | ---: |
| A | 仅在 retained tests 内加/收缩 characterization，不创建文件 | 0 |
| B | legacy/diagnostic production、frontend、.env.example、.agents；第 9.4 节 42 个 DELETE test_*.py 及其直接 support/fixture assets | 至少 -130 |
| C | 第 7 节物理 rename 和 retained 模块的结构 merge；不处理 B/D/E 已拥有路径 | 至少 -24 merge-away；MOVE净 0 |
| D | 第 6.4 节 dormant Product production files/branches；retained test 只删 cases、不删除 test path | 至少 -10 |
| E | 45 个 MERGE test sources、剩余 test package/support收口、全部 docs收口、script merge；不再删除 B 已拥有的42个 test | 至少 -75 |
| F | comment-only | 0 |
| G | verification-only | 0 |

最低总账：500 个当前工作树路径（499 baseline + PLAN.md）− 130 − 24 − 10 − 75 = 261。各数是无重叠下限；实际删除更多可以低于 261，但不得靠为达数字误删 current consumer。最终 action report 还必须按“true DELETE至少215、merge-away至少24、MOVE 20–30”重新分栏对账。

### Phase A — Runtime Reachability & Deletion Map

目标：

- 冻结当前 entrypoints、queue registry、四 Agent output 和 migration identities；
- 关闭两项 uncertain，不产生新的 tracked audit 文档。

为什么：删除前先锁定真实外部行为，才能区分“包级 import 可达”和“运行时执行可达”，也给后续大删提供逐字段回归基线。

具体文件：

- pyproject.toml、compose.yaml、backend/main.py；
- backend_*、worker_runtime.py、stage*_worker.py；
- product_runtime/postgres_worker.py、runner.py、runtime_factory.py；
- persistence manifest/migrations；
- retained tests 中的 app/worker/Product characterization。

DELETE/MERGE/MOVE：

- 本 Phase 不移动或删除生产文件。
- 在现有 retained test 文件内加入 characterization 后，删除重复的旧 characterization case；净文件变化 0。

执行：

1. 保存 git ls-files/LOC/pytest collect 基线到命令输出，不提交 inventory 文件。
2. 对 backend.main 和 worker import probe 记录禁止模块当前污染。
3. 对 3 场景 × 3 role 锁定 public Product projection与四 Agent调用。
4. 证明 canonical request 不设置 Habit、continuation、external action。
5. 记录 EXPECTED_MIGRATION_IDENTITIES、manifest SHA 和 001–007 checksums。
6. 检查公开发布清单；没有 real-perceptor-acceptance owner 证据即执行既定删除。

风险：

- 把 package eager import 误当执行 consumer；
- characterization 锁死内部实现而非 public behavior。

验证：

- pytest --collect-only；
- targeted app/worker/Product tests；
- import probe；
- migration check/status；
- git diff 只含 retained characterization test 的净简化，且无新 fixture snapshot。

预期净文件变化：0。

### Phase B — Dead / Legacy / Duplicate Removal

目标：

- 一次性移除 SQLite、旧 Sleep Runtime、Perceptor、diagnostic backend/frontend、旧 Product task/device/persistence 和旧 replay catalog；
- 先迁小型 current 定义，再删除巨型旧链，不留 shim。

为什么：这些子系统没有 canonical composition consumer，却制造 import污染、第二 authority 和大批墓碑测试；先删除可显著缩小后续 rename/refactor 的搜索空间。

具体文件：

- 第 6.1、6.2、6.3、6.5 节中的 production/config/frontend 路径；
- 仅属于被删 legacy package 的 __init__.py、pyproject package-data/console scripts；
- 第 9.4 节 42 个 DELETE test_*.py 及列出的直接 support/fixture；文档统一由 Phase E 拥有。

DELETE：

- legacy/diagnostic production 路径；
- frontend 与 frontend Dockerfile；
- 将 DEEPSEEK_API_KEY 与四个 SLEEPAGENT_PRODUCT_LLM_* current 键的说明迁入 operations.md/.env.test.example 后删除 compatibility-heavy .env.example；测试模板不给真实 secret，只说明由调用环境注入；
- 第二 app、旧 runtime fixtures、旧 verifier fixtures；
- Perceptor console/tests；Perceptor文档留给 Phase E统一删除；
- .agents 剩余 metadata。

MERGE：

- SleepApiApplicationError、OpaquePageCursorCodec、3 个 current domain records；
- LLM env/HTTP/schema transport；
- product_device current DTO 中仍被 Product runtime 使用的最小定义。

MOVE：

- 此 Phase 只做删除前必要的小定义迁移，物理大改留 Phase C。

风险：

- real acceptance 的仓外调用；
- fast path 仍通过旧 repository nominal type；
- product_device package init 隐藏动态 consumer；
- 删除 frontend 被误解为已有 UI 功能回归。

验证：

- rg 全仓 consumer 归零；
- canonical app OpenAPI、auth、Product API targeted tests；
- Product worker deterministic/live provider tests；
- replay 8-seed registry verify；
- negative import：canonical app/worker 不加载 legacy SleepAgent service、Perceptor、diagnostics；
- pytest 不再引用 deleted modules。

预期净文件变化：至少 -130 个真实 tracked paths。

### Phase C — Physical Structure Contraction

目标：

- 落实第 7 节目录；
- 消灭 backend_*、stage*_worker、重复 product/sleep/device 前缀目录和 re-export shim；
- 业务行为不变。

为什么：只有 dead code消失后才能按剩余真实职责归位；此时移动不会把旧架构一起永久化，也能让 diff主要是可审查的 rename/merge。

MOVE/MERGE：

- backend/main + backend_app + backend_services → app.py；
- backend_runtime → process.py，供 app 与 workers/runtime共同使用，且 process.py 不 import FastAPI；
- backend_settings + backend_keys → config.py；
- product_api → api/product.py；
- sleep_api canonical → api/public.py/public_contracts.py；
- backend_persistence → api/postgres.py；
- demo API/store → api/demo.py；
- product_runtime current core → runtime/；
- worker/runtime/handlers → workers/；
- canonical sleep_domain → domain/；
- demo_cli/journey → simulation/；
- 4 verifier scripts → 1；
- Dockerfile → root。
- pyproject 删除 backend* package discovery，console 更新为 sleepagent.simulation.cli:main；compose、Docker CMD/healthcheck、OpenAPI generator 和 README 全部改为 sleepagent.app:app 与 sleepagent.workers.runtime。
- .env.test.example 只保留 canonical replay配置；.gitignore/.dockerignore 删除 frontend、Radar 与重复规则。

DELETE：

- 所有只做 re-export 的 init 内容；init 文件保留为空或只放版本，不提供兼容 import。
- merge-away 后的源文件和空目录。

风险：

- 循环 import；
- console/compose/OpenAPI/reference docs 仍引用旧 path；
- Worker lazy registry 漏 queue；
- Git 把 rename 误显示为无关大删除，影响 review。

验证：

- python -m compileall；
- mypy sleepagent reference_client；
- import every canonical entry in clean interpreter；
- architecture test 用 AST 断言 process.py 与 workers/** 不 import FastAPI、sleepagent.app 或 sleepagent.api，维持 API/Worker 进程边界；
- compose config；
- 在仓库外临时 venv 安装 production hash lock + wheel，并从临时目录验证 app/worker import、package data、reference client 以及只剩 migrate/demo 的两个 console entry；
- queue configured set == handler set；
- git grep 旧 module paths为 0；
- 001–007 migration文件 checksum不变。

预期净文件变化：至少 -24 个 merge-away source files；20–30 个 MOVE 净变化 0。

### Phase D — Core Module Simplification

目标：

- 删除 dormant Habit/continuation/external/memory sidecar；
- 把 postgres_slice、runtime_factory、runner、worker runtime 和 PostgreSQL adapter 中职责混杂降到当前需要；
- 不通过拆微模块“优化”LOC。

为什么：物理归位只解决“放在哪里”，本 Phase 才删除 retained 巨型模块内部的旧分支，并把纯规则、事务和进程编排恢复到真实边界。

具体动作：

- postgres_slice：pure projector/policy移 domain；SQL/handler留 workers/ingestion；worker_adapters并入。
- fast_path：改纯输入输出；repository lease/persist移出。
- runtime_factory：只留两个 worker builder、shared canonical composition和 configured check。
- runner：只留 canonical MORNING_REVIEW、四 Agent、current safety/tool/result flow。
- product runtime：按第 4 节合并 agents、invocation、tooling、registry、tools、policies、knowledge、reports、results。
- longitudinal memory/cold start：只保 model input 与 publication guards。
- worker_runtime：保留 durable core；删除已无 queue/handler 的 generic branch。
- backend/API PostgreSQL：删除 legacy error/import/helper，合并单 consumer cursor。

DELETE：

- 第 6.4 节 dormant execution slice；
- 合并后空的 services、ports、schemas、agents、tools、policies等子目录。

风险：

- 动态 ToolRequest handler误删；
- current urgent Safety、HITL fail-closed、role隔离改变；
- runner internal result与 PostgreSQL commit hash漂移；
- 将大文件简单搬家而未减少逻辑。

验证：

- Phase A 3×3 characterization逐字段通过；
- exact four Agent roster 和 conditional Safety；
- registered tool definitions == handlers，且 owner/permission不变；
- Product prepared hash、role view、fenced commit integration；
- API public projection和operation状态；
- LOC/file budget重新计算；
- 单文件人工复核：不新增只含一个 Protocol/trivial wrapper 的模块。

预期净文件变化：至少 -10 个真实或 merge-away production paths；production LOC 累计降至不超过 70K。

### Phase E — Test & Docs Contraction

目标：

- 删除旧实现的墓碑测试和施工文档；
- 合并当前行为覆盖；
- 最终测试证明真实 PostgreSQL/API/Worker，而不是源码字符串。

为什么：先稳定生产结构再整理验证资产，避免为了即将删除的路径维护测试/文档，同时确保重要不变量迁移到 canonical tests 而非随旧文件误删。

DELETE/MERGE/MOVE：

- 对 Phase B 已删除的 42 个 test 只做 ledger复核，不再次计数；本 Phase 执行第 9.3 节 45 个 MERGE sources和39个 retained targets的最终收口；
- 删除 Phase B 未拥有的空 test package init，将 provider support 合入 retained test；
- architecture tests收口为 1；
- PostgreSQL Stage tests合为业务 pipeline test；
- docs按第 10 节收口；
- scripts合为 2 个。

风险：

- 只追求 case 数下降导致关键故障窗口丢失；
- PostgreSQL test 因 marker错误继续被跳过；
- docs link/OpenAPI snapshot漂移。

验证：

- pytest collect 数约 650–800，文件不超过 65；
- 所有 postgres/asgi/e2e 显式 marker；
- unit、postgres、asgi lifespan、process e2e 分别跑；
- docs link checker或仓内 Markdown link脚本为 0 broken links；
- OpenAPI snapshot check；
- rg 不存在 old path/Stage 文案/diagnostic route；
- reference_client tests继续通过。

预期净文件变化：至少 -75 个无重叠 tracked paths；docs 不超过 8，tests tracked不超过 85。

### Phase F — Chinese Comment Pass

目标：

- 只对最终保留且结构稳定的生产源码补中文职责和“为什么”注释；
- 不制造 docstring/API metadata 漂移。

为什么：结构稳定前补注释会在移动/删除中产生浪费和冲突；最后单独 comment-only diff最容易证明没有混入行为修改。

范围：

- sleepagent/app.py、config.py、observability.py；
- api、domain、persistence、runtime、simulation、workers 下全部 retained Python；
- reference_client 若不修改逻辑，不强制翻译其外部示例注释。

规则：

1. 每个生产文件顶部用普通 # 中文注释说明职责、入口、状态 owner、不负责什么。
2. 重要 class/function/private function 前说明输入输出及数据库/事务/外部调用/Agent/状态变化。
3. 事务、鉴权、并发、lease/fencing、重试、reconciliation、加密、状态迁移前解释为什么。
4. 不逐行翻译，不注释 import/赋值/getter/明显循环。
5. 默认不用新增 docstring；保留已有对外 docstring，避免 OpenAPI/Pydantic metadata变化。
6. 注释不得继续使用 Phase/Stage 施工叙述；迁移历史说明除外。

风险：

- 注释错误或比代码更抽象；
- 批量 docstring 改变 FastAPI operation description/schema；
- comment pass 混入逻辑变更。

验证：

- comment-only diff单独审查；
- compileall、mypy、OpenAPI snapshot和全量测试；
- 人工抽查每个 retained file header及高风险事务块。

预期净文件变化：0，逻辑 LOC 0；注释 LOC允许增加，但 all Python 总 LOC仍必须不超过 108K。

### Phase G — Final Verification & Deletion Report

目标：

- 从 clean checkout/wheel/PostgreSQL 验证唯一运行路径；
- 输出事实型删除清单和统计，不新增 completion audit 文档。

为什么：只有 clean package、真实数据库和独立进程能证明没有依赖本地残留、错误 marker 或 import副作用；最终统计则证明本任务确实是净收缩。

验证顺序：

1. clean checkout 构建 wheel；在仓库外新建 CPython 3.11 venv，先按 production.lock 的 hash 安装依赖，再以 --no-deps 安装 wheel；从临时目录且 unset PYTHONPATH 后验证，避免源码树遮蔽漏包。
2. empty PostgreSQL 执行 migrate apply、check、status，重复 apply验证幂等。
3. 验证 manifest/ledger/checksum和 runtime schema attestation。
4. 启动 api、demo-api、internal-api、worker，验证 surface隔离。
5. 跑 canonical replay causal chain、三角色 Product、commands/interactions、reconciliation、retention/reset。
6. kill/restart Worker 验证 lease reclaim、fencing、outcome_unknown与幂等。
7. 跑 unit、postgres、ASGI lifespan、e2e、mypy、compile、OpenAPI、wheel/reference client。
8. 跑 import negative、dead reference和目录/LOC预算。
9. 在 PR/提交说明中列“删除路径—原因—替代者”，不写新的 docs/audits completion 文件。

风险：

- 本地可过但 clean wheel漏 package data；
- PostgreSQL只测 fresh install，未测现存 ledger；
- compose replay harness被误标 production。

预期净文件变化：0；只允许修复前面 Phase 暴露的问题，禁止新建兼容层。

## 12. 验证命令清单

实施者可按环境调整 Python executable/DSN，但不得跳过等价证明。下列代码块必须在仓库根目录的同一个 Bash 会话中依次执行；第一步启用 fail-fast，任何 proof 失败都必须令整次验证非零退出。

验证 shell：

    set -Eeuo pipefail
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    cd "$REPO_ROOT"

基线与预算：

    git status --short
    git ls-files | wc -l
    git ls-files '*.py' | wc -l
    git ls-files 'sleepagent/*.py' 'sleepagent/**/*.py' | xargs wc -l
    git ls-files 'tests/*.py' 'tests/**/*.py' | xargs wc -l
    git ls-files 'docs/**' | wc -l
    git ls-files | awk -F/ 'NF > 1 {print $1}' | sort -u

静态与 import：

    python -m compileall -q sleepagent reference_client
    python -m mypy sleepagent reference_client
    ! git ls-files 'sleepagent/*.py' 'sleepagent/**/*.py' 'tests/**/*.py' 'scripts/*' | rg '(^|/)(stage[2-5][^/]*|[^/]*stage[2-5][^/]*)$'
    python -m pytest tests/architecture/test_runtime_dependency_boundaries.py -q
    ! git grep -nE 'backend\.main|sleepagent\.backend_|sleepagent\.worker_runtime|stage[2-5]_worker|RadarPersistenceStore|sqlite3|sqlite_schema|diagnostic_app|integrations\.perceptor' -- Dockerfile README.md compose.yaml docs pyproject.toml reference_client scripts sleepagent tests

第一个 rg 预期无输出；rg 的 no-match 退出码 1 在这里表示通过。retained architecture test 用 AST 断言 class/function identifier 无 Stage2–5。Stage 原始字符串另做 inventory：只允许 persistence/migrations.py、001–007.sql、migration_manifest.json、验证迁移历史的一个 foundation test 及 operations 的迁移说明出现已安装数据库/version/event identifier。业务模块路径、Python 符号、测试文件名、脚本参数和普通文案出现即失败；PLAN.md 本身不参与该检查。

建议增加到 retained architecture test 的 negative import 断言：

    import sleepagent.app
    import sleepagent.workers.runtime
    forbidden_prefixes = {
        "sleepagent.persistence.store",
        "sleepagent.sleep_api.service",
        "sleepagent.integrations.perceptor",
        "sleepagent.product_api.diagnostics",
        "tests.support.diagnostic_app",
    }
    assert not any(
        loaded == prefix or loaded.startswith(prefix + ".")
        for loaded in sys.modules
        for prefix in forbidden_prefixes
    )

这里不把第三方库可能加载的标准库 sqlite3 当作失败条件；前面的 git grep 单独要求 retained 仓库代码不存在 import sqlite3、sqlite3.connect 或 sqlite_schema 引用。另用独立解释器只 import sleepagent.process 和 sleepagent.workers.runtime，断言 sys.modules 无 fastapi、sleepagent.app、sleepagent.api 前缀。

测试收集与纯 unit：

    python -m pytest --collect-only -q
    python -m pytest -m unit -q

契约、临时 Compose 环境与 clean wheel：

    PROOF_ROOT="$(mktemp -d /tmp/sleepagent-wheel-proof.XXXXXX)"
    chmod 700 "$PROOF_ROOT"
    python3.11 -m venv "$PROOF_ROOT/build-venv"
    "$PROOF_ROOT/build-venv/bin/python" -m pip install --require-hashes -r "$REPO_ROOT/requirements/dev.lock"
    PROOF_ENV="$PROOF_ROOT/compose.env"
    PRIVATE_KEY="$PROOF_ROOT/actor-private.pem"
    PUBLIC_KEY="$PROOF_ROOT/actor-public.pem"
    cp "$REPO_ROOT/.env.test.example" "$PROOF_ENV"
    umask 077
    "$PROOF_ROOT/build-venv/bin/python" -c 'from pathlib import Path; import sys; from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; root = Path(sys.argv[1]); key = Ed25519PrivateKey.generate(); root.joinpath("actor-private.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())); root.joinpath("actor-public.pem").write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))' "$PROOF_ROOT"
    chmod 644 "$PUBLIC_KEY"
    PUBLIC_KEY_SHA256="$(sha256sum "$PUBLIC_KEY" | awk '{print $1}')"
    printf '\nSLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_PATH=%s\n' "$PUBLIC_KEY" >> "$PROOF_ENV"
    printf 'SLEEPAGENT_TEST_ACTOR_PUBLIC_KEY_SHA256=%s\n' "$PUBLIC_KEY_SHA256" >> "$PROOF_ENV"
    COMPOSE_ARGS=(-f "$REPO_ROOT/compose.yaml" --project-name "sleepagent-proof-$$" --env-file "$PROOF_ENV")
    cleanup_proof() {
      docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
      case "$PROOF_ROOT" in
        /tmp/sleepagent-wheel-proof.*) rm -rf -- "$PROOF_ROOT" ;;
        *) return 1 ;;
      esac
    }
    trap cleanup_proof EXIT INT TERM
    docker compose "${COMPOSE_ARGS[@]}" config > "$PROOF_ROOT/compose.config"
    rg -F "$PUBLIC_KEY" "$PROOF_ROOT/compose.config"
    ! rg -F "$PRIVATE_KEY" "$PROOF_ROOT/compose.config"
    set -a
    . "$PROOF_ENV"
    set +a
    export SLEEPAGENT_BACKEND_SIGNING_KEY_REF="file:$PUBLIC_KEY"
    "$PROOF_ROOT/build-venv/bin/python" scripts/generate_openapi_snapshots.py --check
    "$PROOF_ROOT/build-venv/bin/python" -m build --no-isolation --wheel --outdir "$PROOF_ROOT/dist"
    python3.11 -m venv "$PROOF_ROOT/venv"
    "$PROOF_ROOT/venv/bin/python" -m pip install --require-hashes -r "$REPO_ROOT/requirements/production.lock"
    "$PROOF_ROOT/venv/bin/python" -m pip install --no-deps "$PROOF_ROOT"/dist/sleepagent-*.whl
    cd "$PROOF_ROOT"
    unset PYTHONPATH
    "$PROOF_ROOT/venv/bin/sleepagent-migrate" --help
    "$PROOF_ROOT/venv/bin/sleepagent-demo" --help
    "$PROOF_ROOT/venv/bin/python" -c 'import sleepagent.app; import sleepagent.workers.runtime'
    "$PROOF_ROOT/venv/bin/python" -c 'from sleepagent.persistence.migrate import discover_migrations; items = discover_migrations(); assert len(items) == 7; assert tuple(item.version for item in items) == tuple(range(1, 8))'
    "$PROOF_ROOT/venv/bin/python" -c 'from sleepagent.simulation.seed_registry import load_replay_seed_registry, verify_packaged_seed; expected = {"normal-one-night", "elevated-vitals", "overlay-matrix", "worsening-vital-trend", "urgent-zero-model", "device-abnormal", "care-escalation", "habit-family-report"}; registry = load_replay_seed_registry(); assert len(registry.seeds) == 8; assert {seed.scenario_id for seed in registry.seeds} == expected; assert len(tuple(verify_packaged_seed(seed) for seed in registry.seeds)) == 8'
    "$PROOF_ROOT/venv/bin/python" -c 'from reference_client.sleep_api_v1_client import SleepApiV1Client'

上述 proof 必须在 clean checkout/CI 执行，且临时目录中不得出现 sleepagent、backend 或 reference_client 源目录。discover_migrations 必须通过正式 loader 对 7 个 SQL 做文件集合、顺序和 manifest checksum 校验；verify_packaged_seed 必须对 registry 中精确 8 个 scenario JSON逐个校验。私钥只留在 0700 临时目录，不得出现在 Compose config、volume 或容器；不得新增 tracked PEM，也不得把源码树加入 PYTHONPATH 来绕过 wheel 缺包。

PostgreSQL proof：

    cd "$REPO_ROOT"
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait postgres
    docker compose "${COMPOSE_ARGS[@]}" run --rm migrate apply
    docker compose "${COMPOSE_ARGS[@]}" run --rm migrate check
    docker compose "${COMPOSE_ARGS[@]}" run --rm test-bootstrap
    docker compose "${COMPOSE_ARGS[@]}" up -d --wait api demo-api internal-api worker
    python -m pytest -m postgres -q
    python -m pytest -m asgi_lifespan -q
    python -m pytest -m e2e -q
    python -m pytest -q
    docker compose "${COMPOSE_ARGS[@]}" down --volumes --remove-orphans
    scripts/verify_backend.sh all
    cleanup_proof
    trap - EXIT INT TERM

cleanup trap 确保命令块无论成功或失败都以同一 COMPOSE_ARGS 执行 down --volumes --remove-orphans；它只在 PROOF_ROOT 精确匹配 /tmp/sleepagent-wheel-proof.* 时删除临时目录。实现中不得使用删除 migration 文件、改 checksum 或重写 ledger 来让 migration check 通过。

## 13. 最终验收标准

全部满足才可称完成：

1. ASGI 只有 sleepagent.app:app 一套权威 composition；旧 backend import为 0。
2. Worker 只有 workers/runtime.py 一套 durable runtime，queue 全部显式注册。
3. 无 SleepApiRuntime/RadarPersistenceStore/SQLite 双轨。
4. 无 Stage2/3/4/5 生产 Python 模块路径、Python 符号、测试文件或脚本命名；历史数据库/version/event identity只集中在 migration metadata/SQL及其单一验证处。
5. canonical app/worker import 不加载 legacy/test/diagnostic 模块。
6. 第二套 tests/support FastAPI backend 和整套断链 frontend 已删除。
7. Perceptor/旧 task runtime/旧 diagnostic device API 无代码、测试、console、文档残留。
8. 每个功能只有一个 canonical实现：API、Worker、Product durable commit、LLM transport、replay registry各一套。
9. 四 Agent exact roster、角色隔离、conditional Safety、tool permission、HITL fail-closed 与 PostgreSQL fenced commit未意外改变。
10. public /api/v1、/product/sleep、demo/internal surface contract通过现有 snapshots和reference client验证。
11. PostgreSQL migration 001–007、manifest、checksum、ledger、RLS、transaction scope、lease/fence/idempotency/outbox/reconciliation通过。
12. production Python不超过95个且不超过70K LOC；all Python不超过165个且不超过108K LOC。
13. test Python不超过65个、tests tracked不超过85个；保留约650–800个代表性 cases而非追求数量。
14. docs不超过8个；无历史 PLAN/review/completion/evidence archive。
15. root tracked目录为6个，sleepagent一级 package目录为6个，职责与第7节一致。
16. 除本 PLAN.md 外没有语义新增源码、测试或文档文件；无新增微型 Port/Adapter/Facade/Contract层。
17. 所有 retained production Python文件有简洁中文职责 header，高风险事务/鉴权/并发/Agent块有“为什么”注释；OpenAPI/Pydantic metadata不因注释改变。
18. clean wheel、Compose replay harness、unit、PostgreSQL、ASGI lifespan、process e2e、mypy、compile、OpenAPI全部通过。
19. 最终统计达到：true DELETE至少215、merge-away至少24、MOVE 20–30、tracked files不超过261。
20. 最终交付说明完整列出删除了什么、为什么、旧 consumer一并删除了什么、功能现在由谁承担；不新增 audit 文档来保存过程。

## 14. 停止条件

只有以下情况允许暂停执行并请求用户决定：

- 找到可验证的仓外 real-perceptor-acceptance owner，且删除会破坏仍受支持的发布契约；
- characterization 证明 canonical Product worker 实际依赖计划删除的 Habit/continuation public输出；
- migration 001–007 checksum/ledger 在干净数据库或现存升级数据库不一致；
- clean PostgreSQL proof 暴露当前 canonical API/Worker 本身不可运行，而修复需要扩展产品语义。

遇到停止条件时，不得默认保留整套旧链。应报告具体 consumer、最小必要能力和两种选择；任何 fallback 都必须继续满足“PostgreSQL唯一权威、无第二 Runtime、无 compatibility shim”的总目标。
