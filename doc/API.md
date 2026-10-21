# 本地服务与 CLI（S4–S6）

当前支持本机操作者、固定离线模型、三个基础工具，以及显式启用的 `terminal_exec/code_exec` 真容器工具。[S4 本机验收](acceptance/S4.md)已通过。配置不会自动读取 .env，以 AGENTGIT_* 进程环境为准。先运行 `uv run --locked alembic upgrade head`，当前 head 为 `0012_replay_source_index`。

## CLI 离线流程

PowerShell 中，首次创建项目的主分支 HEAD 为空：

```powershell
$repo = uv run --locked agentgit init '本地研究' --key project-001 | ConvertFrom-Json
$config = uv run --locked agentgit config $repo.repository_id --expected-version 0 --key config-001 | ConvertFrom-Json
$task = uv run --locked agentgit task $repo.repository_id $config.config_id '比较三个 fixture 框架' --config-version 1 --key task-001 | ConvertFrom-Json
$job = uv run --locked agentgit run $repo.repository_id $task.task_id --config-version 1 --expected-branch-version 0 --key run-001 | ConvertFrom-Json
uv run --locked agentgit worker --once
uv run --locked agentgit status $repo.repository_id $job.job_id --job
uv run --locked agentgit artifacts $repo.repository_id $job.run_id
```

每个新动作使用新的幂等键，仅重试同一动作时复用。同键异参冲突，终态不原地重启。已有主分支先用 `agentgit status <repository_id>` 读取 branch.version/head_commit_id，再作为 run 的 --expected-branch-version/--expected-head；省略 --expected-head 表示预期空 HEAD。

config --file 读取 AgentConfig JSON；run --budget-file 读取 BudgetLimits JSON，均须 schema_version=1。预算默认继承 Task；显式预算每项不得扩大 Task/配置上限。worker 持续领取，--once 处理一次/空队列后退出；单次失败、预算耗尽或未知结果非零。命令与参数错误输出单个 JSON，--help 提供帮助。CLI 的本机身份由 AGENTGIT_OPERATOR_ID 设置，默认 local-operator，直接复用服务，不依赖 HTTP。

## 五工具容器流程

先按 [sandbox/README.md](../sandbox/README.md) 准备并核验已锁定镜像，再为新项目创建五工具配置：

```powershell
$repo = uv run --locked agentgit init '容器研究' --key execution-project-001 | ConvertFrom-Json
$config = uv run --locked agentgit config $repo.repository_id --with-execution --expected-version 0 --key execution-config-001 | ConvertFrom-Json
$task = uv run --locked agentgit task $repo.repository_id $config.config_id '比较三个框架，计算统计并整理产物' --config-version 1 --key execution-task-001 | ConvertFrom-Json
$job = uv run --locked agentgit run $repo.repository_id $task.task_id --config-version 1 --expected-branch-version 0 --key execution-run-001 | ConvertFrom-Json
uv run --locked agentgit worker --once
uv run --locked agentgit artifacts $repo.repository_id $job.run_id
```

`--with-execution` 使用包内已审阅策略启用两个执行工具，不能与 `--file` 同用。默认配置仍是三个基础工具，不探测 Docker。HTTP 的 `POST /agents` 可提交相同完整 `AgentConfig`：增加两个工具并绑定 `bundled_policy()` 对应策略；不同镜像或资源策略在创建配置时拒绝。工具只能由持久 Job/Worker 执行，不提供绕过任务的通用执行接口。

离线计划先生成报告和来源，再由实际 Python 脚本读取 `sources.json` 计算统计，实际 Shell 整理文件。成功有 7 个产物，统计在 `results/statistics.json`，整理后的报告在 `results/report.md`；13 次工具调用、15 次 Mock 模型调用。命令/脚本也作为产物保存，外部测试核验统计答案，验收器不进入容器。也支持仅授权一个执行工具。

只有完整成功、清理确认且通过校验的 manifest 才推进工作区。失败保留旧文件，完整结果保存后崩溃则复用录制并先结算调用预算，再处理取消或到期。缺完整结果的容器调用须先确认停止，再原子丢弃临时文件、结算旧尝试；原请求/输入/策略不变且仍有预算时才创建新容器。每个逻辑执行调用最多两次尝试，截止时间不重置。清理未知时保持待核对，每 30 秒有界重查，外部写入不能走此重试路径。

