# AgentGit 实施进度

当前实现依据设计/实施方案 v1.0.0；本文件记录实际进展，不将规划中的预期视为完成。

| 阶段 | 状态 | 证据 |
|---|---|---|
| S0 | 已归档 | `ff9e971`，设计、实施方案和仓库规则 |
| S1 | 已验收 | [S1.md](acceptance/S1.md)：59 单元 + 2 真实 PostgreSQL 集成通过；构建、实际 HTTP 与静态检查通过 |
| S2 | 已验收 | [S2.md](acceptance/S2.md)：112 项回归通过，含 HEAD 竞争、8 个进程崩溃场景、恢复/隔离与迁移往返 |
| S3 | 已验收 | [S3.md](acceptance/S3.md)：156 项回归通过；离线研究、版本化计划、三工具、来源验证、预算与双 Run 隔离 |
| S4 | 已验收（本机） | [S4.md](acceptance/S4.md)：T01–T20 逐项核对；当前 392 项分批覆盖，真实 CLI 五工具/产物 hash 通过；清理后新 attempt、预算与恢复控制完成，doctor 实测就绪 |
| S5 | 已验收（本机） | [S5.md](acceptance/S5.md)：T01–T09 核对；599 项由三个不重叠组全部覆盖，无失败/跳过；实际 CLI 五工具→Recorded→复合 Recorded→A/B Live→Diff 通过；来源不变，4 个容器独立且清理确认 |
| S6 | 实施中，未验收 | [0.1.3 历史 T01–T12 核对](acceptance/S6-keyboard-and-gates.md)保留 MSIX 安装生命周期等证据；当前候选为 [0.1.4](acceptance/S7-package-014.md)，本轮仅构建/审计/便携运行，未重做安装/升级/卸载。[D1 指南](D1-desktop-demo.md)和[独立 Windows 清单](S6-windows-acceptance.md)绑定新摘要。原生保存、独立用户/原生 DPI/禁止外网及真实依赖缺失交叉复核待补；真实供应商冒烟未运行 |
| S7 | 本机功能核对通过，前置待补 | 冻结验收器、实验/干预、Hybrid、精确统计、桌面及 [0.1.4 Windows 包](acceptance/S7-package-014.md)已实现；[T01–T06 与实施交付/退出矩阵](acceptance/S7-gates.md)齐备。后端最新 497 项定向回归、当前 26 桌面单元和 8 条打包 Electron 流程分别记录，不累加此前 764 项历史结果。新 ZIP 的 Live 10 trial 为 9 通过/1 取消，Hybrid 各 5 全通过，来源不变、40 容器执行清理、原键重试与重开无 POST 已核对。S6/D1 原生/环境前置仍待补，S7 不标整体验收或合并 main |
| S8–S10 | 未开始 | S7 后进入定位、修复搜索与 D2；不能提前算作已交付 |
| S11 | 必选，未开始 | D2 后完成三方产物/结构化状态合并、桌面 Merge Review 及验收，再进入 S12 |
| S12-A/B/C/D | 必选，未开始 | S11 及四项适配全部验收、最终桌面包验证后才达到 D3 |

开发命令见 [DEVELOPMENT.md](DEVELOPMENT.md)。除实施方案第 1.2 节记录的 S6→S7 临时开发顺序调整外，后续阶段仍须满足前置退出条件；D1/D2 不能替代完整交付。

2026-10-23 后续验收：[分发修正与新候选](acceptance/S6-package-hygiene.md)发现旧 ASAR 含测试状态文件/构建配置，撤回此前包审计的过宽结论；改为根白名单并实际拒绝旧包，重建 0.1.1 Setup/ZIP。20 项单元、2 条 Electron 边界、新 ZIP 真实五工具闭环（48.6s）、新 Setup MSIX 安装生命周期（43.6s）通过；27 个运行构建文件未改变。用户将独立 Windows 环境留待稍后。原生保存失败如实保留；S6-T11 的连接错误/恢复已有 UI 证据，Worker 停止、Docker/固定镜像缺失的专项仍待补充。S6 不提前验收或合并。
