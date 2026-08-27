# SleepAgent 文档

- [architecture.md](architecture.md)：当前进程、API、domain、runtime 与 worker 边界。
- [architecture/perceptor-real-radar.md](architecture/perceptor-real-radar.md)：真实云云/Perceptor 雷达的 Push、Pull、canonical、NightEpisode 与 Agent 边界。
- [operations.md](operations.md)：replay/PostgreSQL harness、迁移、验证和 live model 配置。
- [operations/perceptor.md](operations/perceptor.md)：当前 Perceptor 部署组件、健康检查、恢复语义与已知人工编排缺口。
- [contracts/README.md](contracts/README.md)：对外 HTTP 合同和 OpenAPI 快照。
- [audits/P4-REAL-RADAR-FINAL.md](audits/P4-REAL-RADAR-FINAL.md)：P4 最终证据、修复与限制摘要。
- [audits/P4-REPOSITORY-CONSOLIDATION.md](audits/P4-REPOSITORY-CONSOLIDATION.md)：P4 仓库收敛验证记录。

逐阶段审计、施工计划和一次性验收脚本已移至仓库外 owner-only 历史归档；canonical 文档只描述当前行为。
