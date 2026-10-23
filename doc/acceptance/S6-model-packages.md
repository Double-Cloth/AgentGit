# S6 模型版本候选包与生命周期

状态：新安装程序/ZIP 已构建，当前 Windows 用户的 ZIP 独立目录运行、三条真实后端流程、四页 DPI/键盘/长内容通过；**S6 / D1 未最终验收**。源基线 `b649bf3`，后续本批只增加测试和文档，包内产品代码未变。[初次候选](S6-desktop-packages.md)保留旧 `c41635b` 基线，下面不能与旧包摘要混用。

## 构建与包内容

AgentGit 0.1.0、win32/x64，Node.js 24.19.0/npm 12.1.0 构建，Electron 45.0.0/Forge 8.0.1。`npm run make` 成功，主程序与 Setup 均实测 **NotSigned**，未做正式分发信任验证。输出是本地忽略目录，不推送或公开发布：

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGit-0.1.0-full.nupkg | 155355644 | `a1758ae939ab508999c7b9313323190064c872d9c9d7a6ad827f5ffa78a38796` |
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156112384 | `6004d327e6e20598a659f769f5286bde0c931f1f524fee56bca4cc034ce21bab` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `f3a3ae27cd4e44976ef1d5496921c8994c7ba73f58b9ee921add3cf1f267c2df` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.0.zip | 159331483 | `75a1cba8ba5600d2e7bd5b2101c43d4ec5d9e862e041f7f89e40d4afeb5d6f86` |

