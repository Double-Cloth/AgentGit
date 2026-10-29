# S12-A MCP 实施范围

2026-10-29，`codex/s12-adapters`，依据用户明确延续工程的选择及实施方案第 1.2 节。S11/原生/人工前置保持待补，本文不是阶段验收记录。

固定 MCP `2026-07-28`，资料核对于本日：[版本/逐请求元数据](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)、[基础字段](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)、[Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[工具](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。该版本使用逐请求版本和能力，不使用旧 initialize 会话。先实现明确的 HTTP JSON 完整响应子集；不宣称通用 MCP 客户端。

1. 由操作者登记本地 server_identity、唯一 `http://127.0.0.1:端口/mcp` 和本地效果分类。禁止任意主机、DNS、私网、用户信息、查询/片段、代理及重定向。stdio 启动、OAuth、资源/提示词、SSE、额外输入/任务请求均暂不支持，配置不得携带程序或命令。
2. 每次 POST 的协议头/方法/工具名与 `_meta` 对齐。有界分页列出工具；缓存以完整定义摘要绑定本地端点及身份，调用前重新列出并核对固定摘要，变化时拒绝，不替换旧绑定。远端 serverInfo、instructions、annotations 均为不可信数据，不改变本地授权。
3. 先支持闭合 JSON Schema 子集：object/string/integer/boolean/null/array、properties/required/additionalProperties=false、enum、长度/项数/整数上下界；递归深度和节点数有界。拒绝外部或内部 `$ref`、number、组合/正则/自定义头等未支持关键字，不忽略约束。输入及声明输出都验证，不引入新依赖。
4. 工具响应标准化到既有 CAS/事件调用通道，记录 server_identity、tool_name、schema_digest、arguments_digest、response_ref（既有调用轨迹）、latency_ms 和本地 effect_class。只接受文本/结构化完整结果，超时/协议/工具错误使用固定本地错误；传输无重试，已发送写入的失败保持 outcome_unknown。取消只说明关闭传输，不能推断远端动作撤销。
5. 凭据仅由本机进程环境在客户端构造时注入，禁止存入绑定/调用请求；结果中确切凭据脱敏，不反射远端错误/HTTP 正文到异常。先验证内容，再交给既有精确策略与审批网关；Recorded 路径只消费绑定和录制，不构造 HTTP 客户端或重新发现，缺录制明确失败。

先完成契约/传输/适配器及可信独立本地 Server fixture，再接真实 PostgreSQL 调用记录、精确写审批、未知恢复与 Recorded；随后补充可运行演示和既有 S2/S4/S5/S6/S11 兼容性。最终证据另写 `acceptance/S12-A.md`；首增量通过不等于 S12-A/D3 已完成。S12-B 仅在 A 的声明范围测试和工程核对齐备后开始。
