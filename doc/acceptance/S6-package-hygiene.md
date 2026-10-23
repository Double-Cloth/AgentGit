# S6 分发内容修正与连接降级验收

后续候选已更新为[0.1.2 / 执行环境异常](S6-execution-environment.md)；本页 0.1.1 摘要和待补专项是前序记录，不能作为当前候选或当前完整进度。

状态：新候选的包内容、便携真实闭环及 MSIX 上下文安装生命周期通过；S6 未完成。2026-10-23 用户明确将独立 Windows 环境留待稍后，先继续其他验收。本次没有降低阶段门槛，未进入 S7、未合并 main。

## 发现及修正

检查新增验收配置时发现 Forge 的排除规则仅覆盖默认及 real 配置，install/native 配置可能进入下一次分发。进一步比较实际 ASAR，发现已有 0.1.1 候选还包含 README.md、forge.config.cjs 和 test-results/.last-run.json；原 audit-package 只检查部分禁止名称，未拒绝测试结果目录。因此此前“无测试/配置”的宽泛表述不成立，旧候选不再作为交付候选；原执行与安装结果仍是对应旧摘要的历史事实。

forge.config.cjs 改为根目录白名单，仅保留 .build、package.json、node_modules；audit-package 同时拒绝白名单以外的任何根内容。用加强后的审计检查旧便携 ASAR，实际退出 1，明确列出上述四项；新包通过。没有新增依赖、修改产品运行逻辑或改变后端/数据库/固定镜像。新旧 ASAR 中 27 个运行构建文件全部逐字节相同。

## 当前候选

在 `codex/s6-desktop`、前序提交 024638c 上完成本次打包规则修正后运行 `npm run make`。仍是尚未发布的桌面 0.1.1 / Windows x64，后端协议 0.1.0 / Schema 0012_replay_source_index；不是已发布版本的静默替换。今后验收应按本表摘要识别候选，不能仅按版本名认定为此前的包。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156108800 | `4cbb32f364f3260c02a4378003dd87476ebce3847ded3638aed42f87717b90e4` |
| out/make/squirrel.windows/x64/AgentGit-0.1.1-full.nupkg | 155351907 | `8a4c762a354331aa3e1d33b1d9124e4c6edbb13b73445d9433bc7cb85b4b6254` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `38af730e80fb34a0a9f2c9d243c8120fdb057792ccc14cfc2162999bee900d66` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.1.zip | 159327914 | `d5cb0fdd9ac8daca1dc49b7cbd4e2926b591f38f54a08d97e83b0e3774b3b63f` |

Setup 和主程序均 NotSigned。ASAR 为 90 项，摘要 `55c862a5fba4ee7fc731b545bbcbe22e3fbc20481ca51db99b9ae43f2cdf9eca`；27 个构建文件、三套本地字体/OFL、26 项声明依赖授权和唯一生产 Node 包 electron-squirrel-startup 1.0.1 均通过审计。Nupkg 内 ASAR 与打包目录逐字节相同；ZIP 解压后的 73 个文件、376740502 bytes 与打包目录逐字节相同。

