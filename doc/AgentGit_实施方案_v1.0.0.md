# AgentGit 详细实施方案与阶段验收

> 版本：v1.0.0 · 日期：2026-10-20 · 对应产品设计：v1.0.0
> 规划基线阶段：S0（仓库与方案）。实际实现进度及验收见 [PROGRESS.md](PROGRESS.md)。
> 原 S0 轮次边界：仅维护设计与实施文档和 Git 基础规则。2026-10-20 用户已明确授权按本方案自动进入后续实现、测试与合理提交；本文其余“本轮”描述均指 S0 历史轮次。

## 1. 执行原则与交付边界

### 1.1 项目现状与文档分工

项目设计与实施方案放在 `doc/`，文档版本从 `1.0.0` 开始，文件名采用“文档名称_v主版本.次版本.修订版本.md”。界面规范按当前目录中的根目录 `DESIGN.md` 读取；README.md 保留在根目录，仅说明项目目标。用户提供的项目规范继续生效：中文说明与注释、先理解后实现、保持简单、不擅自增加框架或依赖。

| 文档 | 作用 |
|---|---|
| [AgentGit_设计文档_v1.0.0.md](AgentGit_设计文档_v1.0.0.md) | 产品目标、能力边界、研究动机及技术方向 |
| 本文 | 阶段依赖、具体实施任务、数据契约、测试输入/预期、退出标准和排期 |
| [DESIGN.md](../DESIGN.md) | 根目录界面视觉与组件规范，设计规则保持不变 |
| [README.md](../README.md) | 项目目标简介 |

实现前先读取根目录中存在的 AGENTS.md 等规范，并读取 `doc/` 中的设计与实施文档。产品设计与本文的技术约定必须同步维护，发现冲突先修订文档；UI 以根目录 `DESIGN.md` 为准，CLAUDE.md 仅作补充。用户当前指令、AGENTS.md、界面规范及其他文档的优先级保持既定规则。未来版本修订时同步更新文档引用，避免链接失效。

### 1.2 交付点与必选范围

**2026-10-23 临时验收顺序调整（用户授权）：**用户在独立 Windows 测试环境暂未提供后明确指示“可以暂时跳过这一步”。因此，S6 剩余的独立用户安装/升级/卸载、原生保存及相关原生 DPI、禁止外网、真实依赖缺失交叉复核延后补验；允许基于现有本机证据先进入 S7 开发和测试。S6 保持“未验收”，D1 不记为完整通过，不以当前分支合并表示验收。该调整仅改变开发顺序，不删除测试要求，不授权付费模型调用，也不豁免后续阶段门槛；最终交付前须补齐上述证据。具体清单继续以 [S6 退出证据矩阵](acceptance/S6-keyboard-and-gates.md)为准。

2026-10-24 继续执行：用户再次明确“测试环境稍后提供，先继续其他验收”“可以暂时跳过这一步”。S7 自身本机功能与 0.1.4 包内流程核对已归档，继续保留 S6/D1 未验收及 S7 前置待补，开发分支可先推进 S8 的只读证据基础与本机测试；这不表示 S8 前置退出条件已经通过，不将分支合并 main 或将 D2 记为完成。

| 交付点 | 包含阶段 | 可以证明的能力 | 不计入完成的内容 |
|---|---|---|---|
| D0：规划基线 | S0 | Git 可用，设计和阶段测试可审阅 | 任何程序能力 |
| D1：可演示 MVP | S1–S6 | 离线研究 Agent、五个内置工具（含终端命令与 Python 代码执行）、历史恢复、分支隔离、无网络录制回放、结构化差异、Electron 四页桌面应用及 Windows 安装包 | 自动修复、语义合并、多 Agent |
| D2：核心研究版本 | S7–S10 | 受控实验、故障候选定位、有界修复搜索、缩减、独立复测与人工审阅 | 全局最小、严格因果证明、任意 Agent 适配 |
| D3：完整必选版本 | S11–S12（基于 D2） | 三方产物/结构化状态合并，以及 MCP、LangGraph、同进程多 Agent、A2A 四项适配；在桌面安装包中完成兼容性验收 | 任意框架无损恢复、通用多 Agent 操作系统 |

项目完整交付必须完成 S0–S11 以及 S12-A/B/C/D；D1、D2 仅作为中间里程碑，不能代替完整交付。`terminal_exec` 和 `code_exec` 是 S4 实现、D1 必须验收的内置工具，不能推迟为可选适配。S11 结构化合并为必选阶段，在 D2 后完成并验收，再进入 S12。S12 的“必选”表示必须按约定范围实现四个子项，不表示无限扩展各协议和框架的全部能力。

本次修订仅调整 S5 及之后的交付规划和相关总览：S6 使用 Electron 桌面框架，S11 纳入必选主链。S0–S4 阶段正文、测试、退出条件，以及第 3–4 节既有数据/执行/API 契约保持不变；不要求已经实现的后端阶段因桌面化返工。

离线模式必须在无外部 API Key、无互联网条件下完成全部确定性验收。执行工具使用的固定镜像和依赖须在环境准备时预置，任务执行期间禁止自动拉镜像或下载包。真实模型用于适配验证和额外实验，不得成为 CI 或离线 Demo 的隐含条件。

### 1.3 实施方案的主要工程取舍

| 原方案问题 | 明确决策 | 原因和影响 |
|---|---|---|
| Runtime 先做恢复，后做存储 | S2 先完成版本内核，S3/S4 再实现状态机与恢复 | 恢复有可靠基础，避免临时内存实现返工 |
| 单机项目同时引入多个可发布包 | 单一 Python 包，按领域模块分层 | 降低包管理、跨包接口和部署成本 |
| 数据库示意 SQL 被误当作完整迁移 | 改为字段契约，阶段性编写和验证迁移 | 补齐生命周期、FK、归属、Job、幂等与审批 |
| 只有 Commit ID 的 replay 含义模糊 | Recorded 明确来源 Run、起点和终点；Live 明确 Fork 点 | 避免从结束状态“回放”出不同执行 |
| 请求 hash 被当作调用唯一键 | 逻辑调用次序 + 签名 + 请求摘要 | 同参数的两次调用也能返回各自录制响应 |
| “确定性”容易被理解为外部模型可复现 | 只保证受控 Runtime 的逻辑状态和产物一致 | 模型更新和真实网络保持不确定性说明 |
| 快照优化过早 | 每个 Commit 指向完整 state；大内容通过引用去重 | MVP 不实现增量快照和复杂压缩 |
| 异步任务依赖进程内后台任务 | PostgreSQL Job + 单 Worker + 租约与 fencing token | 重启可恢复，不引入 Redis/Celery |
| MCP / 多 Agent 在 P1 与 P3 之间冲突 | 全部归入 P3/S12，四个子项必选并分别验收 | 不作为 D1/D2 前置条件，但属于完整交付 |
| 原方案排除终端与代码执行 | 按当前需求纳入 S4/D1，复用 Docker Linux 容器实现隔离执行 | 增加执行环境、文件状态、资源限制与恢复/重放测试，不直接执行宿主命令 |
| 研究报告依赖在线“当前资料” | 固定离线语料、固定评估时间；联网另作实验 | 验收可重复，且不伪装实时真实性 |
| 搜索可能用尽预算才开始复测 | 筛选、缩减、复测、回归分别预留额度 | 没有复测额度就输出未确认候选 |
| “最小修复”被误当作全局最优 | 有界空间内的较小修复，必要时声明 1-minimal | 不夸大算法保证 |
| 12 周覆盖全部功能 | MVP、研究闭环、扩展分开估算 | 以测试门槛决定进度 |

## 2. 冻结的技术路线与模块边界

### 2.1 选型

后端沿用原文已提出的 Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、Typer 与 Docker Compose。S6 使用 Electron 桌面框架，渲染进程保留 React、TypeScript 与 React Flow，使用 Electron Forge 完成打包。S1 锁定后端依赖，S6 再锁定桌面及前端依赖；不一次性安装未来阶段的包。

- 后端以一个可安装的 `agentgit` 包为单位；API、CLI 和 Worker 共享用例服务与事务边界。
- 元数据只支持 PostgreSQL；单元测试可用内存替身，事务/锁/迁移必须用真实 PostgreSQL，不以 SQLite 代替通过。
- Object Store 使用同卷本地目录，以类型化内容 hash 寻址；绝不复用源码目录存放 Run 工作区。
- 首版任务和工具串行执行，Worker 并发度为 1；先验证两连接竞争和过期 Worker，再考虑吞吐扩展。
- 桌面应用为 Electron + React + TypeScript；S6 锁定 Electron、配套构建工具、Electron Forge 和依赖版本。React Flow 用于 DAG，不引入 Next.js、额外组件库或全局状态框架作为前提；独立 Web 发布不作为首版交付条件。
- 模型先 Mock，再实现一个实际可用供应商适配器。接口绑定供应商与版本，不默认所有“兼容接口”有相同能力。
- 必选执行工具复用已有 Docker 技术路线，在 Linux 容器中提供 POSIX Shell 和 Python 3.12；S4 固定镜像 digest、系统工具与 Python 依赖清单。Windows 开发环境使用 Docker Desktop 的 Linux 容器模式；首版终端语法不是宿主 PowerShell/cmd。
- 不引入向量数据库、通用插件系统、宿主直接代码执行后端、Redis、Celery、S3/MinIO 和 OpenTelemetry 服务栈。新增执行隔离后端或语言需先说明必要性，不能绕过当前容器边界。

### 2.2 调用与依赖规则

| 模块 | 负责 | 不得负责 |
|---|---|---|
| `core` | Schema、哈希契约、Commit/Branch 不变量、领域错误 | HTTP、ORM Session、真实模型调用 |
| `storage` | PostgreSQL Repository、CAS 文件、事务实现、迁移配合 | 决定 Agent 下一步或评价修复效果 |
| `runtime` | Plan/Act/Observe、逻辑状态转换、终止条件 | 直接请求网络或绕过 Tool Registry |
| `adapters` | Mock/实际 LLM、受控工具、调用记录 | 直接推进 Branch HEAD |
| `adapters/execution` | 固定策略创建容器、输入物化、命令/代码执行、有界输出与清理 | 执行宿主命令、接收模型指定的镜像/挂载/提权参数、把容器输出当可信指令 |
| `services` | 用例编排、审批、预算、Job、原子提交 | 复制算法到 API/CLI 两套实现 |
| `replay / diff` | 调用消费和状态比较 | 自动请求新权限或产生线上副作用 |
| `experiments / diagnosis / repair` | 冻结实验、定位、类型化补丁、搜索与报告 | 改验收器、扩大权限、修改既有历史 |
| `api / cli / desktop` | 参数校验、身份上下文、展示与调用用例；桌面主进程桥接固定 API | 自行拼写数据库事务、在 Electron 中执行模型工具或向渲染进程暴露通用宿主权限 |

未来目录以产品设计 6.3 为准。S1 只建立基础模块，其他目录随对应阶段加入，不建大量空包。

### 2.3 运行形态和操作边界

S6 起，本地运行形态由 PostgreSQL、API、Worker、Electron 桌面客户端组成；既有 API/Worker/CAS/数据库及 S4 执行容器的启动和管理方式保持不变。API 与 Worker 共享既有数据根路径；桌面界面通过受限 IPC 到主进程，再调用回环 API，不直接访问数据库、CAS 或 Docker。CLI 管理命令继续本地调用服务，长任务提交到同一 Job 机制。

首版桌面包只包含 Electron、主进程/preload 与编译后的 React 静态资源；用户按既有开发说明准备并启动 Python API、Worker、PostgreSQL 和 Docker。桌面应用负责连接检查、错误提示和重连，不自动安装依赖、迁移数据库或启动/终止这些外部服务。桌面安装包与“一键安装全部后端环境”是不同交付范围；后者需另行设计，不通过变更 S1–S4 达成。

默认仅监听回环地址，使用本地身份令牌、严格 Origin/CORS 和同项目资源检查；CLI 本地管理拥有显式的本机操作者身份。此范围不是公网多租户服务。未来若开放远程访问，需单独补齐身份、租户授权、凭据加密和部署评估，不能只改监听地址后宣布支持。

工作区隔离覆盖受控文件操作，以及必选的 `terminal_exec / code_exec`。这两个工具必须在固定 Linux 执行容器中运行用户提供或 Agent 生成的命令/代码，禁止退回宿主进程直接执行；详细契约见第 3.5.1 节。Docker 容器隔离不等于能抵抗所有内核逃逸，首版范围是本机开发者使用，不包含公网恶意代码托管服务。Worker 的容器管理权限只用于后端固定操作，不提供给模型或执行容器；MCP stdio Server 的宿主进程权限仍在 S12 单独处理，不能复用本工具名绕过执行边界。

