# S4 未知结果核对与恢复继续

本记录归档 ac4f211 时点；后续 HTTP/CLI 接入证据见 [S4-service.md](S4-service.md)。

- 日期：2026-10-21。
- 状态：S4 进行中；本记录完成 S4-T05 的当前 fake effect 核对闭环，不表示全部 S4 或 D1 已完成。
- 前置：`d7d29a0`，见 [精确审批](S4-approvals.md)；Schema head：`0008_call_resolutions`，只新增前进迁移。
- 环境：Windows、Python 3.12.14、真实 PostgreSQL 17.6；无新增依赖。

## 实现

`ReconciliationService` 使用服务器登记的 `OutcomeResolver` 查询结果。原请求、provider/签名、Run/Branch/Job、恢复来源链、actor 与预期状态版本必须匹配；网络查询期间不持有数据库事务。缺少可信肯定回执时不重新排队、不重发；查询过程中控制版本改变时拒绝附着旧结果。

肯定回执、完整响应与 `SavedCallOutcome` 进入 CAS，在一个短事务内建立不可变 `call_resolutions`、更新原调用结果、追加 `call.reconciled` 并重新排队。相同确认重复或并发提交返回同一记录；核对事务失败时结果、记录和 Job 状态全部回滚。旧未知事件、原诊断与原 Commit 保留。

`CallIntent` 显式校验既有 Schema 1 的意图字段，不改变 canonical 字节形式。Worker 将核对结果与当前停止点、原调用前快照、请求、签名、效果及证据引用逐项核验。Runtime 仅完成原调用的观察，不再次调用 Adapter；已计入的步骤、调用和 attempt 不重置、不重复扣除。模型未知时保留预留，确认后按 mock_utf8_bytes 计量结算。核对 ID、actor 和物理 Run ID 不混入逻辑 AgentState。

核对后的停止点可以在同一 Run 继续；若 Worker 领取后或消费结果后崩溃，新恢复 Run 仍从耐久状态推进，原 Run 封存中断事实。取消和预算到期在确认结果结算后落实，不宣称撤销已落地动作。

## 用例证据

| 要求 | 实际验证 |
|---|---|
| S4-T05 | 独立 HTTP 服务先 fsync 效果再断连；Job 停为 outcome_unknown；可信查询并耐久确认；Worker 继续完成三框架报告；实际 POST 和效果计数始终为 1 |
| 预算与逻辑调用 | 完成时仍为 11 模型 + 9 工具调用、20 个逻辑轨迹；未知轨迹替换为确认结果，原未知快照不变；模型预留按真实记录的请求/响应字节之和结算 |
| 控制 | 核对后已有取消、持久墙钟已到期均不再发出新模型/写调用；最终模型响应待核对时的取消不会被 completed 覆盖 |
| 崩溃 | 三个独立进程故障：未知响应录制后、核对后领取租约、核对结果消费检查点后；旧 Run 保持 interrupted，恢复 Run 完成，写动作不重复 |
| 边界与竞争 | actor/provider/签名/版本/Repository/调用失配在查询前拒绝；查询期间可 NOWAIT 锁定 Run/Job；并发确认只建立一条记录；事务故障完整回滚 |
| 拒绝路径 | 无回执、不完整 HTTP 响应、超出原响应上限、缺证据均不推进；删除证据/响应或篡改结果链接后明确失败，无执行回退 |

测试助手通过本地注册表将一次搜索观察绑定到 fake effect 服务，分类为 NON_IDEMPOTENT_WRITE 并要求审批。它只用于验证 Worker 的副作用路径；普通研究 Demo 仍保留三个原工具与原效果分类，没有真实外部写入。

## 回归与剩余范围

最终回归命令：`uv run --locked pytest tests/unit tests/integration --junitxml=.agentgit/evidence/S4-reconciliation.xml`。**235 项通过**（108 单元 + 127 PostgreSQL 集成），耗时 263.05 秒；新增核对用例 24 项，包含全部迁移往返与既有回归。Ruff 检查、76 个文件格式检查和 mypy 46 个源码文件通过；仅有既有 Starlette/httpx 弃用警告。

本地原始报告不入 Git，SHA-256：

- `.agentgit/evidence/S4-reconciliation.xml`：`444b70813ff1c49224da793160fa1e700fdd855a2c23023c6c475eb4343feb76`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

当前业务 HTTP/CLI、鉴权/Origin、分页/下载/错误映射，以及 S4-T09–T20 的全部真容器执行仍待完成。未取得肯定回执的外部系统仍保持 outcome_unknown；没有“批准即成功”或自动重发路径。离线模型用量不代表真实供应商 Token/费用。Linux CI 尚未运行，`doctor --sandbox` 保持 execution_ready=false。