## 控制运行

活动 Run 的 pause/resume/cancel 均要求 --expected-version 和 --key，例如：

```powershell
$current = uv run --locked agentgit status $repo.repository_id $job.run_id | ConvertFrom-Json
uv run --locked agentgit pause $repo.repository_id $job.run_id --expected-version $current.state_version --key pause-001
uv run --locked agentgit worker --once
$paused = uv run --locked agentgit status $repo.repository_id $job.run_id | ConvertFrom-Json
uv run --locked agentgit resume $repo.repository_id $job.run_id --expected-version $paused.state_version --key resume-001
```

已完成的演示不能再暂停。控制先 requested，到安全点才 applied。普通恢复同 Run，creates_new_run=false；崩溃恢复新建 Run，应查询 Job 最新 run_id。未核对动作不会被假装撤销。approvals 分页检查动作原文，decision 使用 approve/reject；决定不直接执行工具。

## HTTP 身份与边界

本机生成令牌，不写入 Git、不作为命令行参数传递：

```powershell
$env:AGENTGIT_API_TOKEN = (uv run --locked python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$env:AGENTGIT_OPERATOR_ID = 'local-operator'
uv run --locked agentgit serve --port 8000
```

另一个终端启动 agentgit worker，使用相同数据库、目录、身份和代码。客户端须配置同一令牌并发送 Authorization: Bearer 头。令牌至少 32 个可见 ASCII 字符，无默认值；未配置为 configuration/503，缺失/无效为 unauthorized/401。令牌不进入 Job、幂等对象和日志。

serve 只监听 127.0.0.1。Origin 默认允许 http://127.0.0.1:8000、http://localhost:8000 及二者的 5173 端口。AGENTGIT_API_ORIGINS 为显式 JSON 数组，只接受回环 HTTP Origin，无通配符/用户信息/路径。预检只允许 GET/POST 和指定头，无 Cookie/credentials；无 Origin 的客户端仍须认证。Host 也受回环限制，不构成公网多租户授权。

除 POST /repositories、GET /capabilities 和公开探测外，业务请求须 X-Repository-ID；资源必须归属该项目，不匹配统一 404。GET /capabilities 仍须认证，不探测 Worker/Docker。修改另须 Idempotency-Key（1–200 字符），体为 application/json、schema_version=1。拒绝重复键、浮点数、未知字段及错误类型；实际接收上限 1 MiB，不依赖 Content-Length。

## 接口契约

