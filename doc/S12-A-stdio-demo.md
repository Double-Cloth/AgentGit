# MCP stdio 单次任务演示

使用本仓库标准库单文件 fixture、当前 Worker 的基础 Python 和隔离测试库。先阅读[启动边界](S12-A-stdio-plan.md)及复制的 Server；后端操作者对解释器/脚本的精确原字节登记批准，渲染器和模型不能登记或修改启动程序。本例没有 Shell、依赖安装、真实凭据或外部业务写入。

先按[开发说明](DEVELOPMENT.md)准备专用测试数据根、测试库和 `0027_mcp_runs`，本演示不自动迁移。CLI、API 和 Worker 使用同一 operator_id 与数据根。创建全新临时目录，复制并审阅 fixture：

```powershell
$env:PYTHONIOENCODING='utf-8'
$mcpStdioRoot=Join-Path $env:TEMP ('agentgit-mcp-stdio-demo-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $mcpStdioRoot -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath tests/helpers/mcp_stdio_server.py -Destination (Join-Path $mcpStdioRoot 'trusted_server.py') -ErrorAction Stop
Get-Content (Join-Path $mcpStdioRoot 'trusted_server.py') -Encoding utf8
$env:AGENTGIT_MCP_DEMO_SCRIPT=Join-Path $mcpStdioRoot 'trusted_server.py'
$env:AGENTGIT_MCP_STDIO_REGISTRY=Join-Path $mcpStdioRoot 'approved.json'
```

确认本地审阅范围后，用以下固定脚本生成批准记录。输出的程序引用绑定路径、两份 SHA256 和当前操作者；登记文件仅创建一次。只有本机可信操作者能设置后端白名单；该机制不隔离恶意登记者或被修改的 Python 安装。

```powershell
$mcpStdioApprovalJson=@'
import hashlib, json, os, sys
from pathlib import Path
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.mcp_stdio import MCPApprovedProgram, MCPStdioRegistry
script=Path(os.environ["AGENTGIT_MCP_DEMO_SCRIPT"]).resolve(strict=True)
runtime=Path(getattr(sys,"_base_executable",sys.executable)).resolve(strict=True)
registry=Path(os.environ["AGENTGIT_MCP_STDIO_REGISTRY"]).resolve()
assert registry.parent==script.parent and registry.name=="approved.json"
program=MCPApprovedProgram(interpreter=str(runtime),interpreter_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest(),script=str(script),script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),approved_by=Settings.from_env().operator_id)
with registry.open("xb") as stream:
    stream.write(encode(MCPStdioRegistry(programs=(program,)).model_dump(mode="json")))
print(json.dumps({"program_ref":program.reference()},ensure_ascii=False))
'@ | uv run --locked python -X utf8 -
$mcpStdioProgramRef=($mcpStdioApprovalJson | ConvertFrom-Json).program_ref
$mcpRepository=(uv run --locked agentgit init 'MCP stdio 演示' --key ('mcp-stdio-init-'+[guid]::NewGuid().ToString('N')) --testing | ConvertFrom-Json).repository_id
$mcpBinding=uv run --locked agentgit mcp-stdio-bind stdio-demo $mcpStdioProgramRef echo --effect NON_IDEMPOTENT_WRITE --testing | ConvertFrom-Json
```

`mcp-stdio-bind` 只允许登记引用、工具和本地效果，不接受 executable/args/env/cwd。它启动已批准程序完成现代协议发现和工具列表，然后关闭自有进程。登记以后的脚本或解释器原字节变化均拒绝；不会切换为未批准程序。

继续保存并提交有界程序，无需 HTTP Server 或 endpoint：

```powershell
$mcpDemoRoot=$mcpStdioRoot
$mcpProgram=@{schema_version=1;binding=$mcpBinding;request=@{schema_version=1;arguments=@{value='stdio 本地演示'}};limits=@{schema_version=1;max_steps=1;max_model_calls=1;max_tool_calls=1;max_replans=0;max_tokens=1000000;max_wall_ms=300000;max_response_bytes=65536;max_context_bytes=131072;max_artifact_bytes=65536;max_files=1}}
$mcpProgramFile=Join-Path $mcpDemoRoot 'program.json'
[System.IO.File]::WriteAllText($mcpProgramFile,($mcpProgram | ConvertTo-Json -Depth 100),[System.Text.UTF8Encoding]::new($false))
$mcpLive=uv run --locked agentgit mcp-submit $mcpRepository $mcpProgramFile --expected-version 0 --key mcp-stdio-live --testing | ConvertFrom-Json
uv run --locked agentgit worker --once --testing
uv run --locked agentgit approvals $mcpRepository $mcpLive.run_id --testing
uv run --locked agentgit status $mcpRepository $mcpLive.run_id --testing
```

之后从[HTTP 演示](S12-A-mcp-demo.md)的 `decision` 步骤继续核对动作、批准、执行、查询产物、显式 Fork 和 Recorded；这里的变量可直接沿用。尽管远端提示只读，本例本地效果固定为外部非幂等写，批准前不会调用 echo 工具；已批准的发现进程不计作工具效果。

成功 Live 后保留日志摘要，再显式 Fork 和提交来源范围的 Recorded；可以在提交回放前移除 `AGENTGIT_MCP_STDIO_REGISTRY` 环境变量。Recorded 应完成、actual_attempts=0，`trusted_server.log` 摘要保持，未读取白名单或启动程序，产物与来源完全相同。缺录制明确失败，无 Live 回退。修改程序后要重新审阅和登记，新引用不能替换已有 Job。

自动化复现：

```powershell
uv run --locked pytest tests/unit/test_mcp_stdio.py tests/integration/test_mcp_stdio_runs.py tests/integration/test_mcp_entrypoints.py
```

测试覆盖真实自有进程、Windows Job Object/子进程限制、期限/断连/超限、白名单和环境过滤，以及三处真实 Worker 终止、发现到工具帧之间的失租、精确审批、完整 Recorded 和缺依赖。使用全新证据目录作为 `--basetemp` 时，先创建它的父目录；不可复用已有测试数据目录。测试只清理自有 Schema/进程，保留共享 PostgreSQL 和原始证据。完整阶段及最终四适配桌面包仍按[工程记录](acceptance/S12-A.md)逐项核对。
