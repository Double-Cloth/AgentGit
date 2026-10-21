# AgentGit 实施进度

当前实现依据设计/实施方案 v1.0.0；本文件记录实际进展，不将规划中的预期视为完成。

| 阶段 | 状态 | 证据 |
|---|---|---|
| S0 | 已归档 | `ff9e971`，设计、实施方案和仓库规则 |
| S1 | 已验收 | [S1.md](acceptance/S1.md)：59 单元 + 2 真实 PostgreSQL 集成通过；构建、实际 HTTP 与静态检查通过 |
| S2 | 已验收 | [S2.md](acceptance/S2.md)：112 项回归通过，含 HEAD 竞争、8 个进程崩溃场景、恢复/隔离与迁移往返 |
| S3 | 已验收 | [S3.md](acceptance/S3.md)：156 项回归通过；离线研究、版本化计划、三工具、来源验证、预算与双 Run 隔离 |
| S4 | 已验收（本机） | [S4.md](acceptance/S4.md)：T01–T20 逐项核对；当前 392 项分批覆盖，真实 CLI 五工具/产物 hash 通过；清理后新 attempt、预算与恢复控制完成，doctor 实测就绪 |
| S5 | 进行中 | [版本/重算](acceptance/S5-progress.md)、[耐久 Recorded](acceptance/S5-recorded-jobs.md)、[Live](acceptance/S5-live.md)：独立预算、配置/计划/文件干预、重新审批及真实容器 A/B 已实现；本批完整基线 413 + 108 项通过，最终边界修正后相关 263 + 2 项通过；复合录制及剩余可信核对来源仍待完成 |
| S6–S10 | 未开始 | S5 验收后进入 S6；采用 Electron + React，交付 Windows x64 桌面安装/便携包 |
| S11 | 必选，未开始 | D2 后完成三方产物/结构化状态合并、桌面 Merge Review 及验收，再进入 S12 |
| S12-A/B/C/D | 必选，未开始 | S11 及四项适配全部验收、最终桌面包验证后才达到 D3 |

开发命令见 [DEVELOPMENT.md](DEVELOPMENT.md)。后续阶段必须先满足前一阶段退出条件，D1/D2 不能替代完整交付。