| 方法与路径 | 输入/查询 | 结果 |
|---|---|---|
| GET /health、/ready | 公开 | 存活/真实数据库就绪，失败 503 |
| GET /capabilities | 认证，无项目头 | 固定版本、能力标识、默认三工具/固定五工具配置；Worker/执行环境为 unknown，不启动执行器 |
| GET /repositories/{id}/runs | after_id、limit=30（1–100）；项目头与路径一致 | 按 created_at/id 倒序的 Run 及当前关联 Job、目标；next_after_id；游标必须属于本项目 |
| GET /commits/{id}/artifacts | after_id、limit=30（1–100） | 所选历史版本的产物，按相对路径排序；游标限定同项目/Commit，下载仍按产物 ID |
| GET /runs/{id}/executions | after_id、limit=30（1–100） | Run 内 Shell/Python 逻辑调用，按逻辑步骤/调用序号/attempt 排序；不自动转到来源 Run |
| GET /runs/{id}/executions/{call_id} | 项目、Run 与调用归属 | 命令/代码参数、物理来源、结果、清理、耗时及可读资源类别；未发布结果为 null |
| GET /runs/{id}/executions/{call_id}/content/{resource} | offset=0、limit=65536（1–1048576） | 固定类别证据的原字节分段，不接受任意 CAS 摘要或宿主路径 |
| POST /repositories | name、expected_version=0 | 201，repository_id、version |
| GET /repositories/{id} | 项目头与路径一致 | 项目、当前主分支 HEAD/version |
| POST /branches | source_run_id、from_commit、name、expected_version=0 | 201，从安全检查点建立分支，保存不可变来源；同名冲突 |
| POST /replays | source_run_id、from_commit、branch_name；recorded 要求 until_commit；live 可提供 overrides/budget；expected_version=0 可省略 | 202，原子创建独立 Task/Job/Run/分支及来源，执行由 Worker 负责 |
| GET /replays/{job_id} | 项目头 | Job 状态、重放范围/证明进度、逻辑状态/用量、独立实际计量 |
| GET /repositories/{id}/branches | after_name 可省略、limit=1..1000 | 分支 HEAD/version 与来源、has_more、next_after_name |
| GET /repositories/{id}/commits | branch_name 默认 main、after_commit 可省略、limit=1..1000 | 当前分支从新到旧的 Commit manifest、has_more、next_after_commit |
| GET /commits/{id} | 项目头 | 完整 manifest、环境和 snapshot，只读且核验内容 |
| GET /diff | left_commit、right_commit、after≥0、limit=1..1000 | common_ancestor（可为 null）、字段/产物/调用变化、total_changes、next_after、has_more |
| POST /agents | config 可省略、expected_version | 201，不可变 config_id、递增 version |
| POST /tasks | config_id、config_version、goal、expected_version=0 | 201，task_id |
| POST /tasks/{id}/runs | config_version、expected_branch_version、expected_head；branch_name 默认 main；budget 可省略 | 202，耐久 job_id/run_id |
| GET /runs/{id}、/jobs/{id} | 项目头 | 状态、checkpoint、控制、版本、预算与剩余量 |
| POST /runs/{id}/pause、resume、cancel | expected_version | 202 受理结果，之后查询实际状态 |
| GET /runs/{id}/events | after_seq≥0、limit=1..1000 | items、next_after_seq，按耐久 seq 升序 |
| GET /runs/{id}/artifacts | 项目头 | 当前 checkpoint 的产物 |
| GET /artifacts/{id}/content | 项目头，不接收磁盘路径 | 核验 CAS 原字节、attachment、nosniff、ETag=manifest object_id |
| GET /approvals | run_id、after_seq≥0、limit=1..1000 | 动作/请求原文、摘要、有效期、actor、状态、游标 |
| POST /approvals/{id}/decision | decision=approve/reject、expected_version | 202 精确决定，不直接启动动作 |
| GET /metrics | 项目头 | 项目 Job 状态；HTTP 请求/错误为当前进程总量 |

goal 示例：`{"schema_version":1,"text":"比较三个 fixture 框架"}`。config/budget 使用 AgentConfig/BudgetLimits；budget 缺字段使用该类型默认值，应从既有 limits 完整复制后收紧。新根项目的 Run 请求：

```json
{"schema_version":1,"config_version":1,"expected_branch_version":0,"expected_head":null,"branch_name":"main"}
```

API/CLI 共用事务和幂等记录。同 actor/route/key 成功重试返回原响应，即使状态已推进；业务修改、响应引用、幂等记录同时提交，失败全回滚。实时状态使用 GET/status。旧版本或分支占用为 409，已有结果/审批不重复执行。

## S5 分支、历史与差异

使用已完成任务的 Run 和 Commit，先读取历史，再选择安全节点：

```powershell
uv run --locked agentgit log $repo.repository_id --branch-name main --limit 20
uv run --locked agentgit checkout $repo.repository_id $commitId
uv run --locked agentgit branch $repo.repository_id --name experiment-a --source-run $job.run_id --from-commit $commitId --key fork-a-001
uv run --locked agentgit branch $repo.repository_id --limit 20
uv run --locked agentgit diff $repo.repository_id $leftCommit $rightCommit --limit 100
```

`$commitId/$leftCommit/$rightCommit` 必须是本项目完整的 64 位小写摘要。Fork 的 `source_run_id` 必填，用于限定来源路径及检查尚未核对的效果；不能只凭摘要继承旧运行的权限。新分支 version=0、HEAD=from_commit，原分支和历史保持不变。Checkout 返回历史数据，不创建可执行工作区。分页分别使用 `--after-name`、`--after-commit`、`--after` 延续服务返回的游标。

HTTP 创建体示例（示例占位符须替换）：