## 3. 必须先明确的数据与行为契约

### 3.1 ID、内容与编码

| 内容 | 决策 |
|---|---|
| Repository、Task、Run、Job、Approval | UUID；操作身份，不作为内容相同的依据 |
| Event | 数据库 BIGINT ID；Run 内连续 seq 从 1 开始；可移植引用同时保存 Run ID 和 seq |
| Commit、Object | 小写十六进制 SHA-256，固定 64 字符；持久化后不可原地修改 |
| 逻辑步骤/调用 | 稳定 `logical_step_id / call_index / attempt`；同参不同调用保留不同 occurrence |
| 内容 Schema | 必含 `schema_version`；不兼容版本明确拒绝，迁移创建新对象并保存来源 |
| 时间 | 存储 UTC，界面按用户时区展示；回放使用录制的逻辑时间，真实耗时独立计量 |
| 凭据 | 只保留不含秘密的 credential reference；禁止进入 Snapshot/Event/Blob/异常堆栈 |

`AGJSON-v1` 是首版受限编码协议，不声称实现全部 RFC 8785：UTF-8 无 BOM、无额外空白；object key 按 Unicode 码点序排序；array 保留顺序；字符串保留原 Unicode，双引号和反斜杠转义，退格/换页/换行/回车/制表使用 JSON 短转义，其余 U+0000–U+001F 用小写十六进制的六字符 Unicode 转义；正斜杠和其他有效 Unicode 不转义。禁止重复 key、孤立代理项、NaN、Infinity 与浮点数。整数范围为 ±9,007,199,254,740,991，以无多余前导零的十进制表示，负零编码为零；超过范围必须是有字段约定的十进制字符串。成本用整数微单位，时长用整数毫秒；二进制对象不经文本转换。

哈希输入固定为：UTF-8 编码的域标记 `agentgit`、codec 版本、对象类型、规范载荷字节长度；每个头字段后追加一个 NUL（包括长度字段），最后接原载荷字节。头字段只能来自受限枚举或十进制数字，不得包含 NUL。对象类型、版本或字节不同必须得到不同 hash；S1 制作黄金向量，S2 实现并验证。前端只消费服务器产生的 hash，避免两种排序实现分歧。

Commit manifest 包含 Schema/codec、Repository ID、有序 parent IDs、state object、environment manifest、事件清单摘要与逻辑事件范围。数据库行 ID、created_at、Branch HEAD、Worker 租约不入内容 hash。Run 与 Commit 的关联独立于 manifest，允许同一内容被多个运行引用。

Recorded 要求逻辑 state/产物 hash 一致；新 Run、Job、审批 ID、实际回放耗时、实际费用属于操作元数据，不计入这项比较。状态中的逻辑预算沿用历史消费记录，真实回放费用另记，不能把回放零费用写回原逻辑预算。完整 state 内使用逻辑调用引用，不嵌入本次恢复生成的操作 ID。

### 3.2 表、字段、索引与归属

以下为待实现的字段契约，不是可执行 DDL。

| 实体 / 引入阶段 | 必备字段 | 约束与查询路径 |
|---|---|---|
| `repositories` / S2 | id、name、created_at | 所有业务根对象归属 Repository |
| `objects` / S2 | id、codec、object_type、media_type、byte_length、storage_key | hash 唯一；长度非负；storage_key 只能由 hash 推导 |
| `object_refs` / S2 | repository_id、object_id、owner_type、owner_id、role | 唯一引用；API 必须通过所属实体验证访问权 |
| `branches` / S2→S4 | repository_id、name、head_commit_id、version；S4 增加 active_writer_run_id | 项目内名称唯一；HEAD 同项目；按预期 HEAD/version CAS；分支最多一个活动写入 Run |
| `commits / commit_parents` / S2 | manifest ref、state ref、environment ref；parent_id、parent_order | 同项目、有序、无环；索引覆盖父遍历；未支持 Merge 前最多单父 |
| `runs / run_commits` / S2 | repository_id、task_id（S3 加入）、branch、base_commit、mode、status、checkpoint、origin_run | 索引覆盖项目+时间、task+时间；Commit 关联有逻辑顺序 |
| `events` / S2 | run_id、seq、type、logical_step_id、payload_ref、caused_by 引用、recorded_at | 唯一 run+seq；因果引用不能指向未来或其他未授权项目 |
| `snapshots` / S2 | run_id、commit_id、event_seq、state_ref、schema_version | 与 Commit 的 state ref 一致；每次恢复校验对象 |
| `external_calls` / S2→S4 | run、logical call、attempt、origin_call_ref、provider/tool、schema/request digest、effect、intent/result refs、status | occurrence+attempt 唯一；记录 proposed/started/succeeded/failed/outcome_unknown；恢复沿用原逻辑动作的幂等身份 |
| `artifacts` / S2 | commit_id、relative_path、object_id、media_type、provenance_ref | commit+path 唯一；路径不得越界，不能覆盖其他 Run |
| `agent_configs` / S3 | id、repository_id、version、config_ref | 项目内版本唯一；更新创建新版本 |
| `tasks / task_nodes` / S3 | goal_ref、active_run、budget/policy refs；node_key、plan_version、deps、status、result_ref | Task DAG 每次改计划验证无环；历史计划保留 |
| `jobs` / S4 | kind、run_id、status、payload_ref、attempt、worker、lease_until、fencing_token、heartbeat、checkpoint | 状态+可领取时间索引；恢复不能仅凭心跳判定副作用未发生 |
| `sandbox_executions` / S4 | run、origin logical call、attempt、input/request/policy/image refs、container_name/id、fencing_token、deadline、status、receipt/output refs、cleanup_status | 执行 intent 先持久化；逻辑调用+attempt 唯一；容器名称/标签可核对归属；确认旧容器停止前不得为同一逻辑调用启动新 attempt |
| `approvals` / S4 | run、checkpoint、call_digest、policy_digest、expires_at、status、actor、decision、consumed_at | 精确动作绑定、一次性消费；重复相同决定幂等，冲突决定拒绝 |
| `idempotency_keys` / S4 | actor、route、key、request_digest、result_ref、job_id | 范围内唯一；同 key 异参返回冲突；持久化先于成功响应 |
| `experiments / experiment_trials` / S7 | checkpoint、validator/input/environment/policy refs、split_manifest；trial、run、group、seed/fixture | 验收配置不可变；trial 与 Run 一一关联 |
| `interventions / debug_reports` / S7–S8 | target commit/event、operator、payload/precondition/provenance refs；evidence、metrics、limitations | 目标与 checkpoint 关系可验证；跨项目引用拒绝 |
| `repair_searches / repair_proposals / repair_trials` / S9–S10 | failed_run、base、validator、budget、strategy；ordered patch digest、status；phase、run、result、usage | 搜索去重不等于复测试验去重；phase 含 screen/reduce/confirm/regression |
| `proposal_reviews` / S10 | proposal、patch/validator/report digests、expected config version、actor、decision | 批准生成新配置版本；并发变更需重新审查 |
| `merge_proposals / merge_conflicts` / S11 | base/ours/theirs、expected heads、candidate、evidence、validation、decision | 审查期间 HEAD 改变使批准失效；冲突解决记录不可覆盖 |

每个对象引用、父关系和 Run/Task 关系都验证同项目归属，不能只检查“这个 ID 存在”。优先使用复合 FK/唯一约束保证可表达的归属，其余由同一用例事务校验并有越权测试。迁移顺序先建对象和根实体，再加关系与必要 FK，避免循环建表依赖。

对象不可变不等于对象安全：私有输入和响应需要访问控制，移到 Blob 不能替代脱敏或加密。MVP 不提供完整原始私有轨迹的公共分享；若脱敏后缺少重放必要信息，返回 `replay_data_unavailable` 并说明原因，禁止声称已完整恢复。

### 3.3 原子提交和崩溃恢复顺序

一次状态转移按以下顺序执行：

1. 校验预期 HEAD、当前 Run 状态、预算和 Worker fencing token；外部调用前先持久化意图及可核对的调用身份。
2. 经 Adapter 得到结果，先持久化调用结果；模型/工具调用期间不持有长数据库事务。
3. 根据“原状态 + 已录制输入/结果”构造新逻辑状态、状态转移事件与 Commit manifest。
4. 将全部对象写同卷临时文件，校验 hash/长度，再原子发布最终 CAS 文件；对象写成功前不建立可见 Commit 引用。
5. 在一个短 PostgreSQL 事务内插入对象索引/引用、状态转移事件、Snapshot、Commit 和 Run 关联，按预期 HEAD+version 更新 Branch；任一步失败全部回滚。
6. 提交后通知查询端；通知丢失由事件游标补查，不影响实际完成结果。

调用 started/completed 事件是独立的耐久记录，不假装和外部世界同时提交。Commit 固定一段已存在调用事件及本次状态转移事件的内容清单。崩溃发生在“调用结果已保存、Commit 未生成”时，恢复应消费保存结果，而非再调用一次。已发布但无数据库引用的 CAS 文件保留为孤儿，GC 仅在维护模式、无活动写者、保留期后清理；MVP 默认关闭自动删除。

Worker 使用短租约、定期心跳、递增 fencing token。每次写入检查 token；租约到期后旧 Worker 不能推进 HEAD。fencing 只能阻止本地提交，不能保证外部系统撤销已经发送的动作；恢复所有未决写调用前先核对结果，不能凭新租约直接重发。

同一 Branch 最多绑定一个非终态写入 Run；暂停/等待审批仍保留该绑定，另一个任务要写同分支时返回冲突或要求先建分支。终止时释放，崩溃恢复时在事务内交接给恢复 Run。调用前检查分支绑定、预期 HEAD 和当前 fencing token，避免两次实际执行最后只剩一次 HEAD 提交。

### 3.4 生命周期、暂停和预算

主状态：`queued → planning → running → completed`；错误为 `failed`，预算耗尽为 `budget_exhausted`，明确取消为 `cancelled`。规划和执行可在安全边界转入 `paused`；需审批转入 `waiting_approval`；进程异常转入 `interrupted`；写调用结果不明转入 `outcome_unknown`。终态不可原地重启，重新尝试创建新 Run。

暂停/取消先记录请求，再在安全 checkpoint 落实。调用已经发送时保留 started 状态直至收到结果或判定未知；取消不表示外部动作被撤销。普通暂停在同一 Run 恢复；崩溃恢复先封存原 Run 的中断事实，创建关联原 Run 的恢复运行；历史 Fork 同样创建新 Run、新工作区和独立分支。

预算至少覆盖：最大步骤、重规划次数、模型/工具调用数、Token、费用、墙钟时间、单次响应大小和并发数。调用前按可验证上限预留，返回后结算；供应商不提供用量时记录 unknown 和保守估计，不能填 0。无法估计费用上限时禁止在声明严格货币预算的搜索中启动该调用。已发出调用可能在取消后继续计费，报告预留上限与实际结算差异，不宣称控制供应商账单的绝对硬上限。

### 3.5 工具、审批与文件安全

工具按 `READ_ONLY / IDEMPOTENT_WRITE / NON_IDEMPOTENT_WRITE / IRREVERSIBLE` 分类，由本地注册策略决定。模型说明和远端 annotations 只作信息，不赋予权限。

- D1 必须提供五个工具：`corpus_search`、`source_read`、`artifact_write`、`terminal_exec`、`code_exec`。前三个在 S3 实现，后两个在 S4 实现；前两个仅访问固定 fixture，产物写入与执行工具仅修改本 Run 授权的文件状态。
- 本地受控产物写入属于预先允许的能力；真实外部写入不进入 Demo，审批链以独立 fake effect server 验证。
- 审批绑定参数、工具签名、checkpoint、策略版本、有效期、操作者和 Run；审批后改参数/切分支/改策略均失效，重新申请。
- 只读失败按有限次数重试；幂等写仅在外部服务真实支持稳定幂等键时重试；非幂等写超时进入 `outcome_unknown`，由查询/人工核对后推进。
- Artifact 路径只允许相对路径，拒绝绝对路径、`..`、盘符、UNC、Windows ADS、保留设备名、大小写冲突和符号链接/目录联接越界；所有最终绝对路径必须仍位于本 Run 根目录。
- 外部资料标记为不可信输入。资料中的“忽略规则、发送文件”等文字不改变 Tool Registry、验收器和权限。

#### 3.5.1 必选终端与代码执行契约（S4）

**功能和输入：**以下为待实现字段，不是现有接口。工具通过原有 Tool Registry、预算和 Job 调用，不新增可绕过 Run/权限的通用宿主执行 API。

