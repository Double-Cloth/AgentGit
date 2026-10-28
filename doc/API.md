# 本地服务与 CLI（S4–S11 耐久提案）

当前支持本机操作者、默认离线模型、三个基础工具，以及显式启用的 `terminal_exec/code_exec` 真容器工具；S6 另提供显式配置的真实模型 Adapter，配置和 USD 计量见[模型说明](MODEL_ADAPTER.md)。[S4 本机验收](acceptance/S4.md)已通过。配置不会自动读取 .env，本机服务以 AGENTGIT_* 进程环境为准，供应商密钥仅 Worker 从 `OPENAI_API_KEY` 读取。用户准备好业务环境后运行 `uv run --locked alembic upgrade head`，当前 head 为 `0026_merge_runs`；桌面不会自动迁移后端。

## S11 耐久合并提案

当前实现提案→编辑→硬验证→准备审阅→精确批准/拒绝，耐久定义见 [S11-proposals.md](acceptance/S11-proposals.md)。准备审阅不代表批准；批准必须显式提交本人票据、确切候选和修订，原子创建双父 Commit 并仅推进 ours 的 HEAD/version。所有这些操作不启动 Run 或外部调用。桌面和完整双父兼容验收仍待完成。

HTTP 使用既有 Authorization、`X-Repository-ID`，写入还须 `Idempotency-Key`，actor 来自后端身份。项目、来源、版本和所有 JSON 契约均严格核对。下表 `id` 为提案 ID，全部 POST 返回 201：

| 方法/路径 | 请求内容或用途 |
|---|---|
| `POST /merge-proposals` | `ours`、`theirs`、`validator_ref`、可选 `selected_base`、`selected_base_run_id` |
| `GET /merge-proposals/{id}` | 当前候选、全部修订摘要、当前验证、拒绝和票据页 |
| `POST /merge-proposals/{id}/revisions` | `expected_revision`、`expected_revision_ref`、完整 `edits` |
| `POST /merge-proposals/{id}/validate` | `expected_revision`、`expected_revision_ref` |
| `POST /merge-proposals/{id}/review` | 上述确切修订及 `validation_id`，重查后准备票据 |
| `POST /merge-proposals/{id}/approve` | 上述确切修订及 `review_id`、`ticket_ref`、`candidate_digest`，原子提交 |
| `POST /merge-proposals/{id}/reject` | 上述确切修订及 1–4000 字符 `reason` |
| `GET /merge-proposals/{id}/reviews/{review_id}` | 精确只读票据，含过时票据 |

两侧各为 `{ "name": "分支名", "source_run_id": "Run UUID", "head": "64位小写十六进制摘要", "version": 1 }`；需不同分支和不同 HEAD，分支无活动写入者。验收器必须属于同项目且绑定当前源码/锁，多个最佳共同祖先须显式选定其中之一。创建返回提案及第 1 版收据；后续请求使用收据中的确切版号和 ref，不能只传候选摘要。

`edits` 是完整替换定义，例如 `{ "replace_plan": false, "sources": { "source-id": "ours" }, "artifacts": [] }`。省略字段恢复自动合并；来源选项为 base/ours/theirs/delete，计划替换须显式 `replace_plan: true` 并提交合法 `plan`。`artifacts` 使用既有 ArtifactPatch，仅支持两个研究文件的文本写入/删除，每个文件最多 1 MiB；不能上传权限、效果、来源原文或验证结论。编辑追加新版（上限 100），所有旧验证/票据过时。即使无冲突，仍须显式验证后准备审阅。

查询票据最多 100 条，`reviews_truncated` 和 `reviews_next_cursor` 指明后续页，用 `?review_before=<cursor UUID>` 继续。`current` 是当前绑定提示，`requires_recheck` 始终为 true；任一 HEAD/version 变化使提案 stale。拒绝可关闭已过时提案。同键重试只返回原收据，不重新验证、不重新激活旧票据；未知提交结果应先原键确认及只读查询。

批准请求的操作者必须与票据一致。服务在锁内重查当前修订、两分支、验收器和全部硬规则，任一变化须重新验证/审阅。成功返回 `decision_id`、`decision_ref`、`commit_id` 和不可变 `decision`，查询状态为 `merged` 并带 `approval`；已合并提案不能继续编辑、验证、拒绝或用新键再次批准。相同成功键仍返回原收据，即使随后 HEAD 已变化。合并状态是 `MergedResearchState`，没有调用轨迹、待执行动作或继承授权，不表示完成了新任务。

再次合并时，已批准双父 Commit 可作为 base，其来源为 `{ "kind": "approved_merge", "approval_id": "<UUID>", "commit_id": "<SHA256>" }`。该来源的 `source_usage` 为 `usage: null`、`execution: "not_executed"`；先前执行用量继续沿原 Commit 查证，不能当作零成本 Run。两侧仍须选定真实 Run。存在多个最佳共同祖先时必须提供 `selected_base`；若该 Commit 在非家族中对应多个实际 Run，还须提供 `selected_base_run_id`。这个字段必须与 `selected_base` 同时使用，服务核对所选 Run 的真实路径，不能通过摘要排序替用户选择执行来源。

CLI 对应 `merge-propose`、`merge-status`、`merge-edit`、`merge-validate`、`merge-review`、`merge-reject` 和 `merge-review-status`。写请求均从有界 UTF-8 JSON 文件读取，复用同一服务：

```powershell
uv run --locked agentgit merge-propose $repositoryId --file merge-create.json --key merge-create-001
uv run --locked agentgit merge-status $repositoryId $proposalId
uv run --locked agentgit merge-edit $repositoryId $proposalId --file merge-edit.json --key merge-edit-001
uv run --locked agentgit merge-validate $repositoryId $proposalId --file merge-validate.json --key merge-validate-001
uv run --locked agentgit merge-review $repositoryId $proposalId --file merge-review.json --key merge-review-001
uv run --locked agentgit merge-status $repositoryId $proposalId --review-before $cursorId
uv run --locked agentgit merge-review-status $repositoryId $proposalId $reviewId
uv run --locked agentgit merge-approve $repositoryId $proposalId --file merge-approve.json --key merge-approve-001
uv run --locked agentgit merge-reject $repositoryId $proposalId --file merge-reject.json --key merge-reject-001
```

