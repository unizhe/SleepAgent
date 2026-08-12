# Plan: SleepAgent 后端工程 rebase — current-freeze remaining work

_Locked via grill — by Codex + SleepAgent 项目负责人_

_Adversarial plan review: APPROVED after 5 rounds_

## Plan authority

本计划以历史文档 `docs/audits/backend-engineering/PLAN.md` 作为 design input，但以当前 repository freeze 作为唯一代码事实。历史文档、历史 build log 和测试名称只能帮助定位 intent，不能覆盖当前代码。

- Freeze commit：`c1cb1e6bf40c9ed7d73e6394ea724f356980b747`
- Freeze tag：`local/repository-architecture-accepted-20260811`
- Freeze branch：`agent/repository-structure-cleanup`
- 审计起点：内层仓库工作树 clean；本计划文件本身是此后的文档变更。
- 测试事实边界：freeze 的 build log 记录过 `1,076 passed, 5 skipped`，但本次环境是 Python 3.13 且当前 `.venv` 没有 pytest，因此该数字不是本次 fresh rerun。任何 execution stage 都必须重新产生自己的 proof。
- 迁移事实：唯一 migration 是 immutable `001_initial_schema.sql`。旧 PLAN 的 `001–020`、`021–025` 链不再存在，也没有兼容旧开发数据库链的义务。

状态词只允许以下五种：

| Status | 含义 | 执行含义 |
|---|---|---|
| `INHERITED` | requirement 已由 freeze 的代码、schema 或强制边界实现 | 不再成为执行阶段，只加回归门禁 |
| `SPLIT` | 一个复合 requirement 的一部分已实现、另一部分仍缺失 | 已实现部分继承，缺失部分必须映射到新 stage |
| `REMAINING` | 当前不能真实执行或不能证明 | 必须进入新 stage 和验收门禁 |
| `SUPERSEDED` | 历史假设、编号或兼容策略已被 freeze/本轮决定替代 | 禁止按旧方式复活 |
| `OUT_OF_SCOPE` | 明确不属于本次模拟后端闭环 | 不得作为 stage 偷渡进来 |

## Goal

从当前 freeze 出发，完成一条 production-shaped、replay-only 的真实后端闭环：外部 verifier 通过受隔离的 Demo HTTP 控制面发起“模拟一晚睡眠”，独立 Worker 把 allowlisted external facts 转换并分批写入 PostgreSQL，现有 Sleep Domain pipeline 产生 committed NightEpisode，现有 Product Runtime 针对 exact revision 完成 prepare/commit，原子生成 elder、family、doctor 三个最小化角色投影，三个经签名授权的 Product API 请求读取同一 `analysis_revision_id` 的 typed `/product/sleep/today`。

闭环同时证明：命令 reservation、租约/fence、重启恢复、因果 trace、对象/字段授权、replay 水印、真实 downstream handler 边界，以及有界 retention 行为。它不要求本期完成真实设备、真实模型、正式通知渠道、托管 KMS 或生产级数据生命周期平台。

## Direct answer: 第一条 `/today` 闭环还缺什么

当前 freeze 已有 `raw → normalization → NightEpisode → fast_path → product_agent → AnalysisRevision + 三 role views` 的核心 PostgreSQL能力，但还不能从公共入口真实跑通。第一条闭环缺少以下九组能力；它们共同构成 Stage 1，而不是单独补一个 endpoint：

1. **可演进 migration release**：当前 runner 只接受 exactly `001`。必须在不改写 `001` 的前提下支持 manifest-pinned `002+`、`001 → target` apply/check/status、checksum/顺序/事务策略校验，以及 API/Worker 对完整 release manifest 的精确 attestation。
2. **新的 canonical normal fixture 与 adapter**：仓库没有 `normal-one-night`。需要 facts-only、单夜、充分质量、non-urgent、无问答/Care/投递的 allowlisted fixture；还需要把 generator output 转成真实 `ReplayObservationInput` 的版本化 adapter 和基于 adapter canonical bytes 的 `replay_ingress_manifest.v1`。generator 生成的内部 ID/provenance 不能直接成为数据库权威。
3. **真实且隔离的 Demo seed**：当前 Demo controller 全部返回 503。`POST /demo/v1/seed` 必须只接收 scenario 标识和 batch size，服务端校验 allowlist/schema/hash/pins，并在短事务中预留 root Operation 与 journey；不能在 HTTP request 内运行一晚、调用 Runner 或插入整套 observations。
4. **服务端 authority bootstrap**：当前 integration tests 靠 admin SQL 手填身份。root journey 必须通过窄幅、审计过的 server-owned bootstrap 创建 replay namespace/generation/run/单 arm/subject/provider/device、elder/family/doctor actors、bindings、epochs 和 grants；请求不能自报 ID、role、scope 或 data mode。
5. **不会烧掉 retry budget 的 root journey**：当前 Worker 没有等待子任务的成功语义，用 retry 轮询会在 5 次后 dead-letter。需要专用 `replay_journey` claim row/handler、`waiting_children` disposition、bounded resume/deadline、append-only checkpoints，以及与外部 root Operation 分离的 wait/failure counters。
6. **有序、幂等的 raw batching**：`ReplayIngressHandler` 是 worker-only 且尚未被 Demo 调用；normalization claim 没有 per-stream predecessor gate，多 Worker 可能把 wake 排到 bed-in 前。需要稳定 batch idempotency identity、`stream_key + sequence + predecessor` 约束、每批原子 raw/inbox/normalization handoff，以及 crash 后补 checkpoint 而非重复语义写入。
7. **root 到 Product commit 的闭合因果链**：root 必须只从 committed receipts 判断全部 intake、一个 finalized current NightEpisode、sufficient/nonurgent fast path、exact Product child、一个 AnalysisRevision 和 exactly three role views；seed import 完成绝不能让 root 成功。任何不可恢复 child failure 以稳定 code 终结同一个 root。
8. **真正的 typed public `/today`**：当前四个 Product read routes 都从同一个内部 `view_json/content: dict` 取数。必须把 `/today` breaking-in-place 为 `product_sleep_today.v1` 的严格 role-discriminated union，并在 Product commit 事务中写入 public projection；API 不得解析或透传 Agent/prompt/tool/source/claim/failure envelope。`trends/care/records` 和尚未实现的 interaction commands 在各自 stage 前返回 typed `501 capability_not_implemented`。
9. **真实外部验证和身份隔离**：同一 canonical app factory 需以独立 Demo API DB role 与 Product/BFF API DB role 启动；Product server 只持 verifier JWS public key，verifier 独占 private key。必须有 clean run 和“seed 已接受、Product commit 前杀 Worker再重启”两次黑盒 HTTP run，证明同 root 恢复且无重复 Episode、AnalysisRevision 或 role projection。

## Stage 1 success criteria

Stage 1 只有同时满足以下条件才算完成第一条真实闭环：

