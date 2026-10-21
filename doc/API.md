# S4 本地服务与 CLI

当前支持本机操作者、固定离线模型、三个基础工具，以及显式启用的 `terminal_exec/code_exec` 真容器工具。[S4 本机验收](acceptance/S4.md)已通过。配置不会自动读取 .env，以 AGENTGIT_* 进程环境为准。先运行 `uv run --locked alembic upgrade head`，当前 head 为 `0009_sandbox_executions`。

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

除 POST /repositories 和公开探测外，业务请求须 X-Repository-ID；资源必须归属该项目，不匹配统一 404。修改另须 Idempotency-Key（1–200 字符），体为 application/json、schema_version=1。拒绝重复键、浮点数、未知字段及错误类型；实际接收上限 1 MiB，不依赖 Content-Length。

## 接口契约

| 方法与路径 | 输入/查询 | 结果 |
|---|---|---|
| GET /health、/ready | 公开 | 存活/真实数据库就绪，失败 503 |
| POST /repositories | name、expected_version=0 | 201，repository_id、version |
| GET /repositories/{id} | 项目头与路径一致 | 项目、当前主分支 HEAD/version |
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

错误含 error.code、message、evidence_refs（无证据时为空）、correlation_id，另有 X-Correlation-ID，不反射输入/URL/堆栈/令牌。validation=422、权限=403、不可访问=404、冲突=409、输出上限=413、配置/设施不可用=503、未预期错误=500。响应不缓存；JSON 日志写 stderr，仅含关联编号、方法、路由模板、状态、整数耗时，不含正文/认证头/查询/幂等键。

模型 usage_kind 为 mock_utf8_bytes，真实供应商用量未接入。批准不提升权限，执行仍检查当前策略与租约。可信核对使用后端 Resolver，不提供任意上传成功回执的 HTTP 接口。`doctor --sandbox` 只读核验执行器同一套 Linux/资源/seccomp/固定镜像前提；满足时返回 `execution_ready=true`，失败非零，不创建容器或下载镜像。
