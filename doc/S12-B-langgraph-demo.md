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

创建返回独立 Job/Run，分支、Run 和 Job 同事务发布。新 Worker 复制已完成取证的逻辑一步/一次工具、保留来源 trace，实际取证为 0；在独立 thread 使用原生 update_state 修改备注后继续 review/publish。状态 API 的 adapter.fork 指明来源，fork_initialized 表示原生更新已耐久完成。来源分支、状态、事件和产物不变；可从已初始化 Fork 的安全历史点再次 Fork。独立 Fork 与下述 Recorded 分别记录模式和来源。

## 成功轨迹范围 Recorded

选择同一固定图来源 Run 的起止点；终点须为完整 completed 检查点。起点支持冷初始化、已预留取证、原生 pending writes、review 暂停、Fork 原生备注更新前后，以及已完成点。Fork 的基点会使用来源自身的种子检查点初始化，范围说明同时固定这两个点。起终点相同是零步恢复，不宣称重新执行原运行。

准备 UTF-8 的 `graph-recorded.json`：

```json
{
  "schema_version": 1,
  "source_run_id": "来源 Run UUID",
  "from_commit": "来源起点 Commit 摘要",
  "to_commit": "来源成功终点 Commit 摘要",
  "name": "graph-recorded-check",
  "expected_version": 0
}
```

```powershell
uv run --locked agentgit langgraph-recorded <repository_id> graph-recorded.json --key graph-recorded-once --testing
uv run --locked agentgit worker --once --testing
uv run --locked agentgit status <repository_id> <new_run_id> --testing
uv run --locked pytest tests/integration/test_langgraph_recorded.py -q
```

相同键与输入返回原 Job/Run；不同范围不能复用原键。adapter.recording 指明不可变范围说明，actual_attempts 为 0；历史逻辑 steps/tool_calls 和来源 trace 保留。来源暂停/恢复由录制依据自动消费；新任务当前的暂停/取消和原期限仍有效。没有真实模型、HTTP 工具或 stdio 进程，缺录制/控制/环境失败，不能回退 Live。可再次 Recorded 或从回放的安全 review 点创建新 Fork；后者通过固定范围核对原始取证，不能把同 hash 当成任意授权。

报告字节与完整逻辑投影必须等于来源；新 thread/checkpoint ID、时间和物理日志单独保留，不要求 native JSON 字节相同。测试同时覆盖来源不变、真实解释器终止接管、缺依赖、伪造说明及开启 tracing 环境后的零连接尝试。这里只声明成功轨迹范围，完整 B-T06 与桌面展示仍须补齐，stdio 路径见下文。

## 受信 stdio 图路径

先按 [MCP stdio 演示](S12-A-stdio-demo.md)审阅并登记可信单文件 Server，准备独立测试后端；当前 head 为 `0028_langgraph_runs`。保留该步骤生成的程序引用和 `AGENTGIT_MCP_STDIO_REGISTRY`，这里使用 READ_ONLY：

```powershell
$binding = uv run --locked agentgit mcp-stdio-bind stdio-demo $mcpStdioProgramRef echo --effect READ_ONLY --testing | ConvertFrom-Json
$program = @{ schema_version=1; binding=$binding; request=@{schema_version=1;arguments=@{value='graph-fixture-value'}}; review_note='来源已核对' }
[IO.File]::WriteAllText([IO.Path]::GetFullPath('graph-program.json'), ($program | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
```

从本文 `langgraph-submit` 步骤继续，使用新的项目/分支及键。仍默认 review 前暂停，经现有 resume 完成；Fork 和 Recorded 文件与 HTTP 图相同，不能覆盖工具、预算或权限。正常图来源有一次绑定发现和一次取证；标准库 fixture 为三个独立 Server 进程、六帧协议请求，其中一次工具调用。原生恢复、Fork 和 Recorded 不追加取证。

成功录制后，即使移除登记与原脚本，Recorded 仍须使用固定来源完成，并保持 Server 日志和计数不变。Live 脚本摘要变化拒绝；发现到工具帧之间失租不发送取证，之后保持 unknown，不通过恢复重发。这里只启动可信 MCP Server，不将 stdio 用作任意 Shell/Python 执行入口。

```powershell
uv run --locked pytest tests/integration/test_langgraph_stdio.py -q
uv run --locked pytest tests/unit/test_langgraph_recorded_contracts.py -q
```

64 项 stdio 用例复用相同 Live/Fork/Recorded 契约，另外核对移除启动环境、发现到调用失租、批准脚本变更及逐个自有进程退出。联合收集命令可同时包含 unit/integration/sandbox/execution；收集成功不代表完整运行或正式验收。
