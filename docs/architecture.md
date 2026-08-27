# Architecture

SleepAgent 只有一个生产 Python package，并收口为七个职责目录：

- `api`：public、product、demo transport 与 PostgreSQL query adapter。
- `domain`：睡眠 episode、quality/risk 与 Product facts 的确定性规则。
- `integrations`：云云/Perceptor Push、只读 Platform API/Pull、签名、规范化与 Push/Pull 对账边界。
- `persistence`：001–013 PostgreSQL migrations、release manifest 与 scoped UoW。
- `runtime`：固定四 Agent roster、provider、tool、policy、安全与结果语义。
- `simulation`：registry-backed replay、journey 与 HTTP demo CLI。
- `workers`：durable lease/fence/heartbeat runtime 和 queue handlers。

`sleepagent.app:app` 是唯一 ASGI composition root；`sleepagent.workers.runtime` 是唯一 Worker CLI。API 与 Worker 共享 PostgreSQL release attestation，但 API 不启动 Worker，Worker 不挂载 HTTP surface。

真实雷达链路为“雷达 → 云云/Perceptor Cloud → Push/Pull → Perceptor integration → canonical observations → PostgreSQL → NightEpisode → 现有 Agent Runtime”。Push webhook 与 live ingestion Worker 已接入 composition root；Pull 具有只读客户端、durable ingress、reconciliation 与 checkpoint 语义，但常驻调度和自动 morning finalization 尚未产品化。详见 [真实雷达架构](architecture/perceptor-real-radar.md)。

Product slow path 从 PostgreSQL `product_agent` work item 读取精确 revision，依次形成 elder/family/doctor 投影，并在 fence 内一次性提交。urgent fast path 保持确定性且不调用模型。真实雷达接入不改变固定 1+2+1 Agent roster、HITL 或角色可见性边界。
