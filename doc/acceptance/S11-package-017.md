# S11 Windows 0.1.7 分发候选

2026-10-29，分支 `codex/s11-merge`。候选已构建并通过内容审计，包含[Merge Review](S11-desktop.md)、固定合并 IPC、三方原文、完整修订/本人票据和批准后显式新任务。20 个不同包内测试节点均已有通过证据，23 次实际尝试中的 3 次失败保留如下；这不表示独立安装或完整阶段验收。版本根从 0.1.6 更新到 0.1.7，依赖没有调整，后端身份仍为 `source-sha256:6bd747bcfae2111d2857a9a51f6ebeb0bd9e7726b6c8e117c29a9ccb784761ff`、API 协议 0.1.0、Schema head `0026_merge_runs`。

## 分发文件

构建前逐个核对[旧 0.1.6](S10-package-016.md)四件套摘要保持，旧文件保留在 `.agentgit/packages/desktop-0.1.6/`。当前四件套独立保存在 `.agentgit/packages/desktop-0.1.7/`，不能凭 out 下通用 Setup 文件名辨认版本。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156151296 | `8ca050f02fafc3d41f67eb6ecbc1978bdb78da655a03bb431675e93920e4d912` |
| out/make/squirrel.windows/x64/AgentGit-0.1.7-full.nupkg | 155395219 | `d10b46d578500df06ab1e445e0e1ed92b17d4944b294b0c34618b43cd937ab7b` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `872608993c08160faec634e80b364302ec17d73f9152f40f9a65841f61ea3f1c` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.7.zip | 159372281 | `67dd5c38ffb77b32dd65d3755cb23e5f4ac7e97a2854cb8d7e5b4c2ffaa460de` |

主程序和 Setup 均为 NotSigned。ASAR 为 101 项、2,908,908 bytes，SHA256 `12635691eb0a032f19397138c778fdbbb92a1f69b95dc94e63f629e02489115d`；38 个构建文件的集合及字节与本地一致，根白名单、禁开发/测试文件、三套字体及 26 项声明依赖授权通过。唯一生产 Node 依赖仍是 electron-squirrel-startup 1.0.1。临时 ASAR 副本分别增加额外构建文件、修改根版本，两项审计都按确切原因拒绝，没有修改交付 ASAR。

ZIP 已检查路径/符号链接并安全解压至 `C:/Users/DC/AppData/Local/Temp/agentgit-portable-0.1.7-h3u7i3o9`，73 个文件、376,895,662 bytes 与打包目录逐项一致；Nupkg 内 ASAR 同样一致，RELEASES 的 SHA1/长度与新 Nupkg 匹配。构建器保留的 UTF-8 BOM 不修改；初次清单检查器用普通 UTF-8 读取导致失败，按该实际编码修正读法后通过，原始分发字节保持。

## 包内复验范围

类型检查、构建及 40 项桌面单元通过。以下均从上述解压目录的 AgentGit.exe 执行，核对测试版本、页脚、实际程序路径和 `isPackaged`；不将开发构建结果计入这 20 个节点。S7/S8/S10/S11 公共启动器移除应用进程 NODE_PATH/NODE_OPTIONS，PATH 仅 System32；旧基础/设施配置保留各自启动器，独立测试后端仍从开发侧启动，不将其声称为整个测试环境无 Node。