```json
{"schema_version":1,"source_run_id":"<Run UUID>","from_commit":"<Commit digest>","name":"experiment-a","expected_version":0}
```

### Recorded 耐久任务

Replay 自行创建新分支，不必预先执行 `branch`；同名分支冲突，不覆盖旧分支。通过 Fork 创建的历史分支不能用普通 `run` 隐式选择执行模式，应使用 Replay。入口接受 Recorded 和 Live，也允许已有 Recorded Run 作为再次 Recorded 的来源。

```powershell
$replay = uv run --locked agentgit replay $repo.repository_id --source-run $job.run_id --from-commit $fromCommit --until-commit $untilCommit --branch-name recorded-a --key replay-001 | ConvertFrom-Json
uv run --locked agentgit worker --once
uv run --locked agentgit status $repo.repository_id $replay.job_id --job
```

HTTP 创建体（替换示例占位符），使用既有身份/项目/幂等头：

```json
{"schema_version":1,"mode":"recorded","source_run_id":"<Run UUID>","from_commit":"<Commit digest>","until_commit":"<Commit digest>","branch_name":"recorded-a","expected_version":0}
```

Recorded 不接受 `overrides/config/budget/prompt/model` 等干预字段。提交先验证范围、完整录制及环境兼容性，只保存耐久任务，不创建执行工作区或调用模型/工具。运行中丢失记录或产物会失败，不回退 Live。起点必须可安全 Fork；`from=until` 合法，只恢复起点，不伪称执行过调用。

`status --job`、`GET /jobs/{id}` 和 `GET /replays/{id}` 的 `replay` 字段区分三类事实：`status=completed` 是新 Job 已结束；`replay.verified=true` 是选定历史范围重算验证通过；`logical_status/logical_usage` 是原历史逻辑状态与用量。选择非终态终点也可完成 Recorded，不能因此认定原任务成功或修复有效。`actual.model_calls/tool_calls/cost_micro_units` 为零；`actual.elapsed_ms` 仅累计已结束尝试的实测耗时，`unmeasured_attempts` 明确标记崩溃等未完成计量的尝试，不把未知耗时记为零。

实际重放暂停/恢复沿用 Run，取消/到期只结束新任务，不改历史逻辑状态；持久截止时间不因重启重置。崩溃接管创建新的 Recorded Run，重算并核验已保存前缀，继续保存剩余证明。原幂等响应保持最初 Job/Run ID，最新 Run 通过 Job 状态查询。版本/录制和耐久任务证据见 [S5-progress.md](acceptance/S5-progress.md)、[S5-recorded-jobs.md](acceptance/S5-recorded-jobs.md)，完整阶段验收见 [S5.md](acceptance/S5.md)。

### 从 Recorded 再次回放

使用相同 `mode=recorded` 请求，把 `source_run_id` 指向已有 Recorded Run，起止点选择该 Run 的 base 或自身保存的连续检查点。恢复 Run 可从自身 base 继续选择范围；不接受任意祖先、其他 Run 的检查点或跳过的证明。

服务逐层核验冻结 Job、不可变请求、证明顺序/归属、父关系、状态/环境及事件索引，再追溯原始录制驱动纯 Runtime。新的证明仍逐项对应用户选择的直接来源 Commit。来源中的 `replay.restored` 是零调用恢复边界，保留为独立检查点；后续状态必须实际重算，不能用旧证明快照代替。

链中原始响应、执行日志或文件缺失仍失败，不因已有成功证明而跳过验证。全链共享 64 MiB 读取预算，最多 32 层解析，超限为 `output_limit`；不自动裁剪链或回退 Live。实际调用计量仍为零，暂停/恢复、取消、持久期限和崩溃接管保持原语义。详情与范围证据见 [S5-compound.md](acceptance/S5-compound.md)。

对于原 Runtime 已保存未知状态、之后取得可信核对回执的模型历史，Recorded 会先重现未知诊断，再结算确认结果，保留原预算和控制顺序。缺诊断、缺证据或结果链接错配会失败，不调用 Resolver 重查。若终点是确认之前的未知检查点，可验证该历史状态，但仍显示 `logical_status=outcome_unknown`，不能把 `verified` 当作任务成功。接管封存及自定义外部写等其他路径的支持边界见 [S5-settlement.md](acceptance/S5-settlement.md)。