1. `POST /demo/v1/seed` 在一个短 PostgreSQL transaction 中验证 demo-controller authority、`Idempotency-Key`、server allowlist 和 generation，创建或复用 CommandReceipt、root Operation、journey claim row、audit/outbox，返回 `202`、`root_operation_id`、`generation`、`status_url`。同 key 同 body返回原root；同key不同body返回409；同generation/scenario/pins的不同合法idempotency keys通过server semantic key收敛到同root。
2. 独立 `replay_journey` Worker 每次只推进一个 durable checkpoint 后 yield；它通过 canonical ingress adapter 写不超过 100 observations 的 batch，不同步调用 normalization、fast path、Product handler，也不在 lease 内 busy-wait。
3. `replay_ingress_manifest.v1` 固定 scenario/schema/generator/adapter pins、adapter canonical sequence hash、count、first/last received time、`night_count=1`。server 只复制 external facts/opaque provider identities，所有内部 ID、data mode、evidence kind 和 provenance pins 由服务端生成。
4. 同一个 replay scope 内只有一个 current finalized、`date_conflict=false`、`assignment_basis=observed_wake` 的 NightEpisode revision；全部 intake canonicalization 与 Episode 的 in-window member set 分别验证，不能错误要求 Episode observation count 等于包含 connectivity observations 的 manifest count。
5. fast path 对 exact episode revision 产出 sufficient quality、nonurgent risk，并只创建一个 Product Operation；Product Worker 对 exact source revision 成功，原子提交一个 AnalysisRevision 与 role set恰好为`{elder,family,doctor}`的三个READY public projections，三者共享analysis revision、authority epochs、policy/schema pins和同一commit transaction。
6. root Operation 仅在上述 committed 条件全部成立后 `succeeded`，result refs 至少包含 NightEpisode/revision、fast-path op、Product op、`analysis_revision_id`、三个 projection IDs 和 manifest hash。等待不是失败尝试；terminal child failure、deadline、date conflict、unexpected urgent 或 projection 缺失使用稳定 terminal code。
7. elder、family、doctor 三个有效签名请求读取同一 `analysis_revision_id`；每个响应只含本角色 schema，cross-role、cross-subject、错误 path/body hash、nonce replay 和旧 epoch 均 fail closed。
8. Product/Demo response body强制 `data_mode=replay`、`synthetic_non_release=true`；所有 `/api/v1` response从Stage 1起强制发送 `X-SleepAgent-Data-Mode: replay` 与 `X-SleepAgent-Synthetic-Non-Release: true`，保留v1 body兼容。`/today` 无 committed projection时返回typed `200 no_data`，不能即时调用Product Runtime，也不能泄露pending operation。
9. clean process run 与 Worker restart run 都通过外部 HTTP verifier。restart run 保留同一 root ID，数据库 invariant tests 另行证明一个 current Episode/revision、一个 Product AnalysisRevision、exact three role projections；verifier 本身不得调用 Runner、repository、internal admin route 或 admin SQL。

## Inherited baseline

以下能力是 current freeze 的 inherited baseline。实现阶段不得重建平行 runtime/store/protocol，只能扩展并用 regression gates 保护：

- typed fail-closed settings、PostgreSQL-only canonical authority、deployment/data-mode/process-role profiles；
- `SleepBackendRuntime` composition root、canonical backend app factory、pool/UoW/repository ownership；
- API、Worker、migration 的独立 compose process topology 与数据库角色/pool budget；
- psycopg 3、explicit UoW、scope GUC reset、READ COMMITTED、timeouts、repository 不自行 commit；
- server UUIDv7、opaque external IDs、namespace/data mode/subject scope、wake-date episode contract、deadline fallback、date-conflict state；
- CommandReceipt/Operation reservation schema、lease generation、fencing token、heartbeat、retry/dead-letter/outcome_unknown、outbox/inbox/journal protocol；
- raw inbox → normalization、normalization → NightEpisode/fast path、fast path → Product Operation 的原子 handoff；
- deterministic urgent zero-model fast path；
- canonical four-role Product Runtime、deny-by-default capability registry、target-specific Safety、prepare/persist-prepared/caller-owned commit；
- Product commit 对 internal attempt、one AnalysisRevision、three role views、terminal Product Operation/outbox 的原子性；
- PostgreSQL principal/actor/binding/grant/epoch authority、request-bound asymmetric JWS、nonce replay protection、cursor authority；
- pending-handle、delivery、retention、shred receipt 的 schema/protocol scaffolding；它们不是可执行 handler，不能因此算完整功能；
- strict Product request DTO 与 ASGI receive/body limiter；
- HTTP-only verifier boundary：`sleepagent-demo` 不导入 Runner/repository。

## Target remaining topology

```text
external verifier
  ├─ demo token ───────> Demo API process / demo DB role
  │                        └─ reserve root Operation + replay_journey
  └─ actor JWS private ─> Product/Sleep API process / BFF DB role
                           server mounts public verification key only

replay_journey Worker
  └─ server registry bootstrap
     └─ ordered replay raw batches
        └─ inherited normalization
           └─ committed NightEpisode revision
              └─ inherited fast path
                 └─ inherited Product prepare/commit
                    └─ 3 typed public role projections
                       └─ GET /product/sleep/today

later real handlers
  ├─ sleep_command + product_interaction
  ├─ induction
  ├─ delivery + reconciliation
  └─ bounded retention
```

Demo tables and `journey_id` are orchestration evidence only. Sleep Domain and Product Runtime must not import Demo modules or use journey state as business authority; every root checkpoint points to canonical committed IDs/hashes.

## Fresh dependency-derived stages

旧 `B0–B6` 全部作为执行阶段废止。新 stages 从 freeze 的真实缺口和依赖关系重新生成：

```text
Stage 1: first /today closure
  ├─> Stage 2: public commands + interactions
  │      └─> Stage 3 care track
  ├─> Stage 3: dedicated read models + abnormal journeys
  └─> Stage 4: induction + delivery + reconciliation
             └─> Stage 5: bounded retention
                    └─> Stage 6: system hardening + final proof
```

### Stage 1 — 模拟一晚到 typed `/today`

#### 1.1 Migration release unlocker

1. 保持 `sleepagent/persistence/migrations/001_initial_schema.sql` byte-for-byte immutable。新增 committed `migration_manifest.json`，按 version、filename、sha256、transactional flag、minimum app release 排序固定当前 release。
2. 把 migration CLI 扩展为 manifest-only `apply/check/status`：session advisory lock；拒绝 unknown/missing/duplicate/gapped file、checksum drift、target 前后多余 ledger row和未完成 migration；API/Worker 绝不执行 DDL。
3. transactional migration 每个文件单独 atomic commit。任何明确标记 non-transactional 的未来 migration 必须 fail closed 并有人工恢复 runbook；Stage 1 的 `002` 必须 transactional。
4. runtime readiness 校验 exact target version、manifest digest 和每一条 applied checksum，不再只看 `MAX(version)`。
5. 支持的唯一upgrade lineage是fresh install `001 → target`与任意schema-valid current `001 → target`，后者可含当前001格式的数据且migration不得删除或重解释它。新增ordered-ingress规则只约束新work；旧generic role rows不做猜测性JSON backfill，public `/today`对没有`public_schema_version`的legacy row fail closed为`no_data`，由新reanalysis生成typed projection。不支持已被squash的旧开发 `001–025` migration lineage。

Stage 1预期新增 `002_replay_journey_and_today.sql`，至少承载：journey/checkpoint/event claim state、waiting semantics、scenario allowlist metadata、ordered ingress sequence/predecessor、typed public projection字段或等价专表、相关unique/FK/CAS/RLS、窄幅replay bootstrap function和最小角色grants。Stage 1必须补齐Episode/current quality/current risk/role projection/journey等本slice可达表的RLS；所有SECURITY DEFINER functions固定safe `search_path`、owner、session role检查，revoke PUBLIC execute，只grant给精确workload role。

#### 1.2 Facts-only fixture 与 ingress contract

1. 新增 canonical `normal-one-night` fixture：一位 subject、一个 provider/device、一个正常睡眠窗口、sufficient quality、nonurgent risk、observed wake；不含 expected output、oracle verdict、question、Care action、delivery、confirmation 或 multi-night data。
2. fixture runtime package 只包含 strict external facts。`expected.json`、golden actions 和 verifier oracle 仅存在 verifier/test distribution，不被 server wheel、runtime image 或 import graph包含。
3. 新 `replay_external_fact_adapter.v1` 把 generator external facts 转成 `ReplayObservationInput`；丢弃 generator 的 internal raw/candidate/observation IDs 和 canonical provenance，使用 server UUIDv7 与 allowlist pins。
4. `replay_ingress_manifest.v1` 对 adapter 输出的 canonical bytes sequence 哈希；旧 `GeneratedReplayManifest` 只能作为 legacy/design input，不能作为 PG intake authority。
5. 每个 stream 的 sequence/predecessor 由 server registry生成。只有 predecessor succeeded 的 normalization work 可 claim；stable batch/body/observation idempotency keys 允许在 batch commit 与 journey checkpoint 之间 crash 后安全补记。

#### 1.3 Durable Demo root 与安全 bootstrap

