# SleepAgent

SleepAgent 是一个以 PostgreSQL 为唯一服务器持久化、以 durable queue 驱动睡眠分析与三角色（elder/family/doctor）投影的后端。它已接入真实云云/Perceptor 毫米波雷达的云端数据：生产 webhook 接收 Push，受限只读 Pull 完成设备发现、History 回补与 SleepReport，随后统一为 canonical observations、NightEpisode，并进入现有 Agent Runtime。

SleepAgent 消费厂商处理后的生理与睡眠数据，不接收雷达 ADC/IQ 原始波形。当前唯一 ASGI 入口是 `sleepagent.app:app`，Worker 入口是 `python -m sleepagent.workers.runtime run`；SQLite 服务器路径、诊断后端和旧前端均已退役。P4 状态为 `COMPLETE_WITH_LIMITATIONS`，尚非 release-ready。

## 本地验证

```bash
cp .env.test.example .env.test
scripts/verify_backend.sh all
```

`compose.yaml` 是 PostgreSQL + replay 的开发/集成 harness，不是 live production 部署清单。总体文档索引见 [docs/README.md](docs/README.md)，Perceptor 的当前[架构](docs/architecture/perceptor-real-radar.md)与[运维边界](docs/operations/perceptor.md)分别说明已交付能力和仍需部署方补齐的编排。
