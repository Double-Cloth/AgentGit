# S6 0.1.1 候选与 MSIX 上下文安装生命周期

后续更正：本页保留旧摘要的安装事实；[分发内容修正](S6-package-hygiene.md)发现旧包包含 test-results/.last-run.json 和构建配置，原审计覆盖不足。因此本页的“无测试/配置”表述撤回，交付候选已由该记录中的新摘要替代。旧生命周期事实不等于新包已通过独立用户验收。

状态：0.1.1 新候选、安装目录三条真实后端流程、便携 ZIP 核心闭环，以及真实覆盖升级/卸载/重装的数据保留通过；**S6 / D1 仍未最终验收**。本次进程继承 Codex MSIX 的文件重定向，不等于独立 Windows 用户、无源码/构建 Node 或整机禁止外网的验收。原生保存成功仍缺证据，不能提前进入 S7 或合并 main。

## 新候选与版本

产品基线 `7083499`，AgentGit 桌面 0.1.1，后端协议 0.1.0 / Schema `0012_replay_source_index`。桌面 patch 版本用于验证真实 0.1.0→0.1.1 升级；页脚/授权面板由 package.json 构建元数据生成，真实 Electron 比较 app.getVersion，修正以前硬编码界面版本的问题。没有升级依赖、后端协议、Schema 或固定容器。

在 desktop 执行 `npm run make`，Node.js 24.19.0/npm 12.1.0、Electron 45.0.0/Forge 8.0.1。Setup 及主程序仍未签名，本地构建未推送/发布：

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGit-0.1.1-full.nupkg | 155356432 | `5935291278ef62501d0bc60ca9fccd16aacc181a501535c5e56fe1c393128a00` |
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156113408 | `4fb7ce80d50ab597e34218af6f467bb8b3773cd513efa246409e06feec3de74d` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `46e61ec7eaa8cb8683f8d31091b8a6182d307e59b476c33e26af6494945086fb` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.1.zip | 159332033 | `de596e82acfb3a14c8d8084f511bdfa3c52265499218af2095f1d69bf77f4b0e` |

ZIP 解压到 `C:\Users\DC\AppData\Local\Temp\agentgit-portable-011-h3frb4t5`，73 个文件、总计 376748853 bytes 与 Forge 包逐字节相同。ASAR 94 项/27 个构建文件，SHA256 `234d1328c73cca1aba54d8e49fea2a7bcf09903336a7ee6b300b7b797baafe61`；Nupkg 的 lib/net45/resources/app.asar 也逐字节一致。安装目录和便携目录的 audit-package 均通过：3 套字体/OFL、26 项声明依赖授权、无项目源码/测试/.env/业务数据。包内生产 Node 依赖仍仅 electron-squirrel-startup 1.0.1。

## 实际环境与路径保护

逻辑安装根为 `C:\Users\DC\AppData\Local\AgentGit`，Windows 普通目录属性不是 reparse point，但 Node 的原生 realpath / promises.realpath 返回实际根 `C:\Users\DC\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\AgentGit`。逻辑 Roaming 偏好根实际为 `C:\Users\DC\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\AgentGit`。旧版 fs.realpathSync 仅返回逻辑路径，不能据此认为没有 MSIX 重定向。

新安装测试首先因逻辑/实际路径不一致而在任何安装操作前拒绝继续；检查这两个明确路径后，通过 `AGENTGIT_INSTALL_PHYSICAL_ROOT` 显式固定实际目标。脚本在覆盖和卸载前重新比对实际根，拒绝任意重定向；数据库/CAS/偏好全部在安装根外。不能静默放宽成接受任意 LocalAppData 下的目录。

`playwright.install.config.ts` 单独选择 install.spec.ts，只有 `AGENTGIT_INSTALL_TEST=1`、Setup 绝对路径及核验 SHA256 齐备才可运行；默认测试不会卸载软件。它还要求 0.1.0 安装基线且没有已有 preferences.json，防止覆盖已有操作者设置。此轮最初已确认安装根和偏好根都不存在，后续创建的设置与安装均属于本任务。

