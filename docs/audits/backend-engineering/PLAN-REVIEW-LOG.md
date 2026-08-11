# Plan Review Log: SleepAgent FastAPI 后端工程基础与模拟闭环
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.

## Round 1 — Codex review

VERDICT: REVISE

1. **Blocker — closure authority 冲突。** 既有 closure plan 仍以 file-backed SQLite 和 lifespan Worker 为目标，本计划未明确覆盖，实施者可能维护两套权威。Fix：显式 supersede SQLite/lifespan 部分，只复用领域与验证协议。
2. **Blocker — runtime/UoW 所有权矛盾。** Singleton runtime 持有 repository 与每事务 pooled connection 不兼容，且 API 仍可拥有 model/provider。Fix：runtime 只持 pool/UoW factory，并使用 API/Worker/Migration capability profile 与独立凭证。
3. **Blocker — 缺少可执行批次与 file map。** 十个阶段仍会迫使 `codex-build` 临场决定路径和中间状态。Fix：增加 B0–B6、精确模块所有权、新 migration 编号和 proof commands。
4. **Blocker — namespace/runtime 拓扑不明确。** 未说明一个 Worker 如何安全处理多个 replay run。Fix：deployment 固定单 data mode，live/replay 分库分凭证；数据库 namespace registry 授权多 run，claim 有 per-namespace concurrency/fairness。
5. **Blocker — Operation/outbox 双重调度权威。** 同一工作可能同时由 Operation 与 event outbox 触发。Fix：冻结 normalization work、Operation、domain event、delivery intent 的唯一职责；下一阶段 Operation 在源事务中显式创建。
6. **Blocker — lease/fencing 不是完整协议。** 缺 claim/heartbeat/checkpoint/finalize 三阶段、server time 和 shutdown 语义。Fix：短事务 claim、DB 单调 generation、独立 heartbeat、所有写 fenced；外部调用不占连接，未知在飞调用不主动释放 lease。
7. **Blocker — LLM unknown outcome 会被 lease reclaim 重复调用。** Fix：加入 append-only invocation journal 和 request-sent reconciliation gate。
8. **Blocker — outbox fan-out、顺序和消费者去重未冻结。** Fix：destination-specific intent、durable inbox/dedupe、per-aggregate predecessor/sequence checkpoint。
9. **Blocker — 醒来日缺少 Episode 运行期语义，且可能静默破坏 `/api/v1`。** Fix：稳定 Episode UUID、日期 provisional/finalize/correction 规则；保留旧 v1 字段语义并新增版本化显式日期。
10. **Blocker — 鉴权 foundation 排序过晚且快照不完整。** Fix：B1 先落 DB authority；权限取四层交集，Operation 保存 AuthorizationSnapshot，并在 claim/commit/effect 重验 authorization/privacy/retrieval epochs。
11. **Blocker — handle consume 与业务 enqueue 不原子。** Fix：重新鉴权、target/state CAS、single-use consume、Operation/outbox/audit 同一 UoW。
12. **High — replay/live 只靠应用字段、demo clock 边界不够强。** Fix：分库分凭证、关键表 DB guard/RLS；HTTP 只能推进 ScenarioClock。
13. **High — UUIDv7 与既有 TEXT/任意 ID 升级不明确。** Fix：仅约束新写入，UUIDv7 canonical string 写 TEXT，旧 ID 保持 opaque/readable。
14. **High — encryption/shred 无数据类别边界。** Fix：subject/namespace DEK 与 retention matrix，receipt 区分 destroyed/expired/retained；KMS/备份传播仍列生产 gate。
15. **High — Safety 可能被实现为 attempt 级一次批准。** Fix：每个 target 独立 hash/checkpoint，任何上游版本变化失效并复审。
16. **High — 测试门禁不可直接执行。** Fix：增加 unit/postgres/real-lifespan/multiprocess E2E 命令，并明确处理现有 3 个失败。

### Codex response

接受上述 16 项并修订 PLAN。另保留两项原选择：不在首期引入 ORM/专用 broker，因为显式 psycopg UoW 和数据库队列已经足够验证协议；不要求对既有任意字符串 ID 做高风险全库 backfill，而是将 UUIDv7 约束施加于新写入。计划同时明确覆盖 closure 中的 SQLite authority，并把数据库身份权威提前到 B1。

## Round 2 — Codex review

VERDICT: REVISE

