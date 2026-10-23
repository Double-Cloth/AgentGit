# S6 关系图键盘修正、0.1.3 候选与退出核对

当前源基线 ed5f0fb，分支 codex/s6-desktop。状态：本机可执行的本轮验证通过，S6 / D1 仍未最终验收；原生保存及独立 Windows 环境的证据未齐，不进入 S7 或合并 main。D1 操作交付见[桌面离线指南](../D1-desktop-demo.md)。设计/实施方案与 DESIGN 摘要均未变化。

## 实际缺陷与修正

逐项核对 S6-T04 时新增两个检查点的回归：Tab 可以聚焦父节点，但 Enter 后详情仍显示 HEAD。原先只接 onNodeClick；React Flow 的键盘选择走节点选择变更，不触发这个鼠标回调。改为处理 select=true 的 onNodesChange，保持外部所选 Commit 为唯一来源，鼠标和键盘走同一更新，不新增状态库或局部副本。

同时发现后加载的 React Flow CSS 覆盖了普通焦点规则。新增更明确的图节点 focus-visible 选择器，复用 DESIGN 的 ink、2px solid、2px offset；未引入新颜色。真实 Electron 校验 Tab / Enter / 空格、详情 Commit 及实际状态字节、鼠标回选和焦点计算样式。真实后端生命周期测试在 100% / 150% / 200% Chromium 缩放下，也逐一用键盘选择最早节点再回到 HEAD。

默认边界测试支持显式打包程序路径，本轮两条边界测试直接运行新 ZIP，覆盖注入、sender/frame、凭据、导航、双击和图选择。四个 Playwright 配置各用独立 test-results 子目录，修正并发运行时互相清理轨迹的测试设施问题；这些目录不进入分发包。安装测试在覆盖前保存自有项目/路径证据，便于失败时核对归属。

## 当前 0.1.3 候选

桌面 patch 版本为 0.1.3，后端协议仍为 0.1.0 / Schema 0012_replay_source_index；依赖、Python 产品代码、固定镜像和执行策略未改变。Node 24.19.0 / npm 12.1.0 / Electron 45.0.0 / Forge 8.0.1 执行 npm run make，Setup 与主程序均 NotSigned，未发布或推送。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156108800 | `25ad496f343c023d16e7632f02aa867eea360cc8db0e021f53671c9f57bfc435` |
| out/make/squirrel.windows/x64/AgentGit-0.1.3-full.nupkg | 155351715 | `46db50e1847c34c89a4667b146c6744d26f567622b73fdbac6e52a99843adffa` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `998fe8da943422f3218cdaa6caf61734584549864dcba8b2a8dcc935e5f07233` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.3.zip | 159328181 | `14c4a162d16bed85213990fa4b4ba6c41a35fa3809e992154a48a261f2a12371` |

