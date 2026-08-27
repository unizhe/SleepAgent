# Operations

## 环境边界

`compose.yaml` 仅用于 PostgreSQL/replay 开发与集成证明。生产部署必须单独提供 live data source、安全密钥注入、网络策略和备份恢复方案。

OpenAI-compatible Product model 使用调用环境注入的 `DEEPSEEK_API_KEY`，以及：

- `SLEEPAGENT_PRODUCT_LLM_MODEL`
- `SLEEPAGENT_PRODUCT_LLM_BASE_URL`
- `SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS`
- `SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS` (default `4000`, large enough for the
  strict structured Agent response schemas)

Replay/demo workers use a 300-second lease with the existing 10-second
heartbeat. A multi-night ScenarioClock advance releases staged facts in one
fenced transaction, so the shorter integration lease can expire while that
operation row is locked.

不要把真实 secret 写入 `.env.test.example` 或版本库。

## 迁移与验证

```bash
python -m sleepagent.persistence.migrate check
python -m sleepagent.persistence.migrate status
scripts/verify_backend.sh core
scripts/verify_backend.sh all
```

迁移 `001–013`、manifest identity 与 checksum 是历史数据合同，不得重命名或 squash。API 入口为 `sleepagent.app:app`，Worker 入口为 `python -m sleepagent.workers.runtime run`，demo CLI 为 `python -m sleepagent.simulation.cli`。真实雷达部署、恢复和当前人工编排边界见 [Perceptor operations](operations/perceptor.md)。

## P3 Terminal Product Demo

CLI 只读取公开 Product API 与 replay-only Demo API；`--trace` 额外展示持久化的 Operation、provider request ID、latency/token、SkillLock、Habit revision 与 Memory ReadReceipt。运行前注入 demo controller token、Product service credential 与仅由 CLI 持有的 actor private-key 路径：

```bash
export SLEEPAGENT_DEMO_CONTROLLER_TOKEN='<demo token>'
export SLEEPAGENT_DEMO_SERVICE_CREDENTIAL='<Product service credential>'
export SLEEPAGENT_DEMO_ACTOR_PRIVATE_KEY='/path/to/actor-private.pem'
```

Product View（live）：

```bash
SLEEPAGENT_BACKEND_MODEL_MODE=live docker compose \
  --env-file .env.test --env-file .env.deepseek.local \
  up -d --build --wait postgres migrate test-bootstrap api demo-api worker

python -m sleepagent.simulation.cli demo cold-start --model live
python -m sleepagent.simulation.cli demo habit-baseline --model live
python -m sleepagent.simulation.cli demo worsening-care --model live
python -m sleepagent.simulation.cli demo longitudinal-personalization --model live
python -m sleepagent.simulation.cli demo urgent-safety --model live
```

Trace View：

```bash
python -m sleepagent.simulation.cli demo habit-baseline --model live --trace
```

Deterministic mode 保留相同 Product 体验；它只改变 Worker 的模型配置并要求相应持久化证据：

```bash
SLEEPAGENT_BACKEND_MODEL_MODE=deterministic docker compose \
  --env-file .env.test up -d --build --wait \
  postgres migrate test-bootstrap api demo-api worker

python -m sleepagent.simulation.cli demo cold-start \
  --model deterministic --trace
```

Live Worker 使用现有唯一 OpenAI-compatible provider。新的 `SLEEPAGENT_PRODUCT_LLM_*` 名称优先；为复用既有本地 DeepSeek 配置，也兼容旧的 `SLEEPAGENT_RADAR_AGENT_LLM_*` 名称。不得把任何真实 credential 提交到版本库。
