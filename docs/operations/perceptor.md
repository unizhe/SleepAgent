# Perceptor operations

本文只描述当前受支持的生产边界，不是 Product CLI，也不把已退役的 P4 验收脚本当作运维命令。

## Required components

- 已应用且校验通过 `001–013` 的 PostgreSQL；API、Worker 使用各自最小权限角色。
- `data_mode=live` 且启用 `perceptor_push` surface 的 `sleepagent.app:app` API。
- 消费 `ingestion` queue 的 `python -m sleepagent.workers.runtime run` Worker。
- 一个预先审核并处于有效期内的 `DeviceBinding`；仓库目前没有 provisioning CLI。
- 完整 Product 链路另需 `fast_path`、`product_agent` 等既有 Worker queue 与相应 model/provider 配置；Push canonicalization 本身不依赖模型 provider。

## Configuration and credentials

Perceptor Push profile 至少需要：

- `SLEEPAGENT_BACKEND_DATA_MODE=live`
- `SLEEPAGENT_BACKEND_ENABLED_SURFACES=perceptor_push`，或允许的 BFF surfaces 加 `perceptor_push`
- `SLEEPAGENT_BACKEND_PERCEPTOR_CLIENT_SECRET_REF`
- `SLEEPAGENT_BACKEND_PERCEPTOR_PROVIDER_ACCOUNT_ID`
- `SLEEPAGENT_BACKEND_PERCEPTOR_NAMESPACE_ID`，并包含在 `NAMESPACE_PREFIXES`

generation、authorization epoch 与 freshness window 通过同前缀配置显式固定。credential 只使用 `env:` 或仓库外 absolute、non-symlink 的 `file:` reference；文件和父目录应 owner-only。不要把真实值、路径、设备标识或 provider response 写入 Git、日志或测试 fixture。

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

1. 通过部署方受控流程完成只读设备发现并建立 `DeviceBinding`；未绑定前只允许 quarantine。
2. 启动 schema-attested API 与 ingestion Worker，再开放 webhook 路由。
3. Push 完成签名/时效检查、加密 raw commit、durable work 和 canonicalization。
4. 部署方的受控 scheduler/orchestrator 使用生产 Pull library 执行 bounded History overlap/backfill 与 SleepReport；仓库没有永久 scheduler 命令。
5. Worker 对账后才推进 checkpoint；no-data 也是一个显式、可重放的 reconciliation 结果。
6. NightEpisode、quality、fast path 和 Product/Agent 使用现有下游 queue，不由 webhook 进程内联执行。

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

DeviceBinding provisioning、常驻 Pull 调度、晨间自动 finalization 与实机 Alarm/urgent 验收仍没有受支持的一键产品命令。部署方若编排这些步骤，应使用 production modules、最小权限凭据、durable checkpoint 和可审计 scheduler，而不是恢复已归档的 P4 session scripts。