1. 将Demo API与Product/Sleep API作为同一canonical app factory的两个capability-scoped process profile运行，使用不同DB roles、pool、credentials和OpenAPI。Demo profile只挂`demo + livez`，BFF profile只挂`api_v1 + product + livez`；任一profile混挂双方surface都在settings validation失败。production profile不挂Demo。
2. `POST /demo/v1/seed` v2 request 固定为 `{artifact_family, scenario_id, batch_size<=100}`。server lookup allowlist；request 不能上传 observations、expected values、IDs、roles、scopes、run/arm 或 data mode。
3. API reservation 创建 external root Operation 与独立 `backend_demo_journey` claim row。root 是整晚唯一 receipt；journey row 是唯一 claim authority，不以 outbox重复触发。
4. `replay_journey` handler 使用窄幅 SECURITY DEFINER bootstrap 创建或复用 namespace/current generation/run/exactly-one active arm/subject/provider/device/three actors/bindings/grants/epochs；actor aliases 和 scope matrix来自 server registry。
5. root origin 是 demo-controller workload，不伪造 actor。server registry固定BFF public-key fingerprint和scope matrix；E2E harness每次生成ephemeral Ed25519 pair，private file只以0600权限挂载到verifier临时目录，Product/Sleep API只挂public key并记录fingerprint。server不得使用可由`test:`配置推导private key的provider。三actors都获 `product:sleep:today:read`，只有elder verifier actor额外获本slice所需 `sleep:episode:read`；demo-controller token/role不能调用Product/Sleep API。
6. journey durable states固定为 `accepted | staging_input | waiting_normalization | waiting_episode | waiting_fast_path | waiting_product | verifying_views | succeeded | blocked | reconciliation_required | failed`。等待计数、deadline与真正 failure attempt分离。
7. 新增 typed `GET /demo/v1/operations/{root_operation_id}`；`GET /demo/v1/trace?operation_id=...` 返回 append-only causation projection。trace不保存 oracle答案，也不成为业务查询authority。
8. terminal error codes至少固定：`scenario_contract_invalid`、`generation_fenced`、`input_quarantined`、`normalization_failed`、`episode_not_committed`、`episode_date_conflict`、`quality_insufficient`、`unexpected_urgent_route`、`product_failed`、`role_projection_incomplete`、`journey_deadline_exceeded`。
9. Stage 1实现只读 `GET /demo/v1/clock`；`POST /demo/v1/advance` 在Stage 3前、`POST /demo/v1/reset` 在Stage 5前均返回typed `501 capability_not_implemented`，避免Stage 1门禁依赖尚未实现的lifecycle行为。

Journey protocol invariants：

1. `backend_demo_journey.root_operation_id`唯一且不可变；row固定namespace/generation/run/arm/subject、scenario hash、generator/adapter/model/policy/schema pins、phase、version、lease generation/token、`resume_at`、deadline、wait count和failure attempt。一个active generation只允许one arm与one semantic journey。
2. CommandReceipt reservation key使用demo service principal、route和caller idempotency key；body hash只作冲突比较。root Operation是workload-origin receipt，actor为null；两者在同一API transaction建立引用，不把demo token/JWS保存进payload。
3. semantic key至少包含generation、scenario/manifest hash、generator/adapter/model/policy/schema pins。不同caller keys的等价请求可复用root；不同pins或body不能错误复用。
4. 每次claim、checkpoint、WAIT和terminal transition都以journey version、lease generation和fencing token CAS。WAIT transaction原子保存checkpoint/phase/`resume_at`并释放claim，不增加failure attempt；wait count有deadline/上限但不触发dead-letter retry policy。
5. `resume_at`使用PostgreSQL server time。child commit可写普通outbox wake-up hint，但claim eligibility仍以journey row为唯一authority；丢失或重复hint不影响正确性。
6. checkpoints是append-only、按`(journey_id, phase, semantic_checkpoint_key)`幂等唯一，只保存canonical refs/hashes/counts。batch commit后checkpoint前crash时，handler依赖stable intake idempotency补记，不创建第二批语义数据。
7. success/failure transaction同时写immutable terminal verification receipt、terminal journey row、root Operation result/error、audit/outbox；不允许journey terminal而root pending，或root succeeded而verification receipt缺失。root polling只投影这些committed rows。
8. replay bootstrap function只接收root/journey ID，不接收caller提供的namespace/actor/role/scope；函数重新验证session DB role、demo workload grant、allowlist和current generation后，从server registry生成authority rows。
9. Stage 1 worker workload grant只允许`replay_journey`、`normalization`/ingestion、`fast_path`、`product_agent`所需handlers。single-worker test profile的claim priority固定让ingestion/normalization、fast path、Product work先于到期journey polling；每次journey claim只推进一个checkpoint，防止root自轮询饿死child queues。

Stage 1 transport/error contract：

| HTTP | Stable code family | Required behavior |
|---|---|---|
| `400` | `invalid_request` / `idempotency_key_required` | malformed header、unsupported batch size或非schema JSON；不创建receipt |
| `401` | `authentication_failed` | bad/missing demo token、service bearer或actor JWS |
| `403` | `authorization_denied` | wrong surface/principal/role/scope/subject/purpose；不透露目标存在性 |
| `404` | `scenario_not_found` / `operation_not_found` | unknown/inactive allowlist ID或不可见root |
| `409` | `idempotency_conflict` / `generation_fenced` | same key different body、stale generation或second conflicting root |
| `413` | `request_too_large` | ASGI limiter在parse前拒绝 |
| `422` | `scenario_contract_invalid` / `input_quarantined` | packaged artifact的schema/pin/count/hash/order不满足；保留受限quarantine receipt |
| `501` | `capability_not_implemented` | route存在但未到对应stage；先完成auth再返回typed error |
| `503` | `dependency_unavailable` | DB/key/authority不可用；不能伪造accepted receipt |

一旦seed已返回202，child失败通过`GET /demo/v1/operations/{root}`的HTTP 200 terminal state与stable error code表达，不把异步业务失败改写成随机5xx。所有error envelope使用server-generated correlation ID与replay watermark，拒绝采用客户端提供的correlation ID。

#### 1.4 Typed Product `/today`

`GET /product/sleep/today` 直接 breaking-in-place 为严格 `product_sleep_today.v1`。现有 generic `ProjectionResponse/content: dict` 被视为未发布 placeholder，不提供兼容 adapter。

公共 envelope 固定为：

```text
schema_version = product_sleep_today.v1
data_mode = live | replay
synthetic_non_release = boolean
state = no_data | ready | degraded | blocked
subject_ref
role = elder | family | doctor
episode_id / episode_revision_id / episode_local_date / assignment_basis
analysis_revision_id
projection_id / projection_version / committed_at
content = ElderTodayContent | FamilyTodayContent | DoctorTodayContent
```

约束：

1. 顶层response是由 `state`判别的严格union。`no_data`仍返回watermark、`subject_ref`与已授权request role，但episode/analysis/projection/committed/content字段全部为null；`ready|degraded|blocked`时这些identity/commit字段和typed content全部non-null。这样`200 no_data`没有伪造projection，也没有schema歧义。
2. committed `content` 以 `role`/`audience` literal作为第二层discriminator；elder/family/doctor 是三个不同 Pydantic model，不允许 `dict[str, Any]`。
3. elder/family最小字段为各自 `summary_text` 与 role-specific `context_notice`；doctor可额外含受控 `evidence_refs`。任何字段增加都需合同版本化和字段授权矩阵。
4. 不暴露 Agent、prompt、tool、graph、internal episode run、claim/source refs、failure details、model/policy hash或内部 content string。
5. Product commit 同一 UoW 写 internal role view 与 `public_schema_version/public_today_json/projection_sha256` 或等价 typed projection row；API只解析受版本约束的 public payload。
6. query必须绑定 current finalized Episode/revision、exact AnalysisRevision、exact role及 current authorization/privacy/retrieval epochs，每个角色最多一条。没有 committed row返回 `200 no_data`。
7. `committed_at` 使用commit阶段的数据库时间语义；当前freeze把role-view row的时间参数取自commit阶段，review不得误报为prepare-time bug；新public field仍须由commit UoW固定并做测试。
8. `/trends`、`/care`、`/records` 在 Stage 3 前返回 typed `501 capability_not_implemented`。所有 `/interactions/*` mutations和 `/api/v1` mutations在 Stage 2 前同样 fail closed；不得返回一个永远被错误 handler拒绝的 202。
9. Stage 1在canonical response path集中注入v1 replay headers，覆盖success、typed error、pagination和operation reads；客户端不能通过自报header覆盖server值。

