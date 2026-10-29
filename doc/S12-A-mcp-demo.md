# MCP HTTP 单次任务演示

本演示使用仓库内可信 echo Server，不访问公网、不配置真实供应商 Key、没有 LLM。它证明一次工具调用、精确审批、独立 Recorded 和原始产物；不是完整 D3 或独立 Windows 安装验收。stdio 尚未接入，不能传入任意宿主程序或命令。

## 自动化复现

已准备独立 PostgreSQL 测试库后，在根目录运行：

```powershell
$env:PYTHONIOENCODING='utf-8'
uv run --locked pytest tests/integration/test_mcp_entrypoints.py tests/integration/test_mcp_runs.py tests/integration/test_mcp_migration.py -q
```

fixture 为每例创建独立 `test_*` Schema、CAS 和本地 Server，结束后清理自有服务/Schema。完整入口例实际调用 CLI 绑定/提交，HTTP 核对并批准同一动作，Worker 执行，CLI 创建来源分支和 Recorded；再次查询、跨项目拒绝及 Recorded 都核对真实 Server 计数。三处进程中断例终止核验过父子关系的实际 Worker 解释器，不以抛异常代替进程终止。用唯一证据目录可保存原始对象、关系和 PID/fencing 证明，参见工程记录。

包内查询/审批复验使用已归档 0.1.7 便携程序，在 `desktop/` 运行 `npx playwright test --config playwright.mcp.config.ts`；可通过 `AGENTGIT_DESKTOP_EXECUTABLE` 指定实际程序，`AGENTGIT_ELECTRON_EVIDENCE_PREFIX` 必须使用未用过的证据前缀。该测试自建独立 Schema/API/Worker/可信 MCP Server，界面只输入已创建的 Run，不增加任意网络地址或宿主程序提交入口。完成后清理自有进程和 Schema，原始证据另存忽略目录。

## 操作者手动流程

先按 [开发说明](DEVELOPMENT.md)准备专用测试数据根、测试库及 `0027_mcp_runs`；以下 `--testing` 不自动迁移数据库。CLI、API、Worker 必须使用同一数据根、Schema 和 operator_id。退出桌面或 Server 不会代替后端控制动作。

终端 A 创建新目录并启动固定测试 Server，保留打印的目录供终端 B 使用：

```powershell
$mcpDemoRoot=Join-Path $env:TEMP ('agentgit-mcp-demo-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $mcpDemoRoot -ErrorAction Stop | Out-Null
$mcpDemoRoot
uv run --locked python -X utf8 tests/helpers/mcp_server.py (Join-Path $mcpDemoRoot 'ready.json')
```

终端 B 将 `$mcpDemoRoot` 设为上述目录，创建新项目、绑定 echo，并保存有界程序；使用无 BOM 的 UTF-8，避免 PowerShell 的默认重定向编码差异：

```powershell
$env:PYTHONIOENCODING='utf-8'
$mcpDemoRoot='终端 A 打印的新目录'
$mcpEndpoint=(Get-Content (Join-Path $mcpDemoRoot 'ready.json') -Raw | ConvertFrom-Json).endpoint
$mcpRepository=(uv run --locked agentgit init 'MCP 本地演示' --key ('mcp-init-'+[guid]::NewGuid().ToString('N')) --testing | ConvertFrom-Json).repository_id
$mcpBinding=uv run --locked agentgit mcp-bind mcp-demo $mcpEndpoint echo --effect NON_IDEMPOTENT_WRITE --testing | ConvertFrom-Json
$mcpProgram=@{schema_version=1;binding=$mcpBinding;request=@{schema_version=1;arguments=@{value='本地演示'}};limits=@{schema_version=1;max_steps=1;max_model_calls=1;max_tool_calls=1;max_replans=0;max_tokens=1000000;max_wall_ms=300000;max_response_bytes=65536;max_context_bytes=131072;max_artifact_bytes=65536;max_files=1}}
$mcpProgramFile=Join-Path $mcpDemoRoot 'program.json'
[System.IO.File]::WriteAllText($mcpProgramFile,($mcpProgram | ConvertTo-Json -Depth 100),[System.Text.UTF8Encoding]::new($false))
$mcpLive=uv run --locked agentgit mcp-submit $mcpRepository $mcpProgramFile --expected-version 0 --key mcp-live --testing | ConvertFrom-Json
uv run --locked agentgit worker --once --testing
uv run --locked agentgit approvals $mcpRepository $mcpLive.run_id --testing
uv run --locked agentgit status $mcpRepository $mcpLive.run_id --testing
```

首个 Worker 应停在 waiting_approval，工具尚未调用。虽然 Server 提示 readOnlyHint=true，本例明确按本地外部非幂等写登记；核对动作原文/摘要、当前 `state_version` 和操作者后，使用实际审批 ID 与版本执行：

```powershell
uv run --locked agentgit decision $mcpRepository APPROVAL_UUID approve --expected-version CURRENT_STATE_VERSION --key mcp-approve --testing
uv run --locked agentgit worker --once --testing
$mcpSource=uv run --locked agentgit status $mcpRepository $mcpLive.run_id --testing | ConvertFrom-Json
uv run --locked agentgit artifacts $mcpRepository $mcpLive.run_id --testing
```

应为 completed，logical_usage.tool_calls=1、actual_attempts=1、has_model=false，产物 `mcp-result.json` 保留固定身份/Schema/参数摘要和原结果。`adapter.recording` 返回明确来源范围；它不是执行授权，仍需显式创建来源分支和新任务：

```powershell
$mcpForkFile=Join-Path $mcpDemoRoot 'fork.json'
$mcpReplayFile=Join-Path $mcpDemoRoot 'replay.json'
$mcpRange=$mcpSource.adapter.recording
$mcpFork=@{schema_version=1;source_run_id=$mcpLive.run_id;from_commit=$mcpRange.from_commit;name='mcp-recorded';expected_version=0}
[System.IO.File]::WriteAllText($mcpForkFile,($mcpFork | ConvertTo-Json),[System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText($mcpReplayFile,($mcpRange | ConvertTo-Json),[System.Text.UTF8Encoding]::new($false))
uv run --locked agentgit mcp-fork $mcpRepository $mcpForkFile --testing
$mcpRecorded=uv run --locked agentgit mcp-submit $mcpRepository $mcpProgramFile --expected-version 0 --key mcp-recorded --branch mcp-recorded --head $mcpRange.from_commit --replay-file $mcpReplayFile --testing | ConvertFrom-Json
uv run --locked agentgit worker --once --testing
uv run --locked agentgit status $mcpRepository $mcpRecorded.run_id --testing
```

Recorded 应完成且 actual_attempts=0，逻辑投影和产物字节与原 Run 一致，不构造 MCP 客户端、不重新发现工具。缺录制/来源不符明确失败，无 Live 回退；实际计数证明以自动化用例的独立 Server 记录为准。手动演示结束仅在终端 A 用 Ctrl+C 停止本次 Server，保留数据库/产物及外部服务；不执行批量进程或数据删除。

通用程序参数文件限 2 MiB，分支/Recorded 文件限 4 KiB；每次调用还受固定 Schema、1 MiB 请求及响应/产物/墙钟上限约束。凭据（需要时）只由后端运行时注入，本文 echo 不需要凭据，不能把 Key 写进程序文件或参数。