1. **Blocker — stage handoff 仍可能断链或双调度。** 原子边界只写 outbox，未明确 raw→normalization、revision→fast path、fast path→Agent、Agent→induction 的唯一 claim row。Fix：逐段把下一阶段 work/Operation 纳入源事务，domain outbox 永远只是 committed event log，不再有平行 induction job。
2. **Blocker — v2 醒来日不能直接落在 v1 非空 date/night_key 模型。** 也未处理同日冲突。Fix：引入稳定 episode anchor 与 v2 nullable/provisional date；v1 仅 compatibility projection；一个 subject/date 只允许一个 canonical main episode，冲突 fail closed 进入 reconciliation。
3. **Blocker — RLS 或 guard 仍是未决分支。** Fix：锁定 `SET LOCAL` + PostgreSQL RLS，migration owner 唯一 bypass；跨 namespace 只经窄幅 claim function，pool reset 后归还。
4. **Blocker — system Operation 与 actor AuthorizationSnapshot 矛盾。** Fix：区分 user `AuthorizationSnapshot` 与 system `WorkloadAuthorizationSnapshot`，系统任务不伪造老人角色。
5. **High — invocation journal 未冻结 send 前状态。** Fix：网络前先提交 `send_started/outcome_possible`；停在该状态只能 reconciliation。
6. **High — DB isolation/锁顺序/事务重试未定义。** Fix：READ COMMITTED、统一锁顺序/timeouts，仅无外部调用短 UoW 可整体重试 40P01/40001。
7. **High — confirmation handle 未显式绑定 privacy/retrieval epoch。** Fix：句柄和同一消费 UoW 增加三类 epoch 与 policy hash。
8. **High — DEK 粒度不能支持不同 retention class。** Fix：DEK 增加 retention domain/generation，审计使用独立假名化密钥域。
9. **High — migration 自举、hash ledger 和 batch 对应不闭合。** Fix：021 专门自举 ledger v2；022 identity/scope；023 work；024 episode v2；025 retention。
10. **High — proof commands 缺 profile/DSN/CLI/markers 落点。** Fix：file map 增加 locks、compose、env、CLI、conftest；命令固定 test profile、compose project、healthcheck 和 marker。
11. **High — 撤权无法取消已经在飞的 effect。** Fix：定义 dispatch permit 线性化点，只承诺阻止尚未取得 permit 的发送，已在飞结果诚实记账/reconcile。
12. **High — B5 声称完整 journey，但 retention 在 B6。** Fix：B5 明确不含 forget/retention，B6 完成后才满足完整门禁。

### Codex response

接受全部 12 项并修订。选择一个 canonical main NightEpisode/subject/date；split-night 在同一 active Episode 内聚合，无法安全合并的同日冲突不自动覆盖而进入 reconciliation。锁定 PostgreSQL RLS，不再保留“等价 guard”分支。保留同步 psycopg、数据库队列和 legacy TEXT PK 的既定取舍。

## Round 3 — Codex review

VERDICT: REVISE

事务与安全审查未发现 blocker/high 矛盾；可执行性审查发现一个剩余 High：

1. **High — clean environment 只安装锁定的第三方依赖，没有安装本地项目。** 后续 `sleepagent-demo` 不存在，而普通 `pip install .` 又会绕过 lock 重新解析依赖。Fix：lock 安装后显式 `pip install --no-deps .`；Docker 同样先按 production lock 安装，再 `--no-deps` 安装项目 wheel。
2. **Consistency — success criteria 对日期和 ID 表述过度绝对。** 无 wake 使用 fallback，legacy ID 也不会回填 UUIDv7。Fix：成功标准改为 wake 优先、fallback 显式 estimated，以及只约束新内部聚合。

### Codex response

接受并修订两项。补充 Dockerfile 所有权、`sleepagent-demo` console script 门禁和 clean-environment 项目安装步骤；同时修正两处成功标准。未改变已批准的事务、安全、RLS、日期状态机或 batch 边界。

## Round 4 — Codex review

VERDICT: APPROVED

最终复核未发现 blocker 或 high-level 矛盾：

- clean environment 与 Docker 都先按 hash-locked requirements 安装第三方依赖，再以 `--no-deps` 安装本项目/wheel，console entrypoint 可验证；
- B0–B6 批次、模块所有权、021–025 migration、PostgreSQL RLS、身份 authority 和 Episode v2 迁移具备可执行顺序；
- UoW、唯一 stage handoff、lease/fencing/heartbeat、invocation unknown outcome、delivery、撤权线性化和故障测试保持闭合；
- 未发现新的安全、数据、日期、Agent 权限或 API 兼容阻塞。

Plan approved after 4 review rounds. This was a Codex-native same-session adversarial review, not a second-model review.
