# LangGraph v1 fixture

固定 LangGraph `1.2.18` 与 `mcp-read-review-publish/v1`，支持范围见[计划](../../../../doc/S12-B-langgraph-plan.md)。`graph.json` 的 fetch→END 是受控错误路由，其余为固定顺序；清单不是任意代码加载器。`cases.json` 固定本地输入、静态断点 next、节点顺序、Fork 备注以及不支持节点/效果反例。

后续用 SDK 原生参考图核对暂停、恢复和状态更新，不用自建状态机冒充 LangGraph。参考图复用已保存的相同工具输入，实际协议请求/模型/进程计数由真实 Worker fixture 独立记录。严格 Recorded 不使用原生 time-travel 的 Live 重执行；原生物理 ID/时间与逻辑投影分别审计。

当前仅固定规格和反例，尚无 B 实现或通过声明。依赖、实际图执行、恢复/Fork、原始审计、演示与兼容测试按计划逐项推进；业务库和原研究 release 不变。
