# Windows 0.1.4 候选与 S7 包内验收

2026-10-24 在 `codex/s7-experiments` 构建、审计并复验桌面 0.1.4；构建/测试入口变更提交为 `663aeaf`，运行视图基于 `14e79ed`。本轮不执行安装、覆盖升级或卸载，S6/D1 独立用户与原生专项继续待补。0.1.3 的四个分发文件已按原摘要归档到 `.agentgit/packages/desktop-0.1.3/`。

## 当前候选

使用锁定环境执行 `npm.cmd run make`，没有新增或升级依赖；package-lock 仅修改根版本。后端协议仍为 0.1.0，Python 源码仍为 `source-sha256:1132d409a7824df2413c62eb852bbf800ef31c7466964d3d16e6922368acd885`，未改变数据库、固定镜像或 uv.lock。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156123136 | `ef29ae8a1d072b41724198928787b91642738597b3d749d8239f2566b93f627e` |
| out/make/squirrel.windows/x64/AgentGit-0.1.4-full.nupkg | 155366005 | `2b48b62a453b4a6388af03a24081751663edeaca66bf4a30af988dd26e82e3cb` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `555f85d3b9fb0f3deda171c59cb70245bf37d9c5acb1a0c0b6ed14e7b838e96f` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.4.zip | 159341528 | `c5797874fb61a8c6a9c6e9caf6cd25da878406ef06294ad8a1220c3089843915` |

同一组文件已另存 `.agentgit/packages/desktop-0.1.4/`，后续重建不能仅凭通用 Setup 文件名识别候选。Setup 和主程序均为 NotSigned；本记录证明本机候选内容及运行范围，不表示已发布或通过独立安装环境。

ASAR 为 92 项，摘要 `ada039155d66e4fbb9dcf525d52dfa99b967d46c8a8b3fa8262d1db26bd9c61e`。29 个运行构建文件与本地文件逐字节一致，额外构建文件也会拒绝；根白名单、三套字体/OFL、26 项声明依赖授权通过。唯一生产 Node 包仍为 electron-squirrel-startup 1.0.1。新审计实际拒绝夹带 `.build/forgotten-test.json` 及包内版本仍为 0.1.3 的两个反例。

ZIP 解压到 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-0.1.4-13tmim1m`；73 个文件、376787089 bytes 与打包目录全部相同，Nupkg 内 ASAR 也与打包目录相同。测试从该解压目录的 AgentGit.exe 启动，使用独立临时工作目录/用户数据目录，`app.isPackaged=true`、Windows x64、版本/页脚均为 0.1.4；新增实验流程的应用进程 PATH 仅为 `C:\Windows\System32`，移除 NODE_PATH/NODE_OPTIONS，没有借用构建用 Node/npm 或 Vite 服务。

## 实际验证

| 范围 | 结果 |
|---|---|
| 静态与单元 | typecheck、build、diff 检查通过，26 单元通过 |
| 包内基础边界 | 2 条通过：连接降级/恢复、四页、隔离桥接、受控提交；旧能力后端保留三种单次分支，禁用新增 A/B |
| 包内既有真实流程 | 4 条通过：Worker 停止/恢复及受控执行环境异常，窗口关闭/重开/崩溃/断连，五工具/Fork/零执行 Recorded/Live/Diff/导出，固定供应商 HTTP 替身的计量与未知预留 |
| 新实验 HTTP fixture | 1 条通过：费用未知不显示为零、空完整样本、旧验收器错误保留已确认报告、断连保留请求、控制原键/原版本重试、键盘入口和同 ID 查询 |
| 新实验真实流程 | 1 条通过，约 6.3 分钟：独立 API/PostgreSQL/Worker，Live 与 Hybrid 共 20 个独立初始 Run；提交响应丢失后原键确认，暂停/恢复/取消、工具响应干预、分组统计和窗口重开 |

本轮包内共 **8 条 Electron 流程**通过，未与源码或历史版本结果重复累计。真实 Live 为 9 passed + 1 cancelled/incomplete；Hybrid A/B 各 5 项全部 passed，A 合计 5 次 recorded，B 合计 5 次 synthetic，均为 test_only。来源指纹不变，40 条容器执行记录清理确认且容器实际不存在，重开查询没有新增 POST，收集的 pageerror/console error 为空。测试 Schema 已移除，CAS 和原始证据保留。

项目 `f1951e0b-0215-4314-82a7-f6d9253a8474`，来源 Run `c6374794-9f58-4372-b769-16af1e1551b3`；Live 实验 `766f654e-03f2-452a-9bef-24b33e4bbdba`，Hybrid 实验 `eea8f7ae-a83e-42c8-a33b-67648c2e6341`。冻结 manifest 的 code_revision 已与上述 Python 摘要复核相同。

没有 Browser plugin，继续使用仓库锁定的 Playwright Electron；实际检查 `agentgit://app/index.html` / AgentGit、非空页面、无框架错误覆盖层。1440/1024/768 和补充 360 内容宽度的表单/报告截图、页面无横向溢出、表格容器滚动及 20px 字段间距断言通过；没有改动 DESIGN 体系。这些 viewport/Chromium 缩放结果不代替 Windows 原生 DPI。