ASAR 90 项 / 27 个构建文件，SHA256 为 `87fbbdcca104bf695b59f53b8fc8ec14677c714da12fd986830eca2aac443d67`。三套字体/OFL、26 项声明依赖授权、生产包 electron-squirrel-startup 1.0.1 及根白名单通过审计。ZIP 解压到 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-0.1.3-gc2t3qqb`，73 文件、376741217 bytes 与打包目录逐字节相同；Nupkg 内 ASAR 也一致。

| 本轮检查 | 最终结果 |
|---|---|
| 类型 / 单元 / 构建 | 严格类型检查、20 项 Node 单元和 make 通过；没有将这些结果写作全部后端回归通过 |
| 新 ZIP 边界 | 2 passed，16.7s，含本轮键盘回归与既有四页/IPC/凭据检查 |
| 新 ZIP 真实后端全组 | 4 passed，约 2.6 分钟：环境异常 41.7s、生命周期/48 组缩放/键盘 37.0s、五工具/Fork/Recorded/Live/Diff/导出 47.8s、模型预算与回放 28.8s |
| 新 Setup 生命周期 | 干净测试基线 0.1.0→0.1.3、卸载、重装查询与新任务、最终卸载，1 passed，42.8s；原来源、数据库/CAS 和独立 Worker 保留检查通过 |

Browser plugin not available，使用项目 Playwright Electron，无 Vite。页面 URL / 标题、非空界面、无框架错误遮罩、控制台错误为空、键盘/鼠标交互和计算样式均核验。图焦点截图在仓库外 `C:\Users\DC\AppData\Local\Temp\agentgit-electron-wRGz1X\graph-keyboard.png`，已实际查看。360px 是补充渲染测试；设备缩放不等于修改 Windows 原生显示设置。

首次全组的 Fork 失败来自本轮新增测试重复使用既有 desktop-fork 分支名；移除重复步骤后，原有纯 Fork 流程和最终全组通过。首次安装回归的卸载退出 0，但主程序等四个文件残留；核对本次项目 UUID、物理安装根和无活动应用后，再次实际卸载清理了主程序，随后因注册表项已不存在报错。该次清理不计通过；偏好按归属归档后重建 0.1.0 基线，完整重跑才得到上述 42.8s 的通过记录。未手工删除主程序或放宽卸载断言来通过检查；MSIX 特有行为仍须在独立环境复核。

## S6-T01–T12 证据与边界

“本机通过”只指所列证据范围，不能替代明确要求的独立用户/离线/原生交互条件。

| 要求 | 当前证据 | 尚缺或限制 |
|---|---|---|
| T01 安装后完整闭环与原生导出 | 新 ZIP 的五工具、历史、纯 Fork、Recorded/Live、Diff 和受控写入通过；新 Setup 已升级/重装并查询和执行任务 | 原生保存成功/取消无写入未通过；独立安装包全流程待执行 |
| T02 防重复、断连恢复、重开终态 | 新 ZIP 边界双击提交/审批只发一次；真实后端 503 恢复、关闭/重开查询与终态一致 | 503 为测试门控，未冒充宿主 API 进程停止 |
| T03 宽度、DPI、长内容 | 四页 48 组 Chromium 宽度/缩放、长中文/JSON、360px 补充无意外溢出 | 独立 Windows 原生显示环境交叉验证待执行 |
| T04 全键盘、焦点与文字状态 | 导航/详情、节点 Enter/空格与鼠标、正确历史状态、ink 焦点及文字状态通过 | 原生保存框交互另计 T01 |
| T05 无模型 Key 且禁止外网 | Mock、固定离线语料、无容器网络与确定性供应商替身已验证；桌面不继承供应商 Key | 当前没有整机禁止外网的独立 Windows 证据，不能声称该环境下全部确定性测试已运行 |
| T06 显式真实模型冒烟 | Adapter/Worker/计量协议的确定性测试已运行 | 用户未明确开启真实供应商调用，单列未运行，不隐含真实费用授权 |
| T07 五工具、执行详情、Fork/Recorded | 新 ZIP 实际 Python/Shell、产物答案和摘要、Recorded 零实际调用/无新增容器、A/B Live/Diff 通过 | 全局离线条件同 T05；原生保存同 T01 |
| T08 渲染隔离、IPC、注入与导航 | 20 项单元及新 ZIP 边界实际验证，无 Node/raw IPC，伪造来源/字段拒绝，脚本只作文本，导航/新窗受限 | 测试受控主进程可构造攻击窗口，不是产品开放宿主能力 |
| T09 身份/项目/URL/凭据边界 | 客户端/路由单元、只读 API 历史集成、新 ZIP 边界及 ASAR 白名单；身份不落 renderer/偏好/包 | 仅声称已覆盖测试的边界，不以无实际凭据的包扫描替代所有运行期校验 |
| T10 独立 Windows x64 环境 | Setup/ZIP 构建、摘要/未签名状态、包内字体资源和本机流程齐备 | 用户明确环境稍后提供；无源码/Node、禁止外网、独立用户的两个包仍待实测 |
| T11 不可用环境提示 | API 未监听、数据库 503、401/版本、Worker 实际停止、私有 Docker 不可达、真实镜像查询 404 传递与提示通过 | 镜像通过测试接缝映射到不存在摘要，未删除共享固定镜像；独立真实缺依赖交叉复核待执行 |
| T12 生命周期、升级和卸载保留业务数据 | 单实例/渲染崩溃/关窗后后端继续、同数据版本 0.1.0→0.1.3 升级/卸载/重装的最终回归通过 | 当前为 Codex MSIX 重定向用户，不替代独立 Windows 用户；首次残留失败已单列 |

本轮再次尝试 0.1.2 原生短流程时，进程存活但窗口捕获为黑屏、accessibility=null；重新选择唯一窗口并重试激活仍返回 `failed to activate captured window`。按 Computer Use 的恢复规则停止继续输入，经 PID/父进程/程序路径核对后只终止本测试应用；测试以 Target closed 失败结束，输出文件不存在，未保存为通过。0.1.3 没有新的原生保存成功证据，不重复启动同一无可用控制条件的测试来制造进度。

## 固定证据

以下位于忽略的 .agentgit/evidence；原生失败和卸载失败清理与最终通过文件分开保存。

| 文件 | SHA256 |
|---|---|
| S6-package-013-inventory.json | `b2fb1f548a3b3ec78b12ee6e0281104236047551834ccc9fb0f1947302285b3b` |
| S6-package-013-audit.json | `5c47f8980817592d7619d2ab42bad55c119b5acba8e8918d4aae6465f6cd196b` |
| S6-package-013-boundary.xml | `29efe43e091d36d1cdc4a19c7bd52f50fdf5569df5072db8b18690acec92d0ad` |
| S6-keyboard-013-qa.json | `99a8dc91b95d5fc1e9f9043de7d0a91d3dd952018b5c016005ba352b5f51c892` |
| S6-portable-013-real.xml | `cd3921ee8c31200c5d99cd271f8b11ca89bc5df805a17cd1049a32a2deca1ea8` |
| S6-portable-013-real-audit.json | `b63913bceb4b198c08226f9262155a3f7558d4b82c88eeeae25ee213dadaa90f` |
| S6-lifecycle-013-qa.json | `2f426b06bd8431b62f039624451efd9cbfcdd7388d5b6dde0125d2f842356aea` |
| S6-environment-013-qa.json | `dd780a24460898b4a2e1ef81ebb8b99ce1a6fc091ec4f427f43c39d7e709fa78` |
| S6-model-013-qa.json | `ff1c62d15290a71380e645ce999f4a8215f31f0c424a162fe1f307db6f258bca` |
| S6-install-013.xml | `3111007cb770849c7f5249617dd813403bc3e546fcdb65ce3d7d12f5ce875487` |
| S6-install-013-qa.json | `3e6609b18ef1b976c2dc0dda6f95f2979607329beac687f4562257dec7935165` |
| S6-keyboard-graph-before.xml | `06641f6659b38a36ffc8749028a387ac47dda8be5983ca487b29f1f2a363c302` |
| S6-keyboard-graph-focus-before.xml | `7e8adaabc90c4296e2036be2db04cf517f6909e4b2b9797fdbdfc24efa8a53e7` |
| S6-native-012-control-failure.json | `25693befdded254c964cf00baf7125daf150c795fdb6af536b21238fc957d072` |
| S6-native-012-failed.xml | `305c5f304ad2eb3a5cb6cff47db80e43f7509a54dcbfe31c4148cecf62ae3636` |
| S6-install-013-cleanup.json | `29f1259af08cf2532fbcf1843cf04de767285bb9cea559520ae0cbbb4c002b72` |

完整目标仍包含 S7–S11 与 S12-A/B/C/D。S6 前置条件未满足时不能以文档或其他测试替代验收继续下一阶段；当前下一步需要原生窗口控制恢复或人工真实保存证据，以及用户稍后提供的独立 Windows 环境。
