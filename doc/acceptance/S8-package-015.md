# Windows 0.1.5 候选与 S8 包内验收

2026-10-24 在 `codex/s8-diagnosis` 构建并复验桌面 0.1.5，功能来自 `0269123` 的桌面报告/干预闭环和 `08c1fb7` 的后端关联。本次 package.json、package-lock.json 仅修改根版本，没有更换依赖。协议仍为 0.1.0，后端源码为 `source-sha256:7f76f71d0a4f84551e4b88f14540a0e29de2c844f5b93cccb9b7dda7f2ac3c9f`，uv.lock 未变。

本轮只构建、审计和运行便携程序，**未执行安装、覆盖升级或卸载**。独立 Windows 用户及原生专项依用户指示继续待补；包内通过不代表 S6/D1、S8/D2 或完整项目已验收。

## 候选文件

构建前核对旧 0.1.4 四个归档文件与 [S7 清单](S7-package-014.md)摘要完全相同，保留在 `.agentgit/packages/desktop-0.1.4/`。新四件套同时保存在 `.agentgit/packages/desktop-0.1.5/`，通用 Setup 文件名不能用来识别历史版本。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156130816 | `70adf52fa1aa54b0dc7dae56a3ad5d3033cd7809f8bb5cd0fbd5c13700e98629` |
| out/make/squirrel.windows/x64/AgentGit-0.1.5-full.nupkg | 155373668 | `71c1fcf6ec7ec03661ece1e8b19a7fb285bc0ab9869c888f42f9543bb37ba822` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `e03114c011944c4eb2a90df27b5e40df3ad301185129cb01955b2d2bd8b8d925` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.5.zip | 159350383 | `538bcc0294b5a74db65baf409d3863e89728bd66d4760b18b67b6099a356a92c` |

Setup 和主程序均为 NotSigned。ASAR 为 96 项、2829781 bytes，SHA256 `a692192b5538e063804d09b27adfe6cb2fb4f92c930f99da435e7be575f15d1c`。33 个运行构建文件与本地构建逐字节一致；根目录白名单、三套字体/OFL、26 项声明依赖授权通过。唯一生产 Node 包仍为 electron-squirrel-startup 1.0.1。审计实际拒绝了包内额外 `.build/forgotten-test.json` 和根版本仍为 0.1.4 的两个反例。

ZIP 解压到 `C:/Users/DC/AppData/Local/Temp/agentgit-portable-0.1.5-yc2rr6cp`，73 个文件、376816535 bytes 与打包目录一致；Nupkg 中的 ASAR 也一致。全部测试后再次核对该目录、归档和四个分发文件摘要，未被运行过程改写。

## 实际验证

静态检查、构建及 **30 项 Node 单元测试**通过。测试均明确从解压目录的 AgentGit.exe 启动，核对 `app.isPackaged=true`、Windows x64、版本/页脚 0.1.5；新 S7/S8 流程的应用进程 PATH 仅为 `C:/Windows/System32`，移除 NODE_PATH/NODE_OPTIONS，不借用构建用 Node/npm 或 Vite 服务。

| 包内流程 | 结果 |
|---|---|
| 基础四页与连接 | 2 条通过：降级/恢复、隔离桥接、固定业务路由、四页查询与受控提交 |
| 既有真实流程 | 4 条通过：实际 Worker 停止/恢复、自有不可达 Docker/镜像查询 404；关闭/重开/崩溃/断连和 Chromium 缩放；五工具/Fork/Recorded/Live/Diff/导出；固定供应商 HTTP 替身的计量与未知预留 |
| S7 实验 | 2 条通过：费用/旧验收器/原键控制边界；独立 API/PostgreSQL/Worker 下的 Live/Hybrid 各十个初始 trial、暂停/恢复/取消、工具响应干预和重开只读 |
| S8 报告与候选 | 2 条通过：明确拒绝与未知响应区分、原键确认、同报告 GET 重试、并列/歧义/缺口、安全文本与精确旧事件；真实桌面生成报告、保存计划干预、带入 A/B 表单并显式提交十次试验，再返回核对效果和事件跳转 |

