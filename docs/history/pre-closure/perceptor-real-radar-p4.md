# Perceptor real-radar architecture

## What is integrated

```text
Real radar
  → YunYun / Perceptor Cloud
  → Push webhook + bounded read-only Pull
  → SleepAgent Perceptor integration boundary
  → canonical observations
  → PostgreSQL
  → NightEpisode
  → existing 1+2+1 Agent Runtime
```

这是云到云接入，不是雷达 UART、MQTT 或本地驱动接入。SleepAgent 消费厂商已经处理的生命体征、在床、移动、睡眠阶段与 SleepReport 数据，不接收 ADC/IQ 波形、点云或原始射频采样。

## Push

生产入口是 `POST /integrations/perceptor/webhook`。API 对 exact request body 使用冻结的 HMAC-SHA1/Base64 契约验证签名：空签名 path、租户 secret 加固定 `&` 后缀，且没有算法 fallback。只有签名、时效和请求结构通过后，原始 body 才会加密写入 PostgreSQL；durable commit 失败时不会返回成功确认。

`DeviceBinding` 是 provider device 到内部 subject 的唯一权威。绑定缺失或歧义时数据进入 quarantine，不自动猜测归属。live `ingestion` Worker 在 lease/fence 内解析并提交 canonical observations，重复请求通过 generation-scoped idempotency 收敛。

## Pull

只读 Platform API 用于 token、产品/设备发现、当前/短时实时读取、History 回补与 SleepReport。Pull 不是第二条实时主链；它补充 Push、修复缺口并在晨间完成报告对账。

`PerceptorPlatformClient` 只允许冻结的只读 endpoint。`DurablePerceptorPullIngress` 将 exact response 加密落库并创建 ingestion work；`PerceptorPullNormalizationProcessor` 完成 normalization/reconciliation，checkpoint 只在 reconciliation commit 后推进。`PerceptorPullBackfillRunner` 提供生产语义，但仓库目前没有常驻 Pull scheduler 或受支持的一键 CLI。

## Canonical boundary

厂商 JSON 只停留在加密 raw ingress 与 provider adapter 内。Agent/LLM 读取的是受治理的 facts、quality、provenance 摘要和允许的 source references；不会收到 raw vendor payload、完整 canonical dump、Episode membership ID 列表或内部 subject/device 标识。

## Reconciliation

Push 与 Pull 是不同 acquisition authority。同一语义事实会合并为一个 canonical authority；重复项不重写，冲突以 append-only evidence 记录。真实夜间证明显示 Push 与 History 为 `NON_IDENTICAL_CADENCE`，因此不能按逐样本相同频率解释。重试、崩溃恢复和 overlap window 都由 durable work、fence、receipt 与 checkpoint 约束。

## Quality

缺失、非法 sentinel、离线、时钟和覆盖率会进入确定性 quality policy。真实夜间可形成 `PARTIAL` NightEpisode：数据仍可用于受限分析，但限制必须传播到 risk、Agent 输出和 Product，不得伪装成完整数据。

## Agent boundary

Perceptor 只替换/增加上游 acquisition source；下游仍是现有固定 1+2+1 runtime。provider context 采用 identifier-free allowlist 投影，frozen Episode 与 acquisition-owned quality 不因 Agent 运行被重写。

## Safety

urgent fast path 的确定性、零模型权威保持不变；Agent、LLM 或 Product 不能覆盖该路径。这个结构性不变量有本地和 PostgreSQL 回归测试，但自然真实流量尚未完成 Alarm → urgent 的端到端设备证明。

## Known limitations

- P4 状态为 `COMPLETE_WITH_LIMITATIONS`，`RELEASE_READY = NO`。
- 真实夜间质量仍为 `PARTIAL`；Push/History 为 `NON_IDENTICAL_CADENCE`。
- provider 身份链已绑定，但独立 IMEI/机身物理相关性仍未解决。
- 自然真实 Push 只证明了 Vital；Alarm/AlarmStop/Connected/Disconnected 只有合同与合成回归，真实 Alarm → urgent 路径未完整生产证明。
- bounded realtime 的真实窗口较短，不能视为长期连续流证明。
- 仓库没有 DeviceBinding provisioning CLI、常驻 Pull scheduler 或自动 morning finalization；这些仍需部署方受控编排。
- 最终真实 Agent/Product 证明中 elder/family ready，doctor 被现有 plan policy 阻断，Product 合法地 `WITHHELD_BY_POLICY`。
