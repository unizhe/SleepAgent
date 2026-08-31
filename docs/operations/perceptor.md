# Perceptor operations

本文只描述当前受支持的生产边界，不是 Product CLI，也不把已退役的 P4 验收脚本当作运维命令。

## Required components

- 已应用且校验通过 `001–017` 的 PostgreSQL；API、Worker 使用各自最小权限角色。
- `data_mode=live` 且启用 `perceptor_push` surface 的 `sleepagent.app:app` API。
- 消费 `ingestion` queue 的 `python -m sleepagent.workers.runtime run` Worker。
- 一个通过 `python -m sleepagent.device_cli` 管理、处于有效期内的
  `DeviceBinding`。
- 完整 Product 链路另需 `fast_path`、`product_agent` 等既有 Worker queue 与相应 model/provider 配置；Push canonicalization 本身不依赖模型 provider。

## Configuration and credentials

Perceptor Push profile 至少需要：

- `SLEEPAGENT_BACKEND_DATA_MODE=live`
- `SLEEPAGENT_BACKEND_ENABLED_SURFACES=perceptor_push`，或允许的 BFF surfaces 加 `perceptor_push`
- `SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_SECRET_REF`
- `SLEEPAGENT_BACKEND_PERCEPTOR_PROVIDER_ACCOUNT_ID`
- `SLEEPAGENT_BACKEND_PERCEPTOR_NAMESPACE_ID`，并包含在 `NAMESPACE_PREFIXES`

generation、authorization epoch 与 freshness window 通过同前缀配置显式固定。credential 只使用 `env:` 或仓库外 absolute、non-symlink 的 `file:` reference；文件和父目录应 owner-only。不要把真实值、路径、设备标识或 provider response 写入 Git、日志或测试 fixture。

自动 Pull profile 还必须显式设置：

- `SLEEPAGENT_BACKEND_ACQUISITION_SCHEDULER_ENABLED=true`
- `SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_ID_REF` 与
  `SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_SECRET_REF` 分别引用厂商 client ID 和
  secret；不得复用 SleepAgent service credential
- Worker queues 包含 `perceptor.history_overlap_pull`、
  `perceptor.sleep_report_pull`、`night.finalization_scan`
- scheduler principal 拥有 `acquisition_schedule` grant，执行 Worker 拥有
  `worker` grant 和三个对应 handler

该开关默认且持续为 `false`。仅配置 queue 而未显式启用，或启用后缺少
Perceptor live provider 配置，进程都会 fail closed。

## Health and readiness

```bash
python -m sleepagent.persistence.migrate check
python -m sleepagent.persistence.migrate status
python -m sleepagent.workers.runtime healthcheck
```

- API liveness：`GET /livez`。
- 单独启用 internal surface 的管理 API 可用 authenticated `GET /internal/readyz` 检查 schema/runtime attestation。
- Worker healthcheck 验证 schema attestation 与函数权限；运行日志应出现 webhook、pull、reconciliation 和 queue outcome，而不是 payload 内容。

## Acquisition lifecycle

1. 使用 `python -m sleepagent.device_cli --help` 查看受支持的
   `discover/list/bind/show/rebind/end/unbind/revoke/validate` 操作。CLI 只调用
   RLS-scoped application service；rebind/transfer 需要 CAS、双方 subject
   authority、有效 IANA timezone 和审计原因。未绑定前只允许 quarantine。
2. 启动 schema-attested API 与 ingestion Worker，再开放 webhook 路由。
3. Push 完成签名/时效检查、加密 raw commit、durable work 和 canonicalization。
4. 先用同一 CLI 创建、列出、暂停或恢复 acquisition schedule。显式启用后，
   `python -m sleepagent.bootstrap.scheduler once` 可执行一次到期扫描，`run`
   可常驻轮询。它只用 `SKIP LOCKED` 原子地产生 idempotent durable operation
   并推进 `next_run_at`；不在 scheduler 内执行 Pull 或 finalization。
5. Worker 对账后才推进 checkpoint；no-data 也是一个显式、可重放的 reconciliation 结果。
6. `night.finalization_scan` Worker 使用独立 finalization aggregate，按
   `OPEN -> SOFT_FINALIZED -> HARD_FINALIZED` 推进；date conflict 进入
   `RECONCILIATION_REQUIRED`。SOFT 必须保留 provisional/coverage caveat，晚到
   material evidence 创建不可变新 revision 并触发 bounded reanalysis。

## Shutdown and recovery

先停止新外部流量和新 Pull 调度，再让 Worker 停止 claim 并 drain 已领取工作，最后关闭 API/数据库连接池。不要在 reconciliation 未 commit 时手工推进 checkpoint。

Webhook durable commit 失败会返回 retryable `503`；厂商重试由 idempotency 安全收敛。Worker 崩溃后 lease/fence 允许其他实例重领；Pull 从 durable checkpoint 与 overlap watermark 恢复。恢复时不得绕过 generation、binding version 或 authorization epoch。

## Troubleshooting

| Symptom | Expected handling |
|---|---|
| Webhook unavailable / `503` | 检查 schema attestation、数据库连接、API role 权限和 body limits；保持上游重试，不返回假成功。 |
| `401 invalid_signature` | 核对 secret reference、冻结签名 path/key 规则与请求时钟；不要启用双算法 fallback。 |
| Binding missing/ambiguous | 数据应 quarantine；修复权威 `DeviceBinding`，不要手工把 raw row 归给 subject。 |
| Provider unavailable | Push ingestion 可继续；暂停新的 Pull，保持 checkpoint，恢复后按 overlap window 重试。 |
| Pull contract failure | 不产生伪 canonical/SleepReport，不推进 checkpoint；检查 endpoint、casing、时间范围和 binding identity。 |
| Quality blocked/partial | 检查离线、时钟、非法 sentinel、覆盖率与 SleepReport；`PARTIAL` 必须带 limitation 下游传播，`UNUSABLE` 不得强行生成 Episode。 |

## Current manual gaps

DeviceBinding CLI、durable scheduler 和晨间 finalization 已有受支持的后端命令，
但 feature gate 不会被部署自动打开。真实 Perceptor 设备的自动 History、
SleepReport、Alarm/urgent 端到端验收仍需由 live acceptance owner 在受控环境完成；
本地/CI 不应为了证明调度器而联系真实设备。该缺口记录为
`LIVE_ACCEPTANCE_DEFERRED`。
