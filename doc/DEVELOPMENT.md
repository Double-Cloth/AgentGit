# 本地开发与测试

实现进度与阶段证据见 [PROGRESS.md](PROGRESS.md)。README 保留项目介绍；方案和设计的 S0 描述是规划时的历史基线。

## 环境

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

`GET /health` 是不依赖数据库的存活探测；`GET /ready` 实际连接 PostgreSQL，连接不可用返回 503 和 `database_unavailable`。S1 无业务 API，身份认证和项目访问控制在 S4 实现。

`doctor --sandbox` 不创建容器、下载镜像或执行命令；在 S4 完成前 `execution_ready=false`，退出码为 1。引擎可达不代表执行工具已实现。资源能力必须由 S4 的实际隔离测试证明，禁止使用宿主进程作为执行回退。

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

S3 验收时 Schema head 为 `0004_research_tasks`；当前 head 已推进至 `0008_call_resolutions`。先升级开发库，再运行：

```powershell
uv run --locked alembic upgrade head
uv run --locked python examples/research_agent/run.py
```

具体输入、产物位置、预算演示与 Mock 计量说明见 [研究示例](../examples/research_agent/README.md)，实测证据见 [S3.md](acceptance/S3.md)。只使用固定合成资料，没有真实模型/互联网依赖。每次演示创建独立 Repository、Run 和受控工作区；实际用户代码执行须等待 S4 容器实现。

