# S12-C 操作者 API/CLI 工程核对

2026-10-31，接续 `74f584c`。固定四角色协作新增 HTTP 与八个 CLI 命令，复用既有准备、显式开始、严格 Recorded、组控制及 Worker 事务。没有新增依赖、迁移或执行能力。最终源码 1,106 单元、55 不同集成通过；真实桌面、独立 API 进程/分发包、更多控制/压力和完整 C/T05/D3 门槛继续，未合并 main。

## 可运行入口与授权

[演示和命令](../../examples/collaboration/README.md)使用[固定输入](../../examples/collaboration/research.json)，也可执行 `tests/integration/test_collaboration_entrypoints.py` 的隔离自动化演示。HTTP 均位于 `/repositories/{repository_id}/collaborations`：prepare、list、单组状态、start、recorded、control、messages、shared。CLI 对应 `collaboration-prepare/start/recorded/control/status/list/messages/shared`；源码/私有输入由至多 1 MiB 的严格 JSON 文件读取，控制文件至多 4096 字节。准备只生成四个暂停任务，执行必须另行原键开始。

HTTP 继续使用 Bearer、本机 Host/Origin、项目头、原键及原始 JSON 限额，路径项目须与 `X-Repository-ID` 一致；actor 来自后端配置，不能由请求正文自报。组、来源、角色及通用 Run/Job/事件/历史/产物/Diff/行为/执行查询统一核对协作所属操作者。跨操作者与 Recorded 跨项目来源保留 403，路径/项目头不一致为 404；不放宽原有来源守卫。拒绝在 CAS 内容读取前发生，角色查询不接受伪造 Lease 或私有对象摘要。

公开查询提供组模式/来源、四个真实任务/Run/Job、控制 revision、quiescent/all_terminal/unknown/failure；角色 adapter 为 `collaboration_role/v1`，展示自身权限、预算、检查点阶段/状态、logical_usage、新意图/复用/未知行数，不读取或返回私有 notes。真实状态和接受收据分开，当前 Run 状态与最近检查点状态也分开。物理意图计数不冒充确认执行次数或账单，离线 Mock 字节不冒充供应商 Token。

消息只返回已归属的信封、明确导出父地址、发送和实际接收检查点，不返回任意 CAS 内容；共享查询核对本组程序/绑定、发布者和版本对象。组/消息以 UUID 游标、共享以路径/版本成对游标分页，每页 1–100；版本上限与 JavaScript 安全整数一致。CLI/HTTP 重开只查询，不自动重发开始/控制，main HEAD 不变。

## 源码、测试与失败尝试

最终源码 **`source-sha256:66c98b7dc67c79ca0d463b34164d41ad38526b37a62a4387c0694cdbc6187439`**，锁仍 **`13df29416247b05eb1ab2a97b9ef2d6035af2903b1e52e8562d3ef1aaa45ed3a`**。执行前冻结 `74f584c9ac6baa66a01092a0b7b5c49374e6fd53` 完整 Git 基线及 **833 文件**覆盖层。最终 **1,106 单元 +55 不同集成通过，零失败/跳过**：新入口 22，既有管理/检查/MCP/LangGraph 入口 33。先单独通过完整 Live 查询 1 项，再通过其余入口 21 项，不重复累计。Ruff/format 528 文件、mypy 219 源文件通过；2,560 节点联合收集只表示收集。

22 新入口覆盖准备/原键与显式开始、真实 Live 完成、消息/共享分页和报告下载、重开只读、严格 Recorded 零客户端/20 自身来源复用、十类其他操作者拒绝、六类项目/头拒绝、认证/字段/版本/大小/游标及准备态取消、CLI 有界文件，以及 CLI→HTTP 暂停→CLI 恢复→Live 完成→Recorded 完成与跨页组查询。通用 Diff 本人查询通过；其他操作者的 Commit、分支历史、Diff、行为查询、执行列表、Job 和产物下载在 CAS 前被拒绝。HTTP 使用真实 ASGI TestClient，CLI 使用实际 Typer 定义，持久执行使用真实 PostgreSQL/CAS/Worker；没有把它们称为独立 HTTP 服务进程或桌面验收。