#### 1.5 Stage 1 proof gate

1. migration tests：fresh `001 → target`、populated valid `001 → target`且legacy rows保持、legacy generic projection不被猜测backfill、checksum/name/order drift、started-not-finished、concurrent migrator、runtime exact-manifest mismatch及newer-than-supported schema。
2. unit/property tests：fixture strict parse、adapter canonical hash、server-owned pins/IDs、semantic root dedupe、batch idempotency、predecessor ordering、root WAIT不消耗failure retry、terminal code、typed role union、forbidden internal keys。
3. PostgreSQL integration：root/CommandReceipt/journey atomic reservation、等价caller keys复用、bootstrap grants、RLS role mismatch、raw→normalization→Episode→fast→Product causation、Product commit exact role-set原子可见、WAIT/reclaim/stale fence、batch commit/checkpoint crash window、terminal receipt/root atomicity，以及由integration fixture推进authority generation后的stale-generation fence；不调用尚未实现的Demo reset route。
4. clean black-box run：real PostgreSQL + migrate/bootstrap + surface-disjoint Demo API process + Product/Sleep API process + Worker process；harness生成ephemeral keypair并校验server mounts不含private key；HTTP seed/poll/trace；签名读取 `/api/v1/subjects/{subject_id}/night-episodes` 和三次 `/product/sleep/today`。
5. recovery black-box run：seed返回后，supervisor只观察public trace；见到`waiting_product`且root未terminal时对Worker发`SIGKILL`，再启动新Worker；同root继续，最终三响应同analysis revision且无重复。另以PostgreSQL fault test在Product prepared/commit边界精确kill，证明prepared不可见与stale worker不可commit。测试不得使用in-process Worker/TestClient。
6. negative gate：unknown/tampered fixture、same key different body、oversize/duplicate/shuffled/predecessor gap、wake-before-bed、missing wake、insufficient/urgent/date conflict、stale generation、wrong role/subject/path/body/nonce/epoch、unsupported queue、public internal-key leakage。

### Stage 2 — 公共写命令与 Product interaction 状态机

依赖 Stage 1；两个 command family并行实施，复用 reservation/auth snapshot/Operation/lease/retry/outbox substrate，但使用显式 command type和真实 handler。

1. `/api/v1`：实现 monitoring activate/deactivate、elder feedback、family feedback、reanalysis 的 PostgreSQL reservation adapter与 `sleep_command` handler。activate/deactivate必须CAS MonitoringSnapshot并写transition receipt；feedback必须按elder-self/family-observation的canonical source追加human fact并创建唯一reanalysis child；显式reanalysis绑定exact current Episode revision并创建新Product analysis operation。每个命令有明确aggregate/CAS/result，不允许no-op terminal success。
2. `/product/sleep/interactions/*`：实现 start、ask、answer、confirm、decline、feedback 和 status；把当前 `interaction.* → product_agent` 错误路由改为专用 `product_interaction` handler/queue。
3. `start/ask` 创建或推进 persistent Conversation/ProductEpisode interaction revision；需要用户输入时进入 `waiting_user`，保存 frozen checkpoint并生成 server-owned opaque single-use handle。
4. `answer` 必须恢复 exact checkpoint与 FactSnapshot，不能静默重算目标。`confirm` 在一个UoW中完成当前authority/epochs、target/version/hash、single-use consume、HumanDecision、local CareAction state、future delivery intents/outbox/audit；`decline`只写decision/audit，明确不产生CareAction或delivery intent。
5. FactSnapshot补齐 exact Episode revision、quality/risk、source scope、authorization/privacy/retrieval epochs、policy/registry/model/schema hashes。任何漂移使旧 handle失效。
6. API OpenAPI中 `Idempotency-Key` 必须 required；Stage 1建立的v1 replay headers覆盖新增command responses，新的Product body继续内置watermark。
7. 在 Stage 2完成 bounded whole-UoW retry，仅允许无外部调用的短 transaction重试 `40P01/40001`；外部调用之后只能 fenced checkpoint/finalize。
8. Stage 2只证明database-local confirmed effect exactly once。delivery intents可以durable pending，但delivery queue在Stage 4前不得enabled/claimed，不能由no-op handler结单；正式deterministic sink effect属于Stage 4 gate。
9. 任何Product model/provider invocation从Stage 2起必须走现有invocation journal/dispatcher boundary；deterministic adapter也记录stable invocation key和digest，不能以“本地模型”为由绕过恢复语义。

门禁：每个命令 `202 → real terminal state`、同key重放、不同body conflict、监控snapshot CAS、feedback source不升级、唯一reanalysis child、撤权后queued op/handle失效、answer/confirm crash恢复、confirm local effect exactly once、decline zero Care/delivery effect、pending delivery未被提前claim、unsupported command typed 501。

### Stage 3 — 专用 read models 与非正常旅程

1. `/trends`：从多个 committed current NightEpisode/AnalysisRevision构建确定性、role-minimized、bounded cursor read model；GET不运行模型、不写数据库。
2. `/records`：返回 immutable lineage/read-only history，使用版本化 DTO和 opaque cursor，不复用 `/today` payload。
3. `/care`：只读取已确认且当前授权的 CareAction/CareFollowup projection；依赖 Stage 2 confirmation语义，未确认候选不可见。
4. 扩展 facts-only fixtures：data insufficient、device abnormal、urgent zero-model、Evidence question、Care confirm/decline、feedback reanalysis、3–7 night follow-up。Stage 5 public reset可用前，每个scenario test使用独立fresh database/namespace/generation；reset可用后才验证同workspace换generation。任何时候都不得在同generation建立多个active arms。
5. 完成 date-conflict deterministic reconciliation、version dispatcher/upcaster、metric/unit/source ontology，以及 generator/Habit/trends对 canonical episode date的全链迁移。
6. 实现durable `POST /demo/v1/advance`：只按generation-bound operation推进ScenarioClock和释放到时external facts，绝不能修改ControlClock、lease、auth、retry、retention或idempotency expiry。same key/body复用，stale generation失败；`GET /demo/v1/clock`可观察committed scenario time。

门禁：四个read route拥有不同schema/query/authorization tests；urgent scenario模型调用为零；question/answer恢复exact snapshot；Care只在confirm后可见；multi-night cursor和epoch drift fail closed；advance重放幂等、跨generation拒绝且ControlClock/lease deadline不变。

### Stage 4 — Induction、delivery 与 reconciliation 真实协议

1. Product terminal commit在同一 transaction中为 analysis group创建唯一 `induction` Operation；Operation是唯一 claim row，不另造 job table，也不由 outbox隐式启动。
2. 真实 induction handler按 claim/fence/idempotency写 persistent replay memory/profile outcome；Product read成功与否不影响 induction可恢复性。
3. source transaction按 destination创建独立 delivery intents。真实 delivery handler在发送线性化点重新校验 current epochs、recipient grant和confirmation，再CAS dispatch permit。
4. deterministic replay sink实现 stable invocation key、append-only journal、idempotent effect和 query-by-key；不接 SMS/email/正式 provider。
5. outcome_possible/unknown、expired lease、orphan intent、date conflict由真实 reconciliation scanner处理，产生 recover/fail/dead-letter receipt。unknown effect在没有 provider query或幂等保证时绝不自动重发。
6. 启用 queue时必须有对应真实 handler；移除 `ReplayNoModelHandler` 对 induction/delivery/reconciliation或任何未知 queue的假成功路径，readiness因缺 handler而失败。

门禁：induction commit/restart、delivery permit撤权竞态、send_started后kill、known delivered/not delivered/unknown查询、duplicate sink call、stale fence、dead-letter和reconciliation receipts。

### Stage 5 — 有界 retention 行为

本 stage只证明 replay raw-domain与subject-forget边界，不扩展成生产数据生命周期平台。

