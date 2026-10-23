# AgentGit 桌面开发

当前为 S6 实施版。四页与受限 IPC 已实现，真实 Electron 的 fixture 边界测试及真实后端五工具/Fork/Recorded/Live/Diff 联调通过；模型 Adapter、完整窗口/DPI/生命周期、原生对话框操作、安装包/便携包及独立 Windows 用户验收尚未完成。阶段退出条件仍以根项目实施方案为准。

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

构建交付命令（尚未验收输出）：

```powershell
npm run package
npm run make
```

输出预期在 `out/`，包含 Windows x64 Squirrel 安装程序与 ZIP。完成后仍须记录摘要、依赖、签名及独立用户安装/升级/卸载证据，不能把上述命令存在当作验收通过。

## 连接已有后端

按 [后端开发说明](../doc/DEVELOPMENT.md) 准备数据库/迁移/固定镜像，在独立终端启动 API 与 Worker。桌面与 API 使用同一操作者预置的 `AGENTGIT_API_TOKEN`；通过环境传递，不放入启动参数、文档或 Git。

```powershell
# 本终端须已配置与 API 相同的 AGENTGIT_API_TOKEN。
$env:AGENTGIT_API_URL = 'http://127.0.0.1:8000'
npm start
```

默认地址为 `http://127.0.0.1:8000`，也可在窗口内显式改为其他回环 HTTP 根端点。首次选择已有项目 UUID，或受控创建项目。API 存活、数据库、身份、能力版本分别检查，Worker/容器保持未知，任务实际结果才提供执行证据。无令牌时可以查看公开探测状态，不能调用业务接口。

主进程读取令牌后在创建渲染子进程前删除环境副本，只在 API 客户端闭包保留；不下发 IPC、不落前端存储或偏好。renderer 无 Node/fs/Shell/Docker 接口、任意 URL 代理或原始 ipcRenderer。项目由主进程固定加头，后端继续强制归属检查。

用户数据目录只保存 `preferences.json` 的连接地址、项目 ID、窗口位置/尺寸。`AGENTGIT_DESKTOP_PROFILE_DIR` 仅是主进程的显式测试目录覆盖；没有界面传路径接口。退出取消 GET 展示请求，不取消正在提交的 POST，不杀外部服务；重开只读取，不自动重发修改。

## 资源与验证边界

本地 Inter、EB Garamond、JetBrains Mono 随资源构建；中文使用 Windows 字体回退。OFL 与固定上游提交/文件摘要在 `src/renderer/public/licenses/`，构建后复制进 `.build/renderer/licenses/`。没有 CDN 或运行期字体下载。

renderer 构建前会从已锁定且已安装的声明依赖生成 26 项授权原文/清单，不联网、不执行依赖代码；包括声明依赖闭包中的类型包，不声称这些包全部进入运行时 JS。连接面板的“版本与开源授权”按需加载包内原文。Electron/Chromium 的授权另由程序目录的 LICENSE 与 LICENSES.chromium.html 随包提供。

`node scripts/audit-package.cjs out/AgentGit-win32-x64 <输出 JSON 路径>` 只读核验 app.asar：构建文件逐字节一致、字体/OFL 对应上游摘要、授权清单存在、开发源码/测试/配置未混入，并记录生产依赖与归档摘要。工具来自已锁定 Forge 依赖，不额外安装审计框架。

`npm test` 检查主进程客户端、参数/项目/来源验证、二进制导出与偏好；`test:electron` 启动真实 Electron，使用受控 HTTP fixture 检查四页、渲染隔离、注入文本、拒绝导航/额外窗口 IPC、双击提交和窗口宽度。fixture 不冒充真实后端执行，详见 [初版证据](../doc/acceptance/S6-desktop-progress.md)。

`test:electron:real` 另外要求已准备 PostgreSQL 测试库、Docker Linux 与固定镜像。它使用独立测试 Schema 和独立 Worker，真实执行五工具、分支/Replay/Diff，并核验来源不变、容器清理和原字节导出。缺设施即失败，不 skip；产品 Python 源码在测试期间应冻结。详见 [真实联调](../doc/acceptance/S6-desktop-real.md)。保存测试替换原生对话框返回值，不算原生对话框交互已验收。

当前 Vite 构建提示 React Flow 的 `use client` directive；应用完全是 Electron 客户端，不使用 RSC/SSR，真实渲染已验证，保留该提示供后续升级核对。官方参考：[Electron 安全](https://www.electronjs.org/docs/latest/tutorial/security)、[Forge Windows maker](https://www.electronforge.io/config/makers/squirrel.windows)、[Playwright Electron](https://playwright.dev/docs/api/class-electron)。
