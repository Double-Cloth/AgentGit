# AgentGit 桌面开发

当前为 S6 实施版，候选版本 0.1.3。四页、受限 IPC、模型选择/计量、真实五工具/Fork/Recorded/Live/Diff 和当前用户安装生命周期已验证；关系图键盘选择已修复。原生保存和独立 Windows 环境仍待验收，阶段退出条件不变。操作步骤见 [D1 桌面离线指南](../doc/D1-desktop-demo.md)，逐项证据见[当前验收记录](../doc/acceptance/S6-keyboard-and-gates.md)。

## 固定构建环境

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

输出在 `out/`，包含 Windows x64 Squirrel 安装程序与 ZIP。当前版本 0.1.3，摘要、键盘修正和完整证据矩阵见[当前候选](../doc/acceptance/S6-keyboard-and-gates.md)。[0.1.2 环境异常](../doc/acceptance/S6-execution-environment.md)、[0.1.1 分发修正](../doc/acceptance/S6-package-hygiene.md)及更早安装记录保留历史事实。独立用户安装/升级/卸载尚待完成，不把 make 成功等同于完成 S6。

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

`test:electron:real` 另外要求已准备 PostgreSQL 测试库、Docker Linux 与固定镜像。它使用独立测试 Schema 和独立 Worker，真实执行五工具、分支/Replay/Diff，并核验来源不变、容器清理和原字节导出。缺设施即失败，不 skip；产品 Python 源码在测试期间应冻结。详见 [真实联调](../doc/acceptance/S6-desktop-real.md)。默认保存测试替换原生对话框返回值，不算原生对话框交互已验收。生命周期用例通过独立 Worker 门控证明关窗后任务继续，检查重开/单实例/渲染崩溃与 503 恢复不新增 POST，并以 Chromium device scale factor 1/1.5/2 验证四页、图节点键盘选择和长内容；不是修改 Windows 显示设置。四个配置分别使用 test-results 的 boundary/real/install/native 子目录，避免跨配置并发清理轨迹；同一配置的运行仍应串行。

复验打包/解压程序时先正常构建，然后设置 `AGENTGIT_DESKTOP_EXECUTABLE` 为该目录的 AgentGit.exe 绝对路径，运行 `npm exec playwright test -- --config playwright.real.config.ts`。测试会将应用工作目录放到新临时目录，清除应用子进程的构建 PATH / NODE_PATH / NODE_OPTIONS，并确认 app.isPackaged；不据此声称已切换 Windows 账户。

当前 Vite 构建提示 React Flow 的 `use client` directive；应用完全是 Electron 客户端，不使用 RSC/SSR，真实渲染已验证，保留该提示供后续升级核对。官方参考：[Electron 安全](https://www.electronjs.org/docs/latest/tutorial/security)、[Forge Windows maker](https://www.electronforge.io/config/makers/squirrel.windows)、[Playwright Electron](https://playwright.dev/docs/api/class-electron)。

如需实际原生保存复验，显式设置 `AGENTGIT_NATIVE_SAVE=1` 并只运行 `--grep 真实设施`。测试写入 `.agentgit/evidence/S6-native-save-request.json`，由操作者在真实保存框选择其中的新临时路径；取消或内容不符会失败。无人值守默认流程不设置该开关。独立 Windows 用户的安装/升级/卸载步骤见 [待验收清单](../doc/S6-windows-acceptance.md)。

也可从 desktop 使用 `npm exec playwright test -- --config playwright.native.config.ts` 单独复验原生保存；仍须 AGENTGIT_NATIVE_SAVE=1 和 AGENTGIT_DESKTOP_EXECUTABLE 的打包程序绝对路径。它用只读 HTTP fixture，不要求 PostgreSQL/Worker/Docker；在真实保存框选择 .agentgit/evidence/S6-native-short-request.json 的 output 路径，随后在第二个保存框取消。每步最多五分钟，最终核对原字节和无额外文件。本机自动控制原生窗口尚失败，该入口不代表已验收，也不进入默认测试或安装包。

安装回归单独使用 `npm run test:electron:install`，不会混入默认 `npm test` / `test:electron:real`。它真实覆盖安装目录并卸载，**只能在本次测试拥有、已准备 0.1.0 基线且没有已有 preferences.json 的环境**运行。必须显式提供 `AGENTGIT_INSTALL_TEST=1`、`AGENTGIT_INSTALL_SETUP` 的绝对路径与其已核验的 `AGENTGIT_INSTALL_SETUP_SHA256`。存在 MSIX 重定向时，还必须明确填写已经检查的 `AGENTGIT_INSTALL_PHYSICAL_ROOT`；脚本固定比较 realpath，不自动接受重定向。不能把当前 MSIX 上下文当作独立 Windows 用户或无构建 Node 的环境。

桌面版本号由 Vite 在构建时读取 package.json，页脚与授权面板和 Electron app.getVersion 一致；后端能力协议仍固定 0.1.0，不随桌面 patch 版本隐式升级。
