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

Product slow path 先把 automatic、显式 report 和 reanalysis 请求统一为 `product.report.run.v1`，重新应用持久化 quality/risk gate，再按精确 NightEpisode current revision、所消费的 Habit/Memory 事实和 runtime manifest 计算 desired-analysis identity。相同 identity 收口到一个 `product.shared_analysis.v1` work item；Planning、Evidence、条件式 Care 和必要 Safety 只运行一次，随后从同一个 `SharedNightAnalysis.v1` 确定性形成 elder/family/doctor 三个投影，并在 fence 内一次性提交。urgent 与 UNUSABLE 仍保持确定性且不调用 Product Agent 或 provider。

elder 可另行使用 `product.elder_narrative.v1` 对已提交 shared analysis 和确定性 elder 投影执行至多一次 SleepCare content-plan render。该 render 具有独立 identity 和 attempt，不会改变 shared analysis revision；失败或 binding 无效时继续使用确定性 elder 投影。family/doctor render 和所有 report GET/list 均不调用模型。

公开 `POST /product/sleep/reports/run` 保留 nonce/replay 消费；仅 report GET/list 使用短时、method/path/body-bound 的 stateless JWS 验证，并通过当前 PostgreSQL authority/epoch SELECT 重新授权。查询事务不 commit，也不调用写入 Memory receipt 的读取路径。`sleepagent-report` 只调用这组 HTTP 接口，不形成新的 composition root。详细接口、状态和配置见 [Product report 运维说明](operations/product-report.md)。真实雷达接入不改变固定 1+2+1 Agent roster、HITL 或角色可见性边界。
