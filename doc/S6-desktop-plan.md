# S6 桌面实施清单

最新增量：当前候选为[0.1.3 / 键盘修正与门槛核对](acceptance/S6-keyboard-and-gates.md)，源基线 ed5f0fb，已补齐 [D1 操作指南](D1-desktop-demo.md)。四条解压包真实流程、打包安全边界及当前用户安装生命周期通过；下文旧版本表述保留前序历史。原生保存和独立 Windows 环境等剩余条件仍待完成，阶段门槛保持不变。

状态：只读 API、四页、真实五工具联调及开发候选包构建已完成，模型配置/预算/Worker 与桌面选择和计量已接通，新候选包复测、四页 48 组缩放/键盘及生命周期已通过；MSIX 上下文 0.1.1 安装/升级/卸载/重装及数据保留通过，继续推进原生保存及独立用户安装验收。分支 `codex/s6-desktop` 从 S5 本机验收合并 `235bbdf` 建立。证据见 [只读 API](acceptance/S6-read-views.md)、[桌面初版](acceptance/S6-desktop-progress.md)、[真实联调](acceptance/S6-desktop-real.md) 、[候选包](acceptance/S6-desktop-packages.md)、[模型桌面接线](acceptance/S6-model-desktop.md)、[新版候选/生命周期](acceptance/S6-model-packages.md)与[最新安装记录](acceptance/S6-install-lifecycle.md)。本清单细化既有实施方案，不修改 S6-T01–T12 的退出条件；S11/S12 仍为必选主链。

## 已确认的界面范围

2026-10-23 增量：[分发内容与连接降级](acceptance/S6-package-hygiene.md)修正包内测试结果和构建配置遗漏，重建 0.1.1 候选并复验新 ZIP 五工具闭环、新 Setup 安装生命周期。新增连接错误/恢复的真实 Electron 检查。用户将独立环境留待稍后；继续补原生保存及 S6-T11 的 Worker/执行环境专项，不将已有部分证据写成全项通过。

模式为 Operate：让开发者完成运行、追溯与分支比较。视觉沿用根目录 [DESIGN.md](../DESIGN.md)，产品事实见 [PRODUCT.md](../PRODUCT.md)。四页采用同一应用外壳与项目上下文；连接设置是窗口内的面板，不成为第五个业务页面。无新的视觉偏好被写入持久设置。

| 页面 | 主要字段 | 必须可操作的交互 | 关键状态 |
|---|---|---|---|
| Runs | 当前项目、目标、固定配置/模型、五工具权限、完整预算、Job/Run ID、状态、用量、来源 | 创建任务并提交、打开已有 Job/Run、进入历史、按项目查看运行；提交按钮防重复，保留可查询的受理 ID | 加载、空列表、排队、运行、审批、暂停、未知、终态、断连/冲突 |
| Timeline | Commit parents、逻辑事件/caused_by、检查点状态、origin、计划、调用、产物 | React Flow 图选择检查点、游标补齐事件、暂停/恢复/取消、审批、查看/受控保存产物 | requested 与 applied 分开；缺证据或来源不可达明确显示 |
| Timeline 详情 | 命令/代码、cwd/argv/stdin 引用、stdout/stderr、退出码、错误、截断、耗时、输入/输出 manifest、文件变化 | 在同一详情区切换逻辑状态与执行证据，不提供宿主执行入口 | 未知不补退出码；失败/半产物不展示为成功输出 |
| Branch Experiment | 来源 Run、安全起点、Recorded 终点或 Live 干预、新分支、预算与授权含义 | 模式互斥；Recorded 禁止干预；Live 提交文件/配置/计划与类型化执行参数，查询新 Job | 模式说明、校验失败、提交中、幂等重试、重算/逻辑状态/实际计量分离 |
| Diff | base/head、共同祖先、JSON Pointer、增删改、前后值、产物摘要、调用差异 | 选择两个同项目 Commit、分页、跳转来源证据 | 无祖先、空差异、二进制、长 JSON、缺对象、版本冲突 |

主内容遵守既有最大宽度、间距和字体；图/代码区域内部滚动。小窗口让次级详情收拢，不裁切主要操作；360px 补充视口不代表移动端交付。状态始终有文字，不依赖彩色圆点。初次视觉概念需包含四页及主要详情状态，不能只画一个首页。

## 现有 API 与只读入口

当前服务已有创建项目/配置/任务、提交 Run、读取 Job/Run、事件、产物、审批、Commit/Branch、Replay 和 Diff，见 [API.md](API.md)。先复用服务层和认证/项目约束，不引入第二套后端。

以下便捷只读入口已在 S6 中定义有界契约、实现、测试并接入页面；保留其范围约束：

1. 项目内 Run/Job 列表：现有 HTTP 主要按 ID 读取；需要项目限定、稳定游标和有界分页，不能让桌面直读数据库。项目初始选择也须由受控创建/读取流程提供，不能假设存在全局枚举 API。
2. 执行详情：现有状态包含调用引用，HTTP 未直接发布完整执行证据。提供按项目、Run/调用归属核验的有界查询，明确物理来源、日志原字节/文本、截断、代码/命令及输出清单；不开放任意 CAS 摘要读取。
3. 已选 Commit 的历史产物列表/下载：当前 Run 产物入口面向最新检查点；Timeline 的历史选择必须保持所选版本，不能误取最新同名文件。下载继续按有归属的产物 ID。

新增入口保持原 Schema 和历史迁移冻结；若只需现有索引与查询，不新增迁移。先检查已有查询能力，避免复制现有服务逻辑。

## 桌面工程与验证顺序

1. 核验官方兼容性并固定 Node.js、Electron、React/TypeScript、React Flow、Vite/构建工具、Electron Forge 和 Windows makers；建立 `desktop/` 与锁文件，不提交依赖目录。
2. 分离 `main/preload/renderer/shared`。主进程固定 API 客户端和原生导出；preload 只暴露类型化业务方法；renderer 不能接触 Node、文件系统、Shell、Python、Docker 或身份令牌。
3. 先完成 sender/frame/业务参数/项目验证、回环与重定向拒绝、请求/响应上限、生产自定义协议/CSP/导航/权限与单实例边界，并通过针对这些边界的测试。
4. 按 DESIGN 概念与可复用 token/组件实现四页，接入真实受控 API，补全加载/空/失败/断连/冲突/审批过期及页面退出取消轮询。开发服务器预览仅为补充，必须启动真实 Electron 进程验证。
5. 实现一个真实 LLM Adapter，保留离线 Mock；测试参数、结构化输出、超时、有界重试、usage 和签名。明确开启的供应商冒烟另记实际结果。
6. 真实 Electron 完成五工具、历史、Fork、Recorded/Live、Diff 和导出闭环，再验证窗口宽度、DPI、键盘、长中文与注入内容。包内资源、字体和路由不得依赖外网。
7. 用 Electron Forge 构建 Windows x64 安装包和便携 ZIP，记录版本/架构/摘要/依赖/签名状态。在独立 Windows 用户环境验证安装、无开发源码/构建 Node.js 的运行、覆盖升级和卸载；保留业务数据，不自动迁移后端。

S6-T01–T12、D1 离线五工具与两个桌面包的验收证据齐全后才能标记 S6 完成；真实模型冒烟按方案单列。当前初版测试使用真实 Electron 与受控 HTTP fixture，不声称完整真实后端或安装交付条件已通过。