ZIP 解压到新临时目录 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-b649bf3-9rq63nr3`，所有 73 个文件与 Forge 包逐字节相同，总计 376747812 bytes。`audit-package.cjs` 通过：app.asar 94 项、27 个构建文件字节一致，SHA256 `f7fd5f67291537831251774d3b862c02934189d3be6d80b8373b847badaaa5f1`；没有 src/tests/scripts/.env/业务数据。3 套字体及 OFL、26 项声明依赖授权可离线读取；生产 Node 依赖仍只有 electron-squirrel-startup 1.0.1。没有新依赖、框架或镜像修改。

## 真正运行的包

`AGENTGIT_DESKTOP_EXECUTABLE` 指向解压后的 AgentGit.exe，app.isPackaged=true。应用的 cwd 为独立临时目录，PATH 只留 Windows System32，清除 NODE_PATH/NODE_OPTIONS，没有 Vite/构建 Node/npm。后端单独准备，测试库/Worker 独立，五工具使用固定 Docker；真实供应商由确定性 HTTP 替身代替，不产生真实费用。

完整三条包测试 **3 passed，105.4s**：生命周期 30.7s，五工具 45.9s，模型接线 28.5s。运行时 win32/x64、内置 Node 24.22.0、Chromium 156.0.8078.48；与前端构建用 Node 是不同角色。

- 五工具→历史/Fork→Recorded 28 检查点零模型/工具/执行器→A/B Live→Diff→主进程原字节导出。来源指纹及 main 不变，4 个独立执行容器清理确认；导出默认仍替换保存框返回路径。
- 外部模型显式选择/费用校验、正常计量、Recorded 实际零新增、Live 独立额度、连续创建任务、缺 usage 保守估计、发送后未知保留费用，均由真实 API/Worker/Adapter/桌面经过。与 [模型接线记录](S6-model-desktop.md)语义相同，不把替身当作账户可用性。
- 将真实 Worker 的测试领取入口暂时门控，先提交 queued Run 再关闭桌面；解除门控后 Worker 完成原任务。重新打开保持项目/同一 Run，POST 列表始终只有原项目/配置/任务/提交四项。
- 真正启动第二个相同 profile 进程：第二实例退出 0，原进程收到 second-instance 且保持一个窗口。真实 forcefullyCrashRenderer 触发产品恢复路径；仅恢复提示的返回值由测试替换，重新加载后的实际 DOM 恢复原运行且未重发。Playwright 旧 Page 可能保留 crashed 标记，测试改从实际 webContents 核对恢复 DOM，再重开继续测试，没有改产品逻辑。
- fixture 返回 503 后恢复，界面从连接设置回到 API 已连接、重新查询原记录，没有新增 POST。该证据不是停止宿主 API 服务或改数据库权限。

## DPI、长内容、键盘与边界补测

之后扩展并单独复验四页矩阵 **1 passed，34.0s**，类型检查通过。Chromium 启动 scale factor 为 1/1.5/2，实测 devicePixelRatio 与之相等；四页各 768/1024/1440/360，共 **48 组**均无根页面横向溢出。图/代码/表格允许容器内滚动。长中文目标真实保存在任务/列表/Snapshot，JSON 原文可查看。

关键路径只用 Tab/Enter 打开历史、逻辑状态/产物详情、四页切换，控件焦点 outline 实测 2px / rgb(20,20,19)；状态有名称和文字。截图核对 1440 时间线、200% 的 768 分支页及其他采样，没有框架错误叠层/裁切；图加载完成后才保存，滚回首屏记录。48 组完整测量和截图目录见 JSON，不把缩放参数当作更改 Windows 系统 DPI 或跨硬件显示器验证。

HTTP fixture 的 Electron 综合边界测试继续 **1 passed，9.4s**，新增待审批动作双击验证：只发一次 decision POST，expected_version=3 来自审批项。已有无 Node/process、未授权窗口、参数/跨项目/导航/注入/凭据/双击新建检查保持通过。模型流程 console.error/pageerror 为 0；生命周期流程 pageerror 为 0。Browser plugin not available，使用现有 Playwright Electron；无需新增浏览器依赖。

截图位于仓库外 `C:\Users\DC\AppData\Local\Temp\agentgit-lifecycle-qa-XX6LnC`，原始证据采用独立名称保存，避免后续 JUnit 默认输出覆盖基线：

| 文件（.agentgit/evidence） | SHA256 |
|---|---|
| S6-portable-acceptance.xml | `9592ae90d0ce401e170bbd39c47870958b1835e3889a814a387b8d24f91eeca4` |
| S6-portable-acceptance-audit.json | `2ee2cd1fa44762d189d36cb1a321c23a6d1f4b340a182d033ef1404ef84b9c43` |
| S6-portable-acceptance-model-ui.json | `8bb97c155ad2018056b3e97162507e079329d9f00aa27d7c657900773eed7297` |
| S6-dpi-four-pages.xml | `3e3317b4a0a53528363a8cfa13a8ad924017bbc8b6352907b3543ee63093d5b9` |
| S6-dpi-four-pages-qa.json | `0d920c3f3acd0ded4c8c67a629d0e47df3bf6a07a44f0f07e7bfc274122d666f` |
| S6-approval-boundary.xml | `37cf02b1d6afc17d8906139b51d3091be8348d71285a6f3e319535077b694ca4` |
| S6-model-portable-audit.json | `b245b4387f1a0ceff0d07dd7eb42a5cf73d827aaeac7db3ae5e8b887b68f1252` |
| S6-model-package-inventory.json | `8e652c1033dd0fdd60ffd018bc5ae16607c0ce518e70ecaa69478684f47214b8` |
| S6-native-save-attempt.xml | `4736611ab05de98b092770cb8e95369765decdd24952ca471ca1642c376b4806` |

## 实际未通过与剩余门槛

曾设置 `AGENTGIT_NATIVE_SAVE=1`，在上述真实五工具流程末尾成功打开 Windows 原生“保存所选版本的产物”。Computer Use 的 set_value 报不可设置、索引点击报点落在模态子窗口；激活/刷新后坐标点击和快捷键的焦点信息仍不可靠。没有输入文件路径或产生导出文件，最后 Escape 正常取消；测试因此以 cancelled=true 失败。此失败单列为 S6-native-save-attempt.xml（另一条模型测试通过），不混进通过计数，也不把成功显示/取消当作成功保存。默认路径替身复测随后完整通过。

第二次原生复验返回黑色截图，重新获取窗口后两次报 `failed to activate captured window`，因此停止 UI 输入；核对本次 Playwright 启动的进程身份后清理其主进程，未停止外部 API/Worker。测试以 tracing.stop 的 Target closed 失败，记录为 S6-native-save-retry.xml，SHA256 `b90a8c2aff891c64bc5b3af0bc69b5262aac41bb55664a8d436e2bc317928419`。随后确认桌面测试 Schema、测试进程和受管容器均无残留。此过程另发现测试 finally 中 trace 导出错误会跳过后续清理，已改为嵌套 finally，确保浏览器关闭和独立后端停止都会执行；产品包未变。

仍需实际原生保存成功和取消无写入的完整证据、独立 Windows x64 用户的安装/运行/同数据覆盖升级/卸载、该环境无源码/前端 Node 且禁止外网的安装包与 ZIP 流程。现进程未提升管理员权限，也没有额外用户会话/可用测试 VM。具体输入和步骤已准备在 [独立用户验收清单](../S6-windows-acceptance.md)，不会用当前 profile 隔离冒充另一个 Windows 用户。

S6-T06 未明确开启真实计费冒烟且账户/预算条件尚未确认，继续单列未运行。S6 保持未验收，不合并到 main，不提前进入 S7 或删除 S11/S12 主链。未重新宣称全后端回归或独立安装已完成。

清理路径修正后仅复验相关流程：类型检查通过，Electron 边界 1 passed（9.1s）、生命周期/四页矩阵 1 passed（31.2s）。没有产品代码或包内容变化；前述五工具/模型完整包记录继续对应同一 b649bf3 产品基线。

| 文件（.agentgit/evidence） | SHA256 |
|---|---|
| S6-cleanup-boundary.xml | `22534a5b33d09a1e5a3f5f7b296a75e53ac751a5cd637ad3cd3b3a693c3d2eda` |
| S6-cleanup-lifecycle.xml | `e95db5601c9c7d048bd75897ae205bfe8524269026ef412203d051e5be602a6b` |