1. 继承 `001` 的 retention job/DEK binding/shred receipt/fence schema；新增最小 `RetentionKeyEnvelopePort` 与 local/test KEK adapter。raw ingress按 `(namespace, subject, raw, dek_generation)` DEK加密并原子写 binding/deadline。
2. Stage 5之后的新raw writes必须逐row/domain引用envelope DEK；不允许继续使用进程全局raw AES key。无需backfill旧development rows，retention proof必须从Stage 5 schema/key path新seed一晚，不能拿Stage 1遗留ciphertext冒充可shred数据。
3. scheduled raw expiry由ControlClock/数据库时间和短test TTL触发；只有normalization已终态且到期的raw可CAS shred wrapped DEK并写immutable receipt。随后通过canonical decrypt/read port必须得到稳定`key_destroyed`且不能恢复plaintext；重复job返回同receipt，已提交派生Episode和`/today`可继续读。
4. replay-only subject forget使用 `POST /demo/v1/reset` 的durable root。首个短transaction原子bump generation、privacy/retrieval epochs，失效旧Product projections/cursors/assertions与未用handles，并创建retention jobs/forget receipt draft；key-provider调用在transaction外，由fenced checkpoints恢复。root只有在所有required DEK outcomes与final receipt committed后成功。
5. forget完成后，旧actor assertion访问`/today`因epoch stale失败；在新generation新建的合法actor访问同subject得到`200 no_data`，不得看到旧结果。旧cursor失败；forget前取得的stale lease不能提交；同caller idempotency key/body在generation bump后仍返回原reset root/receipt；保留最小假名化audit/receipt。
6. final receipt逐retention domain记录`destroyed | expired | retained-with-reason`、key generation、opaque object counts与reason code，不记录raw plaintext/subject identifier。server/API只依赖key-envelope与retention ports，不依赖local provider细节，为未来KMS/HSM/backup policy保留边界。

门禁：post-Stage-5 fresh seed使用per-domain DEK、not-yet-expired、unfinished normalization、stale fence、repeat expiry、repeat forget after generation bump、crash between epoch bump/shred/final receipt、old assertion/cursor与new-generation `/today no_data`、canonical raw decrypt/read failure、receipt/audit allowlist。

### Stage 6 — 系统硬化与最终证据

1. 清除 canonical graph外的散落 env读取和直接 Runner callers；把 `sleep_api/app.py` 收敛为复用canonical factory的test/compat薄入口，禁止其拥有global runtime或in-process durable worker。
2. 补齐所有subject/namespace tables的RLS，尤其 API可读 quality/risk；所有跨namespace queue采用 per-namespace concurrency/fairness；连接回池前验证scope GUC清空。
3. 统一 strict v1 DTO、server-owned correlation ID、error envelope和exception handler顺序；保护或移除公开 `/health`、`/status` 的依赖manifest泄露，保留 `/livez` 与鉴权的internal readiness/status/reconciliation面。
4. 接入结构化日志allowlist、append-only audit和不含PHI labels的metrics：HTTP、queue depth/age、lease reclaim、retry/dead-letter/unknown、Product/Safety、auth deny、provider timeout、retention/reconciliation。
5. 修复production Docker target与oracle removal；server wheel/image不含verifier goldens；更新README、Perceptor、migration、queue/retry/reconciliation、权限、日期、retention和troubleshooting runbooks。
6. 提交 OpenAPI snapshots与reference clients；注册真实 `e2e` marker；canonical real-lifespan tests不得用toy app或monkeypatched TestClient。
7. 完成process kill、PostgreSQL restart/disconnect、load/fairness、UUIDv7 cross-process/clock rollback、security、log leakage和full-suite proof。

最终门禁：所有 inherited regression与Stages 1–5验收重新运行；无enabled no-op handler、无新legacy dependency、生产config fail closed、migration/OpenAPI/dependency/audit artifacts可复现。

## Freeze evidence index

下列证据只说明 current code shape，不等于 fresh runtime proof：

| Evidence | Freeze location | 说明 |
|---|---|---|
| `E-RUNTIME` | `backend/main.py:5`; `sleepagent/backend_runtime.py:76,129,238` | canonical production entry/composition root |
| `E-SETTINGS` | `sleepagent/backend_settings.py:23,117,198` | PG-only、process role、surface/data mode fail closed |
| `E-APP` | `sleepagent/backend_app.py:55,70,89,120,134,197` | surface mounting、body limiter、公开 status drift |
| `E-STANDALONE` | `sleepagent/sleep_api/app.py:17` | 第二 app factory/global compatibility runtime |
| `E-UOW` | `sleepagent/persistence/uow.py:70,89,327,404` | exact scope/GUC、transaction ownership/timeouts |
| `E-MIG` | `sleepagent/persistence/migrations.py:7`; `sleepagent/persistence/migrate.py:1,26,106` | immutable 001、baseline-only runner |
| `E-WORKER` | `sleepagent/worker_runtime.py:43,1662,1965,2020,2071` | queue/lease/fence/drain及no-op handler gap |
| `E-SLICE` | `sleepagent/sleep_domain/postgres_slice.py:603,675,796`; `worker_adapters.py:177` | real raw/normalization/fast handoffs，public ingress gap |
| `E-EPISODE` | `sleepagent/sleep_domain/episode_v2.py:42,51,180,198` | UUIDv7、anchor、wake date、fallback/conflict |
| `E-PRODUCT` | `sleepagent/product_runtime/postgres_worker.py:246,686,755,1162,1210` | prepare/commit、AnalysisRevision、三views |
| `E-PRODUCT-API` | `sleepagent/product_api/router.py:25`; `backend_persistence.py:323,421,543,848` | generic projection与interaction misrouting |
| `E-V1-GAP` | `sleepagent/sleep_api/postgres_runtime.py:643` | canonical v1 command固定503 |
| `E-DEMO-GAP` | `sleepagent/backend_services.py:108,157` | Demo controller固定503 |
| `E-AUTH` | `sleepagent/backend_persistence.py:190,788,834,870` | DB authority/JWS/cursor/handle substrate |
| `E-RLS-GAP` | `001_initial_schema.sql:3004,3013,5201`; `migrate.py:578` | API-readable quality/risk等未完整RLS |
| `E-RETENTION` | `001_initial_schema.sql:5740,5820,5884` | schema有，真实encryption/handler无 |
| `E-TESTS` | `pyproject.toml:52`; `tests/conftest.py:15`; `test_real_lifespan_harness.py:9` | markers有，真实process e2e无 |
| `E-DOCKER` | `docker/Dockerfile:1,39,59` | hash-lock正确，production cleanup path漂移 |
| `E-ORACLE` | `pyproject.toml:44`; `simulation/replay/goldens.py:12` | server distribution仍含/import goldens |
| `E-OBS/DOCS` | `observability.py:19,60,256`; `README.md:45` | canonical wiring和文档仍缺 |

## Historical requirement ledger

旧 PLAN 的每个 requirement 在此获得唯一处置。`S1`–`S6` 指本计划 Stage 1–6。

### Goal、success criteria 与 target architecture

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `G-PRESERVE` | `INHERITED` | 产品定位、四角色roster、Sleep kernel未被backend重写 | regression |
| `G-TOPOLOGY` | `INHERITED` | modular monolith + PG + API/Worker/UoW已成立 | regression |
| `G-REPLAY-CLOSURE` | `REMAINING` | Demo/ingress/public projection/process proof未闭环 | S1–S6 |
| `G-ISOLATED-SIM` | `SPLIT` | mode/surface gate有；server仍含goldens，durable seed无 | S1,S6 |
| `SC-01` | `SPLIT` | canonical app/runtime成立；standalone factory仍拥有compat runtime/worker选项 | S6 |
| `SC-02` | `INHERITED` | API/Worker分进程，API profile不可达handler/model/provider | regression |
| `SC-03` | `SPLIT` | reservation substrate有；v1 commands与interactions不可执行 | S2 |
| `SC-04` | `INHERITED` | canonical authority强制PostgreSQL | regression |
| `SC-05` | `SPLIT` | Episode/fast/Product/三views原子；handles/effects/induction/delivery未接 | S2,S4 |
| `SC-06` | `INHERITED` | wake-date与deadline fallback contract已实现 | regression |
| `SC-07` | `INHERITED` | server UUIDv7、opaque external ID与scope已实现 | regression |
| `SC-08` | `INHERITED` | surface路径和production OpenAPI exclusion已实现 | regression；status drift见6.4 |
| `SC-09` | `SPLIT` | DB authority/epochs有；typed field boundary、queued work、handle/effect撤权未闭环 | S1,S2,S4,S5 |
| `SC-10` | `REMAINING` | 无fresh full suite与真实process E2E/fault matrix | S1–S6 |
| `TA-FLOW` | `SPLIT` | ingress后核心链有；public ingress与后半handlers缺 | S1,S4,S5 |
| `TA-01 runtime` | `INHERITED` | canonical composition root owner正确 | regression |
| `TA-02 app` | `INHERITED` | canonical factory owner正确 | regression |
| `TA-03 worker` | `SPLIT` | loop/queues有；四类queue以no-op冒充 | S1,S2,S4,S5 |
| `TA-04 sleep-api` | `SPLIT` | reads迁PG；commands和standalone ownership未收敛 | S2,S6 |
| `TA-05 product-api` | `SPLIT` | 模块边界有；public read/write semantics缺 | S1–S3 |
| `TA-06 backend-main` | `INHERITED` | production thin entry成立 | regression |
| `TA-RADAR-COMPAT` | `SUPERSEDED` | retired runtime已物理删除且architecture test禁止复活 | none |

