# HTTP contracts

`openapi/backend-bff-v1.json` 与 `openapi/demo-v1.json` 是当前 canonical app 生成的快照。更新 transport 后运行：

```bash
python scripts/generate_openapi_snapshots.py --check
```

Public API 位于 `/api/v1/*`，Product API 位于 `/product/sleep/*`；demo 和 internal surface 由独立 profile 与 credential 隔离。
