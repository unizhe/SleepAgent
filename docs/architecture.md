# Architecture

SleepAgent 只有一个生产 Python package，并收口为六个职责目录：

- `api`：public、product、demo transport 与 PostgreSQL query adapter。
- `domain`：睡眠 episode、quality/risk 与 Product facts 的确定性规则。
- `persistence`：001–007 PostgreSQL migrations、release manifest 与 scoped UoW。
- `runtime`：固定四 Agent roster、provider、tool、policy、安全与结果语义。
- `simulation`：registry-backed replay、journey 与 HTTP demo CLI。
- `workers`：durable lease/fence/heartbeat runtime 和 queue handlers。

`sleepagent.app:app` 是唯一 ASGI composition root；`sleepagent.workers.runtime` 是唯一 Worker CLI。API 与 Worker 共享 PostgreSQL release attestation，但 API 不启动 Worker，Worker 不挂载 HTTP surface。

Product slow path 从 PostgreSQL `product_agent` work item 读取精确 revision，依次形成 elder/family/doctor 投影，并在 fence 内一次性提交。urgent fast path 不调用模型。当前 live 只描述 OpenAI-compatible model provider；live ingestion 不属于已交付能力。