### Historical batches、file map 与 migrations

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `B0` | `SUPERSEDED` | freeze已指定；旧3 failures不再是代码事实 | none |
| `B1` | `SUPERSEDED` | foundation多数成为inherited；剩余按能力重排 | S1,S6 |
| `B2` | `SUPERSEDED` | durable substrate inherited；real handlers/fault proof重排 | S1,S2,S4 |
| `B3` | `SUPERSEDED` | PG vertical core inherited；public ingress/commands重排 | S1,S2 |
| `B4` | `SUPERSEDED` | Product kernel/commit inherited；interaction/effects重排 | S2,S4 |
| `B5` | `SUPERSEDED` | route shells/CLI有；behavior重排 | S1–S3 |
| `B6` | `SUPERSEDED` | governance schema有；bounded behavior/hardening重排 | S5,S6 |
| `FM-01 backend_settings` | `INHERITED` | canonical owner存在 | regression |
| `FM-02 backend_runtime` | `INHERITED` | canonical owner存在 | regression |
| `FM-03 backend_app` | `INHERITED` | canonical owner存在 | regression |
| `FM-04 worker_runtime` | `SPLIT` | owner/loop有；real handler registry缺 | S1,S2,S4,S5 |
| `FM-05 uow` | `INHERITED` | explicit transaction owner存在 | regression |
| `FM-06 migrate` | `SPLIT` | CLI/lock/ledger有；只支持001 | S1 |
| `FM-07 product contracts` | `SPLIT` | strict shell有；generic content错误 | S1–S3 |
| `FM-08 product router` | `SPLIT` | route owner有；capabilities未真实实现 | S1–S3 |
| `FM-09 product service` | `SPLIT` | auth/reservation有；typed query/handlers缺 | S1–S3 |
| `FM-10 demo_cli` | `SPLIT` | HTTP-only边界有；新root/status/recovery verifier缺 | S1,S5 |
| `FM-11 backend/main.py` | `INHERITED` | thin production entry | regression |
| `FM-12 sleep_api/app.py` | `SPLIT` | 仍是第二factory与compat worker owner | S6 |
| `FM-13 pyproject` | `SPLIT` | Python/markers/scripts大部有；packaging/e2e/worker proof漂移 | S1,S6 |
| `FM-14 lock files` | `INHERITED` | production/dev hash locks已提交 | regression |
| `FM-15 compose/env` | `SPLIT` | process topology有；Demo/Product roles与真实handler profile缺 | S1,S4–S6 |
| `FM-16 Dockerfile` | `SPLIT` | hash install/wheel no-deps有；production cleanup坏 | S6 |
| `FM-17 tests/conftest` | `SPLIT` | marker infrastructure有；canonical real-lifespan/process harness缺 | S1,S6 |
| `MIG-021` | `SUPERSEDED` | squash后不存在旧ledger bootstrap链 | S1新manifest |
| `MIG-022` | `SUPERSEDED` | identity/scope多数已吸收到001 | missing deltas从002+ |
| `MIG-023` | `SUPERSEDED` | work protocol多数已吸收到001 | missing deltas从002+ |
| `MIG-024` | `SUPERSEDED` | episode v2多数已吸收到001 | missing deltas从002+ |
| `MIG-025` | `SUPERSEDED` | retention schema多数已吸收到001 | S5 delta从002+序列追加 |
| `MIG-BOOTSTRAP` | `SUPERSEDED` | 不再attest历史001–020；改为immutable001 + release manifest | S1 |

### Old Approach §0–§2

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `0.1` | `INHERITED` | 保留只读盘点/不覆盖用户改动规则；审计起点clean | execution guard |
| `0.2` | `SUPERSEDED` | 用户已指定current freeze；不再按旧计划另造baseline | none |
| `0.3` | `SPLIT` | Python3.11/locks/wheel-no-deps存在；fresh install未在本轮证明 | S6 proof |
| `0.4` | `SUPERSEDED` | 本计划取代旧PLAN authority | this ledger |
| `0.ACC` | `REMAINING` | clean code fact不等于fresh green build | S6 |
| `1.1` | `SPLIT` | canonical settings集中；compat/business modules仍散读env | S6 |
| `1.2` | `INHERITED` | runtime/pool/UoW factory/dependency manifest已成立 | regression |
| `1.3` | `INHERITED` | profile/data-mode/production fail-closed已成立 | regression |
| `1.4` | `INHERITED` | lifespan不启动canonical durable worker；进程入口分离 | regression |
| `1.5` | `INHERITED` | postgres+migrate+api+worker与独立roles/pools成立 | regression；profile扩展S1 |
| `1.6` | `SPLIT` | registry/claim/exact scope有；非operation queues缺namespace fairness | S6 |
| `1.ACC` | `INHERITED` | singleton/API-no-worker/production-surface structural gates有 | regression |
| `2.1` | `SPLIT` | sync psycopg/no ORM rewrite/long work in Worker继承；旧migration history废止 | S1 manifest |
| `2.2` | `INHERITED` | explicit connection→UoW→repository ownership成立 | regression |
| `2.3` | `SPLIT` | 独立CLI/advisory lock/checksum ledger有；multi-file release/attestation缺 | S1 |
| `2.4` | `INHERITED` | canonical path不新增legacy auto-commit dependency | regression |
| `2.5` | `INHERITED` | 001已有核心FK/unique/CAS/scope/body-hash constraints | regression |
| `2.TXN` | `SPLIT` | READ COMMITTED/timeouts/lock discipline有；40P01/40001 bounded retry缺 | S2 |
| `2.H1 raw` | `INHERITED` | raw/inbox/normalization handoff原子 | S1只补public adapter/order |
| `2.H2 normalization` | `INHERITED` | canonical obs/lifecycle/Episode/fast handoff原子 | regression |
| `2.H3 fast-path` | `INHERITED` | quality/risk/urgent/Product handoff原子 | regression |
| `2.H4 Product terminal` | `SPLIT` | attempt/analysis/3 views/terminal/outbox原子；handles/effects/induction/delivery缺 | S2,S4 |
| `2.H5 delivery/reconciliation` | `REMAINING` | schema/fence有，real handlers无 | S4 |
| `2.OUTBOX` | `INHERITED` | event log与claim authority分离 | regression |
| `2.ACC` | `REMAINING` | 缺transaction-boundary process crash proof | S1,S2,S4 |

