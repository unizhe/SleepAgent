# Product Sleep Report

`sleepagent-report` 通过唯一 ASGI Product surface 为当前认证 subject/role 运行和读取精确 wake-date 报告。它不连接 PostgreSQL、不实例化 Runtime，也不接受 caller-selected subject、role 或 vendor alias。

## Commands

```bash
sleepagent-report run --wake-date YYYY-MM-DD [--no-wait] [--timeout SECONDS] [--json] [--trace]
sleepagent-report show --wake-date YYYY-MM-DD [--json] [--trace]
sleepagent-report list [--limit 1..100] [--cursor TOKEN] [--json] [--trace]
```

`run` 要求精确的 finalized、nonconflicting local date。默认行为是提交一次并轮询 `show` 到 terminal state；`--no-wait` 在收到 `202 accepted` 后返回。list 默认 20 条，按 wake date 倒序，cursor 是不含内部 ID 的 opaque token。

## Configuration

CLI 仅从环境加载认证 profile：

- `SLEEPAGENT_REPORT_BASE_URL`：绝对 HTTPS Product service URL；也可使用 `SLEEPAGENT_PRODUCT_BASE_URL`。
- `SLEEPAGENT_REPORT_SERVICE_CREDENTIAL`：service bearer。
- `SLEEPAGENT_REPORT_ACTOR_PRIVATE_KEY`：绝对、非 symlink、权限 `0600` 的 Ed25519 PEM 文件。
- `SLEEPAGENT_REPORT_ACTOR_ID`、`SLEEPAGENT_REPORT_SUBJECT_ID`、`SLEEPAGENT_REPORT_ROLE`：签名 profile；role 只能是 `elder`、`family` 或 `doctor`。
- `SLEEPAGENT_REPORT_ASSERTION_ISSUER`、`SLEEPAGENT_REPORT_ASSERTION_AUDIENCE`、`SLEEPAGENT_REPORT_ASSERTION_KEY_ID`：可选 assertion metadata。
- `SLEEPAGENT_REPORT_AUTHORIZATION_EPOCH`、`SLEEPAGENT_REPORT_PRIVACY_EPOCH`、`SLEEPAGENT_REPORT_RETRIEVAL_POLICY_EPOCH`：当前 governance epochs，默认 1。
- `SLEEPAGENT_REPORT_HTTP_TIMEOUT_SECONDS`、`SLEEPAGENT_REPORT_POLL_INTERVAL_SECONDS`、`SLEEPAGENT_REPORT_WAIT_TIMEOUT_SECONDS`：可选 bounded timeouts。

API profile 还可设置非敏感的 `SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE`，且只接受 `live` 或 `deterministic`。未设置时，live 数据默认使用 `live` identity，replay 数据默认使用 `deterministic` identity；若 replay worker 使用 live Product model，API profile 也必须显式设置 `live`，否则已有报告会按 manifest 不兼容显示为 `stale`。live 模式下 API 与 worker 还必须接收相同的非敏感 `SLEEPAGENT_PRODUCT_LLM_MODEL`、`SLEEPAGENT_PRODUCT_LLM_BASE_URL`、timeout 和 max-token 配置；`compose.yaml` 已统一传递这些 identity inputs，但不会把 API key 交给 API process。deterministic identity 同时绑定 `SLEEPAGENT_BACKEND_DEPLOYMENT_MODE`（仅 `test` 或 `development`）和 replay data mode；live provider endpoint 只以 SHA-256 进入 identity，不公开地址或 credential。

Credential 和私钥内容不得作为命令行参数、写入 shell history 或日志。生产 profile 的私钥路径必须由部署系统提供并限制读取权限。

## States

- `not_run`：night 存在，但没有兼容的 report request/shared artifact。
- `pending`：request/shared analysis 尚在 durable queue 中。
- `ready`：当前 source/context/epochs 对应的 shared analysis 和授权 role projection 已提交。
- `failed`：analysis 终止且没有可见 artifact。
- `stale`：旧 artifact 存在，但 current revision/context/epochs/runtime pins 已改变。
- `policy_blocked`：当前 role 没有可发布投影；doctor 缺少 accepted Safety 时采用此状态。
- `unusable_blocked`：当前 quality 为 UNUSABLE；不会创建 shared analysis 或调用 provider。
- `urgent_handled`：当前 risk 已走 deterministic urgent path；不会创建 shared analysis 或调用 provider。

elder narrative 独立使用 `pending`、`ready`、`fallback`、`failed` 或 `stale`。narrative 不可用时，已验收的确定性 elder projection 仍然可读。

## HTTP and read-only boundary

- `POST /product/sleep/reports/run` 要求 `Idempotency-Key`，使用正常 replay-consuming actor verification，并返回 date-based status URL，不返回 operation ID。
- `GET /product/sleep/reports/{wake_date}` 和 `GET /product/sleep/reports` 使用短时签名的 stateless read verifier。签名仍绑定 service bearer、issuer/audience、method、path、empty body 和当前 authority epochs；重复使用同一短时 GET 签名不会写 replay table。
- GET/list 只运行 SELECT-only UoW，并在退出时 rollback。它们不会创建 work、Memory read receipt、audit row 或 cursor row，也不会调用模型。

`--trace` 只显示 allowlisted gate、shared/narrative created-or-reused state、fallback flag、provider call/token aggregates 和 provider request-ID presence。它不包含 prompt、response、内部 resource ID、actor/subject、provider request ID、raw payload 或私有路径。

## Exit codes

- `0`：request accepted (`--no-wait`)、ready report，或成功 list。
- `1`：server response 不符合公开 report/privacy contract。
- `2`：CLI/profile 配置错误。
- `3`：网络或非-404 API failure。
- `4`：not found 或 report terminal state 不是 ready。
- `5`：等待超时。
- `130`：用户中断。