v1 仅冻结静态整理前输入，未执行 Runtime。v2 源码 bb2446…ad16 的 1,106 单元、33 兼容及 19 新入口通过、2 新入口失败：测试把下载 URL 写漏 `/content`，并把 Recorded 跨项目的既有 403 错断言为 404。修正断言、补共享版本上限及 CLI 有界测试后，v3 源码 86dcfb…6e1e 的全部 22+33 与单元通过。Runtime 终态后 v4 只整理一处测试换行，AST/其余 831 文件相同，并复验 Live；v5 仅强化同一 CLI 用例的组列表分页，保留全部旧调用/断言，复验通过。这些重复不算新节点。

复核通用查询后，v6 补齐分支历史、Diff、行为及执行查询的协作所属校验，并扩大同一 Live 用例的拒绝断言；最终完整 22+33/1,106 已在上述 66c98b…7439 源码复跑。早期报告保持原源码/驱动归属，不把 v3–v5 或旧 Worker 故障测试换标到本版。

## 原始独立核对与封存

最终 v6 标准库读取器核对 **22 原始包、23 组/92 角色任务、2,209 对象记录/2,109,218 字节**，合计 **100 调用台账、35 消息、25 发布、368 Commit**。这些是按物理包的计数，含准备/取消/空库，不作唯一对象数或成功率。100 台账中 60 为三个测试 Live 来源的 Mock/本地调用，40 为两个成功 Recorded 目标的来源复用，不计作 Recorded 实际调用。

两个新 Recorded 目标分别来自正常与预留点暂停/恢复来源，**146 映射检查点/40 复用**完整状态 hash、消息因果/收据、共享头及自身调用来源独立重核。15 份公开查询/拒绝/控制收据逐项对照真实 Run、权限/预算、用量和意图/复用账本；只读/拒绝的 before/after 全表计数一致，公开响应无私有 notes。33 兼容另有 JUnit 和冻结驱动，不宣称具备本轮独立原始包审计。

v6 公开投影/来源审计 SHA256 **`8ba138faa63bc244c584e366997d0120186b0ff5bbd790d740a3e99f4a4fa36e`**，Recorded 投影审计 **`0fd7143fbf9ad60ce2b5d52455b54badb159d6ad07c2a5e20cec06eb24c6cbcc`**。**562 份代码/测试/迁移/锁和演示 JSON 的冻结、工作区、Git 暂存字节完全一致**，审计 **`094abb2b224c0e6ec6d84e9d2e5a07ab9475f1dbe60e07a4b69677e2bfbd277f`**。设计/实施方案与依赖锁摘要保持。

`.agentgit/evidence/S12-C-operator-v6/summary-v1.json` SHA256 **`4d099d64a0ab10ff0fa0189e16f803222f4bf9c79a977934776ffffd352f0042`**，索引 **16,268 文件**，保留 v1–v6 冻结、原始包、首次错误、格式/强化证明、旧封存及最终审计。全部 Runtime 终态后封存和更新文档，早期封存不覆盖。

## 表数勘误及后续

独立新建测试 Schema 的真实 PostgreSQL 目录确认：`0032_collab_recorded_steps` 为 **79 张业务表 + `alembic_version` =80 张表**。此前部分 C 文档把含版本表的总数写成“业务表”，现更正计数口径，不改变历史迁移、表名、原始包或测试结果；目录证明 SHA256 **`d9c91381d5d746d525b2af321c3ee252001c90eace57c48072bd246642e44abd`**。本轮没有新增迁移或迁移业务库。

继续真实桌面展示/控制、更多组/单角色交错及压力、完整 S2/S4/S5/S6/S11 兼容；本轮没有复跑旧实际进程终止、终端/Python 或桌面包验证，不换标旧结果。C 稳定后再进入 A2A；独立 Windows/原生、人工标签/预期、Top-3 4/8 未达标、最终四适配新包与 D3 保持原门槛。