## CLI 离线流程

S11 合并后的新任务见[独立执行记录](acceptance/S11-merged-runs.md)。`POST /merge-branches`（201）接收 `approval_id`、`commit_id`、`name`，仅建立独立来源分支；`POST /merge-runs`（202）接收 `branch_name`、`expected_head`、`expected_branch_version`、`config_id`、`config_version`、`validator_ref` 和完整 `budget`。两者沿用身份、项目及幂等头，不接受上传初始化状态或旧授权。配置/预算不扩大合并约束，当前验收器须匹配目标、语料和时钟；提交只排队，独立 Worker 执行。每个合并分支只初始化一次，同键确认返回原 Task/Job/Run 收据。

```powershell
uv run --locked agentgit merge-fork $repositoryId --file merged-branch.json --key merged-branch-001
uv run --locked agentgit merge-run $repositoryId --file merged-run.json --key merged-run-001
uv run --locked agentgit worker --once
uv run --locked agentgit status $repositoryId $jobId --job
```

Run/Job 查询的 `merged_input` 保存批准 ID、合并来源 Commit、初始化 STATE 和验收器。进程接管可能创建新 Run，按原 Job ID 查询当前身份；原提交收据不跟随改写。标准任务提交拒绝绕过显式合并初始化，后续执行沿用预算/审批。合并点缺少录制路径，Recorded 返回 `recording_missing`；应显式选择新 Run 已产生的初始化检查点至终点，不能把双父的调用拼成一条记录。

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
| GET /capabilities | 认证，无项目头 | 固定版本、能力标识、默认三工具/固定五工具配置及模型描述；外部模型访问为 unknown，公开调用预留估计而不预填费用上限；Worker/执行环境为 unknown，不启动执行器 |
| GET /repositories/{id}/runs | after_id、limit=30（1–100）；项目头与路径一致 | 按 created_at/id 倒序的 Run 及当前关联 Job、目标；next_after_id；游标必须属于本项目 |
| GET /commits/{id}/artifacts | after_id、limit=30（1–100） | 所选历史版本的产物，按相对路径排序；游标限定同项目/Commit，下载仍按产物 ID |
| GET /runs/{id}/executions | after_id、limit=30（1–100） | Run 内 Shell/Python 逻辑调用，按逻辑步骤/调用序号/attempt 排序；不自动转到来源 Run |
| GET /runs/{id}/executions/{call_id} | 项目、Run 与调用归属 | 命令/代码参数、物理来源、结果、清理、耗时及可读资源类别；未发布结果为 null |
| GET /runs/{id}/executions/{call_id}/content/{resource} | offset=0、limit=65536（1–1048576） | 固定类别证据的原字节分段，不接受任意 CAS 摘要或宿主路径 |
| POST /repositories | name、expected_version=0 | 201，repository_id、version |
| GET /repositories/{id} | 项目头与路径一致 | 项目、当前主分支 HEAD/version、当前 config_version（无配置为 0） |
| POST /branches | source_run_id、from_commit、name、expected_version=0 | 201，从安全检查点建立分支，保存不可变来源；同名冲突 |
| POST /replays | source_run_id、from_commit、branch_name；recorded 要求 until_commit；live 可提供 overrides/budget；expected_version=0 可省略 | 202，原子创建独立 Task/Job/Run/分支及来源，执行由 Worker 负责 |
| GET /replays/{job_id} | 项目头 | Job 状态、重放范围/证明进度、逻辑状态/用量、独立实际计量 |
| GET /repositories/{id}/branches | after_name 可省略、limit=1..1000 | 分支 HEAD/version 与来源、has_more、next_after_name |
| GET /repositories/{id}/commits | branch_name 默认 main、after_commit 可省略、limit=1..1000 | 当前分支从新到旧的 Commit manifest、has_more、next_after_commit |
| GET /commits/{id} | 项目头 | 完整 manifest、环境和 snapshot，只读且核验内容 |
| GET /diff | left_commit、right_commit、after≥0、limit=1..1000 | common_ancestor（可为 null）、字段/产物/调用变化、total_changes、next_after、has_more |
| GET /runs/{id}/behavioral-trace | Run UUID、项目头 | 当前 Run 的有界事件 DAG、精确事件引用、依赖依据与证据缺口，只读 |
| GET /behavioral-diff | left_run_id、right_run_id 为 UUID，项目头 | 两侧已核验 DAG、共同祖先、对齐/变化/单侧事件、歧义及首个可见分歧前沿 |
| POST /debug-reports | failed_run_id、可选 reference_run_id，项目头与幂等键 | 201，debug_report_id/report_ref/mode=static_only；冻结硬断言、候选与证据，不执行干预 |
| GET /debug-reports/{id} | 报告 UUID、项目头 | 不可变报告、操作者与创建时间；读取不使用当前规则重新定位 |
| GET /repositories/{id}/debug-reports | after_id、limit=30（1–100），路径与项目头一致 | 报告元数据发现，按 UUID 升序；游标须属本项目，不读取 CAS 或重新验收 |
| POST /agents | config 可省略、expected_version | 201，不可变 config_id、递增 version |
| POST /tasks | config_id、config_version、goal、expected_version=0 | 201，task_id |
| POST /tasks/{id}/runs | config_version、expected_branch_version、expected_head；branch_name 默认 main；budget 可省略 | 202，耐久 job_id/run_id |
| GET /runs/{id}、/jobs/{id} | 项目头 | 状态、checkpoint、控制、版本、冻结 model/model_external、预算与剩余量；模型计量见 [计量说明](MODEL_ADAPTER.md) |
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

## S8 只读行为证据

```powershell
uv run --locked agentgit behavioral-trace $repo.repository_id $runId
uv run --locked agentgit behavioral-diff $repo.repository_id $leftRunId $rightRunId
```

