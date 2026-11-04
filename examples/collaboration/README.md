# 四角色协作操作者演示

范围为固定 `offline-four-role-research/v1`：Supervisor 派发，Researcher 取证，Verifier 独立核对，Writer 生成报告，Supervisor 收尾。各自持独立任务、Run、Job、分支、权限、预算和私有输入。使用本地 Mock 和冻结合成语料，无供应商密钥；不执行 Shell/Python 或远端服务。完整 C/D3、桌面及正式前置仍待核对。

## 自动化演示

准备现有 Python/uv 与独立 PostgreSQL 测试库后，在仓库根目录执行：

```powershell
uv run --frozen pytest tests/integration/test_collaboration_entrypoints.py -q
```

每例建立独立 `test_<uuid>` Schema/CAS，结束后清理本例 Schema。测试运行实际 Typer 命令、ASGI TestClient 路由和持久 Worker，覆盖准备/显式开始、组控制、成功来源 Recorded、分页、重开、查询不执行及身份/项目拒绝；不是独立 API 进程、桌面包或独立 Windows 用户验收。

## CLI：准备、开始和查询

以下命令要求操作者已准备**专用且已迁移至 `0032_collab_recorded_steps` 的测试后端**，配置独立 `AGENTGIT_TEST_DATABASE_URL` / `AGENTGIT_TEST_DATA_ROOT`。与业务库和业务数据目录分离，队列只放本演示；CLI/桌面不会自动执行迁移。当前目录实际为 79 业务表与一个迁移版本表。

CLI 与 API 共用 `AGENTGIT_OPERATOR_ID`，从后端配置取得身份，JSON 不接受自报 actor。项目、任务、Worker 使用同一配置。下面整段执行，避免人为等待耗尽默认 60 秒墙钟预算：

```powershell
$repo = (uv run --frozen agentgit init "四角色演示" --key collab-demo-project --testing | ConvertFrom-Json).repository_id
$live = uv run --frozen agentgit collaboration-prepare $repo examples/collaboration/research.json --key collab-demo-live --testing | ConvertFrom-Json
uv run --frozen agentgit collaboration-status $repo $live.collaboration_id --testing
# 此时四个角色暂停；仅准备不自动执行。
uv run --frozen agentgit collaboration-start $repo $live.collaboration_id --key collab-demo-start --testing
1..5 | ForEach-Object { uv run --frozen agentgit worker --once --testing }
uv run --frozen agentgit collaboration-status $repo $live.collaboration_id --testing
uv run --frozen agentgit collaboration-messages $repo $live.collaboration_id --limit 2 --testing
uv run --frozen agentgit collaboration-shared $repo $live.collaboration_id --limit 2 --testing
```

正常五次领取完成四角色流程，保存 7 条消息/收据、5 次共享发布（`evidence.json` 版本 1→2，以及 `verification.json`、`report.md`、`sources.json` 各版本 1）。新调用为 5 模型、15 工具，均限离线 Mock/本地工具。main HEAD 不变。

JSON 中可显式填写 `private_inputs`，每角色最多一份、notes 最多 16 条且每条至多 4096 字符，并受自身上下文预算约束；通过有界文件导入，不放在命令行明文参数。公开投影不返回 notes、私有输入内容或模型请求正文。

每次逻辑动作保留 actor/route/key 和确切输入，原键重试返回原收据；更换输入须新键。重复演示若要创建另一组，使用新的准备键。状态是只读查询，旧 start 收据中的 running 只表示原请求已接受，实际状态查 `collaboration-status`。

## 严格 Recorded

来源必须是本人、同项目、当前源码/锁下完整成功的 Live 组。Recorded 来源不再作为来源，不继承外部写审批，也不允许改权限、预算或程序：

```powershell
$recorded = uv run --frozen agentgit collaboration-recorded $repo $live.collaboration_id --key collab-demo-recorded --testing | ConvertFrom-Json
uv run --frozen agentgit collaboration-start $repo $recorded.collaboration_id --key collab-demo-recorded-start --testing
1..5 | ForEach-Object { uv run --frozen agentgit worker --once --testing }
uv run --frozen agentgit collaboration-status $repo $recorded.collaboration_id --testing
uv run --frozen agentgit status $repo $recorded.supervisor_run_id --testing
```

四个新 Run 各自绑定自身来源，实际消费录制、重算状态并按类型映射消息/父地址。目标 `physical_call_intents` 为 0，`reused_calls` 合计 20；缺字节、错来源或状态不符明确失败，不回退 Live。`logical_usage` 包含重放的历史逻辑用量；Mock 模型计量为 UTF-8 字节，不冒充供应商 Token。`physical_call_intents` 是新耐久意图行数，不是已确认调用次数或账单。

## 组控制和分页

`collaboration-control <repo> <group> <file> --key <key> --testing` 的 JSON 文件为：

```json
{"schema_version": 1, "action": "pause", "expected_revision": 0}
```

action 支持 pause/resume/cancel；expected_revision 从当前组状态读取。每次操作用自己的键，重试沿原键/原版本；接受回执不表示已安全停止。暂停后持续查询 `quiescent`，安全静止后才能恢复。组恢复保留单角色人工暂停和原 deadline，暂停期间不重置墙钟；已经到期则停止。取消保留已知结果，未知结果不自动重发。准备态可以取消，四个角色结算后 `all_terminal` 才为 true。

组和消息分页使用 `next_after_id` → `--after-id`；共享版本使用 `next_after.path/version` → 同时传 `--after-path` / `--after-version`。每页 1–100 项。重开或断连后先按原组 ID/键查询，不自动重发开始或控制。

## HTTP 对应入口

在**单独的 API 终端**，把服务配置指向与上述 CLI 相同的专用演示测试库/CAS，配置本地令牌及相同 operator_id 后启动：

```powershell
$env:AGENTGIT_DATABASE_URL = $env:AGENTGIT_TEST_DATABASE_URL
$env:AGENTGIT_DATA_ROOT = $env:AGENTGIT_TEST_DATA_ROOT
uv run --frozen agentgit serve
```

CLI 继续使用原终端的 `--testing` 配置。令牌只作 Bearer 认证，项目使用 `X-Repository-ID`，修改额外传 `Idempotency-Key`。`repository_id` 路径与项目头必须一致。

| 方法 | 路径（前缀 `/repositories/{repository_id}`） | 输入 |
|---|---|---|
| POST | `/collaborations/prepare` | `research.json` 完整对象，最多 1 MiB |
| GET | `/collaborations` | `after_id`、`limit` |
| GET | `/collaborations/{group_id}` | 无 |
| POST | `/collaborations/{group_id}/start` | `{"schema_version":1}` |
| POST | `/collaborations/{group_id}/recorded` | `{"schema_version":1}`；group_id 为成功来源 |
| POST | `/collaborations/{group_id}/control` | action、expected_revision |
| GET | `/collaborations/{group_id}/messages` | `after_id`、`limit` |
| GET | `/collaborations/{group_id}/shared` | 成对 `after_path/after_version`、`limit` |

角色沿用 `/runs/{run_id}`、事件、产物、Job 和 Commit 查询，adapter.kind 为 `collaboration_role/v1`。报告下载为 `/artifacts/{artifact_id}/content`，不能提供任意 CAS 摘要或宿主路径。协作角色、历史及产物保留本人校验，拒绝发生在 CAS 内容读取前。其他操作者为 403，路径/项目头不一致为 404；Recorded 跨项目来源沿用既有 403。版本/原键冲突为 409，非法字段或游标为 422，超大请求为 413。