## 通过的流程与计量

| 流程 | 实际结果 |
|---|---|
| 0.1.0 首次 Setup 与安装目录闭环 | [安装基线](S6-installed-baseline.md)，退出 0；五工具/Fork/Recorded/Live/Diff 1 passed，45.5s；仍是本 MSIX 上下文 |
| 0.1.0→0.1.1 覆盖、卸载、重装、新任务 | 最终 1 passed，47.8s；另一次完整回归 44.2s 通过；最终新增“桌面已完成可见”断言后保存截图 |
| 0.1.1 安装目录三条真实后端流程 | 3 passed，103.8s：生命周期/四页 48 组缩放 31.7s、五工具/Fork/Recorded/A/B Live/Diff 43.6s、确定性模型接线 28.1s |
| 0.1.1 ZIP 程序核心闭环 | 1 passed，44.3s；app.isPackaged、footer/app.getVersion、五工具/Fork/Recorded/Live/Diff/导出/来源不变与容器清理 |
| Node / 类型 / 静态 | 桌面 20 单元通过；typecheck、Ruff 通过；没有重新宣称全后端回归 |

安装和 ZIP 进程使用独立 cwd，PATH 只留 System32，清除 NODE_PATH/NODE_OPTIONS；没有 Vite。驱动会话仍有构建 Node/源码，因而不满足独立用户的无开发环境条件。Browser plugin not available，使用现有 Playwright Electron；模型流程 console.error/pageerror 为 0，页面身份/非空/无框架错误叠层和操作有真实截图支持。真实供应商采用确定性 HTTP 替身，没有真实模型费用。

覆盖升级由 0.1.1 Setup --silent 实际重建安装程序目录，日志、注册 DisplayVersion、Electron app.getVersion 和界面均确认 0.1.1；不是运行两次相同版本就记为升级。数据库迁移没有执行。

卸载前后保持相同 310 个 CAS 文件（616461 bytes）、相同总摘要 `15736adfb156f1ce471fa81f2df8293e0e0f43d3e812400615640b29062d7929`、相同来源指纹和 Schema，Worker 存活、API health=200。再装后自动读取原项目/唯一 Run，不新增 POST；随后手工从界面提交另一个三工具任务并完成。旧来源指纹仍为 `57bbe4f3879d00c7e5a1ab7b27103e5b217bb4c86f563a54cab3cdc6d1a14027`。新任务后 426 个 CAS 文件、923315 bytes，再次卸载前后也完全相同。测试保存证据后才由 fixture 删除它自己创建的 PostgreSQL Schema，不能把该测试清理误认为卸载删除业务数据。

当前 Squirrel 卸载后的主程序已不存在，注册项和开始菜单链接已移除，无 AgentGit/Update 进程；仍有 `.dead`（1 bytes）、`app-0.1.1\squirrel.exe`（1899520 bytes）、`app-0.1.1\v8_context_snapshot.bin`（753112 bytes）、`Update.exe`（1899520 bytes）。这些残留如实保留在清单，不宣称安装目录字节为零。业务偏好继续保留，未包含 API 或供应商凭据。

## 复验中发现的边界