| 工具 | 必须支持 | 输入契约 |
|---|---|---|
| `terminal_exec` | POSIX Shell 命令、管道、重定向及工作区中的脚本；文件整理、文本处理、运行本地测试和临时仓库 Git 操作 | `command_ref`、相对 `cwd`、`stdin_ref`（可空）、`input_manifest_ref`、`timeout_ms`、固定 `execution_policy_ref`；容器内固定使用 `/bin/sh -c` |
| `code_exec` | Python 3.12 代码片段或工作区脚本，参数/标准输入、标准库计算、文件处理及生成产物 | `language=python`、`code_ref` 或相对 `script_path`（二选一）、`argv`、相对 `cwd`、`stdin_ref`（可空）、`input_manifest_ref`、`timeout_ms`、固定策略引用；直接调用容器内固定 Python 解释器 |

固定镜像必须包含 `/bin/sh`、Python 3.12、基础 POSIX 文件/文本工具、Git 和 `rg`，支持 Python 标准库 `unittest`；具体版本清单与镜像 digest 一起冻结。额外 Python 包在受审阅的镜像构建阶段锁定，运行时不自动 pip install。首版 `code_exec` 只承诺 Python；不支持的语言返回 `unsupported_language`。终端为有时间上限的非交互式调用，不提供交互 TTY、持久 Shell 会话或常驻后台服务。

**执行边界：**

1. 每个调用/attempt 创建全新容器，使用固定 digest、非 root 用户、只读根文件系统、禁用提权和全部额外 Linux capabilities，保留默认 seccomp；不使用 privileged、host PID/IPC/network 或设备直通。命令执行与可信输入/产物采集使用不同的固定非 root 身份，命令不可访问采集控制通道；控制进程、入口和返回状态不能由用户代码替换。Worker 通过固定参数数组调用容器管理接口，宿主不使用 Shell 拼接用户命令；模型无法指定镜像、启动参数、挂载或 Docker context。
2. 容器使用 `network=none`，不注入宿主环境、凭据、Docker socket、源码仓库、数据库目录或 CAS 目录，不挂载宿主读写路径。冻结验收器、答案标注及其他 Run 的数据不能作为代码输入；硬断言在容器外执行。MCP/A2A 在受控 Adapter 侧通信，不能为代码容器顺带开放网络。宿主 PowerShell/cmd、联网下载、部署和外部系统写入不属于这两个工具的首版能力。
3. 从当前 Run 授权的 `input_manifest_ref` 经有界数据流物化到容器 `/workspace`；只接受合法相对路径和普通文件。`/workspace` 与临时目录使用有大小上限的 tmpfs，内容计入容器内存预算，不在共享持久卷中积累。调用之间仅通过已保存的文件 manifest 传递状态，不恢复进程、Shell 变量或解释器内存。
4. 主命令普通退出后，先核验容器内已无命令执行身份的残留进程，再由独立采集身份在容器销毁前通过有界导出协议取得工作区文件快照；存在后台进程即失败并销毁，不边运行边导出。不能假定容器停止后 tmpfs 文件仍在。所有导出字节、路径和元数据均视为不可信；宿主逐项校验普通文件、路径、大小、数目、重复项和 hash，拒绝软/硬链接、设备、管道和越界归档项，不执行导出文件。导出中检测到文件持续变化、采集权限缺失或协议中断时，返回失败，不声称取得一致的最终工作区。
5. 适配器在导出后终止并确认整个容器停止，再将正常成功且完整校验的输出 manifest 提交到 S2 原子事务；文件增删改成为版本差异，后续工具从新 manifest 开始。失败、超时、取消或导出异常只保存有界诊断，不接受部分工作区为成功状态。发生异常时直接销毁容器，保留上一个安全 checkpoint。
6. 两个工具保守分类为 `NON_IDEMPOTENT_WRITE`，`effect_scope=sandbox`。Run 可预先授权固定的断网容器策略，在该策略内不逐条索取人工批准；修改代码/命令会生成新的调用摘要。申请超出既定资源上限或改变权限时必须先经策略检查；扩大宿主/网络权限不是一个“批准”动作就能启用的隐藏能力。需要精确审批的策略绑定代码/命令、argv、cwd、输入 manifest、镜像 digest、资源上限、checkpoint 和 Run。
7. 执行 intent 和稳定容器名称先落库，按 Run/逻辑调用/attempt 标记容器归属。固定镜像的可信 PID 1 独立维护调用截止时间，到期退出并结束整个容器进程命名空间；不能把超时保证只放在可能崩溃的 Worker 中。正常超时、取消和失去租约时后端管理面也须终止整个容器及后代进程，不能仅结束宿主 Docker CLI。Worker 重启先核对并清理自己管理的遗留容器；未确认旧执行停止时禁止重发，清理状态不明时保留待核对。已耐久保存的完整结果优先复用；否则仅在旧容器已停止、原输入不变且策略允许时创建新 attempt，并重新计入预算，不从半成品继续。

首版固定恢复策略为每个逻辑执行调用最多两次尝试（首次及最多一次新 attempt）。旧执行确认停止且缺少完整结果时，将“丢弃临时文件”决定、原调用身份和已消耗预算原子写入检查点；不补写成功回执，也不重置预算。新 attempt 再检查调用次数和剩余墙钟，继续使用原请求、输入 manifest、镜像与资源策略。清理暂不可确认时保持 `outcome_unknown`，Worker 对未清理容器至多每 30 秒重新领取清理租约；重查仍未确认时保留原待核对 Run，不创建新尝试。普通模型或外部写工具不能进入此恢复路径。清理证据属于操作审计，逻辑状态只记录稳定的已停止调用身份及输入/策略引用。

**默认资源上限：**墙钟 30 秒、策略硬上限 120 秒；有效超时取请求、策略和 Run/trial 剩余时间的最小值。每容器 CPU 1 核、内存 512 MiB、进程数 64；工作区 tmpfs 128 MiB、临时目录 32 MiB；单文件 8 MiB、输入/输出 manifest 各最多 32 MiB 和 1,000 个文件。单条命令最多 16 KiB、代码片段最多 64 KiB；stdout/stderr 各保留至多 1 MiB。超过日志上限仍有界排空或停止执行，必须标明截断，不把不完整输出当完整验证证据；禁止无上限 Docker 日志落盘。无关资源需求不得侵占既定确认/回归预算，限制调整生成新策略版本。

**结果与录制：**保存 `exit_code`、稳定 `error_code`、stdout/stderr 对象引用及截断标记、输入/输出 manifest、命令/代码原文引用、解释器/镜像/策略 digest、实际墙钟与可取得的资源计量、清理结果和完整性标记。无执行退出码时保留 null，不伪造 0；基础设施失败、非零退出、Python 异常、超时、取消、OOM、产物超限分别报告。容器 ID、实际资源计量和清理时间是操作元数据，不混入可重放逻辑状态。

稳定错误至少包括 `execution_failed`（非零退出，保留具体退出码与错误输出）、`execution_timeout`、`execution_cancelled`、`resource_limit`、`output_limit`、`export_invalid`、`sandbox_unavailable`、`unsupported_language` 和 `outcome_unknown`。未取得 OOM/资源限制证据时不能仅凭一个退出码宣称具体原因；清理未确认时不标记 clean，不宣称取消已经完成。

**重放与恢复：**Recorded 直接消费录制结果和输出 manifest，禁止创建容器、执行 Shell/Python 或访问 Docker 管理接口；删除执行运行时后仍须能回放齐全的录制。Live 从 checkpoint manifest 创建新的容器和预算，记录实际新结果，不保证随机/时间相关代码再次产生相同结果。缺镜像、引擎不可用、资源隔离能力不满足时返回 `sandbox_unavailable`，不能回退宿主执行，也不能因此跳过 D1 必选验收。

### 3.6 Replay、Diff 和实验的精确定义

| 操作 | 输入与行为 | 成功判定 / 失败方式 |
|---|---|---|
| Checkout | 选择分支或只读 Commit | 不执行 Runtime、不改变外部文件/原历史 |
| Fork | 同项目安全 checkpoint、唯一分支名 | 新分支起点等于该 Commit，工作区和授权独立 |
| Recorded Replay | source Run、from Commit、until Commit；起点状态 + 后续录制输入 | 到达终点的逻辑状态与产物一致；零 LLM/工具网络出口 |
| Live Replay | from Commit、新分支、配置变更、预算与效果策略 | 新 Run 按条件重执行；只断言隔离和策略，不要求输出相同 |
| Hybrid / S7 | 冻结调用选择规则、明确 miss 策略 | 干预后请求不匹配就停止或转受控模拟器；不套用旧答案 |
| Structural Diff | 两个 Commit，同项目，稳定字段路径 | JSON Pointer 的增删改、产物 hash、调用列表、共同祖先 |
| Behavioral Diff / S8 | 逻辑步骤、工具签名、依赖边 | 第一处可观测分歧；无法唯一对齐时保留歧义 |

Recorded 严格消费每个调用 occurrence，包括 LLM 响应、时间、随机值和工具结果；记录缺失、参数不符、环境版本不兼容分别报错。允许恢复历史产物到新的受控工作区；禁止调用真实网络、外部写工具和任何静默 live fallback。若起终点相同，合法结果是零步恢复，不宣称重执行了原运行。

D1 的 DAG 为单父历史。S11 遇到多个最佳共同祖先时返回 `ambiguous_merge_base` 并要求显式选择，不随意挑选一个或承诺递归合并。

## 4. 服务接口与测试资产的落点

### 4.1 统一接口清单

下列为将来的契约，不是已实现 API。所有长任务必须先耐久保存，再返回 202；读取资源按 Repository 归属校验。修改请求均带身份、幂等键和预期版本。

| 阶段 | 接口 | 必需输入 / 输出要点 |
|---|---|---|
| S4 | `POST /repositories`、`POST /agents`、`POST /tasks` | 项目、不可变配置、目标、输入/预算/策略引用 |
| S4 | `POST /tasks/{id}/runs` | config version、branch、budget；返回 job_id/run_id |
| S4 | `GET /runs/{id}`、`GET /jobs/{id}` | 状态、checkpoint、失败原因、控制请求状态、剩余预算 |
| S4 | `POST /runs/{id}/pause`、`resume`、`cancel` | 预期状态版本；resume 说明是否创建新 Run |
| S4 | `GET /approvals?run_id=...`、`POST /approvals/{id}/decision` | 动作摘要、有效期、精确决定、冲突处理 |
| S4 | `GET /runs/{id}/events?after_seq=...&limit=...` | 按 seq 游标分页、稳定排序、有界 limit |
| S4 | `GET /runs/{id}/artifacts`、`GET /artifacts/{id}/content` | 产物引用与受控下载，不接收任意磁盘路径 |
| S5 | `POST /branches`、`GET /repositories/{id}/branches` | 源 Commit、名称、预期版本；冲突返回 409 |
| S5 | `GET /repositories/{id}/commits`、`GET /commits/{id}` | 游标历史、parents、state、环境及证据 |
| S5 | `POST /replays` | mode、source Run、from/until 或 live 配置；参数按模式互斥校验 |
| S5 | `GET /diff?base=...&head=...` | 项目内只读比较；大对象返回摘要和分页差异 |
| S7 | `POST /experiments`、`GET /experiments/{id}` | checkpoint、groups、repeats、冻结 validator/split/budget/policy |
| S8 | `POST /debug-reports`、`GET /debug-reports/{id}` | failed Run、可选 reference Run、failure assertions、证据 |
| S9 | `POST /repair-searches`、`GET /repair-searches/{id}`、`POST /repair-searches/{id}/cancel` | 冻结条件、预算、候选、剩余额度、停止原因 |
| S10 | `GET /repair-proposals/{id}`、`POST /repair-proposals/{id}/approve` | 精确补丁、独立复测、适用环境、预期配置版本 |
| S11 | `POST /merge-proposals`、`validate`、`approve` | 三方版本、无外部动作验证、批准前 HEAD 重查 |

领域错误至少区分 validation、not_found、forbidden、version_conflict、replay_mismatch、recording_missing、budget_exhausted、outcome_unknown、unsupported_schema；HTTP 层给稳定 code、message、证据引用和 correlation ID，不暴露堆栈或秘密。跨项目资源对无权操作者返回一致的不可访问结果，避免 ID 枚举。

### 4.2 CLI 范围

S4 提供 init/config/task/run/status/pause/resume/cancel/artifacts/approvals；S5 提供 log/branch/checkout/replay/diff；S7–S10 逐步提供 experiment/debug/repair search/report/review。交互模式之外必须能输出稳定 JSON 和非零错误退出码；CLI 与 API 复用同一服务层。

开发者初始化源码仓库使用普通 Git；未来 `agentgit init` 创建的是执行历史 Repository，二者不是同一仓库。本轮仅执行前者，不生成未来 CLI。

### 4.3 固定测试资产