### Live 独立重执行

Live 只提供 `from_commit`，拒绝 `until_commit`。它创建新配置、Task、Job、Run 和工作区，重新执行剩余计划；不要求输出与来源一致。可选 `budget` 是新配置授权上限内的完整 `BudgetLimits`，不减去来源已花费的预算。历史累计用量保留，冻结 `budget_baseline` 后只按新增差额检查配额；恢复继续同一基线及持久截止时间。

`overrides` 可含 `config`（完整 `AgentConfig`）、`plan`（完整 `Plan`）、`artifacts`、`execution`。计划不能修改或删除已完成节点，剩余节点的工具须获新配置授权。配置/计划干预会重新产生待执行决策；旧未完成 attempt、停止证书和待结算轨迹不会成为新 Job 的复用依据。

产物干预使用安全相对路径，每项 `operation=write`（默认）必须在 `text` 与严格 `content_b64` 中二选一，`media_type` 可选；`operation=delete` 不能携带内容/MIME，且目标必须存在。重复路径、Windows 大小写冲突、越界路径和文件上限均拒绝。

执行干预选择 `tool=code_exec/terminal_exec`、版本化文件 `path`，可指定 `node_key`、`cwd`、`stdin_path`、`timeout_ms`，Python 另可提供 `argv`。必须精确匹配一个就绪节点；后端依据新工作区重新生成输入清单和策略引用。终端命令写在文件中，不接收独立 `argv`。不能上传任意 CAS 引用、镜像、容器权限或审批。

```json
{"schema_version":1,"mode":"live","source_run_id":"<Run UUID>","from_commit":"<Commit digest>","branch_name":"live-a","overrides":{"artifacts":[{"path":"execution/statistics.py","text":"from pathlib import Path\nPath('result.txt').write_text('A')\n"}],"execution":{"tool":"code_exec","path":"execution/statistics.py"}}}
```

```powershell
uv run --locked agentgit replay $repo.repository_id --mode live --source-run $job.run_id --from-commit $fromCommit --branch-name live-a --key live-a-001 --overrides-file overrides.json --budget-file budget.json
uv run --locked agentgit worker --once
```

两个文件分别保存上述 `overrides` 对象和完整 `BudgetLimits` JSON；均可省略。Recorded 模式提供这两个文件会被拒绝。

`replay.logical_usage` 是保留的累计用量，`budget_baseline/new_usage` 展示来源基线与新分支消耗；`actual.model_calls/tool_calls` 从新 Job 的物理调用账本统计，排除恢复复用，未知结果另报 `unknown_outcomes`。调用意图已发出但结果未知仍计入实际尝试，不声称外部服务一定收到。`actual.wall_ms` 是新 Job 已启动后的墙钟时间；模型仍为离线 Mock，成本为零。源 Run 的审批不能授权新 Run 的相同参数。

新 Run 首次保存 `live.initialized`，将干预后的状态、文件和新环境固定为检查点；其后可作为 Recorded 来源，预算基线一并验证，部分范围的 `budget.remaining` 也只扣除该基线之后的用量。跨越来源旧环境与 Live 新环境的 Recorded 范围会因环境不兼容拒绝。专项证据见 [S5-live.md](acceptance/S5-live.md)。

错误含 error.code、message、evidence_refs（无证据时为空）、correlation_id，另有 X-Correlation-ID，不反射输入/URL/堆栈/令牌。validation=422、权限=403、不可访问=404、冲突=409、输出上限=413、配置/设施不可用=503、未预期错误=500。响应不缓存；JSON 日志写 stderr，仅含关联编号、方法、路由模板、状态、整数耗时，不含正文/认证头/查询/幂等键。

模型 usage_kind 为 mock_utf8_bytes，真实供应商用量未接入。批准不提升权限，执行仍检查当前策略与租约。可信核对使用后端 Resolver，不提供任意上传成功回执的 HTTP 接口。`doctor --sandbox` 只读核验执行器同一套 Linux/资源/seccomp/固定镜像前提；满足时返回 `execution_ready=true`，失败非零，不创建容器或下载镜像。