### Old Approach §3–§5

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `3.1` | `INHERITED` | server UUIDv7/opaque IDs | regression |
| `3.2` | `SPLIT` | scope/GUC/reset有；RLS未覆盖所有API-readable subject tables | S1 relevant，S6 complete |
| `3.3` | `INHERITED` | stable episode anchor/date-independent identity | regression |
| `3.4` | `INHERITED` | timezone/wake/fallback/correction semantics | regression |
| `3.5` | `SPLIT` | conflict state/constraint有；deterministic reconciler无 | S3,S4 |
| `3.6` | `SPLIT` | v1 overlap/canonical date fields有；generator/Habit/trends未闭合 | S3 |
| `3.7` | `SPLIT` | clock contract/schema有；Demo HTTP controller无 | S1 |
| `3.8` | `SPLIT` | Product strict DTO/body limit有；Sleep v1仍可coerce | S6 |
| `3.9` | `REMAINING` | 只有episode upcaster；无通用dispatcher/metric-unit-source ontology | S3 |
| `3.10` | `INHERITED` | canonical SourceKind与family/self/objective边界 | regression |
| `3.11` | `SPLIT` | Product watermarks有；v1 resources无强制watermark | S1 |
| `3.ACC` | `REMAINING` | date/source单测局部有；完整negative matrix缺 | S3,S6 |
| `4.1` | `INHERITED` | CommandReceipt reservation key/body conflict语义 | regression |
| `4.2` | `INHERITED` | work/operation/outbox/delivery各有唯一authority protocol | regression |
| `4.3` | `SPLIT` | Product reserve正确；v1与interaction handler缺 | S2 |
| `4.4` | `INHERITED` | server-time atomic claim/generation/token/attempt | regression |
| `4.5` | `INHERITED` | heartbeat/fenced checkpoint/finalize/current epoch | regression |
| `4.6` | `SPLIT` | journal/dispatcher substrate有；Product/live provider/sink未统一接线 | S2,S4 |
| `4.7` | `INHERITED` | retryable/terminal/outcome_unknown/backoff协议 | regression |
| `4.8` | `SPLIT` | delivery/inbox/predecessor schema有；real handler无 | S4 |
| `4.9` | `INHERITED` | urgent fast path优先且zero-model | regression |
| `4.ACC` | `REMAINING` | 缺process kill/DB disconnect/unknown delivery proof | S1,S4,S6 |
| `5.1` | `INHERITED` | canonical Product Runtime唯一，retired runtime物理删除 | regression |
| `5.2` | `INHERITED` | 四角色职责与roster固定 | regression |
| `5.3` | `INHERITED` | Agent/Tool/Policy/Service/Commit分层与deny-by-default | regression |
| `5.4` | `SPLIT` | canonical PG caller是Worker；diagnostics/bridge仍有direct Runner caller | S6 |
| `5.5` | `SPLIT` | prepare/persist/commit和prepared不可见有；handles/effects/publication intents缺 | S2,S4 |
| `5.6` | `SPLIT` | exact source revision/quality/risk有；FactSnapshot完整epochs/hashes缺 | S2 |
| `5.7` | `SPLIT` | kernel状态机独立；canonical interaction/followup persistence未接 | S2,S3 |
| `5.8` | `INHERITED` | target-specific Safety、hash invalidation、bounded revision | regression |
| `5.ACC` | `REMAINING` | kernel unit强；PG HITL/effect/crash journey缺 | S2,S4 |

### Old Approach §6–§8

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `6.1` | `SPLIT` | v1 reads/auth/cursor/events/poll有；commands 503/error drift | S2,S6 |
| `6.2` | `SPLIT` | route shell有；today generic、其他reads与interactions无真实行为 | S1–S3 |
| `6.3` | `SPLIT` | Demo surface gate/token有；controller 503 | S1,S5 |
| `6.4` | `SPLIT` | livez/internal token有；public health/status泄露，reconciliation面缺 | S4,S6 |
| `6.5` | `SUPERSEDED` | legacy Radar compatibility已删除，禁止复活 | none |
| `6.6` | `INHERITED` | DTO/application/domain/persistence/Agent层次边界存在 | regression；public mapper S1 |
| `6.7` | `INHERITED` | ASGI receive/body limiter已实现 | regression |
| `6.ACC` | `REMAINING` | OpenAPI/projection/demo/v1 compatibility proof不完整 | S1–S3,S6 |
| `7.1` | `INHERITED` | DB authority schema与三产品角色 | regression |
| `7.2` | `INHERITED` | 四层authority、JWS、nonce、DB resolver | regression |
| `7.3` | `SPLIT` | auth snapshots与claim/commit epoch checks有；effects/download边界未证 | S2,S4 |
| `7.4` | `SPLIT` | handle schema/consume CAS有；Product commit不生成 | S2 |
| `7.5` | `SPLIT` | consume transaction substrate有；creation/matrix/target flow未闭合 | S2 |
| `7.6` | `SPLIT` | exact role rows有；public typed字段最小化不成立 | S1,S3 |
| `7.7` | `SPLIT` | retention/DEK/job/receipt schema有；real key lifecycle/handler无 | S5 |
| `7.8` | `SPLIT` | dispatch permit/journal schema有；current-recipient real send无 | S4 |
| `7.9` | `SPLIT` | redaction helper有；canonical allowlist logging未贯穿 | S6 |
| `7.ACC` | `REMAINING` | 完整auth/handle/log/retention matrix未通过 | S1,S2,S4–S6 |
| `8.1` | `SPLIT` | test oracle局部分离；server仍打包/import goldens | S1,S6 |
| `8.2` | `SPLIT` | allowlist/quarantine schema有；seed implementation无 | S1 |
| `8.3` | `REMAINING` | 所列normal/abnormal/HITL/followup/forget旅程不存在 | S1–S5 |
| `8.4` | `REMAINING` | durable batched seed与server/oracle image proof不存在 | S1,S6 |
| `8.5` | `INHERITED` | demo CLI是HTTP-only，不导入Runner/repository | regression；扩展S1 |
| `8.ACC` | `REMAINING` | repeatability/run isolation/watermark全旅程未证 | S1–S6 |

### Old Approach §9–§10、proof 与交付

| Old ID | Status | Current-freeze judgment | Destination |
|---|---|---|---|
| `9.1` | `SPLIT` | command/outbox局部causation有；未贯穿全链 | S1,S2,S4,S6 |
| `9.2` | `REMAINING` | 仅旧进程内counter/deque，无要求的canonical metrics | S6 |
| `9.3` | `SPLIT` | readiness/privilege checks局部有；public status和semantic handler readiness缺 | S4,S6 |
| `9.4` | `SPLIT` | stop-claim/drain/fence有；bounded cancel与real invocation reconciliation缺 | S4,S6 |
| `9.5` | `SPLIT` | audit schema片段有；commit/delivery chain和telemetry separation缺 | S4–S6 |
| `9.ACC` | `REMAINING` | restart解释链与log-leak proof未成立 | S1,S4,S6 |
| `10.1` | `INHERITED` | unit corpus覆盖contracts/date/source/role/policy/idempotency/error基础 | rerun every stage |
| `10.2` | `SPLIT` | PG tests有；新migration/revocation/crash matrix缺 | S1–S6 |
| `10.3` | `REMAINING` | 所谓real-lifespan test只验证toy app | S1,S6 |
| `10.4` | `REMAINING` | 无真实kill Worker fault suite | S1,S2,S4,S6 |
| `10.5` | `SPLIT` | reference client/cursor/surface tests局部有；无committed OpenAPI snapshot | S1–S3,S6 |
| `10.6` | `REMAINING` | 无真实API+Worker+PG process E2E marker | S1–S6 |
| `10.7` | `REMAINING` | README/Perceptor/migration/queue docs与freeze冲突 | S6 |
| `10.PROOF-COMMANDS` | `SUPERSEDED` | 旧命令对当前e2e/demo必失败，需新proof manifest | below |
| `10.PROFILE-ISOLATION` | `SPLIT` | markers/fixtures有；真实process和strict isolation缺 | S1,S6 |
| `10.DOCKER` | `SPLIT` | lock/no-deps/script有；production target有broken path | S6 |
| `10.FINAL` | `REMAINING` | full green/restart/OpenAPI/migration/audit reproducibility未证 | S6 |

### Historical key decisions、risks 与 out-of-scope