| 资产 | 内容和用途 | 保管原则 |
|---|---|---|
| `tests/fixtures/canonical/` | 编码合法/非法输入、预期字节、对象/Commit hash 黄金向量 | S1 冻结，变更必须升级 codec 或明确修正测试依据 |
| `tests/fixtures/research/` | 3 个虚构框架，各 2 份资料；source ID、URL、采集/有效时间、正文 hash；固定 evaluation_time | 明示为合成资料；不依赖真实网站变动 |
| `tests/fixtures/recordings/` | 完整 Run、重复同参调用、缺响应、签名不匹配、含 fake effect 的录制 | 不含真实 Key/私有用户信息 |
| `tests/fixtures/execution/` | 命令/脚本、Python 计算与异常、预期文件 manifest、超时/资源超限/路径与输出攻击样例 | 全部在测试执行容器内运行，不在宿主执行 fixture；固定镜像和预置依赖 |
| `tests/fixtures/faults/` | 20 个故障：过时来源、空响应、Schema 错误、超时、计划遗漏、错误事实等 | 标注影响路径和可干预决策集合，不只标注首个错误事件 |
| `tests/fixtures/repairs/` | 30 个案例：10 单点、8 联合、4 伪修复、4 无可行修复、4 副作用约束 | 每例声明允许操作、已知解或有限空间无解证明 |
| `tests/fixtures/merge/` | S11 的 30 组：10 无冲突、10 事实冲突、10 计划/约束冲突 | 每组保存人工预期和证据，含副作用阻断 |
| Fake effect server | 维护收到请求数、幂等命中、断连前后是否落地 | 仅测试用；通过请求计数证明“没有真实动作” |

20 个定位样例按任务/模板家族分为 12 开发 + 8 保留；30 个修复样例按五类别分为 20 开发 + 10 保留（各类分别为 6/4、6/2、3/1、3/1、2/2）。同一模板的轻微改写不跨集合，防止泄漏。标注属于评估数据，搜索器不能读取答案。

每个修复任务还区分 screen/reduce 使用的响应样本与 confirm 使用的独立样本；这是试验层隔离，与“任务层 20/10 划分”同时存在。保留结果查看后不得继续调参并称为独立验证；调参后需冻结新版本和新的保留集。

## 5. 分阶段任务、测试与退出标准

每阶段按“契约确认 → 最小实现 → 对应测试 → 验收证据”执行。下列测试 ID 是必须创建的行为用例，不是已有测试文件。证据记录到未来 `doc/acceptance/Sxx.md`：源码版本、环境/Schema、测试命令与退出结果、报告/日志 hash、已知限制。阻断用例失败时不得把依赖阶段标为完成。

### S0：仓库初始化与方案定稿（当前轮）

**目标：**建立可审阅的文档基线，保持项目无源码。

**执行：**阅读规范和设计；初始化本地 Git，默认分支 main；添加最小 `.gitignore / .gitattributes`；设计与实施方案以 v1.0.0 命名并放入 `doc/`，界面规范遵循现有根目录 DESIGN.md；README 只说明项目目标。按用户要求将当前文档与 Git 基础规则归档为首次本地提交，说明采用 Conventional Commits 格式与中文摘要，沿用已有身份及签名设置。不建立远程、不推送、不创建程序空壳，也不替用户设置全局 Git 身份。

| 测试 ID | 操作 / 检查 | 预期 |
|---|---|---|
| S0-T01 | 检查 Git 顶层、HEAD、首次归档提交和远程 | 顶层为当前目录，分支 main，远程为空；首次提交仅含文档与 Git 基础规则，提交后工作区干净 |
| S0-T02 | 枚举工作文件 | 只有 Markdown 与 Git 基础规则，无源码/安装产物 |
| S0-T03 | 核验所有本地文档链接和阶段名称 | 链接存在；S0–S12、D1/D2/D3 边界一致，S12 四个子项均为必选 |
| S0-T04 | 比较界面规范迁移前后的内容 | 除文档版本号外，设计规则与原始基线一致 |
| S0-T05 | 验证忽略规则 | 凭据、运行数据、缓存、依赖目录被忽略；文档和未来 lockfile 不被误忽略 |

**退出：**以上检查通过；文档版本和目录统一，README 仅保留项目目标，首次本地归档提交完成。源码开发、远程推送和部署不属于当前交付。

### S1：工程环境、Schema 与测试基线

**前置：**S0 通过；用户进入实现阶段。

**目标：**使后续代码只有一套可验证的数据语言和本地开发入口。

**实施顺序：**

1. 建立单 Python 包、基础模块、CLI/API 入口及后端依赖锁定；记录 Python/数据库版本与 Windows 路径要求。
2. 只配置 PostgreSQL 开发服务，区分开发/测试数据库；验证数据库不可用时明确报错。
3. 定义核心 Schema、状态枚举、错误 code、目录/凭据配置；禁止未知字段被默默丢弃。
4. 冻结 AGJSON-v1、Commit manifest、环境 manifest 与调用 occurrence 的黄金向量。
5. 建立单元/真实 PostgreSQL 集成测试入口、格式/静态检查和最小 CI；CI 从第一阶段开始维护，不留到项目末尾。
6. 建立 Alembic 初始迁移框架、验收记录模板；前端和研究算法暂不进入。
7. 将 Docker Linux 容器引擎、隔离能力和执行镜像准备方式列入开发/CI 环境检查；S1 只明确前提与错误，不提前实现执行工具。

**预期交付：**`pyproject.toml` 及锁文件、`src/agentgit/core/` 基础契约、最小入口、`migrations/`、`compose.yaml`、`tests/fixtures/canonical/`、开发说明与 S1 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S1-T01 | 全新环境按说明准备并启动 | 入口可执行、健康检查可用、版本可记录 |
| S1-T02 | 全新测试库迁移到 head，再回退初始版本并重做 | Schema 一致，无手工建表步骤；仅在可丢弃测试库回退 |
| S1-T03 | key 顺序不同、数组顺序不同、原始 Unicode 不同 | 前者预期相同编码；后两者依协议区分 |
| S1-T04 | 重复 key、非法 Unicode、浮点、超范围整数、未知 Schema | 全部有稳定错误，无隐式修复 |
| S1-T05 | 开发/测试数据路径配置相同 | 测试启动保护拒绝，不能清理开发数据 |
| S1-T06 | 检查执行容器前提，或使用缺引擎/错误容器模式的环境 | 清楚列出 Linux 容器、资源限制与镜像准备条件；不声称执行能力已可用 |

**退出：**新机器可按文档准备；Schema 与黄金向量评审完成；CI 运行基础校验。此时不宣称 Agent 已可执行任务。

### S2：对象存储、事件和版本内核

**前置：**S1。

**目标：**能耐久提交并恢复一个完整状态，且并发更新不会覆盖别人历史。

**实施顺序：**

1. 实现 CAS put/get/verify、同卷临时发布、不可变覆盖保护；限制单对象大小。
2. 建立第 3.2 节 S2 表、FK/索引、Run/Event Repository；事件 seq 在短事务内分配。
3. 实现完整 state 和 environment 对象、Snapshot、Commit manifest 与单父关系验证。
4. 实现原子提交服务，HEAD 同时比较 expected head 与 version；失去租约检查接口为 S4 预留。
5. 增加只读 restore/log 和 Artifact manifest 读取；验证内容损坏、对象缺失。
6. 仅实现孤儿扫描与 GC dry-run；删除需维护模式且后续恢复测试通过，默认关闭自动 GC。

**预期交付：**`core/`、`storage/`、原子提交用例、基础迁移、CAS/事务/归属测试、S2 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S2-T01 | 重复存相同对象；改变类型或一个字节 | 相同对象复用；类型/字节变化 hash 不同；与黄金向量一致 |
| S2-T02 | 两数据库连接同时按同一个 HEAD/version 提交 | 恰有一个成功；另一个冲突；事件/Commit 无半提交 |
| S2-T03 | 分别在临时文件、最终 CAS 发布、DB 事务中、DB 提交后杀进程 | 可见历史始终完整；最多有可识别孤儿，无丢失被引用对象 |
| S2-T04 | 不存在/异项目 parent、重复 parent_order、构造非法父关系 | 拒绝；既有 DAG 和 HEAD 不变 |
| S2-T05 | 重启后 restore；手工损坏测试对象 | 正常状态 hash 一致；损坏明确失败，不继续 Runtime |
| S2-T06 | 两项目相同内容 hash；通过另一项目实体下载 | 物理去重可用，业务访问被拒绝，不能仅凭 hash 下载 |
| S2-T07 | GC dry-run，包含分支、Run、审批预留引用 | 所有可达对象保留；活动写入期间拒绝执行删除 |

**退出：**全部不变量用例通过，尤其崩溃点和 CAS 竞争；之后才能依赖内核实现恢复。

### S3：可完成任务的离线单 Agent

**前置：**S2。

**目标：**不用互联网即可生成带可追溯来源的研究报告，并耐久记录每一步。

**实施顺序：**

1. 建立固定离线语料与 Mock LLM，生成受 Schema 限制的 plan、tool call 或 final response。
2. 实现 Task DAG、节点前置条件、串行调度和最多 2 次重规划；非法计划明确失败。
3. 实现 `corpus_search / source_read / artifact_write` 三个基础工具、参数和输出验证、Artifact Manager；来源在 working memory 中保留证据。必选终端与代码执行在 S4 加入，不以 S3 的三工具闭环代替 D1 完成。
4. 将每次完整状态转移接入 S2 提交，不允许 Runtime 直接改历史对象。
5. 实现短期上下文、结构化记忆、上下文大小上限；达到上限明确停止，首版不自动做有损总结。
6. 实现步骤/调用/时间预算、重复无进展检测、确定性验收器和终态报告。

**预期交付：**`runtime/`、基础 `adapters/`、Task/配置迁移、离线示例、状态机测试、S3 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S3-T01 | 固定任务“比较三个 fixture 框架” | 完成 report.md/sources.json；三框架齐全，每条要求结论有来源 |
| S3-T02 | 依赖环、缺节点、提前执行后继任务 | 计划被拒绝；不调用工具、不写成功产物 |
| S3-T03 | 工具空响应、错误 JSON、非法参数、模型空响应 | 有类型错误事件；按策略最多重试/重规划，不能无限循环 |
| S3-T04 | 来源失效 fixture、缺证据事实 | 验收失败；不可用流畅文字覆盖硬断言 |
| S3-T05 | 重复无进展调用、max_steps=3、超大小输出 | 分别以 loop_detected / budget_exhausted / output_limit 停止 |
| S3-T06 | 同一 fixture 运行两次并读取全部 Commit | 逻辑结果与预期一致；每步可恢复，Run/工作区相互独立 |

**退出：**离线正常任务、异常任务都闭环；所有转移有事件与状态 hash；不提前连接 MCP 或通用浏览器工具。

### S4：持久化调度、恢复、审批、必选执行工具和服务入口

**前置：**S3；这是把可运行 Demo 变成可靠服务的阶段。

**目标：**API/CLI 可管理任务，Worker 崩溃不会丢历史或盲目重发写动作；`terminal_exec / code_exec` 可在隔离容器内完成真实命令和 Python 代码执行。

**实施顺序：**

1. 建立 PostgreSQL Job、幂等请求表、单 Worker 领取/心跳/租约/fencing；领取事务完成后再执行网络调用。
2. 落实第 3.4 节生命周期，区别“暂停请求已接受”和“已经暂停”；崩溃从最后安全 checkpoint 新建恢复 Run。
3. 实现 Tool Policy、精确审批绑定、一次性消费和 fake effect server；未知结果进入待核对状态。
4. 实现第 4.1 节 S4 API 和 CLI；分页、身份、Origin/CORS、项目边界和错误映射同时接入。
5. 实现请求幂等、状态版本冲突、下载路径保护、结构化日志和最小运行指标。
6. 注入明确的失败点，验证“结果已保存但 Commit 未保存”的恢复；补充操作手册。
7. 冻结第 3.5.1 节执行 Schema、镜像 digest、工具/依赖清单、资源策略和工作区 manifest；建立 `sandbox_executions` 迁移、容器管理与有界输入/产物传输，先验证拒绝路径和实际隔离能力。
8. 实现 `terminal_exec` 的 Shell/管道/重定向与 `code_exec` 的 Python 片段/脚本/argv/stdin，统一退出结果、错误和 stdout/stderr 记录；接入既有 Tool Registry、预算和策略。
9. 实现输出完整性与文件校验、正常成功后的原子 manifest 更新、整容器终止与遗留清理；注入 Worker 崩溃、半导出、资源耗尽和后台子进程故障，确认不存在宿主执行回退。

