# SleepAgent

SleepAgent 是一个以 PostgreSQL 为唯一服务器持久化、以 durable queue 驱动睡眠分析与三角色（elder/family/doctor）投影的后端。

当前唯一 ASGI 入口是 `sleepagent.app:app`，Worker 入口是 `python -m sleepagent.workers.runtime run`。仓库不再包含 SQLite 服务器路径、Perceptor 兼容层、诊断后端或旧前端。

## 本地验证

```bash
cp .env.test.example .env.test
scripts/verify_backend.sh all
```

`compose.yaml` 是 PostgreSQL + replay 的开发/集成 harness，不是 live production 部署清单。架构、运维和合同索引见 [docs/README.md](docs/README.md)。