新增 Python/语料文件已纳入 uv 的本地构建缓存键，避免非 editable 安装复用旧构建；配置依据 [uv 缓存文档](https://docs.astral.sh/uv/concepts/cache/#dynamic-metadata)。执行 `uv build --out-dir .agentgit/packages` 可生成 sdist/wheel，非 editable 包内语料加载已验证。开发时仍使用默认 editable 的 `uv sync --locked`。

## S4 调度与恢复开发入口

当前提供包内 `JobService.enqueue()` 与 `Worker.run_once()`，尚无业务 HTTP/CLI 命令。提交只持久保存 Job/Run/幂等响应，不创建工作区或启动 Adapter。API 进程后续将使用同一提交服务，独立 Worker 负责领取和执行；不使用进程内 BackgroundTasks 保存任务。

`enqueue` 需要 actor、幂等 key、配置版本、分支名称与预期 HEAD/version。同一 actor/route/key 的相同请求返回原 Job/Run，异参冲突；任务或分支存在非终态写者时冲突，暂停和待核对同样占用分支。配置和计划版本在落库事务中再次核验。

Job 固定代码摘要、配置、输入、语料和初始计划版本；Worker 必须使用兼容代码和恢复环境。升级代码不会强行执行旧 Job；应先处理活动任务，再升级。使用新构建启动 Worker 时，代码不匹配返回 `unsupported_schema`，不重新调用 Adapter。不能用修改 payload 的方式绕过检查。

默认租约 15 秒，心跳间隔为租约的三分之一；接管增加 fencing token。失效 Worker 的调用响应和状态提交均被拒绝。暂停/取消通过 `request_control(..., expected_version=...)` 留下 requested 事件，抵达安全检查点才 applied；正在执行的外部调用不会被描述成已经撤销。普通暂停在同一 Run 恢复；首次领取后的持久截止时间不会因暂停、重启或接管而重置。

崩溃恢复封存原 Run 的 interrupted 事实并创建新 Run，原 Commit/检查点不改；新租约使用独立工作区，从完整 CAS 产物恢复。结果已保存时核验 occurrence、参数、工具签名、效果分类、checkpoint 和响应限制后复用；产物由录制写入内容重建，不再次调用工具。已有意图但缺结果时进入 `outcome_unknown`，保留预算预留并等待可信核对；核对前取消也仅记录请求。核对入口与继续语义见下节。

运行新增集成测试：

```powershell
uv run --locked pytest tests/integration/test_jobs.py tests/integration/test_kernel_migrations.py
```

真实进程故障用例仅启动仓库内受信任的 Worker 测试助手，并使用独立测试 Schema/临时 CAS；它们不构成用户 Shell/Python 执行功能。容器执行仍未实现，`doctor --sandbox` 继续返回 `execution_ready=false`。进展和验收缺口见 [S4-progress.md](acceptance/S4-progress.md)。

## S4 工具策略与精确审批

`RecordedCallGateway` 必须提供 `ApprovalService`；缺少策略、未注册 provider、签名或效果分类不匹配均在调用前拒绝。研究 Session 从受信任的本地 Tool Registry 与模型签名生成策略，允许现有三个工具和离线模型。模型和远程说明不能修改效果分类。`ToolPolicy` 的外部写与不可逆动作不能预授权；断网 sandbox 策略预授权只适用于后续受控容器 Adapter，不授权宿主或网络能力。

非预授权动作首次到达网关时，事务保存动作原文/策略和 `approval.requested`，Run/Job 转为 waiting_approval，保留 checkpoint 与分支绑定并释放租约。审批是操作元数据，此时不追加改变绑定 checkpoint 的逻辑 Commit；已经保存的 pending decision 和预算是恢复依据，未发送调用不扣除耐久预算。批准/拒绝通过 `ApprovalService.decide()`，要求相同 actor、预期 Run 状态版本、精确 checkpoint 和策略；同决定重复提交幂等，相反决定冲突。审批 HTTP/CLI 接口将在服务入口步骤接入。

批准只使原 Job 可重新领取，同 Run 使用新 fencing token；后续调用意图与 consumed 状态在同一事务提交，不能先消费再丢失意图。拒绝和过期不会启动 Adapter。Worker 每轮有界扫描等待中的过期审批，并重新排队以落实失败或已有取消请求。普通控制请求也在新调用意图事务内复查：若暂停/取消刚刚到达，则撤回未发出调用的内存计数并立即到安全点，不多发一次模型或工具请求。

`FakeEffectAdapter` 仅供独立集成测试使用，须显式登记 `http://127.0.0.1:<port>`；禁用环境代理与 HTTP 重定向，限定请求/回执大小，端点纳入工具签名。它不注册到离线研究 Demo，不写真实外部系统。测试服务器先 fsync 效果记录再回复或断连；断连时网关记录 `outcome_unknown` 与诊断引用，保留空 result_ref，不自动重发。`lookup()` 可核验服务回执，`resolve()` 供下述可信核对服务调用。审批证据见 [S4-approvals.md](acceptance/S4-approvals.md)。

## S4 未知结果核对与继续

`ReconciliationService.confirm(repository_id, run_id, call_id, actor=..., expected_version=..., resolver=...)` 只接受后端登记的 `OutcomeResolver`，不能把用户上传的 JSON 或“批准”当作外部动作已成功的证明。Resolver 必须匹配原 provider/签名，查询绑定原请求的结果，返回有界 `VerifiedOutcome(payload, evidence)`；查不到可信肯定回执时返回 None，Job 仍为 outcome_unknown，不安排重发。模型服务不支持结果查询时也继续保持未知；当前只验证离线模型与 fake effect fixture。

服务先短事务核对 Run/Branch/Job、来源路径、actor 与状态版本，退出事务后查询服务；再次校验相同停止点后，在一个事务内附着结果/证据、建立不可变 `call_resolutions`、追加审计事件并重新排队。重复同一核对返回原记录，不重复排队；失配、缺失、截断或无证据的回执不能发布。旧未知事件和诊断保留，核对 ID/actor 不写入逻辑 AgentState。

Worker 恢复该停止点时只消费核对结果，严格核验调用前状态、occurrence、请求、签名、预算与证据链接。已计入的步骤/调用数不会重置或重复增加，模型的预留按原请求与确认响应结算；未知轨迹替换为同一逻辑调用的确认轨迹。随后推进新检查点，再处理已有取消或到期预算，不再发送原写动作。核对后的 Worker 若崩溃，可创建新恢复 Run 继续；原 Run 检查点保留。

`tests/helpers/effectful_worker.py` 仅在集成测试中将一次搜索观察绑定到独立 fake 写服务，以验证审批→断连→未知→核对→完成的完整 Worker 流程；它不改变普通研究工具的只读分类。测试服务的实际 POST/效果计数是验收依据。运行 `uv run --locked pytest tests/integration/test_reconciliation.py`；证据见 [S4-reconciliation.md](acceptance/S4-reconciliation.md)。业务 HTTP/CLI 与真实容器执行继续在 S4 后续步骤实现。
