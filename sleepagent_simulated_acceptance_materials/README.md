# SleepAgent 开发期模拟验收材料

## 文件
- `habit_usability_report.simulated.json`
  - 5 条 60+ 适老交互观察。
  - 字段严格使用已给出的 `habit_usability_report` 结构。
- `habit_domain_review.simulated.json`
  - 3 名化名审核人。
  - 12 个睡眠习惯概念。
  - 逐概念覆盖措辞、选项、TTL、持久化资格、安全边界、审批引用和签署引用。
- `provider_observations.simulated.json`
  - 24 个场景，共 68 条观测。
  - 22 个普通场景各 3 次，`data_quality` 与 `urgent` 各 1 次。

## 当前模拟标识
- catalog_hash: `e29a6561b5f42819e6b21fbd47c0cb5e7f2717731e27e7921540a1577d005ef7`
- release_identity_hash: `7daa5cd7947c44bc82298c69899dc19df7dad4497d6e3c23d24087642b1f3f24`

## 接入前需要核对
1. 将 24 个 `scenario` 名称与仓库中的固定场景枚举逐项核对；如名称不同，只替换标识符，不减少记录数量。
2. 将 `release_identity_hash` 替换为当前 `release_identity.identity_hash`。
3. 第二份审核材料采用补强后的建议结构；在接入严格 Schema 前，需要让 Codex据此补充专用 Schema。
4. 第三份材料未加入 provider、model、request ID 和 receipt，是因为现有 observation 格式中没有这些字段；补强 Schema 后应追加对应引用。