**预期交付：**`services/`、`api/`、`cli/`、`adapters/execution/`、Worker 入口、执行镜像构建说明/锁定清单、新迁移、执行/隔离/审批/恢复测试与 S4 验收记录。容器配置及 fixture 程序仅在进入实现阶段后创建，本轮不生成。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S4-T01 | 提交任务后立刻重启 API；Worker 在步骤边界被杀 | Job 仍存在；新 Run 从耐久状态恢复，已保存调用结果不重发 |
| S4-T02 | Worker A 租约过期，B 接管，A 迟到提交；两任务写同分支 | A 被 fencing 拒绝；每个分支仅一个活动写入 Run，竞争任务在调用前拒绝 |
| S4-T03 | 同幂等键同请求重复提交；同 key 改参数 | 前者同 Job/Run；后者 409，不能创建第二次动作 |
| S4-T04 | 审批前、批准后、拒绝后、超时后调用 fake effect | 计数分别为 0、1、0、0；改参数或 checkpoint 后旧审批无效 |
| S4-T05 | fake 写服务先落地再断开连接 | 进入 outcome_unknown，不自动重发；核对后继续；总动作数 1 |
| S4-T06 | 请求暂停/取消时工具仍在运行 | 显示 requested；到安全点生效；不虚构外部动作已撤销 |
| S4-T07 | 无令牌、错误 Origin、异项目 ID、路径穿越/联接 | 无数据泄漏和越界写；稳定错误，无密钥堆栈 |
| S4-T08 | 查询事件分页并重连、下载产物 | 无重复/遗漏 seq；文件 hash 与 manifest 一致 |
| S4-T09 | 终端调用文件/文本工具、管道、重定向、工作区脚本和临时仓库 Git；注入非零退出与不存在命令 | 命令在容器中实际执行，输出和工作区变化正确；非零退出及缺命令可区分，无宿主命令执行 |
| S4-T10 | Python 片段/脚本接收 argv/stdin、读取 JSON 并生成统计文件；注入语法/运行异常与不支持语言 | 正常结果可核验；stdout/stderr、错误和退出码完整；不支持语言在启动前拒绝 |
| S4-T11 | 两个 Run 同路径写入；输入/导出含路径穿越、Windows 特殊路径、软/硬链接与设备项 | Run 文件状态隔离；非法输入/导出拒绝，宿主保护文件和其他 Run 不变 |
| S4-T12 | 尝试访问外网/宿主服务、数据库、Docker socket、宿主凭据；检查运行身份和容器参数 | 无外部连通与秘密暴露；固定镜像、非 root、只读根、无提权/宿主挂载，禁止模型覆盖策略 |
| S4-T13 | 分别触发内存、进程数、工作区容量上限并运行 CPU 密集任务 | 内存/进程/容量限制实际生效，CPU 限额有效；有界失败或超时，不持续消耗宿主资源 |
| S4-T14 | 连续产生超限 stdout/stderr、非法 UTF-8 或大量输出文件 | 有界捕获与明确截断/超限标记；原始字节安全保存，文件总量限制有效，无无限日志落盘 |
| S4-T15 | 超时命令派生后台/脱离进程组的子进程 | 管理面终止整个容器；确认无遗留执行后返回 execution_timeout，未提交部分工作区 |
| S4-T16 | 执行中取消、杀 Worker、租约过期；完整结果保存后在 Commit 前崩溃 | Worker 停止后容器仍按独立截止时间结束；控制/清理状态真实，旧写者被拒绝；完整结果复用，无重复启动 |
| S4-T17 | Docker 不可用、固定镜像缺失、Linux/资源隔离能力不满足 | 返回 sandbox_unavailable；无自动拉镜像、宿主 fallback 或伪造成功；D1 验收保持未完成 |
| S4-T18 | 预授权断网容器策略内执行；超限资源、改镜像/代码/参数/输入后复用旧审批 | 合法预授权调用不逐条要求确认；越界请求拒绝，需审批场景严格绑定新摘要，无权限提升 |
| S4-T19 | 正常调用增删改文件后继续下一工具；非零退出、取消或导出非法文件后继续 | 仅完整成功 manifest 成为下一步输入；失败保留旧 checkpoint，诊断产物不冒充成功工作区 |
| S4-T20 | 代码尝试终止采集进程或伪造成功标记；注入采集进程消失、残留子进程、导出变更或协议中断 | 命令不能控制独立采集身份/通道；无完整证据的故障返回失败并清理，不接受 stdout 自报成功或半产物 |

**退出：**崩溃、审批、未知结果和 S4-T09–T20 的真实容器测试全部通过；两个执行工具均可实际调用，HTTP 重复请求不能变成重复执行。只有 Mock 执行器通过不算本阶段完成。此门槛失败时不能开放 live 分支实验，也不能把执行工具推迟为可选项。

### S5：Branch、Replay 和 Structural Diff

**前置：**S4。

**目标：**能从历史节点开展隔离分支，严格回放旧执行并解释客观差异。

**实施顺序：**

1. 实现 Branch/Fork、只读 Checkout 和新工作区物化；只允许安全 checkpoint。
2. 定义 Recorded 录制范围解析和调用 occurrence 匹配，构造完全没有真实网络客户端的 RecordedAdapter。
3. 重建逻辑时间/随机输入和产物，比较每个目标状态 hash；保留 provenance 和独立运行计量。
4. 实现 Live Adapter，变化配置先验证，非只读动作只可 sandbox/dry-run/精确审批；不复用原动作授权。
5. 实现 JSON Pointer 增删改、产物/调用差异、单父共同祖先和分页输出。
6. 接入 S5 API/CLI，并增加环境不兼容、缺记录、双分支同名冲突的可解释错误。
7. 扩展执行工具录制：恢复代码/命令响应、日志引用和输出 manifest；Recorded 路径不能初始化容器后端，Live 每次从版本化文件状态创建新容器。

**预期交付：**`replay/`、`diff/`、分支服务、录制 fixtures 和 S5 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S5-T01 | 关闭测试进程网络，从 C1 Recorded 到 C4 | state/产物 hash 按历史一致；LLM 和工具网络调用计数均 0 |
| S5-T02 | 同工具同参数连续两次，录制响应不同 | 严格按 occurrence 分别取响应，不按 hash 混用 |
| S5-T03 | 删除一个响应、改请求、改工具签名、改变环境版本 | 对应明确错误；不 fallback live，不返回成功 |
| S5-T04 | Recorded 指定 override；from/until 非同条来源路径 | 参数拒绝；原分支与历史不变 |
| S5-T05 | A/B 从同一点创建，分别写同名产物和修改计划 | 只改变各自状态/文件/HEAD；原分支 hash 保持 |
| S5-T06 | 录制含 fake 外部写；Live 继承原审批 | Recorded 不访问 fake server；Live 必须重新取得授权 |
| S5-T07 | 预置字段增删改、数组重排、二进制产物、无共同祖先 | Diff 与期望一致；无祖先明确展示，不能误给 LCA |
| S5-T08 | 完整录制终端/Python 调用后关闭容器引擎连接，或在测试进程中禁止构造/访问执行客户端，执行 Recorded；再删除一项执行响应或产物对象 | 齐全时输出/manifest hash 一致，容器管理和 Shell/Python 启动计数均 0；缺数据明确失败，无 Live 回退 |
| S5-T09 | 从执行前 checkpoint 建立 A/B Live 分支，修改命令/代码并生成同名文件 | 全新容器使用各自输入和预算，差异可追查；原分支不变，旧进程/临时文件/审批不被继承 |

T08 允许执行客户端禁用注入，是为了在测试数据库与 Docker 共宿主时保留数据库；它必须在任意执行管理或 Shell/Python 启动入口被触及时立即失败，不能以成功 Mock 响应代替，也不能宣称实际关闭了宿主引擎。来源仍须真正执行终端与 Python；零调用和缺数据失败的期望不变。

**退出：**零网络录制回放、分支隔离和错配阻断全部通过；“状态可恢复”和“模型重新答对”分别呈现。

### S6：Electron 桌面应用、四页界面、真实模型适配与 D1 验收

**前置：**S5；页面只消费已验证服务。

**目标：**用户能通过已安装的 Electron 桌面应用完成执行、查历史、Fork、Replay 和 Diff；交付 Windows x64 安装包与便携压缩包，在后端环境已准备的机器上无需 Node.js 或前端开发服务器即可使用。离线 Demo 实际使用五个内置工具，其中终端命令与 Python 执行必须有成功案例。

**实施顺序：**

1. 阅读根目录 DESIGN.md，抽取已有 token；先准备 Runs、Timeline、Branch Experiment、Diff 的字段和交互清单。
2. 建立 `desktop/` 下的 Electron + React/TypeScript 工程及锁文件，分离 `main / preload / renderer / shared`，使用既有颜色/字体替代策略/圆角/断点。锁定 Electron、构建工具、Electron Forge、Windows maker 与兼容的构建用 Node.js 版本，记录开发、检查、打包、安装命令，不新增视觉体系。
3. Runs 提交目标/预算并展示终态；Timeline 展示 DAG、事件、状态、来源、产物和审批/暂停控件，并展示执行命令/代码、stdout/stderr、退出码、截断标记、耗时及文件变更；沿用现有详情区，不另增终端页面。
4. Branch Experiment 明确标注 Recorded/Live，显示起止点与权限；Diff 展示客观差异及原始证据。
5. 采用游标轮询和页面退出取消请求；处理 loading/empty/error/断连/过期审批/版本冲突，防止双击重复提交。
6. 实现一个真实 LLM Adapter：参数/结构化输出校验、超时、有界重试、usage、供应商签名；仍只搭配受控工具。
7. 做 Electron 真实进程端到端、窗口缩放/键盘检查，完成桌面启动说明和演示步骤；开发服务器中的页面测试仅作补充，真实模型冒烟单列结果。
8. 扩展固定研究任务：读取本地资料和结构化数据，调用 `code_exec` 计算统计/生成文件，再调用 `terminal_exec` 整理产物或运行固定本地测试；硬断言在容器外检查预期结果，代码容器不能修改验收器或答案数据。
9. 按下述桌面边界实现主进程 API 客户端、显式 IPC 白名单和受控产物导出；验证运行包中静态资源、字体与路由无需外网，不在渲染进程中提供 Shell、Python、文件系统或 Docker 接口。
10. 使用 Electron Forge 生成 Windows x64 安装程序和便携 ZIP，记录版本、架构、依赖清单、校验摘要及签名状态。在独立 Windows 用户环境完成安装、启动、完整操作、同数据版本覆盖升级与卸载验收；应用升级不自动迁移后端 Schema，跨后端版本须另走既有升级流程。本阶段不包含公开发布或自动更新，macOS/Linux 不列为已支持平台。

**桌面职责与边界：**

| 部分 | 明确实现 | 验收约束 |
|---|---|---|
| 主进程 | 窗口、单实例、连接配置、固定 API 调用、原生保存对话框 | API 地址仅允许显式配置的回环 HTTP 端点；方法、路由、请求头与请求/响应大小按白名单约束；拒绝重定向，不提供任意 URL 代理 |
| preload / IPC | `contextBridge` 暴露类型化业务方法，主进程验证 sender、顶层 frame、参数与项目上下文 | 不暴露原始 `ipcRenderer`、任意 channel/路径/命令；只接受本应用受信任窗口和文档，子 frame、其他窗口和跳转页面不能借用权限 |
| 渲染进程 | React 四页、React Flow、查询轮询、状态与错误呈现 | `nodeIntegration=false`、`contextIsolation=true`、`sandbox=true`、保留 `webSecurity`；生产加载包内自定义协议资源并设置 CSP，阻止非白名单导航/新窗口，权限请求默认拒绝 |
| 身份与 API | 主进程从操作者预置的 `AGENTGIT_API_TOKEN` 读取令牌并附加认证头；界面只获取连接状态 | 令牌不通过 IPC 下发，不写前端存储、日志或安装包；固定回环请求不携带渲染页面 Origin，保持既有 Host/认证/项目/幂等校验，不扩展后端 CORS 或允许 `null` Origin |
| 产物导出 | 按产物 ID 走现有受控下载，主进程弹出保存对话框并有界写入用户选择的目标 | 渲染进程不能指定任意宿主路径；外部资料、Markdown、HTML 和终端输出作为不可信数据展示，不能当作本地可执行页面或 IPC 指令 |
| 生命周期与数据 | 保存非敏感窗口/连接偏好到 Electron 用户数据目录；断连后以 Job/Run 和事件游标恢复展示 | 桌面退出不代表任务取消，不关闭外部 API/Worker/PostgreSQL/Docker；重开不重发修改请求，已有 CAS/数据库不迁移到安装目录，卸载保留业务数据 |

