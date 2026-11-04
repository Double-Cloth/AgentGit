# S6 Windows x64 开发候选包

状态：**构建、包内容核验与当前用户的便携程序真实闭环通过；S6 / D1 未最终验收。** 安装程序尚未完成独立 Windows 用户的安装/覆盖升级/卸载，原生保存对话框仍待真实交互；真实模型 Adapter、完整 DPI/键盘/生命周期也未全部完成。不将开发候选当作正式发行或已签名软件。

## 构建与交付文件

应用源基线 `c41635b`，AgentGit 0.1.0；构建机 Node.js 24.19.0、npm 12.1.0、Electron 45.0.0、Forge 8.0.1。命令在 `desktop/`：`npm run make`。生成以下忽略目录中的本地候选，未推送或公开发布：

| 文件 | 大小（bytes） | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156105216 | `9929cfed8b2c37399647e15d986e91097b325f6416053f35d48bc411e0e0999f` |
| out/make/squirrel.windows/x64/AgentGit-0.1.0-full.nupkg | 155348965 | `236e63a31887ff8e262c7fe0c13f0730a3447a76577660d91663fe77fd8ed8bf` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `f2b09843bff4d6b6ecfb366410526258e8610e69acae8e772e2ac53b47c42b8f` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.0.zip | 159326206 | `84c68891c8a8d543b2ea279965c9fb929d85efd26fbf5e85823572d8f122eb1f` |

主程序 PE signature `50-45-00-00`、machine `0x8664`，FileVersion 0.1.0。主程序与 Setup 的 Authenticode 实测均为 **NotSigned**；没有签名证书或正式分发信任验证。Forge 构建还保留依赖中的 fs.rmdir 废弃提示，不以无提示作为通过依据。

## 内容、资源和依赖

`scripts/audit-package.cjs` 核验最终 app.asar：92 个归档项、25 个构建文件与本机构建逐字节相同，没有项目 src/tests/scripts、`.env`、运行数据或开发配置。3 套本地字体和 OFL 均与固定上游 SHA256 一致。app.asar SHA256 `d7ecf58b8a62eccbec0bc4ab0b07ba7cc97529f95e0e108a7e163dd6ed7931dd`。

Forge 已移除开发依赖和构建 scripts，主进程只保留 electron-squirrel-startup 1.0.1（Apache-2.0）。React、React DOM、React Flow 等打进渲染资源，26 项声明依赖闭包的完整授权/版本/原文摘要随包提供（包含类型包，不声称全部进入 JS）。Electron/Chromium 的 LICENSE、LICENSES.chromium.html 在程序目录。连接面板可按需查看包内授权，已通过实际 Electron 检查。

实际解压 ZIP 到新的测试目录，逐个比较它与 Forge 打包目录中的 **73 个文件**，全部字节一致（总计 376730829 bytes）；包括主程序、运行库、app.asar 与授权，而不只比较 ZIP 文件名。

## 脱离前端构建环境的真实运行

测试用 `AGENTGIT_DESKTOP_EXECUTABLE` 指向解压后的 AgentGit.exe，工作目录为新的独立临时目录，确认 app.isPackaged=true。应用子进程 PATH 仅保留 Windows System32，移除 NODE_PATH/NODE_OPTIONS，不包含构建用 Node/npm/uv；前端没有开发服务器。后端是另外准备的真实 API/Worker/PostgreSQL/固定 Docker 镜像。

最终含完整授权资源的 ZIP 程序综合测试 **1 passed，43.4s**（总计 43.8s）：

- 包内页面、字体与授权加载；四页核心流程，真实 Python/Shell 五工具任务完成。
- 容器外断言 3 框架、9 claims、6 来源，历史代码/日志与版本产物可见。
- Fork、Recorded 全范围 28 检查点重算，模型与执行客户端禁止入口命中均 0，实际模型/工具调用均 0。
- A/B Live 各一次模型/工具调用，同名文件原字节 A/B 不同；Diff 展示各自摘要。
- 真实主进程受控保存写出 A，但测试替换 SaveDialog 返回值，不能据此宣布原生对话框操作已验收。
- 来源索引指纹与 main 分支不变；4 个独立容器清理确认，逐个实际 inspect 为 404。

实测进程为 win32/x64、Electron 45.0.0、内置 Node 24.22.0、Chromium 156.0.8078.48；该内置 Node 与构建机 Node 是不同角色，不要求用户安装 Node。此窗口观测到 devicePixelRatio=1.5、内容 1280×901；不是完整 100%/150%/200% DPI 矩阵。

前一轮 `06cf800` 候选的 Forge 打包目录与 ZIP 程序也分别跑过真实闭环（44.7s / 43.1s）。最终源基线又加入了授权资源，故上表及本节最终证据以 `c41635b` 的 ZIP 为准，不混用旧包摘要或把未执行的 Setup 当作安装成功。

## 原始证据

均在忽略的 `.agentgit/evidence/`：

| 文件 | SHA256 |
|---|---|
| S6-package-audit-final.json / S6-portable-package-audit-final.json | `5029289ecc27446589fd64d44141e8acc0474c2f3fd0189fdb8a653f9302a065` |
| S6-package-file-inventory-final.json | `d00f037d1e8d182ecb4272a5f00f406f61a8fde508394d05f53851251b93b0cb` |
| S6-electron-real-portable-final.xml | `7be8fc9015c0325e6ca8ae198f5109337047354d393d6f03ad2e28cdcb1b6417` |
| S6-real-portable-audit-final.json | `fb3659398db53af9f261f45f79b64430d54f16cafaeb876364527a6f1d85046f` |

旧开发目录基线证据见 [S6-desktop-real.md](S6-desktop-real.md)。这批工作没有修改 Python 产品源码或冻结镜像，也没有重新宣称后端 615 项全部运行过。