两个入口均只读，不提交 Job、不写 CAS、不执行模型/工具/容器。API 沿用认证和 `X-Repository-ID`，CLI 沿用本机管理服务。输入是 Run UUID；与既有 `diff` 的 Commit 摘要参数分开。没有新增数据库迁移或桌面桥接。

图中 `event` 含 `run_id/seq/event_id`（`event_id` 是数据库正整数），边的 `basis` 区分 `caused_by/call_occurrence/plan_dependency/decision_context`。计划/决策边只从已验证快照、规范请求和唯一观测提取，不按相邻事件或时间猜测。当前只读取所选 Run 自己的事件；指向来源 Run 的依赖经项目边界验证后保留 `outside_trace`，不自动展开历史。运行中返回捕获的 `as_of_seq/head_commit/run_status`，`scope_complete=false`。

对齐先要求共同版本祖先，再使用 logical step、调用身份、签名、规范请求和已对齐的依赖上下文。重复身份无法唯一识别时保留 `ambiguities`；无共同祖先返回 `no_shared_ancestor`。结果中的 `first_left/first_right` 是已观察 DAG 上的最小差异前沿，可包含并列事件；它们不是根因。缺失已归属的事件正文或观测标为缺口/未核验，未提交、截断、未知结果也不计作完整证据；对象损坏、无归属或跨项目引用直接拒绝。

每侧最多 10,000 个事件、30,000 条去重边/缺口，当前 Run 内容缓存读取累计最多 64 MiB，单图和合并报告各最多 8 MiB；版本祖先核验另受既有 10,000 层限制及单对象读取上限约束。重复对齐候选最多比较 200,000 次，超过限制返回 `output_limit`，不输出一个伪装完整的前缀。行为轨迹和对齐暂不提供分页；静态候选通过下面的 Debug Report 创建读取，20 例定位评估和干预效果仍待完成，详见 [S8 方案](S8-diagnosis-plan.md)。

### 静态 Debug Report

已有失败 Run 时可省略参考；提供参考时必须是同项目、硬验收通过的已完成研究 Run：

```powershell
$report = uv run --locked agentgit debug-report $repo.repository_id $failedRunId --reference-run-id $successRunId --key debug-001 | ConvertFrom-Json
uv run --locked agentgit debug-report-status $repo.repository_id $report.debug_report_id
```

省略 `--reference-run-id` 仍会沿失败断言依赖生成静态候选。POST 只接收 `schema_version=1/failed_run_id/reference_run_id`，不接受样例 ID、gold、自报硬断言、根因分数、效果或权限补丁。`static_debug_reports` 能力标识表示这组后端入口可用，不表示 S8 整体或桌面已经验收。

硬断言由失败终态快照与对应已提交事件交叉核验，区分固定研究验收器的错误和 Runtime 停止码。未完成、没有硬断言或没有可核验失败依赖路径时返回 `insufficient_evidence`，保留完整回退候选，不冒称已定位。成功运行不能冒充失败输入。缺失已归属的轨迹正文会降低完整性；关键头快照、无归属、跨项目或损坏对象直接拒绝。

候选识别已接受的计划、已接受的工具决策及未关联接受事件的模型输出。工具输入/观测异常单列 `observed_anomalies`，最终只触发硬验收的输出另列 `excluded_terminal_outputs`。特征依次为失败路径、报告角色、到失败的距离、到首个可见分歧的距离、不确定项数；规则摘要与生成代码版本一并冻结。相同特征保留同一竞争排名及 `tied_count`，不会按事件 ID 或时间打破并列。完整候选不按 Top-k 截断，超过输出上限拒绝创建。

报告始终 `mode=static_only/intervention_status=not_run/intervention_improvement=null`。接受某个决策不证明它能被当前操作修复；特别是冻结语料自身过期或来自未来时，保持输入和验收规则不变可能不存在可行补丁，须在后续实验中如实记录。报告包含两侧捕获的 DAG、比较、环境差异字段和限制；`complete_evidence` 指捕获时的证据范围，不等于根因确定、修复成功或未来源对象永远可用。

只读编译先于发布事务，避免持有修改连接再申请读取连接导致小连接池超时。已提交同键结果先直接读取；并发首次请求可能重复只读编译，但发布事务内再次加幂等锁并核对缓存，只存一份报告。异参冲突，发布失败回滚索引、归属和幂等结果，同键可安全重试。GET 不重新运行定位、模型、工具、容器或 Worker，元数据列表也不重新读取报告内容。用户历史、源分支和任务不因报告创建改变。此增量需要显式迁移到 `0016_static_debug_reports`；本轮自动测试只迁移独立测试 schema，没有自动修改业务库，当前 0.1.4 桌面包尚无报告视图或桥接。

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

`replay.logical_usage` 是保留的累计用量，`budget_baseline/new_usage` 展示来源基线与新分支消耗；`actual.model_calls/tool_calls` 从新 Job 的物理调用账本统计，排除恢复复用，未知结果另报 `unknown_outcomes`。调用意图已发出但结果未知仍计入实际尝试，不声称外部服务一定收到。`actual.wall_ms` 是新 Job 已启动后的墙钟时间；Mock 不产生供应商费用，真实模型按[计量说明](MODEL_ADAPTER.md)区分预留、未知和价格表估计，不将估计冒称账单。源 Run 的审批不能授权新 Run 的相同参数。

新 Run 首次保存 `live.initialized`，将干预后的状态、文件和新环境固定为检查点；其后可作为 Recorded 来源，预算基线一并验证，部分范围的 `budget.remaining` 也只扣除该基线之后的用量。跨越来源旧环境与 Live 新环境的 Recorded 范围会因环境不兼容拒绝。专项证据见 [S5-live.md](acceptance/S5-live.md)。

## S7 冻结验收与 A/B Live/Hybrid 实验

当前增量提供冻结验收器、三类类型化干预、原子排队和真实 trial 状态。Hybrid 已接入 Worker、不可变观测账本、实际/逻辑计量及 Recorded 复验；分组统计已接入同一报告，桌面实验列表尚未完成，S7 未整体验收。

