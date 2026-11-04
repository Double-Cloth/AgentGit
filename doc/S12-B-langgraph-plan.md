# S12-B LangGraph 支持范围与实施计划

2026-10-29，接续 A 工程提交 `58e3d7c`。继续使用用户对 S12 工程先行的明确授权，正式 S11/D2/原生人工前置及最终四适配新包/D3 保持。根设计/实施规范未变化，B 不降为可选，也不宣称支持任意 LangGraph 图。

固定 Python LangGraph `1.2.18`：[官方 PyPI](https://pypi.org/project/langgraph/1.2.18/)，wheel SHA256 `f430f28ea7fbc129c3cf7dc31c19c33dc27d93aeebd25c6d64fd5c1bad93d27d` 已独立下载核对。其余必需传递依赖随 uv 锁定，避免无关升级；不接 Agent Server/LangSmith 托管服务，不引入另一个持久数据库。官方[checkpointer](https://docs.langchain.com/oss/python/langgraph/checkpointers)和[time-travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)用于核对原生语义；原生 time-travel 会重执行后续节点，不代替项目严格 Recorded。

当前解析为 checkpoint `4.3.1`、langchain-core `1.7.1`、langsmith `0.14.15`，无原包升级。禁用传递依赖自动注册的 `langsmith_plugin` pytest 插件；产品图另用局部 tracing_context(enabled=False)，不依赖测试配置关闭产品遥测。

## 声明的图

首批只支持 `mcp-read-review-publish/v1`：START→fetch→review→publish→END，fetch 失败时直接 END。fetch 使用已固定的 MCP READ_ONLY 工具，经原调用网关/预算；review 为纯审阅备注处理，publish 只发布固定路径的 AgentGit 产物。review 前可配置原生静态断点。图清单必须匹配已登记节点/边与版本；不导入用户 Python、Callable、表达式或任意远程图。

此批明确拒绝外部写效果、未知节点、Shell/代码节点、循环、并发、动态 Send、子图、自定义 reducer、任意 Python 状态和动态 interrupt 响应。业务暂停/备注不是工具权限批准；S4 容器与原精确审批策略保持。A 的外部写不受 B 只读范围限制而降级。

用实际 `StateGraph` 执行，复用原生 checkpoint、pending writes、静态暂停、`invoke(None, config)` 恢复及 `update_state` Fork 语义。用 SDK 原生节点边界逐段驱动控制，内部边界自动续跑，用户配置的 review 边界可停留。首批 Fork 仅允许 fetch 完成后、review 前的完整安全原生 checkpoint，创建独立 Run/thread/分支并编辑审阅备注；保留已完成 fetch 的录制，不重新获取已保存输入。来源状态和分支不变，对比采用相同受支持图/断点配置的原生参考图 next/状态/产物与事件顺序。

## 与版本内核连接

沿用原 Job/Run/History/CAS/事件/租约/分支写锁，只扩展 jobs 类型约束，迁移仍只在隔离测试 Schema。图清单、固定 MCP 绑定、预算、代码/锁/SDK 版本进入不可变输入。原研究和 MCP 路径不借用图 Task 身份。

通过 SDK 公共 checkpointer 接口连接已有 History，可用公共 checkpoint tuple 还原局部内存索引；持久权威仍为 AgentGit，而不是仅依赖 InMemorySaver。保存原生 checkpoint 与 pending writes 的有界 AGJSON 表示，不读写 SDK 私有存储实现，不启用 Pickle 或任意类型反序列化；不支持的原生值明确拒绝。同步 durability、有限单节点并发、租约/fencing 保护与 SDK 原生 ID→Commit 映射均须核对。逻辑状态、原生物理 ID/时间与本次实际用量分别记录，不伪装成字节无差别原生恢复。

fetch 先预留逻辑用量和确切调用前检查点，再调用既有网关；已有完整结果优先消费，进程中断或失租不得重复实际请求。原生 pending writes 耐久后恢复应跳过已经完成的节点。排队取消/暂停、原生静态暂停/恢复、失败与墙钟限额各自保留状态，不借恢复扩大权限。

Recorded 固定来源 Run/起止 Commit，核对图/SDK/环境、原始调用和原生输入。重算图逻辑投影与产物，消费来源录制和已记录的控制，禁止真实客户端、模型或 stdio 进程；缺失依赖明确失败。原生随机 checkpoint ID、时间与实际遥测不进入逻辑等价投影，保留原值供审计。SDK 追踪通过局部上下文明确禁用，测试即使预设 tracing 环境也必须零遥测请求，不修改全进程身份设置。

## fixtures、验证与退出

先固定 `tests/fixtures/langgraph/v1/` 图清单、输入/预期和不支持节点反例，再锁依赖和实现。用可信本地 MCP fixture 获取固定输入；原生参考图使用相同录制输入，独立核对暂停/恢复/Fork 语义，实际网络计数另行核对，不能以第二次参考执行增加外部效果。

覆盖真实 Worker 进程中断（意图/结果/pending writes/checkpoint）、HEAD/版本/跨项目、Fork 来源不变、失败/取消/暂停/预算、缺录制、追踪关闭及原生值拒绝；检查实际请求、状态/产物和所有原始关系。重新执行 S2/S4/S5/S6/S11 相关兼容、包内查询/控制/来源展示。提供可运行演示及 `acceptance/S12-B.md` 的 T02/T05/T06 映射；本机 B 核对齐备后才进入 C，最终包与正式前置保持待补。
