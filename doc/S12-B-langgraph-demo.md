# S12-B 固定图 Live 工程演示

本轮提供固定图 Live/Fork 的 CLI、现有查询控制与耐久 Worker，完整 Recorded 和正式 B/D3 仍待完成。仅使用固定 `mcp-read-review-publish/v1`、已登记的本地 READ_ONLY MCP 工具，不接受任意节点、代码或命令。

## 可重复的自动化演示

按 [开发文档](DEVELOPMENT.md)准备 Python/uv 和现有 PostgreSQL 测试数据库；集成 fixture 为每例新建 `test_<uuid>` Schema 和独立 CAS，结束后只清理本例 Schema，不迁移业务库。下面的测试自动启动并关闭可信回环 MCP fixture。

```powershell
uv run --locked pytest tests/integration/test_langgraph_entrypoints.py -q
uv run --locked pytest tests/integration/test_langgraph_runs.py -q
uv run --locked pytest tests/integration/test_langgraph_migration.py -q
uv run --locked pytest tests/integration/test_langgraph_forks.py -q
```

入口演示用实际 CLI 定义提交、同键查询、现有 API 的 ASGI TestClient 路由和 Worker 接线：fetch 完成后显示 paused/next=review、一次工具和零模型；显式 resume 后得到三节点完成状态和 `report.md`，反复查询不增加请求，跨项目读取拒绝。原生日志以 `langgraph-native.json` 同时发布，保存 native checkpoint/pending writes 与原生 ID；它与报告共同受文件数和总字节预算约束。

进程测试另启动实际 Python Worker，在 `after_intent / after_result / after_fetch_writes / after_fetch_checkpoint` 四个确切边界验证 PID/父子关系、源码/锁，再终止实际解释器并接管。只有意图的来源保持未知；已有耐久结果或 pending writes 的路径不构造真实客户端、不增加工具请求。该自动化例不是桌面包或独立 API 服务进程的验收替代。

## 操作者入口

在操作者已准备并迁移的专用测试后端环境中，先沿用 `mcp-bind` 或受后端白名单约束的 `mcp-stdio-bind` 固定只读工具。下面使用已准备的 HTTP echo Server；地址换成其实际登记端口，请求必须匹配该工具 Schema。生成文件使用 UTF-8 无 BOM，不手写或放宽工具定义。

```powershell
$endpoint = "http://127.0.0.1:8000/mcp"
$binding = uv run --locked agentgit mcp-bind trusted-fixture $endpoint echo --effect READ_ONLY --testing | ConvertFrom-Json
$program = @{
    schema_version = 1
    binding = $binding
    request = @{ schema_version = 1; arguments = @{ value = "graph-fixture-value" } }
    review_note = "来源已核对"
}
$text = $program | ConvertTo-Json -Depth 64
[IO.File]::WriteAllText([IO.Path]::GetFullPath("graph-program.json"), $text, [Text.UTF8Encoding]::new($false))
```

默认固定 SDK/图、review 前暂停和有界预算；若显式提供 graph，只能匹配已登记的完整清单，pause_before 可为 `["review"]` 或 `[]`。不传入模型/工具授权继承或源码路径。

```powershell
uv run --locked agentgit langgraph-submit <repository_id> graph-program.json --expected-version <version> --key graph-live-once --branch <branch_name> --head <head_commit> --testing
uv run --locked agentgit worker --once --testing
uv run --locked agentgit status <repository_id> <run_id> --testing
```

空分支省略 `--head`；已有分支必须传入确切 HEAD/版本，冲突拒绝。同 actor/route/key 的确切输入返回原 Job/Run，不能换输入。恢复通过既有 `resume` 控制入口和同一 Worker 领取，保留原墙钟期限；原生暂停不表示工具权限批准。

## 独立备注 Fork

选择来源 Run 的完整 fetch 后、review 前检查点，准备以下 `graph-fork.json`，替换两个来源标识；`name` 必须是新分支。来源可继续或已经完成，选定的历史点仍须通过安全/录制/环境检查。只允许修改备注，不接受工具、权限、图或预算覆盖。

```json
{
  "schema_version": 1,
  "source_run_id": "来源 Run UUID",
  "from_commit": "来源安全 Commit 摘要",
  "name": "review-note-fork",
  "expected_version": 0,
  "review_note": "独立分支补充说明"
}
```

```powershell
uv run --locked agentgit langgraph-fork <repository_id> graph-fork.json --key graph-fork-once --testing
uv run --locked agentgit worker --once --testing
uv run --locked agentgit status <repository_id> <new_run_id> --testing
```

创建返回独立 Job/Run，分支、Run 和 Job 同事务发布。新 Worker 复制已完成取证的逻辑一步/一次工具、保留来源 trace，实际取证为 0；在独立 thread 使用原生 update_state 修改备注后继续 review/publish。状态 API 的 adapter.fork 指明来源，fork_initialized 表示原生更新已耐久完成。来源分支、状态、事件和产物不变；可从已初始化 Fork 的安全历史点再次 Fork。该功能不等同于完整 Recorded，后者仍在实现。
