# S3 离线研究 Agent

本例只比较 Aster、Birch、Cedar 三个虚构框架，使用六份固定合成资料和 Mock LLM，不依赖 API Key 或互联网，也不会访问资料中的 `.invalid` URL。评估时间固定为 2026-10-20 UTC。

先按 [开发说明](../../doc/DEVELOPMENT.md) 准备 Python、PostgreSQL 并升级数据库，再从项目根目录运行：

```powershell
uv run --locked alembic upgrade head
uv run --locked python examples/research_agent/run.py
```

JSON 输出包含 Repository、Task、Run、最终 checkpoint 和独立工作区路径。成功时工作区有 `report.md` 和 `sources.json`；后者保存逐条结论、引用原文、来源 URL/正文 hash/采集时间/有效期以及读取调用的逻辑标识。资料中的正文始终作为不可信输入，不改变工具白名单。

```powershell
uv run --locked python examples/research_agent/run.py --max-steps 3
```

该命令以非零状态退出并报告 `budget_exhausted`，保留中间历史。每次演示创建新 Repository 和 Run，不覆盖之前运行。

正常任务包含规划、搜索、六次读取、两次产物写入、最终独立验收；生成 21 个状态 Commit，记录 11 次 Mock 模型调用与 9 次工具调用。读取节点必须等待搜索完成；写报告必须等待六份资料读取完毕。重规划最多两次，旧计划版本保留。

预算中的 `tokens` 是明确标记为 `mock_utf8_bytes` 的合成计量，按模型请求/响应 UTF-8 字节数计数，并非真实供应商 Token 或账单。模型调用前预留输入字节和最大响应量；截断响应仍记录原始返回长度。Mock 费用为零，真实模型成本将在 S6 单独记录。

本例继续保留 S3 同步三工具入口。S4 已接入持久 Job/Worker、暂停恢复、精确审批和两个真实容器工具；五工具启动命令、统计/整理产物及预算说明见 [API/CLI 容器流程](../../doc/API.md#五工具容器流程)。S4 仍待补齐清理后新 attempt 的预算续跑与最终验收，本例不能代替 D1 验收。
