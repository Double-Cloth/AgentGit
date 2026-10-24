# 本地开发与测试

实现进度与阶段证据见 [PROGRESS.md](PROGRESS.md)。README 保留项目介绍；方案和设计的 S0 描述是规划时的历史基线。

S6 桌面工程已建立；固定 Node/Electron 版本、开发/检查/构建命令、身份预置和字体授权见 [desktop/README.md](../desktop/README.md)。当前候选 [0.1.5 的本机证据](acceptance/S8-package-015.md)已归档，独立用户/原生专项仍未验收；新增 S9/S10 后端未进入该包。用户已授权延后这些专项、先继续其他开发与本机验收；范围见实施方案第 1.2 节。

## 环境

S10 开发位于 `codex/s10-repair`，从 S9 本机后端退出提交 `fa37baf` 继续。[首增量](acceptance/S10-kernel.md)是纯删除与确认协议，不启动模型/工具或 Job；测试命令 `uv run --locked pytest tests/unit/test_repair_validation.py`。实际耐久执行、共享预算、确认池唯一开启、审批、Repair Lab 与评估尚待接入，不应把合成协议 UUID 当作数据库 Run 查询或把其计量当成实测费用。

S9 开发位于 `codex/s9-repair`；[有限模板、顺序枚举、预算契约及持久意图](S9-repair-plan.md)已经建立。`0017_repair_search_ledger` 保存冻结搜索、编排租约、全部提案、试验意图、Job 归属、结算及审计；`0018_repair_dispatch_control` 保存未决标记并约束派发。Recorded/Live/Hybrid 通道与物理结算见[派发验收](acceptance/S9-trial-dispatch.md)，后续[自动搜索循环、模板 API/CLI 和新版 Mock 预算](acceptance/S9-search-loop.md)已经核对。`repair-worker` 只编排，研究 Job 由另一个 `worker` 执行；入口示例见 [API.md](API.md#有界修复搜索s9)。本轮仅在独立测试 schema 迁移/回退，不自动修改业务库，尚无本轮新桌面包。真实 Run 执行期间不得修改 `src/agentgit/**/*.py/.json` 与 `uv.lock`，这些字节全部参与冻结环境摘要；先结束所有相关试验再继续源码变更。

S8 的行为图、依赖对齐与静态 Debug Report API/CLI 见 [S8 方案](S8-diagnosis-plan.md)与 [API.md](API.md)。`behavioral-trace/behavioral-diff` 继续只读既有历史；`debug-report` 在 `0016_static_debug_reports` 发布不可变静态候选和硬断言，不启动 Worker 或执行历史动作。完整 20 例定位评估、干预结果及桌面视图尚待完成，源码增量未进入 0.1.4 桌面分发包。

S7 新增 `0013_branch_experiments`、`0014_frozen_interventions` 及 `0015_hybrid_observations`，保存不可变实验/trial、干预归属和 Hybrid 观测账本，运行状态仍由既有 Job/Run 管理。新入口和示例见 [API.md](API.md)；本次自动化验证仅迁移独立测试 schema，没有自动迁移用户的业务库或重建桌面包。历史迁移保持冻结。

通用 `RecordingReader` 的冻结输入可以是不透明字节，不能要求所有历史输入都按研究 Agent 的 JSON 解析。Hybrid 来源仍必须严格验证受控 JSON、policy、Job 归属及全部输入引用；CAS 缺失或损坏不能进入通用兼容路径。改动此处时同时运行 `test_recording_reader.py` 与 Hybrid/Recorded 交叉用例。

S7 分组统计和 `actual.model_usage` 为只读投影，不新增迁移。统计分数与合计使用十进制字符串，模型计量读取原始调用及可信核对归属；检查点用量可能滞后，不能用旧 `actual.cost_micro_units` 推断未知费用。示例见 [API.md](API.md)，当前[验收范围](acceptance/S7-statistics.md)明确区分后端、桌面桥接和待实现视图。

- Python 3.12；`.python-version` 与 `requires-python` 限定 3.12 系列。
- uv 0.12.19；后端版本由 `uv.lock` 冻结，使用 `--locked` 阻止隐式改锁。
- PostgreSQL 17.6；Docker Compose 只启动本地开发数据库，端口限定 `127.0.0.1:55432`。
- Docker Desktop Linux 模式。S1 只探测引擎；执行镜像、实际隔离和资源限制在 S4 单独冻结与验收。

Windows 在项目根目录用 PowerShell 执行：

```powershell
uv python install 3.12
uv sync --locked
docker compose up -d --wait
docker compose exec postgres createdb -U agentgit agentgit_test
uv run --locked alembic upgrade head
uv run --locked agentgit doctor --database
uv run --locked agentgit doctor --database --testing
```

`createdb` 只在首次准备测试库时执行，已存在时报错，不删除旧库。Compose 中的用户名/密码为公开的本地占位值。使用其他环境时通过 `AGENTGIT_*` 进程环境变量设置配置；程序**不会自动加载 `.env`**，可参考 `.env.example`。不要在命令记录或提交中填写真实凭据。配置错误与数据库连接错误只返回固定信息，不输出 URL 或驱动异常。

## 入口与诊断

```powershell
uv run --locked agentgit version
uv run --locked agentgit doctor --sandbox
uv run --locked uvicorn agentgit.api.main:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` 是公开存活探测；`GET /ready` 实际连接 PostgreSQL，失败返回 503 和 database_unavailable。S4 业务 API、身份和项目边界已接入，操作见 [API.md](API.md)，业务请求须配置本地身份令牌。

`doctor --sandbox` 不创建容器、下载镜像或执行命令；只读检查当前固定策略、Linux/cgroup v2、资源/seccomp 能力与预置镜像，满足才返回 `execution_ready=true`，失败退出码为 1。S4 的实际隔离测试已完成，证据见 [S4.md](acceptance/S4.md)；禁止使用宿主进程作为执行回退。

## 检查命令

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked pytest tests/integration
```

默认 pytest 只运行单元测试，不要求数据库。显式选择 integration 后，缺少真实 PostgreSQL 即失败，不以 skip 或 SQLite 代替通过。GitHub Actions 配置 Windows/Linux 单元检查及 Linux PostgreSQL 集成检查；本地执行不代表远程 CI 已运行。

`AGENTGIT_DATA_ROOT` 与 `AGENTGIT_TEST_DATA_ROOT` 默认是 `.agentgit/dev`、`.agentgit/test`，不能相同或互为祖先。路径用 `resolve()` 核验符号链接/目录联接；Windows 路径比较遵循平台大小写语义。测试库必须与开发库名称不同，且以 `_test` 结尾；仅更换用户名或主机别名不能绕过保护。

迁移默认连接开发库，只前进。集成用例显式设置 Alembic `testing` 属性才允许测试库回退；不要在正式或共享数据库运行回退测试。S1 revision 只建立 Alembic 基线；S2 通过 `0002_version_kernel` 和 `0003_parent_guards` 增加版本内核。S2 集成测试在独立测试 Schema 中运行完整迁移，不清空共享 public。

## 内容协议

领域内容采用 AGJSON-v1；规范、限制和固定向量见 [canonical fixtures](../tests/fixtures/canonical/README.md)。金额使用整数微单位，时长使用整数毫秒，不接受浮点数。Blob 直接保存二进制原字节。

基础契约使用 Pydantic strict/extra=forbid/frozen。载入内容时调用 `parse_contract`，同时校验 AGJSON 与显式 Schema 版本；未知版本返回 `unsupported_schema`，未知字段返回 `validation`，不默默丢弃字段。

S1 实现参考官方资料：[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[SQLAlchemy psycopg 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)、[Pydantic 配置](https://docs.pydantic.dev/latest/api/config/)。这里只冻结工程契约，不宣称 Agent 已可执行任务。

## S2 版本内核调用

`HistoryService` 提供项目/Run 创建、输入登记、耐久事件、完整状态原子提交、只读 log/restore、按实体读取产物和 GC dry-run；HTTP/CLI 业务管理在 S4/S5 接入。构造时传入 PostgreSQL engine 和独立数据根下的 `ObjectStore`，不要使用源码目录作为存储目录。

环境的 `input_ref` 必须先用当前项目的 `register_input` 从实际字节登记。`commit` 必须携带 expected HEAD/version；state 与事件 payload 必须本身符合 AGJSON，不能依赖类型隐式转换。artifacts 是**完整清单**，空元组表示无文件；历史产物不自动继承。

CAS 默认单对象 8 MiB，使用支持硬链接的本地文件系统。故障注入入口仅供本地测试。GC 只有 dry-run，不提供删除；不得把扫描报告用作自动删除清单。阶段限制和测试证据见 [S2.md](acceptance/S2.md)。

## S3 离线研究演示

S3 验收时 Schema head 为 `0004_research_tasks`；当前代码 Schema head 为 `0018_repair_dispatch_control`，S10 纯协议增量未新增迁移。用户自行准备业务环境后再运行：

```powershell
uv run --locked alembic upgrade head
uv run --locked python examples/research_agent/run.py
```

具体输入、产物位置、预算演示与 Mock 计量说明见 [研究示例](../examples/research_agent/README.md)，实测证据见 [S3.md](acceptance/S3.md)。只使用固定合成资料，没有真实模型/互联网依赖。每次演示创建独立 Repository、Run 和受控工作区；S4 容器工具使用下文独立 Worker 入口。

新增 Python/语料文件已纳入 uv 的本地构建缓存键，避免非 editable 安装复用旧构建；配置依据 [uv 缓存文档](https://docs.astral.sh/uv/concepts/cache/#dynamic-metadata)。执行 `uv build --out-dir .agentgit/packages` 可生成 sdist/wheel，非 editable 包内语料加载已验证。开发时仍使用默认 editable 的 `uv sync --locked`。

## S4 调度与恢复开发入口

`JobService.enqueue()` 与 `Worker.run_once()` 已接入业务 HTTP/CLI，见 [API.md](API.md)。提交只保存 Job/Run/幂等响应，不创建工作区或启动 Adapter；独立 Worker 使用相同 PostgreSQL/CAS 领取执行，不使用 BackgroundTasks 保存任务。

`enqueue` 需要 actor、幂等 key、配置版本、分支名称与预期 HEAD/version。同一 actor/route/key 的相同请求返回原 Job/Run，异参冲突；任务或分支存在非终态写者时冲突，暂停和待核对同样占用分支。配置和计划版本在落库事务中再次核验。

Job 固定代码摘要、配置、输入、语料和初始计划版本；Worker 必须使用兼容代码和恢复环境。升级代码不会强行执行旧 Job；应先处理活动任务，再升级。使用新构建启动 Worker 时，代码不匹配返回 `unsupported_schema`，不重新调用 Adapter。不能用修改 payload 的方式绕过检查。

默认租约 15 秒，心跳间隔为租约的三分之一；接管增加 fencing token。失效 Worker 的调用响应和状态提交均被拒绝。暂停/取消通过 `request_control(..., expected_version=...)` 留下 requested 事件，抵达安全检查点才 applied；正在执行的外部调用不会被描述成已经撤销。普通暂停在同一 Run 恢复；首次领取后的持久截止时间不会因暂停、重启或接管而重置。

崩溃恢复封存原 Run 的 interrupted 事实并创建新 Run，原 Commit/检查点不改；新租约使用独立工作区，从完整 CAS 产物恢复。结果已保存时核验 occurrence、参数、工具签名、效果分类、checkpoint 和响应限制后复用；产物由录制写入内容重建，不再次调用工具。已有意图但缺结果时进入 `outcome_unknown`，保留预算预留并等待可信核对；核对前取消也仅记录请求。核对入口与继续语义见下节。

运行新增集成测试：

```powershell
uv run --locked pytest tests/integration/test_jobs.py tests/integration/test_kernel_migrations.py
```

## S5 版本与录制基础

`VersionService` 提供来源 Run 路径校验、安全 Fork、只读 Checkout、分支历史和结构化 Diff；入口见 [API.md](API.md)。`0010_branch_forks` 保存不可变来源 Run、起点和操作者，Fork 与幂等响应在同一事务中提交。名称冲突不覆盖分支；未核对的写入检查点拒绝 Fork，已可信清理的容器失败需匹配精确的停止证据。

Checkout 只读 CAS/数据库，不物化工作区或启动工具。Diff 使用 JSON Pointer 展示字段、数组、产物摘要/MIME 与调用变化；二进制文件只比较摘要。共同祖先基于经过校验的单父 Commit 路径，历史及输出有界，分页不会把超限结果标为完整。

`RecordingReader.load()` 必须提供来源 Run、起止 Commit 和预期 `EnvironmentManifest`。读取器逐项核验事件、调用 occurrence、参数/签名/响应、对象所有权，以及执行工具的输入、策略、日志和输出 manifest；缺对象返回 `recording_missing`，环境不兼容或错配直接失败。`RecordedAdapter` 仅持有录制字节，精确顺序消费且不调用传入的真实执行回调，禁止 Live 回退。

`RecordedReplayer` 现在用原 `ResearchRuntime` 和 `MemoryWorkspace` 逐检查点重算状态与产物，比较来源 Commit 的 state_ref；只把起点快照用于恢复，后续快照作为比较证据。普通调用、执行输入上下文、产物写入和成功执行输出均在内存中重建。模型/工具响应严格由 `RecordedAdapter` 提供，不实例化执行服务或宿主工作区。

Recorded 同时校验当前代码、Python、依赖锁、模型/工具签名、Prompt、固定语料/评价时间/种子及完整输入，不接受改变配置。逻辑调用计数沿用历史，实际模型/工具调用为零；暂停/恢复、取消、步骤/工具预算和当前版本的墙钟停止事件已覆盖。墙钟触发点从相同代码的录制事件恢复，实际回放耗时不写回逻辑预算。

来源中 `call.reused` 表示崩溃前已有完整结果，重算沿用 Runtime 缓存消费入口，保持先结算、后取消/到期的顺序；普通录制不会误走恢复路径。已验证结果保存后、观察前崩溃的正常继续/取消/到期三个真实执行来源。

`sandbox.execution_discarded` 由单独的纯状态逻辑重建：读取器先验证原物理调用、固定策略/输入、同来源链、唯一清理证明及该 Run 的丢弃引用归属，重算器再比较原安全状态或精确的 `outcome_unknown` 状态，保留旧轨迹、扣除一次旧尝试用量并追加停止证书。丢弃本身没有调用响应，不补造成功结果。之后继续消费新 attempt 的录制，或按原取消/截止时间/预算/尝试上限停止。缺清理证明或输入漂移明确失败，不访问引擎补齐证明。

Recorded 已接入耐久 Job、Worker、`POST /replays` 和 `agentgit replay`，操作见 [API.md](API.md#recorded-耐久任务)。`0011_replay_jobs` 增加不可变 `replay_requests/replay_checkpoints`，绑定来源范围、独立 Job 和逐检查点状态摘要；数据库复核来源顺序、父关系和新 Run 归属。提交复用原配置、创建独立 Task，不继承原工作区或动作授权，也不执行 Runtime。Worker 在租约和心跳下重算，证明随 Commit 同事务保存。

Recorded 的实际暂停/取消/期限与来源逻辑状态分别记录；重启后先验证已保存前缀，再保存剩余证明。实际耗时通过操作事件记录，未完成计量的崩溃尝试明确展示为未知；原逻辑预算不归零。等价分支允许共享 Commit 内容，但已发布父边只校验、不再次插入；父边不可补写和篡改的数据库保护不变。预算读取按当前 Job 的 Run 来源链过滤快照，避免共享 Commit 的其他 Run 被误选。

Live Job 已接入相同管理服务和 Worker：`prepare_live` 在内存中验证干预、生成独立初始快照，提交事务登记新配置/Task/Job、分支来源及 `replay_input` 归属。Worker 只从当前 Job 的初始输入或自身恢复链物化新工作区，先保存 `live.initialized`。新环境显式绑定 `budget_baseline`，Runtime 保留累计 occurrence 编号，按用量差额检查新增预算。Recorded 重算 Live 来源时必须验证同一基线。

新 Job 的 `origin_run_id` 恢复边界在第一次执行时为空；来源 Run 仅用于历史 provenance，不提供结果复用和旧审批。接管后恢复链止于该 Job 的初始 Run。执行干预重新绑定文件、清单和固定策略，每次实际尝试仍创建独立受控容器。初始化 Commit 事务回滚、提交后崩溃和结果落库后崩溃分别验证；截止时间不重置。HTTP/CLI 输入和计量说明见 [API.md](API.md#live-独立重执行)，专项记录见 [S5-live.md](acceptance/S5-live.md)。

CLI 的两个 Live 输入文件分别有 1 MiB 读取上限；来源文件逐项读取，累计超过 64 MiB 立即停止，纯产物工作区不套用容器的 32 MiB 清单上限。`storage/roles.py` 将超长路径的引用角色映射为有界摘要键，短角色保持原值；完整路径仍在状态/产物表，现有 100 字符引用列及历史迁移不变。该辅助函数属于宿主存储层，镜像内路径校验源码保持冻结。

复合 Recorded 由 `ProjectionReader` 验证直接来源的证明和自身 Job 恢复链，递归读取底层录制；`RecordedProjection` 只提供来源映射和待验证事件，`RecordedReplayer` 仍调用原 Runtime 重算底层状态，再逐层比较状态/产物摘要和逻辑事件。重复恢复点按零调用恢复处理，不重复消耗录制；不得把旧证明当成新运行成功结果。

递归读取共享全链累计 64 MiB 预算，并限制 32 层；纯重算入口也有独立深度保护。`0012_replay_source_index` 增加 jobs(repository_id, origin_run_id) 索引，供恢复来源和复合读取定位冻结 Job；历史迁移快照不变，正式库保持只前进，隔离测试库允许往返。证据见 [S5-compound.md](acceptance/S5-compound.md)。

原 Runtime 直接保存未知检查点的模型来源已接通：未知诊断使用独立 `UncertainRecording` 类型，确认回执通过 `SettlementReader` 核验并作为 `RecordedSettlement` 提供给原 Runtime `_settle`。两段事实分别重算，未到确认范围时不能提前采用结果；原有普通 Recording 的未知结果禁用规则保持不变。详见 [S5-settlement.md](acceptance/S5-settlement.md)。

接管封存来源使用 `RecordedUnknownStop`：原调用可以位于同 Job 的旧 Run，依据意图、原请求和调用前状态重算 `run.outcome_unknown`。未观察到的诊断不补造轨迹；已有未知计量保持一次，后续确认仍由原 `_settle` 处理。未确认或仅到封存点的范围保留预留和未知状态，见 [S5-sealed-unknown.md](acceptance/S5-sealed-unknown.md)。

完整 S5 已通过本机验收，当前 599 项用例全部覆盖。`MemoryWorkspace` 对工作区总字节施加 64 MiB 上限，超限必须缩小范围或明确失败，不物化宿主文件作为回退。耐久批次证据见 [S5-recorded-jobs.md](acceptance/S5-recorded-jobs.md)，阶段映射及完整回归见 [S5.md](acceptance/S5.md)。

调度进程故障用例启动仓库内受信任的 Worker 测试助手，并使用独立测试 Schema/临时 CAS；用户 Shell/Python 始终在容器中执行。实际容器基础设施与持久执行服务使用单独套件验证，准备方式与边界见 [sandbox/README.md](../sandbox/README.md)；完整五工具接入及本机验收见 [S4.md](acceptance/S4.md)。早期进展保留在 [S4-progress.md](acceptance/S4-progress.md) 与 [容器边界](acceptance/S4-sandbox-boundary.md)。

## S4 工具策略与精确审批

`RecordedCallGateway` 必须提供 `ApprovalService`；缺少策略、未注册 provider、签名或效果分类不匹配均在调用前拒绝。研究 Session 从受信任的本地 Tool Registry 与模型签名生成策略，按配置允许基础工具、容器工具和离线模型。模型和远程说明不能修改效果分类。`ToolPolicy` 的外部写与不可逆动作不能预授权；断网 sandbox 策略预授权只适用于受控容器 Adapter，不授权宿主或网络能力。

非预授权动作保存原文/策略和 approval.requested，Run/Job 转为 waiting_approval，保留 checkpoint/分支绑定并释放租约。审批是操作元数据，不改变绑定检查点；已保存 pending decision 和预算是恢复依据。ApprovalService.decide 要求 actor、状态版本、检查点和策略匹配；同决定幂等，相反决定冲突。HTTP/CLI 共用管理事务，决定只重新排队，不直接启动动作。

批准只使原 Job 可重新领取，同 Run 使用新 fencing token；后续调用意图与 consumed 状态在同一事务提交，不能先消费再丢失意图。拒绝和过期不会启动 Adapter。Worker 每轮有界扫描等待中的过期审批，并重新排队以落实失败或已有取消请求。普通控制请求也在新调用意图事务内复查：若暂停/取消刚刚到达，则撤回未发出调用的内存计数并立即到安全点，不多发一次模型或工具请求。

`FakeEffectAdapter` 仅供独立集成测试使用，须显式登记 `http://127.0.0.1:<port>`；禁用环境代理与 HTTP 重定向，限定请求/回执大小，端点纳入工具签名。它不注册到离线研究 Demo，不写真实外部系统。测试服务器先 fsync 效果记录再回复或断连；断连时网关记录 `outcome_unknown` 与诊断引用，保留空 result_ref，不自动重发。`lookup()` 可核验服务回执，`resolve()` 供下述可信核对服务调用。审批证据见 [S4-approvals.md](acceptance/S4-approvals.md)。

## S4 未知结果核对与继续

`ReconciliationService.confirm(repository_id, run_id, call_id, actor=..., expected_version=..., resolver=...)` 只接受后端登记的 `OutcomeResolver`，不能把用户上传的 JSON 或“批准”当作外部动作已成功的证明。Resolver 必须匹配原 provider/签名，查询绑定原请求的结果，返回有界 `VerifiedOutcome(payload, evidence)`；查不到可信肯定回执时返回 None，Job 仍为 outcome_unknown，不安排重发。模型服务不支持结果查询时也继续保持未知；当前只验证离线模型与 fake effect fixture。

服务先短事务核对 Run/Branch/Job、来源路径、actor 与状态版本，退出事务后查询服务；再次校验相同停止点后，在一个事务内附着结果/证据、建立不可变 `call_resolutions`、追加审计事件并重新排队。重复同一核对返回原记录，不重复排队；失配、缺失、截断或无证据的回执不能发布。旧未知事件和诊断保留，核对 ID/actor 不写入逻辑 AgentState。

Worker 恢复该停止点时只消费核对结果，严格核验调用前状态、occurrence、请求、签名、预算与证据链接。已计入的步骤/调用数不会重置或重复增加，模型的预留按原请求与确认响应结算；未知轨迹替换为同一逻辑调用的确认轨迹。随后推进新检查点，再处理已有取消或到期预算，不再发送原写动作。核对后的 Worker 若崩溃，可创建新恢复 Run 继续；原 Run 检查点保留。

`tests/helpers/effectful_worker.py` 仅用于独立 fake 服务的审批→断连→未知→核对→完成验证，不改变普通工具分类。运行 `uv run --locked pytest tests/integration/test_reconciliation.py`，证据见 [S4-reconciliation.md](acceptance/S4-reconciliation.md)。业务 HTTP/CLI 已接入；真容器执行见下文。

## S4 执行记录与受管工具后端

`ExecutionService` 接入 `RecordedCallGateway` 的受管动作扩展：在事务外校验输入字节，在短事务内复核 Run/检查点授权、Job 租约和策略，并与 `external_calls` 意图原子创建 `sandbox_executions`。事务提交后才创建固定容器；普通 `invoke` 不能接管容器工具。结果保存事务同时附着日志、文件、操作证据、完整调用结果和执行记录。

`ExecutionPolicy` 固定断网与资源上限，签名绑定镜像/依赖/解释器及请求 Schema；请求只携带策略引用。`authorize_inputs` 仅供后端从当前工作区和授权任务输入登记，不是任意 CAS 上传接口。读取字节前及保存 intent 时均复查当前 Run/检查点的引用归属，不能凭其他 Run/项目的对象摘要读取输入。终端命令最多 16 KiB，Python 片段最多 64 KiB；支持 Python 片段或工作区脚本、相对 cwd、argv/stdin。不支持语言和不够 8 KiB 完整结果封装的响应预算在启动前拒绝，原始日志单独有界存入 CAS。

非零退出、超时、取消、资源限制及非法导出保存稳定错误和诊断，失败不发布输出 manifest。资源证据不可取得时不凭退出码推断原因。容器 ID、墙钟、cgroup 及清理前状态另存操作 metadata，不进入可重放执行结果。暂停等待安全点；取消和失去租约会停止整个容器。取消结果不直接把 Run 标成已取消，仍须由 Runtime 安全点落实；失效租约不能保存迟到结果。

Worker 接管时在心跳保护下先核对当前及恢复来源 Run 的未清理执行，按持久身份清理旧容器并保存审计证据。确认清理不等于恢复丢失的输出；没有完整结果仍保持待核对，不重新发送原调用。完整保存后、checkpoint 提交前的故障可直接复用结果，复用不需要 Docker 或执行服务实例。

真实服务套件需 PostgreSQL 与预置镜像同时可用：

```powershell
uv run --locked python sandbox/prepare.py
uv run --locked pytest tests/execution
uv run --locked pytest tests/unit tests/integration tests/sandbox tests/execution
```

共享 fixture 位于 `tests/conftest.py`，仅请求时连接相应基础设施，默认单元测试仍不访问 Docker/数据库。新增 Linux 容器 CI job 会在准备阶段构建并显式冻结本次测试镜像，再运行容器及执行服务套件、保存报告和测试摘要；该摘要不自动发布为开发/生产策略。远程 CI 是否通过必须看实际运行，不能以本地通过替代。

两个容器工具现已接入生产 Registry、Agent 配置/计划、Worker、录制观察和工作区推进。`agentgit config --with-execution` 使用包内固定镜像策略，严格核验已审阅源码；普通配置无需 Docker。当前 workspace 的字节和 MIME 登记为本 Run/检查点输入；结果须匹配当前调用完整录制，核验所有文件后分阶段替换目录，替换失败恢复原目录。模型不接触 Docker 身份/参数。

完整结果在观察前崩溃，恢复先只消费已保存的调用，不创建缺失意图、不启动 Adapter；结算原预算、恢复完整文件后才处理取消或持久期限。新调用仍执行正常预算和权限检查。五工具 CLI/HTTP 入口与产物说明见 [API.md](API.md#五工具容器流程)。

清理后新 attempt 已实现：`stopped_executions` 记录已确认停止但没有完整结果的稳定调用身份，不冒充成功；丢弃决定和旧尝试预算同检查点原子保存，新尝试从相同输入创建新容器并另计预算。固定上限两次，取消/到期/预算不足先结算再停止。清理失败的 Job 每 30 秒有界重查，未确认时不创建新尝试，普通模型/外部写仍须可信核对。新尝试在 stage 与意图事务内复核停止依据、源链、输入和策略。

[S4 本机验收](acceptance/S4.md)与 [S5 本机验收](acceptance/S5.md)已完成；`doctor --sandbox` 共用执行器的只读前提检查，固定镜像和隔离能力可用才返回就绪，不启动容器或执行命令。继续按实施方案进入 S6 Electron 桌面；必选 S11/S12 的完整交付不变。
