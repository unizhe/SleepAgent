# P4 real-radar final summary

## Scope and final architecture

P4 将真实毫米波雷达通过云云/Perceptor Cloud 接入 SleepAgent：生产 Push webhook 与只读 Pull 在 integration boundary 内验证、加密落库、规范化并对账，形成 canonical observations、NightEpisode，再进入现有 1+2+1 Agent Runtime。系统处理厂商派生数据，不处理 ADC/IQ 原始波形。

## Real evidence obtained

- 真实 Push 合同、租户签名 golden 和 production webhook 均已证明；一次生产边界验收记录 12 个 authenticated raw、5 个绑定后 accepted event、20 个 canonical observation，重复 canonical 写入为 0。
- Platform token、设备发现、Current、bounded realtime、History 与 SleepReport 的只读合同已实证；生产代码没有签名 fallback。
- 最终实夜保留 567 个 authenticated Push，完成 11 个 History window、29,721 个 Pull backfill，冲突和重复 canonical 写入均为 0。Push 与 History 的关系为 `NON_IDENTICAL_CADENCE`。
- 实夜生成 `PARTIAL` NightEpisode，并在隔离 PostgreSQL 中完成 backup restore、membership、quality、canonical/provenance、DeviceBinding 与 migration ledger 的语义一致性复核。

## Push, Pull and reconciliation proof

Push 只有在 signature/freshness/contract 校验和 PostgreSQL durable commit 成功后才确认；未知设备 fail-closed 到 quarantine。Pull exact response 加密入库，normalization 和 semantic reconciliation 由同一 live ingestion Worker 执行；checkpoint 只在 reconciliation commit 后推进。crash-after-commit、retry、overlap、no-data 和 duplicate 均有稳定回归。

真实数据证明 Push/History 不是逐样本同频。系统按 acquisition authority 记录来源：同事实合一，冲突 append-only，Pull 回补不覆盖原始 Push authority。

## Agent, Product and privacy proof

冻结实夜随后通过真实 OpenAI-compatible provider/DeepSeek 链路：最终记录 15 次 provider transport 和 14 次 durable Agent invocation。elder/family projection ready；doctor 被既有 plan policy 阻断，最终 Product 合法地 `WITHHELD_BY_POLICY`。

provider send-time audit 证明没有 raw vendor payload、完整 canonical dump、membership ID 列表或 direct private identifier。公共 Product projection 使用 typed allowlist 与稳定 pseudonym；私有内部审计 lineage 保留但不会进入公共 durable JSON。urgent deterministic fast path 仍保持零模型权威。

## Remediation rounds

P4 经历了多轮失败、取证和窄修复，最终保留的生产根因/回归包括：

- 真实 Push `OnBed`/`DateTime` casing 与 string-encoded `data` shape；Push/Platform 分离 path 下固定 ampersand key 规则。
- History leading context、SleepReport hourly bucket/no-data 与非法 sentinel 语义。
- quality policy 曾使 `PARTIAL` 不可达，以及大 membership SQL batching 问题。
- reconciliation checkpoint 的 crash/retry/no-op、跨 channel duplicate prevention 与 DeviceBinding authority。
- provider context 曾包含完整 membership IDs/direct subject ID，且 FastPath 曾尝试重写 acquisition-owned frozen quality；现由最小投影和 immutable authority 修复。
- large-source load 与 heartbeat row lock 的 lease ordering、sticky lease-lost、RESERVED invocation recovery 和 stale-fence dispatch。
- public `subject_ref` 曾泄露内部标识；现由字段 allowlist、pseudonym 和 CAS reprojection 修复，同时保留授权角色的健康内容。

## Known limitations

- 实夜 quality 仍为 `PARTIAL`，Push/History 仍为 `NON_IDENTICAL_CADENCE`。
- provider identity binding 已证明，独立 IMEI/机身物理相关性仍 `UNRESOLVED`。
- 自然真实 Push 未观察到 Alarm/AlarmStop/Connected/Disconnected；真实 Alarm → urgent live-device 路径未完整生产证明。
- bounded realtime 真实窗口不足以证明长期连续流。
- 没有 DeviceBinding provisioning CLI、常驻 Pull scheduler 或自动 morning finalization；部分 acquisition orchestration 仍由部署方承担。
- doctor projection 的既有 plan policy 阻断仍存在。

## Final verdict

```text
P4_COMPLETION = COMPLETE_WITH_LIMITATIONS
RELEASE_READY = NO
```

逐阶段施工报告、根计划和运行脚本已在清理前进入仓库外 owner-only、checksum-pinned 历史归档；本文只保留当前工程事实，不替代未来 release validation。