| 范围 | 不同通过节点 | 证据及主要结果 |
|---|---:|---|
| Merge Review | 3 | `S11-package-017-merge.xml`；真实正例、真实错误引文/拒绝、明确 HTTP fixture；双父、显式新任务及 Recorded、完整修订/本人票据和重开只读 |
| 连接/基础四页 | 2 | `S11-package-017-boundary.xml`；本地资源、隔离桥接、连接降级与恢复 |
| 真实设施/模型接线/生命周期 | 4 | `S11-package-017-real.xml`；实际独立 Worker/容器、历史/Fork/零执行 Recorded/Live/Diff、关闭重开、键盘与 Chromium 缩放；模型接线使用本地 HTTP 替身，无付费调用 |
| 实验 | 2 | `S11-package-017-experiments.xml` 中 HTTP fixture；真实例最终见 `S11-package-017-experiments-v3-experiments.xml`，Live 9 通过/1 取消、Hybrid A/B 各 5 通过，来源不变、容器清理及重开只读 |
| 诊断 | 2 | `S11-package-017-diagnosis.xml` 中 HTTP fixture；真实例最终见 `S11-package-017-diagnosis-v2-diagnosis.xml`，A 0/5、B 5/5、事件/来源精确跳转及重开 |
| Repair Lab | 7 | `S11-package-017-repair.xml`；联合 Prompt/Plan、字段缩减、六加四/精确批准、候选交接、取消、未知、观测禁采纳及桥接/HTTP 边界 |

原始 XML 共 23 次尝试：实验首次截图 30 秒超时，第二次数据库不可用、执行前退出；诊断首次同样截图超时。旧报告与失败现场独立保留，不计为通过。恢复既有 Docker Desktop 和原 PostgreSQL 容器后，核对原镜像/卷及测试库可用；没有重建业务数据，也不推断退出原因。仅测试捕获增加窗口恢复/聚焦、关闭截图动画、60 秒期限和视口元数据，实验捕获先回到顶部；产品、分发包和后端未改。后续实验、诊断及七条 Repair Lab 全过。保留四档宽度截图；Chromium 缩放、测试返回保存路径均不替代原生 Windows DPI/保存验收。

## 原始证据复核

证据根为 `.agentgit/evidence/`，汇总 `.agentgit/evidence/S11-package-017-final/summary-v1.json`，SHA256 `900ad219cabb31578c55178d0743adfa8975264cf9ec72e87c35988a70c3ff3d`。汇总绑定九份 XML、20 个节点、3 次失败、四件套、冻结文件及独立分析结果。封存脚本为 `seal_s11_package_017.py`，复用已冻结分析函数而不执行旧版脚本主程序或修改历史证据；本次原始包另名保存。

- Merge Review：`S11-package-017-merge/offline-audit-v1.json`；正例 436 对象/978,518 bytes、23 模型/18 工具/231,407 Mock 字节，显式新任务占 10 模型/9 工具/108,644 字节，Recorded 零新调用；负例 251 对象/519,606 bytes、硬冲突且零票据/批准。三个来源指纹不变，独立初始化不继承消息/完成/用量/授权，原运行前 332 文件另存。
- 联合修复：`S11-package-017-joint-archive.json`，SHA256 `8ac36aca6f8a9dbc650e7808e31fbf7b9d0f2be57c911e58db3797a1af5a0b3f`；1,723 对象/4,497,539 bytes。六加四为 `verified_under_fixture`，58 模型/48 工具/655,324 Mock 字节；批准只创建当前精确配置版本，不增加 Job/Run。
- 观测补丁：`S11-package-017-observation-archive.json`，SHA256 `eb4fb0b8147d0295044f8787631eb29551b64318f89c3b7685e747139618601f`；1,445 对象/2,694,896 bytes。实际六加四仍为 test_only，正式采纳禁用且 API 拒绝，仅可保存实验定义。
- 未知边界：`S11-package-017-unknown-archive.json`；890 对象/2,121,887 bytes，未知调用/预算预留保持，取消与重开不重发、不伪造终态。排队取消零试验、候选交接只预填和重开查询均核对。

这些本地 Mock 工程结果不改变冻结的三十组研究成绩，不证明定位优势、任意合并或全局最小修复。

本轮仅构建、审计并运行便携应用；没有执行安装、覆盖升级或卸载，没有自动迁移业务数据库或调用付费模型。独立 Windows 用户/原生 DPI/保存等验收、S8 人工标签、S11 人工预期以及完整阶段前置仍待补，沿用用户的暂缓决定。本候选通过也不能宣告完整 S11 退出或进入 S12。