首次连接失败应区分 API 不可达、未就绪、认证失败、版本不兼容及执行环境未准备。连接面板放在既有窗口内，不增加业务页面。应用只根据既有探测与业务结果提示依赖状态，不能把 API 存活当作 Worker 或 Docker 已就绪；缺少可靠探测时显示未知或任务尚未取得进展，不推断具体服务已停止。服务启动命令和令牌预置方法在 D1 操作说明中给出。Electron 负责展示与桌面集成，Agent 的终端/Python 仍只由 S4 的后端固定容器执行。

**预期交付：**`desktop/`、真实模型适配器、Electron 端到端与 IPC 测试、Windows x64 安装包/便携包和构建清单、D1 桌面操作说明及 S6 验收记录。交付包不包含凭据、数据库、CAS、Run 产物或开发机配置；缺签名证书时如实标注未签名，不能把构建成功等同于通过正式分发信任验证。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S6-T01 | 从已安装桌面包创建任务→完成→历史→Fork→Recorded/Live→Diff | 全流程可操作，节点可追溯，原历史未改，产物经原生对话框受控保存；无需前端开发服务器 |
| S6-T02 | 连续点击提交/审批、API 断连后重连、Run 终态后重开桌面窗口 | 不重复创建动作；游标补齐；终态与 API 一致 |
| S6-T03 | 768/1024/1440px 窗口、100%/150%/200% DPI、长中文与长 JSON/表格；另测 360px 渲染视口 | 桌面最小内容宽度为 768px；遵守 DESIGN 断点，无裁切/意外横向溢出，仅图/代码容器滚动；360px 为补充布局测试，不声称移动端交付 |
| S6-T04 | 全键盘切换/聚焦/打开详情，状态只靠颜色的情况 | 控件可操作、有名称和文本状态；焦点按 DESIGN 可见 |
| S6-T05 | 无外部模型 API Key 且禁止外网，本地 API 身份令牌已配置 | 离线 Demo 和全部确定性测试仍通过 |
| S6-T06 | 明确开启真实模型冒烟，执行固定小任务 | Adapter 错误/usage 可追踪；报告实际结果，不作为离线必过项 |
| S6-T07 | 离线任务实际调用五个工具，桌面查看命令/代码与产物，再 Fork 和 Recorded | 执行工具均产生可核验结果，错误/截断状态可见；修订文件差异正确，回放不启动任何执行容器 |
| S6-T08 | 尝试从渲染进程访问 Node/fs/child_process；伪造 IPC sender/frame/channel/参数；注入 HTML/脚本/导航/新窗口 | 宿主能力不可访问，未授权 IPC 和导航拒绝，注入内容不执行；不能借 Electron 执行 Agent 命令或修改后端文件 |
| S6-T09 | 错误令牌、跨项目 IPC、任意 URL/端口/路由、重定向；检查渲染存储、IPC 返回、日志与安装包 | API 认证及项目检查保持有效；越界请求拒绝，令牌不泄露；无需放宽既有 CORS、Origin 或 S4 安全配置 |
| S6-T10 | 独立 Windows x64 用户环境中安装和运行安装包/便携包，移除开发源码及前端构建用 Node.js，禁止外网 | 包内资源/字体/路由可用；已准备的后端可连接，D1 全流程完成；记录产物版本/摘要/签名状态，不依赖 Vite 等开发服务器 |
| S6-T11 | API 未启动、数据库未就绪、Worker 停止、Docker/固定镜像缺失、版本不兼容 | 按真实探测或请求结果给出明确状态与操作说明，不假报就绪、不自动安装或更改后端；环境未满足时不能标记五工具验收通过 |
| S6-T12 | 执行中关闭/重开桌面、渲染进程崩溃、重复启动、同数据版本覆盖升级及卸载 | 单实例行为明确；查询恢复且不重复提交，不杀外部服务；升级可重新连接，卸载后数据库/CAS/业务产物仍完整 |

**退出：**S6-T01–T12 全部通过（T06 真实模型冒烟单列实际结果），D1 离线端到端及五工具真实验收通过；安装包和便携包均能运行四页核心流程，IPC/凭据边界、生命周期和安装证据完整。仅开发模式或浏览器演示通过不能算完成。字体只能用已有授权资源或 DESIGN 列出的替代字体，并随包提供授权允许的本地资源。若已有 token 在实际 UI 的对比度检测不达标，先记录并同步修订 DESIGN，禁止悄悄改色或宣称已达标。

### S7：受控实验、冻结验收器和 Hybrid

**前置：**D1；不依赖诊断/搜索实现。按第 1.2 节用户授权的临时顺序调整，当前可先开发、测试 S7，S6/D1 的剩余验收仍单列待补。

**目标：**同一 checkpoint 的 A/B 实验可重复、可比较、可解释。

**实施顺序：**

1. 定义只读 validator manifest：规则、输入、固定评估时间、fixture split、代码版本与 digest。
2. 实现 experiment/trial 数据和 Runner；每个 trial 都有新 Run、独立工作区、明确预算和环境。
3. 实现 `PROMPT_PATCH / PLAN_PATCH / TOOL_RESPONSE_PATCH` 基础干预及前置条件校验，不做自动候选生成。
4. 实现 Hybrid 精确匹配和明确的 miss 行为；变更后的模型调用必须由 Mock/实际模型重新产生，不回填旧答案。
5. 同 checkpoint 运行 A/B 各 5 次；统计成功数、硬约束通过率、Token、时延、实际/估计费用与方差。
6. Branch Experiment 增加实验 manifest、分组和 trial 列表；支持取消和恢复未完成 Job。

**预期交付：**`experiments/`、实验迁移、冻结验证器、实验 UI 扩展和 S7 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S7-T01 | A/B 各 5 个固定 trial | 恰有 10 个独立 Run；共同起点相同，其余差异可列举 |
| S7-T02 | Patch 尝试改 validator/权限/测试 split | 全部拒绝；验收器 digest 始终不变 |
| S7-T03 | 干预使原调用参数改变，Hybrid 录制不命中 | 按冻结策略停止或调用受控模拟器；不能套用旧输出 |
| S7-T04 | 替换工具响应后生成成功报告 | 显示 synthetic observation 和 test_only；不当作实时世界修复 |
| S7-T05 | 预算不足、取消、Worker 中断后恢复 | trial 状态与真实执行一致，未完成不可计为通过 |
| S7-T06 | 同 fixture 重跑实验、对照实际 usage | 确定性指标可复核；真实模型波动、未知费用如实披露 |

**退出：**冻结、隔离和匹配测试全部通过；所有实验报告列明控制变量和残余不确定性。

### S8：行为对齐与故障候选定位

**前置：**S7。

**目标：**从失败断言追踪相关决策，并提供有实验依据的候选排序。

**实施顺序：**

1. 用 logical step、工具签名、规范请求和 caused_by 建立依赖图；不按时间戳直接对齐。
2. 先做规则排序：失败依赖路径优先、距离和首个分歧为次序；不引入不透明的 LLM 根因分数。
3. 将“注入位置、首个可见错误、可修复决策点”分别标注，允许多个合理候选。
4. 通过 S7 单变量干预获取效果证据；没有足够预算时只显示静态候选，不假装完成因果验证。
5. 建立 20 个故障样例和时间首异常基线，冻结 12/8 划分；生成 Debug Report。
6. Diff/Timeline 增加候选、证据跳转和对齐歧义说明。

**预期交付：**`diagnosis/`、行为 Diff、标注集、基线结果、Debug Report 和 S8 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S8-T01 | 同因果依赖、不同到达时间的轨迹 | 对齐结果一致；无因果关系的事件不能凭时间强配 |
| S8-T02 | 失败 Run 无成功参考、仅有硬断言 | 仍可提取失败依赖候选，清楚说明证据不足 |
| S8-T03 | 过时来源→缺少验证决策→错误报告 | 报告区分输入异常和可修复决策，证据链可点击 |
| S8-T04 | 20 例完整评估 | 输出 Top-1/Top-3、未定位数、平均调用/成本和基线，不漏失败案例 |
| S8-T05 | 保留 8 例、规则冻结后评估 | 目标 Top-3 命中至少 6/8 且不低于时间基线；这是目标，不是实测结论 |
| S8-T06 | 缺失/跨项目事件证据或歧义对齐 | 缺证据明确标记，越权引用拒绝，不编造确定根因 |

**退出：**行为和证据正确性必过。若 S8-T05 未达到，记录定位能力未达标，S9 可使用显式全候选列表继续验证搜索工程，但 D2 不得宣称已证明定位优势；后续须在新保留集复验。

### S9：类型化补丁与有界修复搜索

**前置：**S7；优先使用 S8，降级候选来源必须记录。

**目标：**在有限空间和预算内找到候选修复，正确解释无解、未知与预算耗尽。

**实施顺序：**

1. 冻结 Patch Schema：target checkpoint/decision、operator、payload、precondition、provenance、effect policy、test_only。
2. 先做可解释模板生成，不默认让 LLM 自动写补丁；只开放三类基础操作和字段白名单。
3. 默认最多 8 个候选原子补丁、组合深度最多 2；先枚举单点，再按固定次序枚举可兼容的双点，暂不引入 Beam Search。
4. Patch 有稳定应用次序；不交换的补丁保留不同顺序语义；前置条件失效或 DAG 环立即拒绝。
5. 去重键包含 checkpoint、ordered patch set、environment、validator、策略版本；搜索提案去重不吞掉有意的重复 trial。
6. 搜索使用持久化 Job 和预算台账，保存每次候选、试验、成本、停止原因；为 S10 复测预留额度。
7. 按安全、达标与否、patch_count、修改幅度、成本的字典序展示候选；此时状态只到 screened/needs_confirmation。

**预期交付：**`repair/` 补丁/候选/搜索模块、修复迁移、CLI/API 与 S9 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S9-T01 | 只有 Prompt 单点有效的 fixture | 找到该候选，记录独立 trial；不能提前标 verified |
| S9-T02 | A、B 单独失败，仅 A+B 成功 | 枚举联合候选；不会因单点失败提前宣告无解 |
| S9-T03 | 改 validator/放宽权限/非法路径/插入任务环 | 补丁拒绝且不执行 trial，安全事件可追查 |
| S9-T04 | 候选重复、顺序不同、跨环境相同 payload | 完全相同提案复用；有序语义/环境不同不误去重 |
| S9-T05 | max_trials/调用/Token/时间任一达到阈值 | 不启动新 trial，返回 budget_exhausted、已有证据与未确认原因 |
| S9-T06 | 有限空间全部验证失败；另一个空间未穷尽 | 前者仅称“声明空间内无解”；后者称“未找到”，不泛化 |
| S9-T07 | 搜索中断、重启、取消 | 已有试验保留；未决写结果先核对，取消后不扩展新候选 |

**退出：**单点、联合、安全、预算、去重和重启行为通过；默认预算内能产出可审查候选，不把筛选通过包装为稳定修复。

### S10：缩减、独立复测、Repair Lab 与 D2 验收

**前置：**S9。

**目标：**输出有精确补丁、独立证据、适用条件和成本的修复建议，完成核心研究闭环。

**实施顺序：**

1. 对成功组合做逐补丁删除测试，之后再做预定义字段级缩减；非单调场景每个变更都重新执行。
2. 在确定性 fixture 下，若删除任何单个补丁均不再达标，且测试完整，标注该条件下 1-minimal；预算不足或只测部分删除时不标。真实模型的删除测试只能形成经验缩减证据，不能从少量随机结果推断严格 1-minimal。
3. 锁定一个最终候选后才打开 confirm 样本，默认 6 次独立试验；不得看到 confirm 失败后继续用同一保留集选补丁。
4. 默认确认门槛为硬安全测试零违规、硬断言经验通过率至少 80%（6 次至少 5 次）；报告成功数/总数和 95% Wilson 区间。小样本通过不等于已证明线上稳定。
5. 再跑固定回归任务；受控合成任务的 verified 必须附 `verified_under_fixture`，真实模型证据单列。
6. 完成 Repair Lab：失败断言、候选、试验、缩减、预算、精确 Diff、区间、限制、批准/拒绝与版本冲突。
7. 批准只创建 Agent 配置新版本，绑定精确提案与预期配置版本；`TOOL_RESPONSE_PATCH` 只能批准保存为实验，不能应用正式 Agent。
8. 完成 30 例、20/10 任务划分和四种自动基线，包含失败/无解/伪修复结果；产出原始可复核数据与 D2 报告。