包内共 **10 条 Electron 流程通过，无跳过**，没有累加源码或历史版本测试。S8 真实 Mock 为 A 0/5、B 5/5，保留原报告和来源指纹；S7 Live 为 9 passed + 1 cancelled/incomplete，Hybrid 两组各 5 项通过，A 合计 5 次 recorded、B 合计 5 次 synthetic，均为 test_only。S7 的 40 条容器执行记录均清理确认，容器实际不存在。重开查询没有新增 POST，最终 pageerror/console error 为空，独立测试 Schema 已清理。

S8 是独立工程示例，不属于冻结 20 例，不修改其样例、划分、标签或排名。七个冻结静态定位实现文件摘要仍相同；保留集主要注入点 Top-3 仍为 4/8，未达 6/8，人工标签全部待审阅，不声明定位优势或严格因果。

1440/1024/768px 及补充 360px 的表单、报告、候选截图及页面无横向溢出检查通过；八候选未被压缩裁字，键盘焦点能进入精确事件标题。沿用 DESIGN，未重新设计。生命周期的 device scale factor 1/1.5/2 是 Chromium 强制缩放，不是 Windows 原生 DPI 验收。

API/数据库错误与供应商计量使用明确标注的 HTTP fixture；实际 Worker/Docker 异常仅操作测试自有进程/context/镜像查询，不停止外部服务或删除共享镜像。导出仍替换原生保存框返回值。未访问付费供应商、未做真实供应商冒烟，不把固定替身的估价视为供应商账单。

## 复验入口

从 desktop 目录执行既有 typecheck、test、make 和 audit-package；将 ZIP 解压到新目录，再设置以下变量运行两个新阶段的专项。真实流程须预置仓库测试数据库、固定镜像与独立 Worker 所需设施。保持 Python 产品源码及 uv.lock 不变，不自动迁移业务库。

```powershell
$env:AGENTGIT_DESKTOP_EXECUTABLE='<新 ZIP 解压目录>\AgentGit.exe'
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S8-new-package-check'
npx.cmd playwright test --config playwright.diagnosis.config.ts
npx.cmd playwright test --config playwright.experiments.config.ts
```

本轮既有四页回归使用 `playwright.config.ts --reporter=line`，既有真实流程使用 `playwright.real.config.ts --reporter=line`，分别设置 basic/legacy 独立前缀以保留旧证据。Browser 插件不可用，使用仓库锁定的 Playwright Electron。Node 的递归 rmdir、React Flow `use client` 和 NO_COLOR/FORCE_COLOR 既有提示保留，不是测试失败。

## 固定证据与剩余门槛

原始证据位于 `.agentgit/evidence/S8-package-015-*`，新 S8 原始 JSON 与截图另位于 `C:/Users/DC/AppData/Local/Temp/agentgit-s8-real-ynLXNy/` 和 `agentgit-s8-boundary-pqLV2d/`。汇总绑定 40 份审计、报告、截图及测试结果，以及包字节和规范摘要。

| 证据 | SHA256 |
|---|---|
| S8-package-015-audit.json | `a358ad7fcb2a71b335c70db7f50b3b3f42c2e3df2a57fb568c6bf0c5b3b968c7` |
| S8-package-015-audit-negative.json | `26c5ac3d54ed420766fffbcb85f215bb79aa8a32b4a9f17c994af6431b4a6167` |
| S8-package-015-inventory.json | `cbaa31164986353a8167a2f9abfa2d2f4dd98e23a91a6ec8a5774fff1451d6a9` |
| S8-package-015-diagnosis.xml | `b03813839ec36b420b1c0d94203bf231f81de34cce2b24f645742bd3f76ca85b` |
| S8-package-015-experiments.xml | `6c33f5edf7a3cc2ab04e78517d7bab67926fca58b2199edf5bce329e9ec5e74c` |
| S8-package-015-summary.json | `113a7194bc5391c2c1067e45fd7e96519ad20c07819aa99e6948c0a9ad6a527a` |

独立 Windows 用户安装/覆盖升级/卸载、原生保存/DPI、整机禁外网及真实依赖缺失交叉验收仍按 [S6 清单](../S6-windows-acceptance.md)待补。S7 前置未因此补齐；S8 标签与其他适用故障的效果证据、S9–S11 和 S12-A/B/C/D 继续保留。后续 S9 只能按实施方案显式使用完整候选降级验证搜索工程，不因本包通过而宣称 D2 定位优势或完整交付。
