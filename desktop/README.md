# AgentGit 桌面开发

当前候选版本为 [Windows 0.1.7](../doc/acceptance/S11-package-017.md)，包含既有四页、S7/S8、Repair Lab 和 Merge Review；包内容审计、40 单元及 20 个不同打包 Electron 测试节点通过。23 次尝试保留 3 次失败和后续复验，合并/显式新任务、联合修复、候选交接、取消/未知及观测禁采纳均有真实设施证据。原生保存和独立 Windows 环境仍待验收，S6/D1 阶段退出条件不变。既有安装生命周期证据对应历史候选，不能替代新包独立安装验收。操作步骤见 [D1 桌面离线指南](../doc/D1-desktop-demo.md)，剩余门槛见 [S6 清单](../doc/S6-windows-acceptance.md)。

S7 已接通验收器、实验/干预及实验列表共八个固定 IPC，以及来源冻结、A/B 表单、manifest、精确统计、trial、控制和重开查询。新 ZIP 的真实独立 API/Worker 流程验证 Live 9 通过/1 取消、Hybrid 各 5 全通过，recorded/synthetic 来源可区分。[S7 操作步骤](../doc/S7-desktop-demo.md)、[桌面记录](../doc/acceptance/S7-desktop.md)和[退出核对](../doc/acceptance/S7-gates.md)分别说明使用与证据范围。0.1.3 与 0.1.4 的分发文件均按摘要独立归档，不静默替换历史候选。

## 固定构建环境

S10 使用[原子准备](../doc/acceptance/S10-prepared.md)发布搜索与采样条件，加上只读 workflow 共 15 项固定 IPC；已进入 0.1.6。可运行 `npx.cmd playwright test --config playwright.repair.config.ts` 核对完整专项；其中桥接和页面 fixture 不单独证明真实设施闭环，真实部分分别保存原始 CAS。后续见[桌面计划](../doc/S10-desktop-plan.md)。

随后已接入[Repair Lab 第五页与只读 workflow](../doc/acceptance/S10-lab.md)，页面专项包含原键、票据/版本、观测禁采纳、断连与重开流程，仍使用明确 HTTP fixture。真实 PostgreSQL/API/Worker 的同次桌面链路和新包待补。旧四页配置现也接受 `AGENTGIT_ELECTRON_EVIDENCE_PREFIX`；复验时设置独立前缀以免覆盖默认报告。

Windows x64，Node.js **24.19.0**，npm **12.1.0**。使用 `package-lock.json`，不隐式升级依赖。

| 组件 | 固定版本 |
|---|---|
| Electron | 45.0.0 |
| Electron Forge / Windows makers | 8.0.1 |
| React / React DOM | 19.3.1 |
| TypeScript | 7.0.2 |
| Vite | 8.3.5 |
| React Flow | 12.12.0 |
| Playwright | 1.64.1 |

从 `desktop/` 执行：

```powershell
npm ci
npm run setup
npm run typecheck
npm test
npm run test:electron
npm run test:electron:real
npm start
```

`setup` 使用已锁定 Electron 的安装程序和内置校验清单准备构建机二进制，不是应用运行时下载。npm 12 的 `allowScripts` 只批准固定 `electron-winstaller@5.4.4` 的本地 vendor 选择脚本；不全局允许依赖脚本。实际已检查该脚本只选择 vendor 的 x64 7-Zip 文件。

`start` / `dev` 编译后启动真实 Electron，加载 `agentgit://app/index.html` 包内资源，不启动 Vite 开发服务器。renderer 由 Vite 直接构建，main/preload 由 tsc 构建，Forge 负责打包；未使用实验性 Forge Vite plugin。

构建候选命令：

```powershell
npm run package
npm run make
```

输出在 `out/`，包含 Windows x64 Squirrel 安装程序与 ZIP。当前版本为 [0.1.6](../doc/acceptance/S10-package-016.md)。[0.1.3 键盘/逐项核对](../doc/acceptance/S6-keyboard-and-gates.md)、[0.1.2 环境异常](../doc/acceptance/S6-execution-environment.md)、[0.1.1 分发修正](../doc/acceptance/S6-package-hygiene.md)及更早安装记录保留历史事实。独立用户安装/升级/卸载尚待完成，不把 make 成功等同于完成 S6。

## 连接已有后端

按 [后端开发说明](../doc/DEVELOPMENT.md) 准备数据库/迁移/固定镜像，在独立终端启动 API 与 Worker。桌面与 API 使用同一操作者预置的 `AGENTGIT_API_TOKEN`；通过环境传递，不放入启动参数、文档或 Git。

```powershell
# 本终端须已配置与 API 相同的 AGENTGIT_API_TOKEN。
$env:AGENTGIT_API_URL = 'http://127.0.0.1:8000'
npm start
```