**预期交付：**缩减/确认模块、Repair Lab、审批记录、评估资产、D2 报告、S10 验收记录和复现说明。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S10-T01 | A+B+C 达标，C 冗余；A+B 各自必要 | 新 trial 证实删 C，保留 A+B；每次删除有独立记录 |
| S10-T02 | 随机候选筛选偶然通过，确认集失败 | 保持 rejected/needs_review；不能借旧 trial 伪造稳定性 |
| S10-T03 | confirm 预算不足或样本数不足 | 不标 verified，展示实际 n 和未确认原因 |
| S10-T04 | 删除测试未完成、非单调补丁、先后顺序冲突 | 不标全局最小；1-minimal 仅在完整规定测试后声明 |
| S10-T05 | 工具响应替换使 fixture 通过 | 标记 test_only，正式配置采纳动作禁用 |
| S10-T06 | 审查后改 proposal/config version，重复批准或拒绝 | 旧审查失效；相同决定幂等；不启动工具，不改原配置 |
| S10-T07 | 30 例+四种基线按同预算执行 | 每例有数据；安全绕过/验收器篡改/未授权写次数均 0 |
| S10-T08 | 10 个保留任务，模型/策略冻结后评估 | 给成功率、改动量、成本、回归率、置信区间与失败分析；未通过不隐去 |

**退出：**安全与证据测试全过；成功推荐全部可追溯、确认与回归完整；30 例/四基线报告可复现。算法效果目标为：在开发集中声明可修复的 12 例里至少验证修复 10 例；保留集不设临时调参后的“保证胜出”结论，按冻结结果如实评估。未达到预注册目标时 D2 算法效果项标未达标，不影响报告真实的工程完成项。

### S11：必选的三方产物与结构化状态合并

**前置：**D2 已验收；本阶段属于完整交付必选主链，完成后进入 S12。D2 的修复研究验收可单独记录，但不代表完整项目完成。

**目标：**支持有限 Schema 和来源约束的合并提案，冲突不能静默进入正式历史。

**实施顺序：**

1. 为 Markdown 章节/JSON、Fact、TaskNode 冻结受支持 Schema、来源和三方差异规则；只合并可校验状态/产物，不合并进程、容器或待执行外部动作。
2. 实现 merge base 和冲突生成；多个最佳共同祖先须显式选择，校验证据、单位/有效期、任务 DAG、预算及已有约束。
3. 增加提案、冲突和审批迁移/API/CLI；效果记录仅审计。验证器无外部写权限，校验结果与批准均绑定 base、候选摘要和 expected heads。
4. 在 Electron 中增加 Merge Review：展示 base/ours/theirs、事实与计划冲突、来源、人工选择、验证结果和批准/拒绝；编辑候选后必须重新验证，旧批准失效。
5. 批准事务中复查双分支 HEAD、候选和审批；原子创建双父 Commit 并 CAS 更新目标分支，失败保留原历史，重复提交不能生成第二个合并。
6. 对双父历史重新验证 Log、DAG、Fork、Diff、Recorded 范围选择及恢复；Recorded 仍沿显式来源 Run 的唯一录制路径消费，不把两条执行轨迹拼接为一条。合并 Commit 只表示已验证状态整合，不代表已执行合并后的任务；继续执行须走既有新 Run/审批/预算流程。

**预期交付：**`merge/`、Electron Merge Review、API/CLI、合并迁移、30 组样例、双父历史回归测试、包含合并界面的更新桌面包与 S11 验收记录。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S11-T01 | 10 组无冲突三方变更 | 结果符合人工预期、来源保留、双父顺序稳定 |
| S11-T02 | 10 组事实冲突，含不同单位/有效期/缺证据 | 全部给冲突或先完成显式转换验证，不以 LLM 自信覆盖 |
| S11-T03 | 10 组计划/约束冲突，含任务环、超预算、付款记录 | 不合法候选阻断；外部写动作计数 0 |
| S11-T04 | 拒绝提案、批准前 HEAD 改变、多个共同祖先 | 不更新 HEAD；过期批准失效；歧义 base 明确处理 |
| S11-T05 | 两个操作者竞争批准、同键重试、提交中断；对成功双父 Commit 查询历史/Diff/Fork/Recorded | 最多创建一个有效合并，HEAD CAS/事务保持一致；历史可追溯，双父顺序稳定；Recorded 不拼接录制路径、不启动外部调用，缺路径明确拒绝 |
| S11-T06 | 在更新后的桌面安装包中完成提案→冲突选择→验证→批准/拒绝，途中修改候选或断连重开 | 所有证据可查；变更后旧验证/批准失效，拒绝不更新 HEAD；重开不重复合并，未经解决/验证不能批准 |

**退出：**30 组样例、S11-T01–T06 及相关历史/桌面回归全过；验收记录和可运行桌面包齐全。S11 未通过不能进入 S12 或宣称完整交付；未支持类型返回 unsupported，不扩展为通用自然语言记忆合并。

### S12：必选协议、框架和多 Agent 适配（D3）

**前置：**D2 与必选 S11 已完成；版本、双父历史、恢复、隔离和副作用契约稳定。

**目标：**完成 MCP、LangGraph、同进程多 Agent、A2A 四个必选子项，使它们消费同一事件/状态/版本契约，并保留恢复、隔离和效果策略。

**实施顺序：**S12-A → S12-B → S12-C → S12-D，逐项实现并验收；A2A 必须在同进程协作稳定后开始。每项先固定支持范围、协议/框架版本和 fixture，再实现适配、故障处理、录制回放与兼容性测试。确定性验收使用 Mock LLM、可信本地测试 Server 和本机独立进程，不依赖公网账号；本地协议通信允许回环连接，Recorded 回放的实际网络请求必须为 0。

| 子项 | 具体交付 | 专属测试与退出标准 |
|---|---|---|
| S12-A MCP | 先可信本地测试 Server 的 tools/list、Schema 缓存、call、超时/错误；固定实现时协议版本 | schema 变更正确拒绝、错误映射、远端只读提示不能覆盖本地权限；Recorded 零真实请求 |
| S12-B LangGraph | 支持一类明确图结构的事件/checkpoint 映射 | 暂停/恢复和 Fork 与原框架预期一致；不支持节点明确拒绝，不能声称无损恢复任意图 |
| S12-C 同进程多 Agent | Supervisor + Researcher + Verifier + Writer，独立 Run/预算/权限和消息因果边 | 子任务失败/取消、消息重复、共享产物竞争、跨 Agent 数据隔离全过 |
| S12-D A2A | 在同进程协作稳定后接远程任务/消息/产物与身份 | 未授权消息拒绝、重复任务幂等、断连/过期/取消可追查，按当时官方版本执行契约测试 |

**预期交付：**四个适配/协作模块、锁定的依赖与协议版本、四组本地 fixtures 和演示、自动化契约/兼容性测试、包含 S11 与四项适配展示的最终桌面包、独立的 `doc/acceptance/S12-A.md` 至 `S12-D.md` 及 D3 汇总报告。

| 测试 ID | 输入 / 操作 | 明确预期 |
|---|---|---|
| S12-T01 | 本地 MCP Server 列出/调用工具；修改 Schema；注入超时与工具错误 | Schema 不兼容调用拒绝，错误标准化；远端只读声明不覆盖本地效果分类与审批 |
| S12-T02 | 在声明支持的 LangGraph 图中暂停、恢复、Fork；提供不支持节点 | 支持范围内状态与产物符合原图语义，Fork 隔离；不支持节点明确拒绝 |
| S12-T03 | 四角色任务流中注入子任务失败/取消、重复消息及共享产物竞争 | 因果边完整，重复消息不重复生效；预算/权限/私有上下文隔离，竞争可检测 |
| S12-T04 | 本机独立进程 A2A 端点执行能力查询、任务/消息/产物传递；注入未授权、重复、断连、过期与取消 | 身份校验生效，重复任务幂等，异常和取消状态可追溯；未知写入结果不盲目重试 |
| S12-T05 | 对四个子项的可录制成功轨迹分别 Recorded 回放，并注入缺失录制 | 网络请求为 0，状态/产物 hash 符合声明的可重放逻辑投影；缺失依赖明确失败，无 Live 回退 |
| S12-T06 | 每项接入后运行 S2/S4/S5/S6/S11 相关兼容性用例，包含崩溃恢复、HEAD 竞争、双父历史、跨项目请求、终端/Python 执行回放及桌面查询/审批 | 核心原子性、恢复、分支/项目隔离、合并及执行策略保持有效；任何框架或子 Agent 都不能绕过容器边界；最终桌面包可展示适配后的轨迹和证据 |
| S12-T07 | 测试 HTTP 地址白名单、重定向、私网访问和凭据脱敏；审查 stdio Server 启动配置 | 仅允许明确登记的端点，本地测试回环地址显式授权；越界连接拒绝，凭据不进入历史，stdio 仅启动可信已批准程序 |

**退出：**四个子项的专属测试、S12-T01–T07 和兼容性测试全部通过，四份验收记录、最终桌面包及 D3 报告齐全；D3 汇总确认 S0–S11 和 S12-A/B/C/D 均已验收。不以单个适配器完成代替 S12 完成；未通过时明确记录未完成，不能把 S11/S12 降为可选或删去。资源/提示词等非约定 MCP 接口、任意 LangGraph 图和公网多租户部署仍不在本阶段范围内。

## 6. 测试执行、统计和验收纪律

### 6.1 测试层次

| 层次 | 覆盖范围 | 执行时机 | 阻断规则 |
|---|---|---|---|
| 单元 / Schema | 哈希、状态转换、DAG、Patch、Diff、预算 | 每次相关修改与 CI | 失败不得合入该阶段 |
| PostgreSQL 集成 | FK、CAS、租约、事务、迁移、审批幂等 | S2 起每次相关修改 | 不接受仅内存替身通过 |
| 故障注入 | CAS 发布/DB 提交/工具结果/Worker 租约关键崩溃点 | S2/S4/S5、相关回归 | 半提交或重复副作用为阻断 |
| 真实执行容器 | Shell/Python 功能、权限/文件/网络隔离、资源/输出上限、子进程清理和执行恢复 | S4 必过，S5/S6/S12 相关回归 | 两工具均真实执行；仅 Mock 通过、宿主回退、遗留进程或半产物为阻断 |
| 无网络端到端 | CLI/API/Worker/Electron/产物/Recorded | S6 起每个交付点 | 网络出口非零或原历史被改为阻断 |
| Electron 桌面 | 真实主进程/preload/渲染进程，四页、Repair Lab、Merge Review、窗口缩放、键盘和 IPC 边界 | S6/S10/S11 与相关 UI 修改 | 主操作不可达、数据错误、IPC 越权或凭据泄漏为阻断；浏览器测试不替代桌面验收 |
| 桌面打包安装 | Windows x64 安装/便携运行、升级、卸载、外部后端连接与断连恢复 | S6 首次、S11 更新包、S12 最终交付 | 仅开发模式可运行、资源缺失、破坏业务数据或依赖隐含前端开发环境为阻断 |
| 研究评估 | 固定任务分割、基线、样本隔离、成本 | S8/S10 固定版本后 | 不能用不完整数据宣布算法效果 |
| 协议 / 框架契约 | MCP、LangGraph、多 Agent、A2A 与核心恢复/隔离兼容 | S12 每子项及 D3 汇总验收 | 四项全部通过；未完成任何一项不得标记 D3 完成 |
| 真实模型实验 | 单供应商适配、实际变异、实际成本 | 明确开启并有预算时 | 与离线验收分开，跳过项写明原因 |

S1 冻结实际运行命令和工具版本；本文不提前伪造尚不存在的测试命令。测试报告必须注明执行/未执行，不能把设计中的预期列作已通过结果。

### 6.2 搜索默认预算与样本约定

初始实验配置采用以下可调整默认值，调整后生成新策略版本，不能在同一次搜索中静默变更：

| 额度 | 默认设计值 | 含义 |
|---|---|---|
| 原子补丁数 / 组合深度 | 8 / 2 | 小规模先枚举；不保证覆盖所有有效补丁 |
| 搜索总 trial 上限 | 30 | baseline 重跑、筛选、删除、确认、回归都计入；失败/超时也占额度 |
| screen / reduce / confirm / regression | 14 / 6 / 6 / 4 | 额度独立；筛选不能占用确认和回归预留 |
| 单 trial 最大模型调用 / 工具调用 | 6 / 12 | 调用前预留；重试同样计数 |
| 单 trial 最大 Token | 4,000 | 含输入与最大输出估计，超限不开始下一调用 |
| 搜索总 Token | 60,000 | 同时约束所有阶段，不能把单 trial 上限简单当总预算 |
| 单 trial / 总墙钟 | 60 秒 / 15 分钟 | 已在途调用按超时和未知结果策略处理 |
| confirm 阈值 | 6 次至少 5 次硬断言通过 | 安全零违规；显示 95% Wilson 区间与小样本限制 |
| 回归 | 4 个固定任务全部通过 | 不从正在修复的单一案例推断无回归 |

