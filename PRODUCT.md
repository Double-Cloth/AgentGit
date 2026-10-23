# AgentGit 产品上下文

<!-- impeccable:product-schema 1 -->

本文仅整理用户已确认的 [产品设计](doc/AgentGit_设计文档_v1.0.0.md)、[实施方案](doc/AgentGit_实施方案_v1.0.0.md)及当前实现事实，不新增产品承诺。技术与阶段要求仍以这些项目规范为准，视觉继续遵守 [DESIGN.md](DESIGN.md)。

## Platform

web

## Stack

既有后端为 Python 3.12、FastAPI、Pydantic、PostgreSQL、SQLAlchemy、Alembic、Typer 和 uv。S6 的用户指定方案为 Electron + React/TypeScript、React Flow、Electron Forge，交付 Windows x64 安装包与便携 ZIP；渲染界面使用 Web 技术，桌面主进程承担受限系统集成。已建立 `desktop/` 初版并固定 Node.js 24.19.0、Electron 45.0.0、Forge 8.0.1 等版本；初步真实 Electron 边界/四页测试通过，安装交付尚未验收。

## Users

管理和调试 AI Agent 执行历史的开发者。需要查看一次运行发生了什么、从安全检查点创建独立分支、比较客观差异，并在后续阶段通过受控实验验证修复，而不是凭一次模型回答判断因果或成功。

## Product Purpose

提供执行状态的版本管理、可审计的 Replay 与隔离实验，逐步完成故障候选定位、有界最小修复搜索、独立复测和受约束合并。D1 是中间桌面演示里程碑；完整目标还包含 S7–S11 及 S12-A/B/C/D，不能把 D1 或 D2 当作全部交付。

## Positioning

把版本化状态、类型化干预、隔离重执行和验证证据串联起来。Recorded 验证已选历史的受控状态转换；Live 从独立输入、预算和授权重新执行。恢复状态、复现历史、重新答对和完成修复是不同事实，需要分别展示。

## Operating Context

首版是 Windows x64 本机桌面应用，连接用户已准备的回环 API、独立 Worker、PostgreSQL 与 Docker Linux 固定镜像。桌面退出不取消任务、不关闭外部服务，不自动安装或迁移后端。Agent 的 Shell/Python 只在既有固定容器执行，不转交给 Electron、宿主 Node.js 或系统 Shell。

操作者通过 `AGENTGIT_API_TOKEN` 预置本地 API 身份；主进程读取并使用，渲染进程、IPC 返回、前端存储、日志与安装包均不包含令牌。连接和窗口偏好可以保存在用户数据目录，业务数据库、CAS 与 Run 产物不搬进安装目录。

## Capabilities and Constraints

- S1–S5 已完成本机验收：五个工具、耐久执行与审批、未知结果核对、版本历史、Branch、Recorded/Live 和结构化 Diff；当前完整回归 599 项通过，见 [S5 验收](doc/acceptance/S5.md)。
- S6 首批业务页面为 Runs、Timeline、Branch Experiment、Diff。连接配置留在现有窗口内，执行详情留在 Timeline 的详情区域，不新增独立终端页面。
- 状态/事件/产物使用现有服务及项目边界。修改请求防双击并保持幂等；轮询使用游标且退出页面取消请求；断连或重开窗口不能重发修改动作。
- 主进程 API 客户端只有固定回环端点、路由、方法和有界请求/响应；IPC 有业务白名单、sender/frame/参数/项目验证。渲染进程启用隔离、sandbox、CSP 和导航限制。
- 外部资料、Markdown、HTML、代码和日志均是不可信数据。产物仅按 ID 下载，由主进程的原生保存对话框确定导出目标，渲染进程不能传入任意宿主路径。
- 无外部模型密钥、无外网时离线 Demo 和确定性测试必须可用。真实模型 Adapter 属于 S6；真实供应商冒烟需明确开启、单列实际结果，不成为离线隐含条件。

## Brand Commitments

产品名称为 AgentGit。界面继承根目录 DESIGN.md 的视觉体系、允许的字体替代、组件和断点；不另外发明品牌配色或装饰体系。字体和其他资产须有授权并满足安装包离线使用要求；已取得本地 Inter、EB Garamond、JetBrains Mono 及 OFL，记录上游提交和摘要，没有把未提供的商业字体视为已具备。

## Evidence on Hand

真实 PostgreSQL、固定 Linux 容器、完整五工具来源、CLI 与 API、逐检查点 Replay 证明、A/B Live 文件与预算隔离均有本机记录。原始运行证据在忽略的 `.agentgit/evidence/`；可提交的验收说明位于 `doc/acceptance/`。真实 Electron 与从 ZIP 解压的程序已通过独立测试库/Worker/容器五工具→Fork→Recorded/Live→Diff 联调，应用进程没有构建用 Node/npm 的 PATH。Windows x64 开发候选安装程序和 ZIP 已构建、均未签名；导出测试替换了原生对话框返回值，尚无独立 Windows 用户安装/升级/卸载证据，各项不能互相替代。

## Product Principles

1. 展示客观状态、来源与证据，明确未知、失败、验证范围及能力边界。
2. 未知副作用不自动重发，Live 不继承旧授权，实际计量与历史逻辑用量分开。
3. 用已有服务形成可操作闭环，保持简单，不加入未经要求的框架或复杂抽象。
4. 离线、隔离和安装后可运行必须实际验证，不用模拟成功或开发服务器代替交付。

## Accessibility & Inclusion

S6 要求完整键盘操作、可见焦点、有名称的控件和文字状态；支持长中文与长结构化内容。桌面最小内容宽度 768px，验证 768/1024/1440px 及 100%/150%/200% DPI，360px 是补充渲染检查。对比度须实际检测；现有 token 不达标时先记录并同步修订 DESIGN，不能静默改色或宣称已通过。