新 ZIP 解压位置为 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-clean-s5zvtjxj`。旧候选保留的 test-results/.last-run.json 为测试状态文件；本次实际比较还发现尚未分发的中间打包目录会收进原生保存失败的 error-context.md，新白名单也已将其排除。

## 验证范围与结果

| 检查 | 实际结果与范围 |
|---|---|
| 类型与单元 | npm run typecheck 通过；npm test 20 passed |
| Electron 边界 | 默认配置 2 passed；新增连接降级和既有四页/IPC/注入/双击提交综合测试 |
| 新 ZIP 连接降级 | 1 passed，1.1s；实际未监听端口→数据库 HTTP 503→版本不兼容→401→恢复；错误态创建/选择按钮禁用，恢复后启用，只有 GET、无控制台错误 |
| 新 ZIP 真实后端 | 1 passed，48.6s；独立测试 Schema/Worker、真实五工具、28 检查点 Recorded、A/B Live、Diff/导出；Recorded 实际模型/工具调用均 0，来源不变、容器清理确认。导出仍替换保存框返回值 |
| 新 Setup 生命周期 | 1 passed，43.6s；任务自有 0.1.0 基线→0.1.1 覆盖升级→卸载→重装查询/新任务→再次卸载；数据库/CAS/来源/Worker 保留检查通过，最终程序已卸载 |

安装范围仍是当前 Codex MSIX LocalCache 重定向上下文，路径核验规则未放宽。恢复旧安装前先核对上次 QA 的偏好 repositoryId 和物理根，原字节归档后仅移除本测试自己的 preferences.json；恢复器原生路径推导出的目标必须等于固定安装根。没有修改用户其他数据或停止外部后端服务。

连接降级使用真实 Electron 加受控 HTTP 错误响应；只有 API 未监听是实际关闭测试监听器，不声称停止了宿主数据库。Worker/容器始终显示“未知 / 未探测”，不把 API 存活当作执行环境就绪。Worker 停止、Docker/固定镜像缺失的桌面专项仍需补充，不以这些连接 fixture 替代 S6-T11 全项。

Browser plugin not available，沿用项目 Playwright Electron 流程，无 Vite 开发服务器。检查 agentgit://app/index.html / AgentGit 标题、连接面板和业务界面非空、错误提示与控件状态、无框架错误遮罩及控制台错误；连接截图在仓库外 `C:\Users\DC\AppData\Local\Temp\agentgit-connection-qa-5QvNkM`，安装截图在 `C:\Users\DC\AppData\Local\Temp\agentgit-install-qa-hhxBD4`。没有调整 UI 设计或宣称本轮重做全部 DPI 矩阵；此前矩阵与生命周期证据对应的 27 个运行文件未改变。

## 原生保存仍未通过

新增单独的 playwright.native.config.ts / native-save.spec.ts，仅显式提供 AGENTGIT_NATIVE_SAVE=1 和已打包程序路径才运行。短流程用只读 HTTP fixture 提供中文原字节，经实际 Timeline 打开真实保存框；成功后才核对文件原字节，再打开一次检查取消不新增文件，不替换 showSaveDialog。每个交互最多等待五分钟，不进入默认测试或分发包。

本机最后一次尝试在保存阶段等待 600 秒后失败，原测试进程已结束且清理了自己的应用/监听器。Computer Use 有原生对话框可访问性节点，但截图与窗口状态不一致，激活/Raise 失败；未获得成功保存或取消无写入的完整证据。之后只完善入口、进程 PID 记录和超时上限，并通过类型检查，没有将未复验的交互写成通过。

## 固定证据

原始证据在忽略的 .agentgit/evidence；以下文件名与摘要固定，不使用后来覆盖的默认输出冒充本次结果。

| 文件 | SHA256 |
|---|---|
| S6-package-clean-audit.json / S6-clean-portable-audit.json | `286f67d1c49ae3af635a45823e0452e9984914aa4ac261f60f63ef4b14dc21cb` |
| S6-clean-package-inventory.json | `b9acee798b39872580c4270768367603d4b3839b5d64a74c446c231034a4936c` |
| S6-clean-runtime-comparison.json | `2414fbc20afa5c7c95b7e217bbf04d1f78e99192f860b1e5b079e0eb6b4a6f6e` |
| S6-old-package-rejected.json | `1bcdd4b3c66666d307f9612aeed55a89a24ed1028b63e9355c1c4c98fa5cfb89` |
| S6-clean-portable-real.xml | `8f7a0e3419abfbe23e1a30e7f0dc14cfa542676f47a3ccf501e15b7d6f22c5f1` |
| S6-clean-portable-real-audit.json | `b7b1a79c79c0beeb726acaa06da9af193c6fcb3a4d598812bf10777e1f216d14` |
| S6-clean-install.xml | `d5e69d5925e2bec5de297662044993a24a363fe52ab21139dad7d73d9eb21993` |
| S6-clean-install-qa.json | `f6a1ce9bcf92026d3b67280b8c1f6e1388629b972ac1a1fa278e1bd5e198258e` |
| S6-clean-connection.xml | `ed5ea8943f8753bf9ddc1ffec8faa934936e5f4a91bf54c9667a1b5b29b1707d` |
| S6-native-short-failed-600s.xml | `d9c5917044e20beb15a96619572f1ef4a7615e9b556e67dfd8a96c64aa463d2b` |

独立用户环境按用户指示稍后提供；原生保存、S6-T11 尚缺的专项及独立环境验收继续保留。S6-T06 没有明确开启，真实供应商冒烟仍单列未运行。本轮开始及结束核对设计/实施方案摘要均未变化。
