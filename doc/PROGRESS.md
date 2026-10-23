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
| S6 | 实施中，未验收 | [只读 API](acceptance/S6-read-views.md)、[桌面初版](acceptance/S6-desktop-progress.md)、[真实联调](acceptance/S6-desktop-real.md)、[候选包](acceptance/S6-desktop-packages.md)、[模型基础层](acceptance/S6-model-foundation.md)、[模型运行时](acceptance/S6-model-runtime.md)、[模型桌面接线](acceptance/S6-model-desktop.md)、[新版候选/生命周期](acceptance/S6-model-packages.md)、[0.1.1 安装/升级](acceptance/S6-install-lifecycle.md)：20 项桌面单元、真实 Electron 与便携程序五工具/Fork/Recorded/Live/Diff 通过；Windows x64 安装/ZIP 已构建并核验；模型配置/Worker/预算/恢复/Recorded/Live 及桌面显式选型/计量已接通，独立后端三条包流程、四页 48 组缩放及键盘通过；新增 14 项 PostgreSQL 验证及 322 项单元通过；既有 PostgreSQL、执行工具分批回归通过，本增量去重 701 项。MSIX 上下文实际安装/覆盖升级/卸载/重装及数据保留通过，新候选为 0.1.1；原生保存及独立用户安装/升级/卸载仍待完成；真实供应商冒烟单列未运行 |
| S7–S10 | 未开始 | D1 后进入受控实验、定位、修复搜索与 D2；不能提前算作已交付 |
| S11 | 必选，未开始 | D2 后完成三方产物/结构化状态合并、桌面 Merge Review 及验收，再进入 S12 |
| S12-A/B/C/D | 必选，未开始 | S11 及四项适配全部验收、最终桌面包验证后才达到 D3 |

开发命令见 [DEVELOPMENT.md](DEVELOPMENT.md)。后续阶段必须先满足前一阶段退出条件，D1/D2 不能替代完整交付。