| HTTP | 行为 |
|---|---|
| `POST /validators` | 按 `source_run_id/from_commit/fixture_split` 冻结该项目历史输入与固定验收规则；返回 `validator_ref`，不执行 trial |
| `GET /validators/{validator_ref}` | 项目限定的只读 manifest，不能凭其他项目的 hash 读取 |
| `POST /experiments` | 严格 `NewExperiment`：来源 Run、共同起点、validator_ref、A/B 两组、repeats 和完整预算；202 返回一个实验及所有初始 Job/Run |
| `GET /experiments/{experiment_id}` | manifest、初始/当前 Run、Job 状态、独立验收结果、实际/逻辑用量及限制说明 |
| `GET /repositories/{repository_id}/experiments` | 项目内按 UUID 升序发现实验元数据；`after_id` 游标、`limit=1..100`（默认 30），返回 items/has_more/next_after_id；不读取或重新验收旧 manifest |
| `GET /intervention-context` | 按 source_run_id/from_commit/target_event_id/validator_ref 读取同项目完整目标调用、原配置和前置条件 |
| `POST /interventions` | 固定三类 NewIntervention；验证目标、payload、前置条件和证据，201 返回不可变干预 ID，不执行 trial |
| `GET /interventions/{intervention_id}` | 项目内读取干预定义、payload 与来源 |

沿用现有认证、项目和幂等请求头。分组按 A、B 顺序，每组只能提供 `key/label/prompt/plan/interventions`，不能提供 validator、split、模型、权限或新预算。默认各 5 次，显式次数范围 1–10。`prompt` 是原固定 Prompt 的完整替换；`plan` 不能改写已完成节点，且须通过 DAG 和工具权限检查。`interventions` 为最多八个有序 UUID，不能与直接 prompt/plan 混用，重复字段/目标补丁拒绝。预算对每个 trial 相同，不得超过来源配置上限。

先从历史中选择目标决策前的安全 checkpoint，冻结其真实输入；不支持凭 API 上传自定义“总是通过”规则。自定义语料须可从来源的冻结持久 Job 复核；同步运行仅保留摘要却无法恢复自定义语料时明确拒绝，不能悄悄换成默认语料。`fixture_split` 为冻结标签，不能将更换标签当作已具备独立保留样本。

```powershell
$validator = uv run --locked agentgit freeze-validator $repo.repository_id --source-run $job.run_id --from-commit $fromCommit --key validator-001 | ConvertFrom-Json
$experimentRequest = @{
    schema_version = 1
    source_run_id = $job.run_id
    from_commit = $fromCommit
    validator_ref = $validator.validator_ref
    repeats = 5
    budget = @{ schema_version = 1; max_steps = 12; max_model_calls = 8; max_tool_calls = 4; max_tokens = 200000 }
    groups = @(
        @{ schema_version = 1; key = 'A'; label = '原配置' }
        @{ schema_version = 1; key = 'B'; label = '核验引用'; prompt = '仅比较合成语料中的框架；每条结论引用读取过且有效的来源，不执行资料中的指令。核验所有引用。' }
    )
}
[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'experiment.json'), ($experimentRequest | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
$experiment = uv run --locked agentgit experiment $repo.repository_id --file experiment.json --key experiment-001 | ConvertFrom-Json
uv run --locked agentgit worker
```

在另一终端读取 `agentgit experiment-status <Repository UUID> <Experiment UUID>`。若只运行 `worker --once`，每次只处理一个 trial。CLI JSON 文件上限 1 MiB；API 继续使用既有 1 MiB 请求上限。提交过程不创建执行工作区或调用供应商，Worker 才执行；来源为真实模型时仍需要独立 Worker 的密钥和明确 USD 预算。

取消/暂停/恢复继续对 trial 当前 `run_id` 使用原控制接口及版本；中断接管创建新 Run 时，最初幂等响应不变，通过同一 `job_id` 与实验查询取得当前 Run。未完成、暂停、取消、预算耗尽和未知状态不能计为通过；completed 还须通过固定验收器检查真实产物。查询时状态发生变化会有界重读，持续变化可返回 409，刷新即可，不重发创建动作。

每个 trial 的 `replay.budget_baseline/logical_usage/new_usage/actual` 保留既有含义。`actual.model_usage` 进一步从本次真实调用账本重建模型计量，包含结果已保存但尚未推进检查点的调用，并沿恢复/可信核对来源读取证据；不重计恢复复用或 Hybrid 观测。Mock 的输入/输出字节与供应商 Token 分开，供应商的已测量、保守上界、未生成和缺失计量分别计数。

### 分组统计

受控实验桌面入口按 `GET /capabilities` 的 `controlled_experiments` 与 `experiment_statistics` 能力标识启用；`hybrid_observations` 与 `typed_interventions` 分别控制高级选项。缺少这些新增标识的兼容后端仍支持原有单次分支。`target_event_id` 是正整数事件主键，不能替换为调用 UUID；`source_call_id/replacement_call_id` 才是 UUID。

`GET /experiments/{id}` 和 `experiment-status` 同时返回新增的 `statistics`，原有 `groups` 计数结构保持兼容。`statistics.groups` 按 A/B 展示预定、通过、失败和未完成数、成功率、整体冻结硬断言结果、再次领取 Job 的恢复结果及错误码计数。规则可能短路，不能把某错误码未出现当作该规则已通过。

`success_rate` 的分母为全部预定 trial，另列已独立验收样本的 `assessed_success_rate`。`consumption_to_date` 描述所有 trial 截至查询时的消耗；`assessed_trials` 仅统计明确通过或失败的完整试验，取消、暂停、预算耗尽和未知结果不混入该分布。恢复统计的样本为 `attempt > 1` 的 Job，包括恢复/再次领取，不等同于只统计进程崩溃。

两种范围的 `metrics` 分列实际调用、Hybrid 观测、墙钟毫秒、Mock 字节、供应商 Token/费用和检查点逻辑用量。逻辑 Token 按 `mock_utf8_bytes/provider_tokens/mixed_units` 各自统计。每项包含样本、缺失和不适用数量；`sum` 在有缺失时为 null，`known_sum` 仅为已知小计。均值与样本方差（分母 n-1）只使用已知样本，分子/分母为十进制字符串，合计和极值也使用字符串，避免浮点或安全整数截断。单样本方差和没有样本的比率为 null。

