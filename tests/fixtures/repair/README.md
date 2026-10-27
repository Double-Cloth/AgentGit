# S10 修复评估资产

当前只有 `methods-v1.json`，冻结五种方法的协议源码摘要、固定全局 Prompt 和共同预算，范围为 methods-only，不是 30 例数据集。dataset_release_ref=null、heldout_execution=not_started，不能据此宣称数据、划分或研究评估已经完成。

方法及执行接线在 `tests/integration/repair_benchmark_protocol.py`、`repair_benchmark_execution.py`。选择只读取共同冻结空间和公开轨迹，不读取 case_id、split、正确补丁或 trial 成败。主方法明确降级并保留生产顺序，无排序基线可以与它等价。固定全局 Prompt 只提议一次；不缩减方法为 20/0/6/4，其余为 14/6/6/4，总资源和独立六加四门槛相同。

旧工程 Prompt 例只用于接线冒烟，含预定锚点，不计入未来任务集或 10/12 目标。新 30 例仍须按任务家族建立资产，先冻结方法/模型/输入和 20/10 划分，再执行保留集；不能把同模板轻微改写跨 split，也不能用标签选择起点或候选。见[评估计划](../../../doc/S10-benchmark-plan.md)。

方法或预算变化须新建版本，说明是否已见相应任务/保留结果，不能覆盖旧摘要。本资产不批准正式配置或外部调用，实际试验继续经过既有 Job/Run、验证和权限检查。
