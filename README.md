# AgentGit

AgentGit 旨在为 AI Agent 提供执行历史的版本管理与调试能力，让开发者能够回溯状态、隔离分支、重放执行并比较差异。

通过受控实验、故障定位与最小修复搜索，帮助开发者找到可验证、可审计的修复方案。

当前 Windows x64 候选为 [0.1.8](doc/acceptance/S12-package-018.md)，包含六页工作区及 MCP、固定 LangGraph、四角色协作、A2A。实际包内34项通过，同产品源码2,855个技术节点有分批覆盖；原失败和50项历史冻结研究单列。[D3就绪报告](doc/D3-readiness-report.md)保留独立Windows／原生、人工标签／定位目标和S11人工预期等门槛，正式D3尚未通过。

按[本地开发说明](doc/DEVELOPMENT.md)准备独立API、Worker、PostgreSQL和固定Docker镜像，再使用[桌面说明](desktop/README.md)连接。分发文件在 `.agentgit/packages/desktop-0.1.8/`，摘要见候选记录；桌面不嵌后端、不自动迁移业务库。固定离线演示见[D1指南](doc/D1-desktop-demo.md)，四项适配演示见[最终候选记录](doc/acceptance/S12-package-018.md)。
