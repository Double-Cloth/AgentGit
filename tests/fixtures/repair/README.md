# S10 修复评估资产

`methods-v1.json` 冻结五种方法的协议源码摘要、固定全局 Prompt 和共同预算，范围为 methods-only，不是完整 30 例故障数据集；其 dataset_release_ref=null、heldout_execution=not_started 保留冻结时事实。新增 [`v1/`](v1/README.md) 只冻结三十套新输入、拟议家族和 20/10 划分，72 项输入/开发正常控制通过，故障运行及研究评估仍未完成。

方法及执行接线在 `tests/integration/repair_benchmark_protocol.py`、`repair_benchmark_execution.py`。选择只读取共同冻结空间和公开轨迹，不读取 case_id、split、正确补丁或 trial 成败。主方法明确降级并保留生产顺序，无排序基线可以与它等价。固定全局 Prompt 只提议一次；不缩减方法为 20/0/6/4，其余为 14/6/6/4，总资源和独立六加四门槛相同。

旧工程 Prompt 例只用于接线冒烟，含预定锚点，不计入未来任务集或 10/12 目标。新 30 例仍须按任务家族建立资产，先冻结方法/模型/输入和 20/10 划分，再执行保留集；不能把同模板轻微改写跨 split，也不能用标签选择起点或候选。见[评估计划](../../../doc/S10-benchmark-plan.md)。

方法或预算变化须新建版本，说明是否已见相应任务/保留结果，不能覆盖旧摘要。本资产不批准正式配置或外部调用，实际试验继续经过既有 Job/Run、验证和权限检查。

`regressions-v1.json` 新增四套独立回归规格，包含框架顺序、精确数值和三个日期边界，与三十例来源全部不同。四项正常 Worker Run 已通过；正式评估仍须登记并实际执行候选的四次回归。公开合法起点与固定只读环境见[准备记录](../../../doc/acceptance/S10-benchmark-origins.md)，本目录仍无完整研究运行 release。

`faults-v2.json` 是冻结前的运行故障草案，保留原输入/20/10/类别与分母。旧 13 家族提案中同源指令不能按目标改名跨 split；v2 合为 11 家族，所有外部指令只在保留集，开发路径是独立导出缺陷。正式驱动读取此文件；`condition_for` 仅保留 v1 构造兼容。机制契约与两个新开发源/对照已核对，仍无保留执行或完整运行 release。见[五方法原型及规格修正](../../../doc/acceptance/S10-dataset-driver.md)。

后续 `declarations-v1.json` 与 `evaluation-release-v1.json` 已形成[完整运行冻结](../../../doc/acceptance/S10-evaluation-freeze.md)，绑定上述故障草案的确切字节及全部运行条件。开发集正式执行中，保留未开始；旧资产中的 not_started/null 是创建时事实，不回写覆盖。声明是评估元数据，不能作为 Agent 输入或预填成绩。