API/数据库降级和供应商调用使用明确标注的 fixture；执行环境异常只操作自己的测试 Worker/context/镜像查询，不停止外部服务或删除共享镜像。导出仍替换原生保存框返回值，没有补齐真实保存对话框。独立 Windows 用户、原生保存/DPI、整机禁止外网及真实依赖缺失交叉复核仍按 [S6 清单](../S6-windows-acceptance.md)待补。未访问付费供应商或产生真实计费，真实供应商冒烟未运行。

## 重复验证入口

在 desktop 目录先执行 typecheck、test、make 和 audit-package；将 ZIP 解压到新目录，再设置以下测试变量。实验测试入口支持源码与明确指定的打包程序，两者都检查版本/页面身份；打包模式另核对进程路径及受限 PATH。证据前缀限制为 1–80 个字母、数字、下划线或连字符，避免覆盖历史默认结果。

```powershell
$env:AGENTGIT_DESKTOP_EXECUTABLE = '<新 ZIP 解压目录>\AgentGit.exe'
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX = 'S7-package-014'
npx.cmd playwright test --config playwright.experiments.config.ts
```

后端必须预置测试库、固定镜像和测试身份；测试只创建自己的 Schema/Worker/工作区。新增实验流程及旧流程均按当前协议运行，不自动安装后端或迁移业务库。仍需按 [S7 退出核对](S7-gates.md)保留控制变量、残余不确定性与前置待补项，不能把本包通过当作完整项目交付。

## 固定证据

原始结果位于 `.agentgit/evidence/S7-package-014-*`。最终汇总包含全部用例、实现文件/需求摘要、包字节与签名状态，以及 42 份正反审计、报告和截图摘要。已核对分发目录与独立归档均符合清单 SHA256，package-lock 相对 14e79ed 仅根版本变化。

| 文件 | SHA256 |
|---|---|
| S7-package-014-audit.json | `781fdeef6207602cdb3ef9489f140de6c9f7728fd7cb5f87da28fc9a197350cc` |
| S7-package-014-audit-negative.json | `15380f862837ce038f26bde90d2862212b7828433446c110a00ef99fc9f95c2a` |
| S7-package-014-inventory.json | `69b64985c4d57413973662ff46bd81d87c345449195b9efb8db9c6ac9955806f` |
| S7-package-014-unit.xml | `ec388c4ec0c4bcbc0b84b1ee43d173ee58cd69008dec3a8b6954abd42843d4d5` |
| S7-package-014-basic.xml | `1f0ec17cebee9885aa9b62ecb3ad3de2efcb87ce02d5d6b3707c35d536adad8b` |
| S7-package-014-legacy.xml | `9f53f43d36c044559764a8098bd3321e6deb6d7f3950657c42634ac203a6c101` |
| S7-package-014-experiments.xml | `a8fa77b84b466da5821b9cfd4c6511b8dc1532757da55d90a5f40d6717b05829` |
| S7-package-014-real.json | `1350d3e318c8675bf9b3f7b8ad0737aaf652a04a7bd0d043ca550926afc17b48` |
| S7-package-014-cleanup.json | `41132825fa952d191664062f7bf74757f5db0d60e0c4fd07cd43e16010bbfb1e` |
| S7-package-014-summary.json | `b769da0961dd7882995e5ff8c441048815ee71598c9161aaee8333f1f77704cd` |