默认地址为 `http://127.0.0.1:8000`，也可在窗口内显式改为其他回环 HTTP 根端点。首次选择已有项目 UUID，或受控创建项目。API 存活、数据库、身份、能力版本分别检查，Worker/容器保持未知，任务实际结果才提供执行证据。无令牌时可以查看公开探测状态，不能调用业务接口。

任务排队时提示尚无执行进展，持续等待可按[后端开发说明](../doc/DEVELOPMENT.md)检查 Worker。执行详情收到 sandbox_unavailable 时提示运行 `agentgit doctor --sandbox`，核验 Docker Linux 和固定镜像；该通用错误不能断定具体缺少哪项，也不自动修复环境。环境异常回归单独选择 real 配置的 `--grep 执行环境异常`；只停止自己的测试 Worker、使用私有 Docker context 和镜像查询故障注入，不停止外部 Docker 或删除固定镜像。

主进程读取令牌后在创建渲染子进程前删除环境副本，只在 API 客户端闭包保留；不下发 IPC、不落前端存储或偏好。renderer 无 Node/fs/Shell/Docker 接口、任意 URL 代理或原始 ipcRenderer。项目由主进程固定加头，后端继续强制归属检查。桌面也移除继承的 OPENAI_API_KEY；供应商密钥只在独立 Worker 预置。Runs 选择外部模型和 Live 继承/切换外部模型时必须另填 USD 上限，见 [模型与费用](../doc/MODEL_ADAPTER.md)。

用户数据目录只保存 `preferences.json` 的连接地址、项目 ID、窗口位置/尺寸。`AGENTGIT_DESKTOP_PROFILE_DIR` 仅是主进程的显式测试目录覆盖；没有界面传路径接口。退出取消 GET 展示请求，不取消正在提交的 POST，不杀外部服务；重开只读取，不自动重发修改。

## 资源与验证边界

本地 Inter、EB Garamond、JetBrains Mono 随资源构建；中文使用 Windows 字体回退。OFL 与固定上游提交/文件摘要在 `src/renderer/public/licenses/`，构建后复制进 `.build/renderer/licenses/`。没有 CDN 或运行期字体下载。

renderer 构建前会从已锁定且已安装的声明依赖生成 26 项授权原文/清单，不联网、不执行依赖代码；包括声明依赖闭包中的类型包，不声称这些包全部进入运行时 JS。连接面板的“版本与开源授权”按需加载包内原文。Electron/Chromium 的授权另由程序目录的 LICENSE 与 LICENSES.chromium.html 随包提供。

`node scripts/audit-package.cjs out/AgentGit-win32-x64 <输出 JSON 路径>` 只读核验 app.asar：构建文件逐字节一致、字体/OFL 对应上游摘要、授权清单存在、开发源码/测试/配置未混入，并记录生产依赖与归档摘要。工具来自已锁定 Forge 依赖，不额外安装审计框架。

打包和审计仅接受 .build、package.json、node_modules 三个根入口；新增配置、测试失败轨迹和其他根文件一律不分发。审计实际拒绝旧候选，并验证新候选的 ZIP / Nupkg 内容；不要只检查已知禁止文件名。

`npm test` 检查主进程客户端、参数/项目/来源验证、二进制导出与偏好；`test:electron` 启动真实 Electron，使用受控 HTTP fixture 检查四页、渲染隔离、注入文本、拒绝导航/额外窗口 IPC、双击提交和窗口宽度。fixture 不冒充真实后端执行，详见 [初版证据](../doc/acceptance/S6-desktop-progress.md)。

后续边界复验可设置 `AGENTGIT_ELECTRON_EVIDENCE_PREFIX`（1–80 位字母、数字、下划线或连字符），例如 `S7-statistics`，将本轮截图/观测写入独立前缀，保留旧 S6 证据。该变量只控制测试产物名，不进入应用参数或安装包。

`test:electron:real` 另外要求已准备 PostgreSQL 测试库、Docker Linux 与固定镜像。它使用独立测试 Schema 和独立 Worker，真实执行五工具、分支/Replay/Diff，并核验来源不变、容器清理和原字节导出。缺设施即失败，不 skip；产品 Python 源码在测试期间应冻结。详见 [真实联调](../doc/acceptance/S6-desktop-real.md)。默认保存测试替换原生对话框返回值，不算原生对话框交互已验收。生命周期用例通过独立 Worker 门控证明关窗后任务继续，检查重开/单实例/渲染崩溃与 503 恢复不新增 POST，并以 Chromium device scale factor 1/1.5/2 验证四页、图节点键盘选择和长内容；不是修改 Windows 显示设置。四个配置分别使用 test-results 的 boundary/real/install/native 子目录，避免跨配置并发清理轨迹；同一配置的运行仍应串行。