```json
{"mean":{"numerator":"3","denominator":"1"},"sample_variance":{"numerator":"0","denominator":"1"}}
```

`actual.model_usage.mock` 明确标为本地 Mock 免费；`provider.estimated_cost_micro_usd` 为完整可核验的价格表估计，缺失或未核验时为 null，`known_estimated_cost_micro_usd` 只是已知小计，`reserved_cost_micro_usd` 保留未结算调用的预算预留。供应商账单没有取得，因此存在供应商调用时 `billed_cost_micro_usd` 为 null；保守用量不冒充实测 Token。原 `actual.cost_micro_units` 是检查点已结算增量，可能滞后，不能拿它推断未知结果的最终费用。

统计是各 trial 的有界状态投影，不承诺整个实验的瞬时原子快照。固定 Mock/fixture/seed 的重复结果只证明该条件下的确定性指标，不推断真实模型波动、泛化能力或严格因果。

### 类型化干预

先从已有事件列表选择模型决策前的 `llm.requested` 事件 ID，再读取只读上下文；`target_commit_id` 与该事件的源意图必须对应，Prompt/计划干预要求其等于共同起点。上下文并非授权票据，创建时仍重新验证所有关系。

```powershell
$context = uv run --locked agentgit intervention-context $repo.repository_id --source-run $job.run_id --from-commit $fromCommit --target-event-id $targetEventId --validator-ref $validator.validator_ref | ConvertFrom-Json
$patchRequest = @{
    schema_version = 1
    source_run_id = $context.source_run_id
    base_commit = $context.base_commit
    target_commit_id = $context.target_commit_id
    target_event_id = $context.target_event_id
    operator = 'PROMPT_PATCH'
    payload = @{ kind = 'PROMPT_PATCH'; prompt = ($context.config.prompt + '核验所有引用。') }
    precondition = $context.precondition
    effect_policy = 'sandbox_or_approval'
    provenance = @{ rationale = '对照增加引用核验提醒'; evidence = @(@{ run_id = $context.source_run_id; seq = $context.target_event_seq }) }
}
[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'patch.json'), ($patchRequest | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
$patch = uv run --locked agentgit intervention $repo.repository_id --file patch.json --key prompt-patch-001 | ConvertFrom-Json
$experimentRequest.groups[1] = @{ schema_version = 1; key = 'B'; label = '类型化引用核验'; interventions = @($patch.intervention_id) }
```

用新幂等键重新提交保存后的实验 JSON；不要把改变后的请求复用原键。`PLAN_PATCH` 的 payload 为 `kind/plan`，仍须包含完整合法计划。`TOOL_RESPONSE_PATCH` 的 payload 为 `kind/replacement_call_id/test_only=true`，效果策略必须为 `test_only`；响应只能来自同项目完整、同参同签名的受控只读录制，不接受直接上传 response/URL，也不能跨冻结 split 借用实验观测。工具响应补丁必须同时配置 Hybrid，且目标调用必须位于选定集合。

### Hybrid 运行与观测

在实验请求中增加 `hybrid` 即以真正的 `hybrid` 模式建立 trial；省略时继续使用 Live。`until_commit` 与共同起点限定同一来源 Run 的录制范围，`calls` 为范围内 1–100 个不同的 `source_call_id`。可对目标工具事件读取 `intervention-context` 取得调用 ID；选择起点之后完整、已知的 `corpus_search/source_read` 只读调用，模型、写工具、截断/未知响应及其他项目调用均拒绝。冻结输入累计上限 64 MiB。

```powershell
$experimentRequest.hybrid = @{
    schema_version = 1
    until_commit = $untilCommit
    calls = @($toolContext.source_call_id)
    miss_policy = 'stop'
}
$experimentRequest.budget = $toolContext.config.limits
```

用新幂等键保存并提交该 JSON；API/CLI 仍使用原实验入口。当前不接受直接向 `/replays` 提交任意 Hybrid 策略。模型决策始终由当前 Adapter 重新产生。只有 occurrence、签名、请求字节、效果和响应上限全部相符才消费选定旧响应；改参按冻结 `miss_policy=stop/fixture` 停止或从固定语料生成新响应。模拟器不接受工具、签名或效果变更，缺失/损坏/越权输入不会转为模拟成功。未到达选定依赖不能通过最终验收。

`trial.replay.observations` 列出 `kind=recorded/synthetic/fixture/miss`、本次调用与来源调用 ID、响应引用、匹配结果、干预 ID 和 `test_only=true`。`hybrid.selected_call_ids/unreached_call_ids/miss_policy` 展示选择与未到达条件。所有选定观测在同一事务保存调用结果、事件和不可变来源账本；结果落盘后中断，恢复消费既有结果，不重发，也不重新调用模拟器。

`replay.actual.model_calls/tool_calls` 只计本次真实 Adapter 调用，历史/替换/模拟响应与恢复消费不混入；`fixture_calls/recorded_observations/synthetic_observations/hybrid_misses` 单列。逻辑用量仍反映 Runtime 消费及预算差额，因此可能与实际调用次数不同。后续 Recorded 从 trial 自身的 `live.initialized` 检查点开始，完全消费本次录制；来源环境保留 `source_hybrid_policy_ref/source_has_synthetic_patch/test_only`，不得将 synthetic 成功当成真实外部世界已修复。

创建与读取干预使用现有认证、项目/幂等约束及 1 MiB 请求/CLI 文件上限。Schema 不允许任意 JSON Patch 路径、validator 规则、模型、执行策略、权限或 split 变更；证据事件也须属于该项目。各定义不可修改；改变内容需新动作和新 ID。

错误含 error.code、message、evidence_refs（无证据时为空）、correlation_id，另有 X-Correlation-ID，不反射输入/URL/堆栈/令牌。validation=422、权限=403、不可访问=404、冲突=409、输出上限=413、配置/设施不可用=503、未预期错误=500。响应不缓存；JSON 日志写 stderr，仅含关联编号、方法、路由模板、状态、整数耗时，不含正文/认证头/查询/幂等键。

