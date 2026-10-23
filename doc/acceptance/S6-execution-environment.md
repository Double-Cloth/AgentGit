# S6 执行环境异常与 0.1.2 候选

状态：S6-T11 新增桌面提示与受控异常验证通过；0.1.2 ZIP 的四条真实后端流程、Setup 的当前用户安装生命周期通过。S6 / D1 仍未验收，未进入 S7 或合并 main。用户已明确独立 Windows 环境稍后提供，当前继续其他验收，不重复要求该环境或降低门槛。

## 行为与实现

源基线 `3a50233`，在 `codex/s6-desktop` 上承接[分发白名单修正](S6-package-hygiene.md)。Timeline 在 queued 时显示“尚在排队，未取得执行进展”及检查 Worker 的建议；任务取得进展后该提示消失。不根据 API 存活推断 Worker 已停止或就绪。

执行结果 error_code 为 sandbox_unavailable 时，现有 Notice 显示明确错误及后端 `agentgit doctor --sandbox` 检查建议。只建议核验 Docker Linux 引擎与已审阅固定镜像，不猜测具体缺少哪项，不自动安装、变更环境或重发动作。退出码未知、失败结果不完整、没有输出工作区等原始事实继续显示。没有新增接口、依赖、样式或颜色，沿用 DESIGN 的正文、状态与 Notice。

桌面版本升为 0.1.2，package-lock 只同步根版本，依赖未变化；后端协议仍为 0.1.0、Schema 0012_replay_source_index、固定镜像和 Python 产品代码未改变。安装测试从 package.json 读取目标 patch 版本，继续要求显式 Setup 摘要、固定物理根、任务自有 0.1.0 基线且没有已有偏好，不自动接受任意路径或版本。

## S6-T11 验证范围

真实 PostgreSQL / API / 独立 Worker 与实际 Electron 加载新 ZIP 程序，页面是 agentgit://app/index.html。新测试在独立 Schema 和目录运行，不停止外部服务、不删除共享镜像：

| 场景 | 操作与证据 |
|---|---|
| Worker 停止 | 测试自己的 Worker 进程实际正常退出，保存 PID / alive=false；通过桌面提交任务，等待跨过轮询周期仍 queued 且 state_version 未变，出现排队说明；重新启动 Worker 后同一 Run 完成，POST 列表不增加 |
| Docker 不可达 | 仅该 Worker 的 DOCKER_CONFIG / DOCKER_CONTEXT 指向新建的私有 context，端点为不存在的本机 named pipe；实际 Docker 连接失败，经执行服务形成 sandbox_unavailable，Run 为 failed |
| 镜像查询缺失 | 仅 Worker 内的测试接缝将镜像 GET 映射到不存在的全零摘要；真实 Docker 返回 HTTP 404，经过同一执行服务形成失败结果。接缝禁止任何非 GET 请求；未物理移除固定镜像，不把该故障注入写成本机固定镜像实际缺失 |

两个执行失败结果均为 exit_code=null、output_manifest_ref=null、integrity_complete=false、cleanup_confirmed=true；界面明确提示不可用，未把未知退出码写成 0 或将 Run 写成 completed。1280/768/360px 的错误提示和页面无意外横向溢出。当前宿主 context 仍为 desktop-linux，固定镜像仍为 sha256:37837ca77fde6fa5ab6e183ea9484888a107eb79359d2788069c295478fa8967。

此前 API 未监听、数据库 HTTP 503、401 和版本不兼容的 UI 检查见[连接降级记录](S6-package-hygiene.md)。上述证据覆盖桌面对探测/业务错误的响应；独立 Windows 清单保留真实缺失依赖的交叉复核，不把测试接缝等同于宿主服务状态。

## 当前候选与复验

Node.js 24.19.0、npm 12.1.0、Electron 45.0.0 / Forge 8.0.1 执行 npm run make；Setup 与主程序仍为 NotSigned，未推送或发布。以下摘要替代 0.1.1 当前候选，旧记录保留历史事实：

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156108800 | `dd95b479a1f81381eb5582fcfe2216de6cb00c17909a040478a7463607e13ec0` |
| out/make/squirrel.windows/x64/AgentGit-0.1.2-full.nupkg | 155351789 | `fc4833a553ed0b7a98019d8aa52034efaf3d60be0397c1d3afa33b081e083f74` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `c78a00d6620c73b69dfb7097b5be680befe783304d3a5d3fc16d99a7c1fdc16b` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.2.zip | 159328118 | `446e60ca18669458fd3c7becd87aa9c04750db11eaf95d6733cc118431fb0118` |

