# Operations

## 环境边界

`compose.yaml` 仅用于 PostgreSQL/replay 开发与集成证明。生产部署必须单独提供 live data source、安全密钥注入、网络策略和备份恢复方案。

OpenAI-compatible Product model 使用调用环境注入的 `DEEPSEEK_API_KEY`，以及：

- `SLEEPAGENT_PRODUCT_LLM_MODEL`
- `SLEEPAGENT_PRODUCT_LLM_BASE_URL`
- `SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS`
- `SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS`

不要把真实 secret 写入 `.env.test.example` 或版本库。

## 迁移与验证

```bash
python -m sleepagent.persistence.migrate check
python -m sleepagent.persistence.migrate status
scripts/verify_backend.sh core
scripts/verify_backend.sh all
```

迁移 `001–007`、manifest identity 与 checksum 是历史数据合同，不得重命名或 squash。API 入口为 `sleepagent.app:app`，Worker 入口为 `python -m sleepagent.workers.runtime run`，demo CLI 为 `python -m sleepagent.simulation.cli`。