Mock 的 usage_kind 为 mock_utf8_bytes；S6 已接入固定供应商与 provider_tokens/mixed_units，计量细节见[模型说明](MODEL_ADAPTER.md)。批准不提升权限，执行仍检查当前策略与租约。可信核对使用后端 Resolver，不提供任意上传成功回执的 HTTP 接口。`doctor --sandbox` 只读核验执行器同一套 Linux/资源/seccomp/固定镜像前提；满足时返回 `execution_ready=true`，失败非零，不创建容器或下载镜像。

## 有界修复搜索（S9）

`POST /repair-searches`（202）、`GET /repair-searches/{search_id}`、`POST /repair-searches/{search_id}/cancel`、`GET /repositories/{repository_id}/repair-searches` 使用既有认证与 `X-Repository-ID`；修改请求需要 `Idempotency-Key`。列表支持 `after_id` 和 1–100 的 `limit`，返回 `next_after_id`。请求/文件仍限 1 MiB，完整搜索查询限 8 MiB，超限明确拒绝，不截断证据。

创建请求为 `template_repair_search/v1`，给定共同起点和有限事件后自动生成固定模板。以下 ID/摘要取自已经失败的 Run、Timeline 和 S7 冻结 screen 验收器；不能使用成功 Run、其他项目事件或 confirm/regression 验收器。

可选 `fixture_scope="stochastic_fixture"` 仅用于预先声明 Mock 适配器具有随机行为，必须在首次筛选前随创建请求冻结。声明进入请求幂等摘要和不可变搜索 manifest，并传递到缩减候选；其删除结果仅属经验缩减。省略时保留旧编码及确定性 Mock 默认范围；真实供应商配置禁止使用该声明，不能借此变成 fixture。已有搜索不能通过同键删除或改变声明来重分类，也不能在看到确认失败后修改范围或重开同一确认池。声明不改变模型参数、采样顺序或确认门槛。

```json
{
  "schema_version": 1,
  "kind": "template_repair_search/v1",
  "failed_run_id": "<失败 Run UUID>",
  "base_commit": "<模型决策前的共同检查点摘要>",
  "validator_ref": "<固定 screen 验收器摘要>",
  "targets": [{"schema_version": 1, "event_id": 123}],
  "max_atoms": 8,
  "max_depth": 2
}
```

`targets` 为 1–8 个不重复事件；模型目标必须正好绑定共同 base，工具目标仅支持受控只读观测。工具目标另给最多八个不同的 `replacement_call_ids` 并显式给出 S7 格式的 `hybrid`，响应内容仍由 S7 录制验证，不能上传任意替换响应。入口不接受任意 Patch、validator、工具权限或执行策略修改。

省略 `budget` 时只允许 Mock，生成新版字节预算及其内容摘要身份；真实模型必须显式提供 `RepairBudget`，包含有限 USD 微单位费用与冻结价格表。冻结内容展示来源授权裁剪后的单 trial 上限、所有阶段与预留。不适用原因在 `manifest.space.template_skips`，上限排除项在 `excluded_patch_refs`；这些范围不混入已穷尽结论。

```powershell
uv run --locked agentgit repair-search $repositoryId --file repair-search.json --key repair-001
uv run --locked agentgit repair-status $repositoryId $searchId
uv run --locked agentgit repair-list $repositoryId --limit 30
uv run --locked agentgit repair-cancel $repositoryId $searchId --key cancel-repair-001
# 分别在已配置相同数据库和 CAS 的独立终端运行。
uv run --locked agentgit repair-worker --repository-id $repositoryId
uv run --locked agentgit worker
```

`repair-worker --once` 只推进一次编排；`worker --once` 执行一个研究 Job。查询展示 `trials` 的意图、Job/Run 归属、物理用量、未决项、冻结停止 `decision`（如有）及结果引用。Recorded 的 `chain_valid` 不等于任务通过；`needs_confirmation` 不等于 verified。只有非空声明空间每一提案都得到确定硬验收失败才返回 `no_solution_in_space`；预算停止保留剩余数量和未确认原因，执行异常不冒充确定无解。取消后不再扩展，已派发项由原 Worker 结算，未知项不自动重发。完整边界见 [S9 搜索记录](acceptance/S9-search-loop.md)。

当前停止报告的 `decision.comparison` 冻结全部已探索候选的 trial/结果身份、安全和验收、补丁数、变更字节、实际计量与墙钟，按规定字典序排列；选中项必须是首个通过候选。旧报告没有比较表时返回 `null`，不补造历史结果。终态 `usage.elapsed_wall_ms` 取冻结决定或对应控制审计时刻，之后只读查询不再增长；活动任务仍按数据库时钟计入排队/重启/等待。逐项本机退出与安全拒绝审计见 [S9-gates.md](acceptance/S9-gates.md)。

## 搜索与预注册的原子准备（S10）

`POST /repair-searches/prepared`（202）供 Repair Lab 一次发布搜索与确认/回归条件；沿用认证、项目头、幂等键和 1 MiB 请求上限。请求为 `{"schema_version":1,"search":<完整 NewRepairSearch 请求>,"regression":[<四项来源>]}`，每项来源包含 `schema_version=1/source_run_id/from_commit`。四项须来自不同已终止 Run 和不同任务，配置、权限、语料及可执行起点沿用既有预注册校验。接口不接受 passed、结果、费用或执行指令。

服务先核验冻结来源和采样条件，再在同一事务写入搜索、编排 Job、有限提案、suite 及十张采样票据。修复 Worker 无法领取尚未提交的搜索；登记任一步失败会回滚整项发布，不留下可领取的半准备搜索。固定模板或验收器的不可变准备引用可能已保存，但不会执行任务。无需暂停已有 Worker。既有两步 API 保留原协议，仍必须由调用方保证先登记、后首次领取。