真实模型费用需显式给出货币、最高单价版本和总额，默认没有“无限费用”；缺价格/usage 时保守预留并标估计。若预算不足以完成最小确认+回归，预检就提示只能产生未确认候选。以上数值是起始约束，S7/S9 用离线运行数据检查是否足够，并将必要调整记录到新策略版本。

预留同时作用于 trial、Token、调用数、费用和时间，而不是只保留 10 个 trial 名额。例如按单 trial 4,000 Token 最坏上限，confirm+regression 至少保留 40,000 Token，screen+reduce 在总 60,000 限制下最多消费 20,000；因此 14+6 是试验次数上限，不保证一定用满。预估无法覆盖必要确认和回归时，尽早返回未确认候选，不挤占预留额度。

缩减阶段若 6 次额度不足，保留已找到候选并注明未完成最小化；不占用 confirm 保留额度。预算没有剩余时不宣告“没有修复解”。搜索报告保存探索了多少单点/组合、剩余空间、每阶段消费和原因。

### 6.3 同预算基线与研究结论

自动基线至少四类：首异常节点局部补丁、一次性全局 Prompt 补丁、无定位排序的有界候选枚举、不做缩减的同预算组合搜索。候选与输入来自相同任务约束；禁用真实模型时全局 Prompt 由固定模板生成，启用真实模型时其提议调用同样计入预算。人工调试可额外记录，但不充当可自动复现的四类基线之一。

所有方法使用相同任务划分、允许操作、冻结验收器、效果策略、trial/Token/时间/费用上限和独立确认/回归规则。报告搜索成功率、确认通过率、patch_count、变更字段/文本规模、成本、时延、回归失败率和证据覆盖率。不能只比较“搜索到了几个通过”而忽略是否做了复测。

统计单位分别说明：任务级成功率以任务为单位；单候选确认通过率以独立 trial 为单位；同一任务多个 trial 不当作多个独立任务。给出原始计数和区间，说明托管模型 seed 不能保证独立或确定；结果支持的结论范围仅限该数据集和条件。

### 6.4 完成定义与缺陷处理

阶段完成必须同时满足：交付位置存在、对应行为测试实际通过、验收记录有版本和证据、文档反映真实行为、无秘密/运行数据进入 Git。依赖变更后的关键不变量需回归；无新变化/风险时不重复扩大测试。

历史破坏、越权、真实副作用误触发、验收器可改、未确认结果冒充 verified 属于阻断缺陷；暂停依赖交付并先修复。算法指标未达目标按事实记录，不能通过删样例、改标签、放宽硬断言或重复选择保留集来“验收”。

备份恢复至少在 D1/D2 各验证一次：停止写者后备份 PostgreSQL 与 CAS，恢复到空环境，核对所有引用、hash、HEAD、checkpoint；中断 Job 保持待核对，不自动执行外部写调用。迁移在生产/正式数据上只前进，回退通过备份恢复，不依赖危险 downgrade。

## 7. 工期、依赖和范围收缩

### 7.1 参考工作量

下表以一人、每天约 6 小时有效开发为估算单位，含本阶段实现与测试，不含等待外部服务/账号；不是固定截止日期。

| 阶段 | 参考工作日 | 主要不确定性 |
|---|---|---|
| S0 | 当前轮 | 文档审阅 |
| S1 | 2–3 | Windows 环境、依赖和迁移基线 |
| S2 | 6–8 | 文件与数据库崩溃一致性、哈希协议 |
| S3 | 5–7 | 计划约束、离线任务、模型输出错误 |
| S4 | 14–20 | 原服务/恢复 6–8 日，新增终端/Python 容器执行、产物传输和隔离测试 8–12 日 |
| S5 | 8–12 | 调用匹配、执行结果/文件 manifest 重放、分支隔离 |
| S6 | 13–20 | 原界面/模型适配 7–10 日，新增 Electron IPC、安全边界、打包安装及生命周期验收 6–10 日 |
| S7 | 5–7 | 实验隔离、验证器冻结和样本设计 |
| S8 | 5–7 | 标注集、依赖对齐和定位效果 |
| S9 | 6–8 | 搜索去重、组合合法性、预算 |
| S10 | 6–8 | 缩减、独立确认、基线与 Repair Lab |
| S11 | 8–12（必选） | 冲突语义、证据、并发批准、双父历史兼容和桌面 Merge Review |
| S12 | 每子项 3–8，四项共 12–32（必选） | 版本差异与框架恢复边界，A2A 另含身份集成 |

D1 累计约 48–70 个工作日；D2 在其上增加 22–30 日，总计 70–100 日；必选 S11 再增加 8–12 日，之后 S12 四项增加 12–32 日，D3 完整必选范围合计 90–144 日。加 25% 缓冲并向上取整约为 113–180 日，按每周 5 天约 23–36 周；若每天只有 3 小时，按有效投入时间重新估算。与原规划相比，Electron 增加约 6–10 日，S11 的 8–12 日计入必选总量；S1–S4 的工作量不变。以上为全项目总工作量，不能直接当作当前剩余工期；每阶段结束用实际耗时更新剩余估计，不能为了维持日历跳过验收。

### 7.2 依赖与停止点

主链为 S0 → S1 → S2 → S3 → S4 → S5 → S6（Electron / D1）→ S7 → S8 → S9 → S10（D2）→ S11（必选合并）→ S12-A → S12-B → S12-C → S12-D（D3）。S9 的搜索器只硬依赖 S7，定位未达标时可用完整候选集验证；报告中必须保留此区别。S11 与 S12 四项全部纳入必选主链。

若时间受限，按以下顺序缩范围：

1. 保留 Windows x64 的桌面安装/便携交付，推迟 macOS/Linux、自动更新及一键配置完整后端环境。
2. 保留四页和 Repair Lab 的核心列表/详情，推迟搜索树动画、WebSocket、复杂布局和语义摘要。
3. 研究版本继续使用三种 Patch、小候选枚举和单 Worker，不加入自动 LLM 提议、Beam Search 或多供应商。
4. 若仍超出能力，调整排期或请用户明确变更范围；D1/D2 可分批交付，但必须说明 S11 与 S12 仍待完成，不能据此宣布完整项目完成。

未经用户明确变更范围，不得删去 `terminal_exec / code_exec`、Electron 桌面安装交付、S11 或 S12-A/B/C/D。任何情况下不得删去：执行容器与资源边界、原子提交、HEAD CAS、写动作保护、严格 Recorded、冻结验收器、独立复测标记、证据审计和项目隔离。

## 8. 风险、待选项和下一次工作的起点

| 风险 / 事项 | 当前处理 | 后续触发条件 |
|---|---|---|
| 实际模型/账号未知 | 不假定供应商；离线先完成 | S6 选一个已有可用模型，记录价格/能力与预算 |
| Electron/Node/构建与打包工具版本 | 本次仅设计，不安装依赖或伪造 lockfile | S6 核验并锁定 Electron、构建用 Node.js、Electron Forge 与 Windows maker 的兼容版本 |
| 桌面包与后端分离 | 首版连接既有回环 API，后端仍按原说明准备和启动 | S6 在独立 Windows 环境验证，无服务时明确提示；不以桌面化更改 S1–S4 |
| 桌面原生权限 | 受限 preload、IPC sender/frame 校验、包内资源与 CSP；令牌留在主进程 | S6/S11/S12 的真实安装包测试，渲染内容不得触发宿主执行 |
| 安装包签名与分发 | 构建记录签名状态，不假定已有签名证书或公开发布渠道 | S6 产出本地可验证包；正式分发前另行落实证书与信任验证，自动更新后置 |
| DESIGN 属于参考营销体系 | 保留 token 和规定字体替代，产品字段按 AgentGit 业务组织 | S6 如需新增组件/修正对比度，先补齐 DESIGN 的应用规范 |
| Windows 文件行为不同 | 所有 CAS 临时发布同卷，文件路径与联接作为必测 | S2/S4 加 Windows 集成验收 |
| 终端/代码执行的宿主风险 | 固定断网 Linux 容器、无宿主挂载、独立采集身份、资源上限和真实退出核验；不承诺通用恶意代码托管 | S4 验证实际引擎限制与故障清理；能力缺失阻断交付，不回退宿主 |
| 凭据/脱敏影响可恢复性 | 秘密不持久化；缺少必要数据明确不可重放 | 接入真实供应商或私有资料前测试边界 |
| 调用结果不明 | 暂停核对，绝不假设未执行 | 网络断连、Worker 崩溃时进入 outcome_unknown |
| 研究效果未超过基线 | 完整记录失败、保持留出集 | S8/S10 达到预注册条件后才声称效果 |
| 存储容量增长 | 完整 state + CAS 引用去重 + 大小上限 | 有真实数据证明瓶颈后再做增量快照/GC 自动化 |

下一次若开始实现，从 S1 的环境和 Schema 基线开始；先交付 S1 验收记录，再进入 S2。本轮不执行 S1 的任何代码、安装或测试脚本任务。

## 9. 官方资料核验及适用范围

本轮核验日期：2026-10-20。以下资料支持具体工程约束，不作为自动修复有效性的实验证明：

- [RFC 8785：JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html)：原始 Unicode 字符串应保留；本文 AGJSON-v1 是受限自有协议，不能以“排序 JSON”冒称完整 JCS。
- [PostgreSQL：显式锁](https://www.postgresql.org/docs/current/explicit-locking.html)：行锁在事务边界内使用；本文据此设计短事务和竞争测试。
- [PostgreSQL：SELECT / SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html)：可用于队列型消费，跳过锁定行不提供通用一致快照；Job 租约、fencing 和外部动作核对仍是本项目必须实现的约束。
- [FastAPI：Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)：响应后的后台任务机制与重型跨进程任务有不同适用范围；本文因耐久恢复要求选择持久化 Job，不把 BackgroundTasks 当作任务恢复存储。
- [MCP：Tools（本轮官方页面版本 2026-07-28）](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)：工具接入在 S12 按当时协议实现；本地权限校验不依赖模型自报或远端工具说明。
- [Docker：运行容器](https://docs.docker.com/engine/containers/run/)：提供镜像 digest、用户、资源限制与 capabilities 等配置依据；本文将这些选项组成固定执行策略，仍须在目标环境验证实际生效。
- [Docker：none 网络](https://docs.docker.com/engine/network/drivers/none/)：执行容器仅保留自身回环接口，不连接宿主服务或外网。
- [Docker：tmpfs](https://docs.docker.com/engine/storage/tmpfs/)：文件随容器停止消失，大小上限与容器内存限制需同时考虑；因此成功产物须在容器停止前完成有界采集。
- [Docker：安全边界](https://docs.docker.com/engine/security/)：容器管理权限与 Linux 内核隔离均需受控；容器不是对所有未知恶意代码的绝对安全承诺。
- [Linux：PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)：命名空间 init 进程退出会结束该空间内的其他进程；独立截止时间守护据此设计，仍须在 S4 验证 Worker 崩溃后的实际终止与清理结果。

2026-10-21 补充核验 Electron 官方资料，仅用于 S6 及之后的桌面规划，不修改 S0–S4 契约：

- [Electron：进程模型](https://www.electronjs.org/docs/latest/tutorial/process-model)：主进程负责窗口和生命周期，渲染进程承载页面；本项目据此分离 main、preload 和 React renderer。
- [Electron：安全建议](https://www.electronjs.org/docs/latest/tutorial/security)：参考上下文隔离、进程沙箱、CSP、导航限制、IPC sender 校验与受限桥接；具体约束必须通过 S6-T08/T09 验证。
- [Electron：使用 Electron Forge 分发应用](https://www.electronjs.org/docs/latest/tutorial/forge-overview)：区分应用打包、生成安装程序和发布；本项目首版完成本地打包与安装验收，不将公开发布和自动更新作为隐含交付。

## 10. S0 检查记录

- 已阅读产品设计与完整界面规范；未发现磁盘上的 AGENTS.md / CLAUDE.md，遵守用户在会话给出的规范。
- 已执行 `git init -b main`；当前文档与 Git 基础规则按用户要求纳入首次本地归档提交，没有远程，不执行推送；具体提交 ID 以 Git 历史为准。
- 当前根目录保留 README、DESIGN.md、两项 Git 基础规则和 `.git`；设计与实施方案存入 `doc/`，版本均为 v1.0.0；未生成源码或安装依赖。
- 界面规范原始基线 SHA-256：`3D972F34F8D4F33B442FA28220CF3B7CA2F13C0A97C468E3988148F82521F9DF`；迁移后仅将元数据 version 从 alpha 改为 1.0.0，设计规则不变。
- 文档本地链接和 Markdown 围栏检查通过；Git 忽略/换行规则检查通过。
- 本节为当前文档阶段记录；后续 S1–S12 的行为测试均未执行，也没有任何程序功能已完成的声明。