ASAR 90 项/27 个构建文件，摘要 `08254b7fc865ba9c029763ef18724a0a01f822c4c72dba2de6ec80ccde028cd2`。严格根白名单、三套本地字体/OFL、26 项声明依赖授权和唯一生产 Node 包 electron-squirrel-startup 1.0.1 均通过审计。Nupkg 内 ASAR 逐字节一致；ZIP 解压到 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-0.1.2-465stfxt`，73 文件、376741037 bytes 与打包目录逐字节一致。

| 检查 | 结果 |
|---|---|
| typecheck / 单元 / Python fixture | 严格类型检查通过；20 项 Node 单元通过；fixture 的 Ruff 检查/格式通过。未声称本轮重跑全部后端测试 |
| 默认 Electron 边界 | 2 passed，11.6s；连接错误恢复、四页、IPC/凭据/注入/双击提交 |
| 新 ZIP 真实设施 | 4 passed，约 2.5 分钟：环境异常 41.1s、生命周期及四页 48 组缩放/键盘 32.3s、五工具/Fork/Recorded/Live/Diff/导出 44.9s、模型预算/计量/Recorded/Live 29.2s |
| 最终环境专项 | 完善测试异常清理范围后再运行，1 passed，41.2s，保证测试 Worker 停止操作在 finally 保护范围内 |
| 新 Setup 安装生命周期 | 0.1.0→0.1.2 实际覆盖升级、卸载、重装查询和新任务、再次卸载，1 passed，42.5s；来源、数据库/CAS 和 Worker 保留检查通过 |

安装仍位于 Codex MSIX LocalCache 的当前用户重定向环境，最终程序已卸载；偏好仅保留本次测试创建的连接信息，不含身份令牌。升级前按上一轮 QA 核对偏好归属、原字节归档、固定物理根后才恢复测试基线。安装及 ZIP 均没有运行 Vite，ZIP 的应用子进程 PATH 只保留 System32；不据此声称已取得独立 Windows 用户或移除了宿主开发源码/Node。

Browser plugin not available，沿用项目 Playwright Electron。页面标题与 URL、非空界面、无框架错误遮罩、控制台错误为空、实际错误/恢复交互均核验；已查看普通与窄视口截图。最终异常截图在仓库外 `C:\Users\DC\AppData\Local\Temp\agentgit-environment-qa-FQCKjD`，全组异常截图在 `agentgit-environment-qa-LesMzV`，安装截图在 `agentgit-install-qa-firBPh`（均为同一 Temp 下）。没有重设 Windows DPI；缩放使用 Chromium device scale factor。

## 固定证据与未完成事项

原始文件在忽略的 .agentgit/evidence：

| 文件 | SHA256 |
|---|---|
| S6-package-012-inventory.json | `9c1bde28d12d6f243874dc064356a5408926dfc5c9e4e952782f8bf086021a6e` |
| S6-package-012-audit.json | `1eb594a865ec63e595756c5d22ba6c22e14da3023184706876ab7f846810ee3a` |
| S6-package-012-boundary.xml | `fd748e034763bacb1c8c937d954d414df7b85b328aba85ef4c3cf8036a2656ce` |
| S6-portable-012-real.xml | `684eb43527254f90a802c83cd84ed3cb975f827df05fc1613af506d262be5610` |
| S6-portable-012-real-audit.json | `2692384900b29478bbe210c5d6af1447a4a2d5cd2e18dce1dc8625fd0d7ca5e1` |
| S6-lifecycle-012-qa.json | `feae186bd0e65a1754de711b7b00a103675142439e5d93aeed00b3541ff75561` |
| S6-model-012-qa.json | `1e7f723e6f413cd713f6034c9746d8f25f9075ea31e2028f549bc64a2f19f1bd` |
| S6-install-012.xml | `5512b4840b8660b667792b2f1b23d297b8a5497525d920265eebaf91661c5dfb` |
| S6-install-012-qa.json | `51510ce5fb7c7bd2d10f33f2dc5676cb9ee3759029bf6a957cd0900df5a56d16` |
| S6-environment-012-final.xml | `0bff85ae8983b04fabe2d7846d9085962bccb83d643ecda3bf8de8f27623be4b` |
| S6-environment-012-final-qa.json | `ab271763fcbef787acd38b7b1279d0797871cc3389ee0969f29ec6bc0e516653` |

真实供应商未调用，模型接线继续使用确定性 HTTP 替身；S6-T06 未明确开启，单列未运行。原生保存成功/取消无写入尚缺证据，独立 Windows 用户的无源码/Node、禁止外网、完整安装/ZIP/覆盖升级/卸载及真实依赖缺失交叉复核仍待环境。S6 不提前验收；S7–S11 与 S12-A/B/C/D 仍在完整目标内。