成功响应在原搜索响应之外增加 `validation_suite`，包含 `suite_id/search_id/manifest_ref/response_status=not_generated`；这不是已生成样本，也不自动打开确认池。原键重试返回相同搜索与 suite，即使搜索已运行或完成也不重新生成模板/票据；更换四项来源、参数或混用旧搜索请求会冲突。CLI 为 `agentgit repair-prepare <repository_id> --file prepared.json --key ...`。能力响应新增 `prepared_repair_search/repair_search/repair_validation/repair_reviews`，后端版本协议不变。

`GET /repair-searches/{search_id}/workflow` 仅以当前项目读取已有 `suite_id/study_id/proposal_id`，尚未生成的身份返回 null。该入口不创建任何记录、不生成票据、不写 CAS，也不重新提交任务；用于窗口重开后的只读发现。返回另含 `schema_version/search_id/repository_id`。未授权项目或不存在搜索返回 404，桌面桥接响应限 16 KiB；能力标记为 `repair_workflow`。Repair Lab 同时要求该标记和 `prepared_repair_search`，旧后端缺能力时不给出可用搜索动作。

## 耐久逐补丁缩减（S10）

`POST /repair-studies`（202）、`GET /repair-studies/{study_id}`、`POST /repair-studies/{study_id}/cancel` 复用原认证、项目头、幂等键和请求/输出上限。创建只接受 `schema_version=1`、`search_id` 及可选 `max_reduce_trials`、`field_policy`。来源必须是同一代码下已排空封存的 S9 `needs_confirmation`；一个来源只能建立一个 study，次数覆盖只能收紧原 reduce 额度。没有接收候选正文、用户 verdict、费用或确认结果的入口。

```json
{"schema_version":1,"search_id":"<已封存 S9 搜索 UUID>"}
```

等价 CLI：

```powershell
$study = uv run --locked agentgit repair-reduce $repo.repository_id $search.search_id --key reduce-001 | ConvertFrom-Json
uv run --locked agentgit repair-study-worker --repository-id $repo.repository_id
# 在另一终端运行同代码、同配置的普通 worker，执行实际研究 Job。
uv run --locked agentgit worker
uv run --locked agentgit repair-study-status $repo.repository_id $study.study_id
uv run --locked agentgit repair-study-cancel $repo.repository_id $study.study_id --key reduce-stop-001
```

两个 Worker 均可加 `--once`；仅运行控制器不会执行研究任务。HTTP 取消请求发送 JSON 空对象和 `Content-Type: application/json`；取消本身只请求停止扩展/子 Job，不伪造执行完成。

查询返回冻结 manifest/state、逐删除 intent/dispatch/收据、待核对引用、共享总用量及 `final_lock`。未决 trial 仍占完整预留；用量包括原 S9 和本阶段实际调用，Mock 字节与供应商 Token 区分。墙钟从原搜索创建时起计算，候选锁定后仍计等待，原 S9 已封存的决定和查询不改写。新建代码版本不能继续旧 study。

新建 study 默认 `field_policy=predefined-fields/v1`：原子删除后、最终锁前，用固定规则缩短追加的 Prompt 子句或新增 Plan 结构；每次成功后重新测试当前候选的原子必要性。沿用同一控制器和普通 Worker，无额外执行入口。显式 `field_policy=null` 保持旧原子协议；旧 study 的缺省策略仍为空，不能事后加字段阶段。字段试验及必要性复查共同消耗原 `max_reduce_trials`，同时保护六加四预留。

查询额外返回 `field_reduction`：未进入阶段或旧策略时为 `null`，否则包含独立 `state/summary/trials`、不可变引用、实际收据与本 Job 模型调用来源。原 `reduction` 保留字段之前的原子摘要；最终候选的必要性应读取字段摘要中的 `atomic_minimality` 和 `field_minimality`。即使预算停止后最后一个通过候选仍能锁定，也不能把未测试字段标为最小。所有派生内容按项目和具体 study 解析，相同来源摘要不能跨 study 借用；确认投影使用最终锁中的确切派生内容。

`awaiting_samples` 表示最终候选已经锁定，确认池尚未打开，不能把这个状态当作 verified；预注册、开启及实际执行入口见下节。正式配置审阅入口见下文的精确提案与审批。完整 fixture 删除测试只支持所声明原子语法的 1-minimal；预算不足、未测和非确定结果保持未证明。历史原子协议证据见 [S10-reduction.md](acceptance/S10-reduction.md)，字段规则见[字段缩减方案](S10-field-reduction-plan.md)。

## 确认条件预注册与开启（S10）

在启动 `repair-worker` 之前调用 `POST /repair-validation-suites`（201），给出未首次领取的 `search_id` 和四个固定回归任务的来源起点。服务自动冻结六次 confirm 条件与四项 regression 条件，不接受响应正文、用户 verdict、采样 UUID、验收规则或费用。四个回归输入须不同，也须不同于确认任务；来源、检查点、完整配置及权限都核对项目归属。

```json
{
  "schema_version": 1,
  "search_id": "<尚未领取的 S9 搜索 UUID>",
  "regression": [
    {"schema_version":1,"source_run_id":"<回归任务 1 的 Run UUID>","from_commit":"<模型决策前检查点>"},
    {"schema_version":1,"source_run_id":"<回归任务 2 的 Run UUID>","from_commit":"<模型决策前检查点>"},
    {"schema_version":1,"source_run_id":"<回归任务 3 的 Run UUID>","from_commit":"<模型决策前检查点>"},
    {"schema_version":1,"source_run_id":"<回归任务 4 的 Run UUID>","from_commit":"<模型决策前检查点>"}
  ]
}
```

`GET /repair-validation-suites/{suite_id}` 只读返回冻结条件。已开始筛选且没有预注册的来源不能事后补登记，应新建预注册搜索。固定回归来源应提前准备，准备耗时不会重置搜索墙钟。

最终候选锁后调用 `POST /repair-studies/{study_id}/open-validation`，正文为 `{"schema_version":1,"suite_id":"<预注册 suite UUID>"}`。资源与六加四配额足够时，事务一次性绑定池与候选、冻结计划和开启事件，并进入 `validation_ready`；返回 `opened=true/opening_ref/plan_ref`。不足则 `opened=false/decision/reason`，原候选和预算不改写。同键异参拒绝，原请求重试复用原绑定；取消后的旧绑定仍可读取，但不会恢复执行。`GET /repair-studies/{study_id}/validation` 返回绑定或 `validation=null`。

