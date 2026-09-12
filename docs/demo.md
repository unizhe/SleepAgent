# Portfolio demo

The repository exposes one public demo entry:

```bash
scripts/run_portfolio_demo.sh --env-file .env.test
```

It creates and later force-drops one uniquely named `sleepagent_portfolio_*` PostgreSQL 16 database, then uses sanitized deterministic fixtures, the deterministic model boundary, and real application/persistence paths. It never drops the configured database or unknown state. It does not contact Perceptor hardware, an external model, email/SMS, or any other provider. The script fails if PostgreSQL, schema/bootstrap authority, database-create authority, or any story proof is unavailable.

## One story

1. **Production-shaped input** — sanitized Perceptor Push/Pull enters durable ingress, Observation V2, NightEpisode projection, and immutable late revision/finalization paths.
2. **Sleep analysis** — one `SharedNightAnalysis` produces elder/family/doctor zh-CN projections under the fixed role runtime.
3. **Care governance** — CareStrategy produces a structured candidate; deterministic policy creates a Proposal; a human authority approves it.
4. **Care execution** — terminal `list → start → complete` semantics record trusted-operator human attestation.
5. **Follow-up** — later HARD-finalized nights produce a `CareOutcome` with `causal_claim=false`.
6. **Personalization** — the resulting receipt stays pending and is excluded until an elder ACCEPT; rejection/supersession remain non-authoritative.
7. **Next cycle** — a new shared analysis pins and consumes the accepted governed Memory revision. This last read receipt is mandatory for demo success.

The input and downstream closure legs run in separate unique fixture scopes inside the same invocation; this is a reproducible public proof of the connected production surfaces, not a claim that CI contacted one physical radar session.

## Setup

Follow the root [Quickstart](../README.md#quickstart). For Docker-backed development infrastructure:

```bash
cp .env.test.example .env.test
docker compose --env-file .env.test up -d --wait postgres
docker compose --env-file .env.test run --rm migrate
docker compose --env-file .env.test run --rm test-bootstrap
scripts/run_portfolio_demo.sh --env-file .env.test
```

Normal output is product-focused:

```text
睡眠输入：Observation V2 与夜间修订已确认
睡眠报告：共享分析与三角色投影路径已确认
照护建议：HARD 证据上的治理候选已确认
人工批准：ApprovalGrant 与 CarePlan 已确认
计划执行：可信操作员人工完成记录已确认
执行后观察结果：非因果 CareOutcome 已确认
个性化记忆确认：待审候选经人工 ACCEPT
下一周期读取：已确认 Memory 修订被固定并读取
测试状态：隔离临时数据库已清理
PORTFOLIO_DEMO = PASS
```

Use `--trace` to expose the exact pytest nodes and normal test output. Without it, internal IDs, hashes, SQL rows, provider chunks, and traces stay hidden.

The demo removes only the uniquely named temporary database it created. The configured PostgreSQL service and its existing databases remain untouched.