1. 第一次实际路径保护失败揭示 MSIX 上下文，未执行升级/卸载。上述显示路径与真实路径都保存到证据中；没有将其改名为独立用户环境。
2. 第一次重装任务的 API 已完成时，截图仍处于轮询前的运行中；增加界面终态可见性断言，重新跑完整安装回归后再保存最终截图。产品轮询逻辑未更改。
3. 为复验恢复旧基线时，复制到 Temp 子目录的 Update.exe 按自身位置把根推导为 Temp/AgentGit；日志表明这是本次新建的临时安装，不是原安装根。发现后核验其 ASAR/绝对根并卸载，再把恢复器放在 LocalAppData 的任务专用目录，先按[同提交官方根路径规则](https://github.com/Squirrel/Squirrel.Windows/blob/eef37460ae/src/Squirrel/UpdateManager.cs)验证推导出的实际目标等于已固定安装根，才恢复 0.1.0。没有修改产品代码规避该规则；旧测试偏好先按归属核对、逐字节备份，仅移除本次测试自己的偏好文件以重新建立干净基线。

脚本复用了原 real-backend 的本地测试客户端，没有第二套产品 API。fixture 新增 storage_audit，仅通过本地测试进程 stdin 查询 Schema/CAS/来源和 Worker 状态，不发布到 HTTP/IPC，也不进入安装包。

## 证据与仍待条件

最终升级/重装截图在仓库外 `C:\Users\DC\AppData\Local\Temp\agentgit-install-qa-BeEwyv`，包括 upgraded.png、reinstalled-completed.png（已完成可见）、实际升级日志。阶段固定证据在忽略的 .agentgit/evidence：

| 文件 | SHA256 |
|---|---|
| S6-installed-011-real.xml | `363da8e4e097ec4aedc2144252d85d36ba2822412eaddc8654938c3c43c0d8fa` |
| S6-installed-011-real-audit.json | `b0f5c8bba54bac3f40ce5adb971485b228562bbacb46585555ed8a4cc956333b` |
| S6-installed-011-model-ui.json | `2544ae3786f07625c6d80dc87d47f5d60ba039e37083512f7bb4d30e9c63f35c` |
| S6-installed-011-lifecycle.json | `46fac77f2104a9c3a15d394cea1d1111262eeb2fad0dab0bdc3c6dcdd4508170` |
| S6-install-lifecycle-final.xml | `594e6067b39279502fc19f14321d30ff3bbc5b277834e408c041a352f4f96f8d` |
| S6-install-lifecycle-qa-pre-clean.json | `06267426e92de422ee1b412165364f2b8218f050249c27dc0e10727ba06672d9` |
| S6-install-path-context.json | `87e0faea9f25e67f64cd45718d23b9c91892bec99a9e44d9b2671ea5b509d788` |
| S6-installed-011-state.json | `b826212f1315777be8dcd6a1c5ed6576456cb8ec6d18bccd4db7eb0c84ef308f` |
| S6-portable-011-real.xml | `a01d3f20326f71fbbde9205351245854a0f755fc505611026c9ff761cb025782` |
| S6-portable-011-real-audit.json | `038539ca666f6a152fa907e9101b8d0a4ce432bf53ec51ef3b91a7e295db1374` |
| S6-package-011-inventory.json | `11738c5b88e403d1eda51a46226734b1c09372d8163bc9677c598eec878e1c5e` |
| S6-package-011-audit.json | `f3b1156a4033a34028a3076ee9677a58272232cec2590854335cdd112a7d4253` |
| S6-installed-011-audit.json | `f3b1156a4033a34028a3076ee9677a58272232cec2590854335cdd112a7d4253` |
| S6-portable-011-audit.json | `f3b1156a4033a34028a3076ee9677a58272232cec2590854335cdd112a7d4253` |
| S6-nupkg-011-asar.json | `296dc95b8d149e7a0d5dccfe5b123d9507b8ee9f43607c76889f1ed69b743b31` |

S6 尚需实际原生保存成功/取消无写入，以及独立 Windows x64 用户下的安装包与 ZIP、无源码/前端 Node、禁止外网、覆盖升级/卸载证据；[待验收清单](../S6-windows-acceptance.md)已更新。此前 Computer Use 的窗口/焦点失败没有在本轮被伪装为通过；读取输入桌面名为 Default 也不证明原生对话框可自动交互。S6-T06 真实供应商冒烟没有明确开启，继续单列未运行。