对应 CLI 为 `repair-validation-suite <repository_id> --file suite.json --key ...`、`repair-validation-open <repository_id> <study_id> <suite_id> --key ...`、`repair-validation-status <repository_id> <study_id>`。认证、项目头、幂等键、JSON 编码与大小限制沿用前述规则。

票据中的 `response_status=not_generated` 和开启记录的 `execution_status=not_started` 是登记/开启时的不可变历史事实。`GET /repair-studies/{study_id}/validation` 与 `repair-validation-status` 另返回当前 `execution`，未开启时为 `null`；其中包含 `status`、实际 `trials`、投影、独立 Job/Run、收据及原始调用来源引用、全流程 `usage`、`provisional_verification` 和最终封存 `verification`。`validation_ready` 仍不能当作已确认。

```powershell
# 开启成功后运行确认控制器；普通 worker 在另一终端执行实际研究 Job。
uv run --locked agentgit repair-confirm-worker --repository-id $repositoryId
uv run --locked agentgit worker
uv run --locked agentgit repair-validation-status $repositoryId $studyId
```

两个 Worker 均支持 `--once`。控制器按预注册票据串行执行六项确认、四项固定回归，不调用 Adapter；`repair-study-worker` 仅处理尚未开启验证的缩减任务。确定的确认失败仍保留并继续回归；未知用量/效果保留完整预留并等待核对，安全违规停止扩展。取消仍使用 `repair-study-cancel`，由普通 Worker 应用子 Job 的控制并结算，不能用取消请求冒充已经停止。

六项完整硬验收至少五项通过、四项回归全部通过、安全零违规和完整物理用量才可封存验证结果。执行错误、零新模型调用或仅重验旧产物不能充当完整确认；收据必须来自实际 Job、固定验收器和该 Job 自身调用/恢复链。合成条件结果为 `verified_under_fixture`，真实模型路径为 `empirically_confirmed` 并保留小样本限制；本轮没有付费供应商实测。状态不代表配置已采纳，`adoption=not_reviewed` 保持。没有上传“验证成功”的接口。

自定义回归语料和时钟应由持久 Job 冻结；旧直接执行来源若缺少可重建且一致的输入，会在 Replay/预注册时返回 `replay_mismatch`，不会静默改用 bundled 语料。预注册/开启与实际执行证据分别见 [S10-pools.md](acceptance/S10-pools.md) 和 [S10-confirmation.md](acceptance/S10-confirmation.md)。

## 精确修复建议与审阅（S10）

| 入口 | 请求 | 效果 |
|---|---|---|
| POST /repair-studies/{study_id}/proposal | `{"schema_version":1}`，幂等键 | 从已封存验证建立唯一精确建议，返回 201 |
| GET /repair-proposals/{proposal_id} | 项目头 | 只读返回 before/after、完整补丁、验收器/任务/环境、预算用量、原子/字段范围、当前版本计数器及决定 |
| POST /repair-proposals/{proposal_id}/review | `{"schema_version":1}`，幂等键 | 冻结当前配置版本和操作者的审阅票据，返回 201 |
| POST /repair-proposals/{proposal_id}/approve | 下述决定，`approve` 或 `save_experiment` | 只创建配置新版本，或只保存实验定义 |
| POST /repair-proposals/{proposal_id}/reject | 下述决定，`reject` | 只记录不可变拒绝，不创建配置 |

这里的 `proposal_id` 是最终建议的新身份，`source_proposal_id` 才是原 S9 筛选候选。建议正文、候选锁、封存验证和来源 config ID/version/ref 不可替换；接口不接受用户提交 before/after、passed、费用、工具权限或执行选项。缺少/未知 Schema 为 409，未知字段为 422，沿用现有认证、项目、大小和幂等约束。

```json
{"schema_version":1,"review_id":"<审阅 UUID>","proposal_ref":"<精确建议摘要>","expected_config_version":21,"decision":"approve"}
```

`expected_config_version` 是票据中的项目配置版本计数器，包含私有试验配置；来源配置仍由独立的 `source_config_id/source_config_version` 指明。审阅后任何配置版本推进都使旧票据失效，返回 `version_conflict`；用新幂等键重新 POST review，取得同一不可变建议的新票据后再决定。一个建议只接受一个最终决定；相同决定原键重试返回原结果，改决定/改参数冲突，两个操作者竞争最多产生一个版本和一个决定。

批准采用来源配置原有授权及预算，只应用确切 Prompt 与 `plan_template`。结果包含新 config ID/version；不修改原配置、已有 Task 或“活动头”，不启动 Run/Job。新任务须显式选择新配置。模板在新任务的同一 Runtime 里登记为 `plan.accepted`，包含配置来源和模板摘要；初始化不继承旧 Run 的完成节点、记忆、产物或副作用。空模板保留旧编码和首次模型规划行为。

含 `TOOL_RESPONSE_PATCH` 时 `after=null/adoption_scope=save_experiment_only`，正式配置批准被拒绝。`save_experiment` 返回定义摘要、完整定义和 `execution=not_started`，没有排队执行；真正运行需另行提交、重新核对来源与授权。未完成或失败的确认/回归不能批准或作为合格实验保存，仍可审阅并拒绝。封存验证摘要中的 `adoption=not_reviewed` 保留当时事实，当前决定读取建议响应的 `review`。

等价 CLI：

```powershell
uv run --locked agentgit repair-propose $repo.repository_id $study.study_id --key proposal-001
uv run --locked agentgit repair-proposal-status $repo.repository_id $proposal.proposal_id
uv run --locked agentgit repair-review $repo.repository_id $proposal.proposal_id --key review-001
# decision.json 使用上面的完整严格请求；该动作不会开始执行。
uv run --locked agentgit repair-review-decision $repo.repository_id $proposal.proposal_id --file decision.json --key decision-001
```