| Old ID | Status | Current-freeze judgment |
|---|---|---|
| `KD-01` | `INHERITED` | replay-first、production-shaped backend范围仍有效；真实provider延后 |
| `KD-02` | `INHERITED` | modular monolith、API/Worker分进程仍有效 |
| `KD-03` | `INHERITED` | PostgreSQL queue/outbox、不提前引入broker仍有效 |
| `KD-04` | `INHERITED` | sync psycopg + explicit UoW、不做ORM/async big-bang仍有效 |
| `KD-05` | `INHERITED` | one Worker program、多queue/handler profile仍有效 |
| `KD-06` | `INHERITED` | canonical wake-local-date及bed/wake/policy保存仍有效 |
| `KD-07` | `INHERITED` | UUIDv7 string + explicit scope、不回填legacy IDs仍有效 |
| `KD-08` | `INHERITED` | PostgreSQL identity authority、IdP只替换adapter仍有效 |
| `KD-09a` | `INHERITED` | persistence渐进收敛、不big-bang rewrite仍有效 |
| `KD-09b` | `SUPERSEDED` | 临时legacy runtime compatibility已被物理删除边界取代 |
| `KD-10` | `INHERITED` | managed KMS延后、先锁ports/receipt语义；执行范围被本计划缩成bounded retention |
| `RISK-01` | `SUPERSEDED` | dirty/旧3 failures不是current-freeze事实；fresh suite未知另列风险 |
| `RISK-02` | `SUPERSEDED` | 旧migration upgrade chain不存在；immutable001/new002+替代 |
| `RISK-03` | `REMAINING` | PG queue capacity/fairness阈值仍需S6 load proof |
| `RISK-04` | `OUT_OF_SCOPE` | KMS/IdP/provider SLA/RPO/multi-region |
| `RISK-05` | `SPLIT` | clock rollback unit有；UUIDv7 cross-process proof缺 |
| `OOS-01` | `OUT_OF_SCOPE` | 真实Perceptor/device、真实用户迁移、临床验证 |
| `OOS-02` | `OUT_OF_SCOPE` | external IdP、managed KMS/HSM、production TLS/WAF、正式通知渠道 |
| `OOS-03` | `OUT_OF_SCOPE` | multi-region/HA/failover/backup-PITR/RPO-RTO |
| `OOS-04` | `OUT_OF_SCOPE` | Kafka/Redis/Celery/微服务拆分 |
| `OOS-05` | `OUT_OF_SCOPE` | mobile/frontend重构与新IA功能 |
| `OOS-06` | `OUT_OF_SCOPE` | diagnosis/medication/PSG replacement/超catalog action |
| `OOS-07` | `OUT_OF_SCOPE` | high-frequency waveform/blob architecture |
| `OOS-08` | `OUT_OF_SCOPE` | 未批准的legacy/fixture/archive删除；既有cleanup只是freeze事实 |

## Proof command manifest

实现可补充等价命令，但不得省略这些语义门禁。命令不得在argv或日志中打印private key、bearer、demo token或DSN password。

```text
python3.11 -m pip install --require-hashes -r requirements/dev.lock
python3.11 -m pip install --no-deps .
python3.11 -m pytest -q -p no:cacheprovider -m unit

COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test config
COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test up -d --wait postgres
COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test run --rm migrate apply
COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test run --rm migrate check
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python3.11 -m pytest -q -p no:cacheprovider -m postgres
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python3.11 -m pytest -q -p no:cacheprovider -m asgi_lifespan

COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test up -d --wait demo-api api worker
sleepagent-demo verify backend --scenario normal-one-night --mode clean
sleepagent-demo verify backend --scenario normal-one-night --mode restart-worker-before-product-commit
sleepagent-demo verify backend --suite commands-interactions
sleepagent-demo verify backend --suite read-models-abnormal
sleepagent-demo verify backend --suite effects-reconciliation
sleepagent-demo verify backend --suite bounded-retention
SLEEPAGENT_SETTINGS_PROFILE=test-postgres python3.11 -m pytest -q -p no:cacheprovider -m e2e

COMPOSE_PROJECT_NAME=sleepagent_backend_rebase docker compose --env-file .env.test build production
python3.11 -m pytest -q -p no:cacheprovider
```

E2E必须通过真实network socket访问独立进程。外部verifier先以HTTP断言公共语义；数据库invariant/fault tests使用独立test role验证unique/FK/CAS/count，不给verifier后门。

## Key decisions & tradeoffs

1. **旧PLAN只作input**：不延续B0–B6，也不把历史“已通过”文字当事实。代价是ledger较长，但每项requirement都有明确去向。
2. **第一条slice只做read-only normal night**：root止于committed Episode、Product、三role `/today`；不为了首个gate提前实现trends/Care/delivery。代价是其他routes必须显式501。
3. **Demo API与Product API分角色/进程**：复用一个factory和代码库，但使用不同DB role/credentials。增加一个compose service，换取principal/session_user/RLS边界不被放宽。
4. **root receipt与journey claim分离**：root Operation是外部语义receipt，journey row是wait-aware唯一调度authority。多一个持久对象，换取等待不耗retry、无长lease/busy wait。
5. **adapter bytes是ingress authority**：manifest哈希真实PG intake对象，不哈希generator内部canonical对象。多一个adapter版本，换取generator与domain边界可验证。
6. **typed public projection在commit时生成**：不在read时解析内部Agent envelope。增加commit mapper/schema责任，换取字段授权、版本和commit原子性。
7. **`/today` placeholder可breaking-in-place**：不保留generic compatibility，避免双合同；其他未实现capability保持route但501。
8. **001 immutable、002+ additive**：只证明001→当前release；不支持squash前dev migration chain，避免伪兼容。
9. **真实协议、deterministic adapters**：induction/delivery/reconciliation必须真实claim/fence/journal/effect；只把外部sink替换为deterministic replay adapter，禁止no-op success。
10. **bounded retention**：scheduled raw expiry保留derived projection；subject forget才bump epochs并使旧projection不可见。它证明key/invalidations/receipt interface，不承诺KMS/backup/regulatory completeness。
11. **`/api/v1` body compatibility**：旧body用mandatory replay headers，不强行breaking schema；新Product/Demo contracts在body内watermark。
12. **at-least-once + idempotent effect**：不宣称distributed exactly-once；unknown send进入reconciliation且默认不重发。

## Risks / open questions

1. 本轮没有可用Python3.11+pytest环境，也未启动live PostgreSQL；所有“inherited”判断来自code/schema形状，执行前必须由proof command重新验证。
2. `002`会同时触及journey、ordering、projection、RLS和bootstrap，migration较大。实现时可拆成连续transactional `002+`，但不得改001、改变stage语义或留partial release manifest。
3. 新per-stream ordering可能暴露现有fixture中真实乱序/同timestamp假设；quarantine并修fixture/adapter，禁止回退到single worker侥幸排序。
4. Product internal deterministic bundle仍含in-memory collaborators；Stage 1只允许它作为model-side deterministic adapter，所有closure authority、attempt、analysis和public projection必须是PostgreSQL committed。
5. Demo root周期性resume会增加queue churn；用bounded `resume_at`、wait count、deadline与metrics控制，不能用failure retry替代。
6. 当前production Docker cleanup路径已漂移且server package含goldens；在修复并检查wheel/image contents前，不得声称oracle隔离。
7. local/test key provider只证明DEK interface与cryptographic erasure语义；它不能外推到managed KMS、backup media或生产法律义务。

没有待用户决定的blocking open question。实现中若发现需要改变scope、公共contract、root成功条件、identity topology或retention语义，必须停下重新grill，而不是自行扩展。

## Out of scope

- 真实Perceptor/雷达设备、真实用户数据迁移、临床验证、诊断、用药或PSG替代能力。
- live model/provider质量与SLA；第一条closure使用deterministic model adapter。
- 外部IdP、managed KMS/HSM、backup/media propagation、legal-hold product、监管policy platform。
- 正式SMS/email/notification provider；delivery只到deterministic replay sink。
- production域名、TLS/WAF、HA、多区域、自动failover、PITR和RPO/RTO承诺。
- Kafka、Redis、Celery、微服务拆分或生产级workflow/data-lifecycle平台。
- 移动端/前端重构和本计划外的新产品信息架构。
- 高频波形/大对象存储架构。
- 支持squash前旧development `001–025`数据库upgrade。
- 未经另行批准的legacy/fixture/archive删除；本计划只允许为server/oracle物理隔离移动或重新打包明确列出的fixture。
