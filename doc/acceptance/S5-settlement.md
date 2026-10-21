# S5 Recorded 模型核对来源批次

本批接通原 Runtime 保存未知状态、随后由可信 Resolver 核对并继续的模型来源。它是完整 S5 的增量，尚不替代恢复封存来源、自定义外部写来源和 S5 汇总验收。

## 已实现

`UncertainRecording` 只重现经过核验的历史 `call.outcome_unknown` 诊断。它保留原请求、空回执或诊断字节、未知轨迹和 Token 预留；普通 `Recording` 仍拒绝把未知诊断作为完整结果，未知类型也不能进入结果缓存复用路径。

`SettlementReader` 分别验证核对事件、call_resolutions、原调用身份、原检查点、请求/结果摘要、actor、provider 签名、证据和对象归属。允许的调用限定在来源 Job 自身恢复链，历史 Fork 的其他 Job 不提供授权或结果。读取器只读取已发布证明，不调用 Resolver 或发送原动作。

纯 Runtime 先重算未知检查点，再通过原 `_settle` 结算确认响应。原步骤/调用数不重复增加，模型预留转换为实际逻辑用量；确认轨迹替换未知轨迹。取消和持久期限在结算后处理，不能借控制请求丢掉已确认的用量。每份范围内的核对证明必须恰好消费一次。

范围可以止于历史未知检查点。此时后来的确认事件不属于范围，不会提前采用；`verified=true` 仅证明未知状态被重现，`logical_status=outcome_unknown` 和尚未释放的预留仍保留，不能当作任务成功。

## 验证与当前边界

模型 fixture 在首次计划、中间决策和最终回答处模拟回执丢失，分别覆盖继续、取消和到期。重放期间禁止模型、工具、Resolver、网络和宿主工作区调用，逐检查点比较状态与产物；既有源分支不变，实际调用为零。

缺少核对证据、确认响应或原未知诊断，以及账本结果链接错配均明确失败。调用前用量、响应上限被改和额外未消费证明也被拒绝。核对历史再次经过复合 Recorded 的组合用例单独验证。

首批未知诊断单元与模型来源专项 **16 通过**、101.60 秒（`.agentgit/evidence/S5-settlement-source.xml`）；后续预算/证明错配专项 **3 通过**。复合组合专项 **1 通过**、21.02 秒（`S5-settlement-compound.xml`）。

扩大回归：单元及核对来源、原 Runtime、耐久 Replay、复合来源、录制读取和迁移共 **286 通过**、664.82 秒（`S5-settlement-core-final.xml`）；真实执行恢复、Recorded Job/Runtime/产物及复合执行来源共 **19 通过**、353.59 秒（`S5-settlement-execution-final.xml`）。

回归另发现 Live 来源的 Recorded 部分范围报告错误使用累计用量计算剩余额度。报告现按冻结的 `budget_baseline` 计算，保留原累计历史；新增真实 Live → 部分 Recorded 用例先复现失败，修正后通过。修正后的单元全量及核对、Live、Replay Job 相关 **249 通过**、361.18 秒（`S5-settlement-report-final.xml`），包含上述复合组合用例。上述分批有重叠，当前全仓收集 **570 项**，不声称本批一次运行全部测试。

Ruff、155 个文件格式检查、mypy 的 80 个源码文件及 sdist/wheel 构建通过；固定镜像源码/系统包检查与 doctor 通过。本批不改变依赖或镜像，测试保留现有 Starlette/httpx 弃用警告。证据文件在本机 `.agentgit/evidence/`，不提交。

本批结束时尚待接通的 Worker 接管 `run.outcome_unknown` 来源，现由后续 [接管封存批次](S5-sealed-unknown.md) 完成；fake 外部写的原 Runtime 专项见 [S5 汇总](S5.md)。容器未知执行继续采用已验证的清理/丢弃路径，本批不宣称支持任意未知容器输出的可信恢复。真实模型供应商的查询能力尚未接入；上述来源使用受控 Mock 模型和后端可信 Resolver fixture。