复验打包/解压程序时先正常构建，然后设置 `AGENTGIT_DESKTOP_EXECUTABLE` 为该目录的 AgentGit.exe 绝对路径，运行 `npm exec playwright test -- --config playwright.real.config.ts`。测试会将应用工作目录放到新临时目录，清除应用子进程的构建 PATH / NODE_PATH / NODE_OPTIONS，并确认 app.isPackaged；不据此声称已切换 Windows 账户。

当前 Vite 构建提示 React Flow 的 `use client` directive；应用完全是 Electron 客户端，不使用 RSC/SSR，真实渲染已验证，保留该提示供后续升级核对。官方参考：[Electron 安全](https://www.electronjs.org/docs/latest/tutorial/security)、[Forge Windows maker](https://www.electronforge.io/config/makers/squirrel.windows)、[Playwright Electron](https://playwright.dev/docs/api/class-electron)。

如需实际原生保存复验，显式设置 `AGENTGIT_NATIVE_SAVE=1` 并只运行 `--grep 真实设施`。测试写入 `.agentgit/evidence/S6-native-save-request.json`，由操作者在真实保存框选择其中的新临时路径；取消或内容不符会失败。无人值守默认流程不设置该开关。独立 Windows 用户的安装/升级/卸载步骤见 [待验收清单](../doc/S6-windows-acceptance.md)。

也可从 desktop 使用 `npm exec playwright test -- --config playwright.native.config.ts` 单独复验原生保存；仍须 AGENTGIT_NATIVE_SAVE=1 和 AGENTGIT_DESKTOP_EXECUTABLE 的打包程序绝对路径。它用只读 HTTP fixture，不要求 PostgreSQL/Worker/Docker；在真实保存框选择 .agentgit/evidence/S6-native-short-request.json 的 output 路径，随后在第二个保存框取消。每步最多五分钟，最终核对原字节和无额外文件。本机自动控制原生窗口尚失败，该入口不代表已验收，也不进入默认测试或安装包。

安装回归单独使用 `npm run test:electron:install`，不会混入默认 `npm test` / `test:electron:real`。它真实覆盖安装目录并卸载，**只能在本次测试拥有、已准备 0.1.0 基线且没有已有 preferences.json 的环境**运行。必须显式提供 `AGENTGIT_INSTALL_TEST=1`、`AGENTGIT_INSTALL_SETUP` 的绝对路径与其已核验的 `AGENTGIT_INSTALL_SETUP_SHA256`。存在 MSIX 重定向时，还必须明确填写已经检查的 `AGENTGIT_INSTALL_PHYSICAL_ROOT`；脚本固定比较 realpath，不自动接受重定向。不能把当前 MSIX 上下文当作独立 Windows 用户或无构建 Node 的环境。

桌面版本号由 Vite 在构建时读取 package.json，页脚与授权面板和 Electron app.getVersion 一致；后端能力协议仍固定 0.1.0，不随桌面 patch 版本隐式升级。

## S7 受控实验

Branch Experiment 的“受控 A/B 实验”先核对来源 Run 和安全起点，再显式冻结共同验收器。每组默认 5 次，可选完整 Prompt、完整计划或既有有序干预 ID；模型、权限、split、预算及验收规则不能按组修改。外部模型须输入每个 trial 的 USD 上限，金额按两组次数分别授权，仍由后端核对不扩大来源权限。

Hybrid 的只读上下文使用正整数事件 ID，返回调用 UUID、签名和前置条件；响应补丁只引用完整同参录制，并显式标为 test_only。先冻结补丁，再加入 B 组；不会提供应用正式配置动作。统计区分全部消耗与已验收完整试验，按有界 BigInt 显示分数、方差、Token 和微美元；未知费用不显示为零，近似数值带 `≈`。

提交和控制动作固定请求与幂等键。断连禁用操作并保留当前工作区；恢复连接后继续查询，未知提交结果只用原键重试。退出和重开不自动 POST，可从当前项目实验列表或 Experiment UUID 查询历史报告。不兼容验收器显示错误，不能据此重判为失败或零成功率。暂停和取消须等待 Worker 安全点落实。

源码本机专项命令（在 `desktop` 目录）：

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
npx.cmd playwright test --config playwright.experiments.config.ts
```

复验 ZIP 时，把 `AGENTGIT_DESKTOP_EXECUTABLE` 设为新解压目录下 AgentGit.exe 的绝对路径，并设独立的 `AGENTGIT_ELECTRON_EVIDENCE_PREFIX`（仅 1–80 个字母、数字、下划线或连字符）。打包模式验证真实程序路径、版本/页脚和仅 System32 的应用 PATH；省略 executable 则测试源码构建。实验配置和旧 real 流程均支持证据前缀，避免覆盖已归档的默认文件。每次测试使用独立临时用户数据目录，不用它冒充独立 Windows 账户。

该专项包含明确标注的 HTTP fixture 边界与独立 PostgreSQL/API/Worker、固定容器真实流程，不替代独立 Windows 安装、原生保存和 DPI 验收。真实联调须先具备仓库测试配置、PostgreSQL 和已冻结镜像，使用自己的测试 Schema 和 Worker；不会操作外部服务或调用付费模型。

## S8 故障报告与候选干预

“差异 → 故障定位”可创建/发现/读取冻结报告，查看并列候选、断言、依赖对齐、歧义及缺口。事件按钮按 Run/seq/数据库 ID 在 Timeline 精确核对；缺证据不跳到相邻事件。核对候选上下文后可显式保存单项 Prompt/计划干预，带入既有 A/B 表单；交接不启动 Job，仍须显式提交预算和试验。已有实验可在报告内核对效果，未完成时改善未知。

先 `npm run typecheck`、`npm run build`、`npm test`，再设置新的 `AGENTGIT_ELECTRON_EVIDENCE_PREFIX` 并运行 `npx playwright test --config playwright.diagnosis.config.ts`。两项分别为 HTTP 边界和真实独立 PostgreSQL/API/Worker 联调，使用免费 Mock；源码/uv.lock 在真实运行中须保持不变。详见 [S8 桌面工程验收](../doc/acceptance/S8-desktop.md)。已生成 [0.1.5 Windows 候选并完成包内复验](../doc/acceptance/S8-package-015.md)，标签人工审阅及独立 Windows/原生验收仍待补。

## S10 修复实验室

第五页支持显式准备搜索及预注册来源、缩减、锁后六次确认/四项回归、完整配置对照及精确审阅。批准只创建配置版本；观测替换仅允许保存实验。未知提交保持原键，重开通过只读查询发现既有流程。[同次真实 Electron/API/Worker/CAS 用例](../doc/acceptance/S10-lab-real.md)已验证联合 Prompt/Plan、字段缩减、版本竞争和批准；[0.1.6 包内复验](../doc/acceptance/S10-package-016.md)再次覆盖真实流程及其余边界。固定 Mock 不代表实际模型效果。

Repair Lab 需要已准备的 API、普通 Worker，以及分别启动的 repair-worker、repair-study-worker、repair-confirm-worker。它们使用同一数据库、CAS 和项目身份，控制器只编排、普通 Worker 执行；桌面不会自动启动或迁移后端，命令见 [API/CLI 说明](../doc/API.md)。

在 `desktop` 下设置唯一证据前缀后，运行 `npx.cmd playwright test --config playwright.repair.config.ts repair-real.spec.ts` 验证真实设施；只运行页面/桥接 fixture 时明确选择 `repair-bridge.spec.ts repair-lab.spec.ts`。两次须使用不同前缀，避免覆盖报告。真实设施需要仓库测试配置及 PostgreSQL，保持运行中的 Python 包、fixture、helper 和锁文件不变；仅创建、删除自己的测试 schema，模型免费且离线。

故障候选核对上下文后可显式前往 Repair Lab，预填原来源/起点、screen 验收器及请求事件；confirm/regression 拒绝，四项回归和执行授权仍需填写。[交接验收](../doc/acceptance/S10-candidate-handoff.md)以 `repair-handoff-real.spec.ts` 实测没有搜索或新 Job/Run；不是自动采用候选补丁。

`repair-boundaries-real.spec.ts` 在自己的测试 Worker 上验证[排队取消及确认结果未知](../doc/acceptance/S10-lab-boundaries.md)。未知试验保留预留，取消/重开不自动重发；页面显示核对要求、未知计量和未结算状态。该固定故障注入不调用供应商，独立于研究成功率评估。

`repair-observation-real.spec.ts` 验证[真实观测禁采纳](../doc/acceptance/S10-lab-observation.md)：显式准备完整录制和 Hybrid 范围后实际六加四通过，仍不能批准正式配置；API 拒绝绕过，页面只保存精确实验定义且不执行。所有 Repair Lab spec 均可按需单独选择、设置唯一证据前缀运行，真实部分需测试数据库和冻结 helper。

## S12 固定图展示与控制

时间线区分 Graph/MCP 的逻辑历史用量与本次实际尝试，显示固定工具、图版本/next、Fork 备注及 Recorded 来源。研究分支表单限制固定程序提交，图创建继续使用后端操作者入口。`playwright.langgraph.config.ts` 的六项真实流程覆盖 HTTP/批准 stdio 的暂停/恢复/取消、产物、Fork/Recorded 来源、窄窗与重开；准备、唯一证据前缀和运行冻结要求见 [固定图演示](../doc/S12-B-langgraph-demo.md#原生控制与桌面流程)。当前验证是开发版 Electron，历史 0.1.7 候选包保持原字节；最终四适配新包及正式原生/人工验收继续待补。
