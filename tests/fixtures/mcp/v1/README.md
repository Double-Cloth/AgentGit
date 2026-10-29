# MCP v1 本地 fixture

固定协议为 MCP `2026-07-28`，只覆盖声明的 HTTP JSON 完整响应子集，规格和拒绝范围见 `doc/S12-A-mcp-plan.md`。`tools.json` 是独立本机测试 Server 的固定 echo 定义；readOnlyHint 不是本地权限依据，测试会将同一定义明确登记为外部非幂等写并核对审批。

`tests/helpers/mcp_server.py` 使用系统分配的回环端口、独立进程和测试自有 ready 文件。`/control`、`/state` 只供故障注入/请求计数，不是产品 API；Server 不执行 Shell、Python 脚本或外部网络动作。凭据测试只用公开虚构 sentinel，Server 不保存头部或凭据日志。

运行 `uv run --locked pytest tests/unit/test_mcp_contracts.py tests/unit/test_mcp_http.py` 验证无数据库契约。`uv run --locked pytest tests/integration/test_mcp_adapter.py` 需要已准备的独立 PostgreSQL 测试库；每例自有 Schema、CAS 和 Server，进程结束由 fixture 清理。可设置唯一 `AGENTGIT_MCP_EVIDENCE_DIR` 归档原始对象/事件；目标已存在的同名文件会拒绝覆盖。复现某次旧结果应使用相应冻结源码/规范和实际驱动，不修改旧归档或源码 guard。

这些成功/故障例不构成通用 MCP 兼容矩阵、真实供应商结果或正式 D3 验收。
