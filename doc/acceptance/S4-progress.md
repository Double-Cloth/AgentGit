# S4 调度与恢复进展

本记录归档 `c90f4d0` 时点；后续策略与审批进展见 [S4-approvals.md](S4-approvals.md)，阶段总状态见 [PROGRESS.md](../PROGRESS.md)。

- 日期：2026-10-21。
- 状态：S4 进行中，未满足阶段退出条件；D1 尚未完成。
- 前置：[S3 验收](S3.md)。完整范围仍依照设计/实施方案 v1.0.0，未删减必选执行工具或后续阶段。
- 环境：Windows、Python 3.12.14、真实 PostgreSQL 17.6；无新增依赖。
- Schema head：`0006_job_budget_status`。`0005_durable_jobs` 引入调度与活动写者约束；随后用新迁移补齐 Job 的预算耗尽状态，未重写历史迁移。

## 已实现

`JobService` 使用 PostgreSQL 耐久队列、行锁/SKIP LOCKED、租约和递增 fencing token；提交在同一短事务内建立 Run、Job、Task/Branch 绑定和幂等响应。冻结配置/输入/语料/代码摘要与计划版本，落库时核验版本；同幂等请求不创建第二次动作。分支活动写者由数据库触发器保护，终态释放、暂停保留、恢复原子交接。

`Worker` 在领取事务结束后执行 Adapter；独立心跳线程续租。调用意图、调用结果、事件与 HEAD 写入校验租约，失效 Worker 无法写入迟到响应或推进 HEAD。控制请求与实际 applied 状态分别保留，按状态版本拒绝并发旧请求；跨恢复的墙钟截止时间不重置。

接管创建关联旧 Run 的新恢复 Run，保留旧 Commit 和检查点；新物理工作区按 Run/租约隔离，从 CAS 完整清单重建。完整调用结果严格匹配后复用；本地产物通过录制内容重新物化，实际工具调用次数不增加。意图存在但结果缺失时保存 outcome_unknown 状态与保守预算预留，不自动重发。终态新尝试与同一 Job 恢复区别处理，前者不会复用旧 Run 的调用。

## 已取得的测试证据

| 实施方案用例 | 当前覆盖 | 边界 |
|---|---|---|
| S4-T01 | 重建服务后处理持久 Job；真实 Worker 子进程在第 4 个检查点、第 1/17 个调用结果落库后退出；接管后完成，实际计数仍为 11 次模型、1 次搜索、6 次读取、2 次产物写入 | 已覆盖当前三工具；容器调用恢复尚未覆盖 |
| S4-T02 | 两 Worker 竞争只领取一个；租约过期后 token 递增；旧 Worker 的调用前提交、心跳与迟到模型响应拒绝；同分支第二任务在调用前冲突 | 后续需补容器清理与失租联动 |
| S4-T03 | 相同 key 的并发相同请求返回同 Job/Run；异参冲突；事务注入故障时 Job/Run/绑定/幂等记录全部回滚 | 业务 HTTP 409 映射与其他修改接口尚未实现 |
| S4-T06 | queued 暂停/取消；调用中暂停保留 requested 至安全点；同 Run 恢复；心跳在长调用期间延租；墙钟期限跨暂停不重置 | 执行中容器取消尚未覆盖 |
| 未决调用 | 真实进程在模型调用内退出；新 Worker 不调用模型、保留步骤/调用数和预算预留；未知结果取消仅保留 requested | fake effect 的核对后继续仍待实现 |
| 回归与隔离 | 完整迁移往返、无租约上下文禁止执行受调度 Run、Job/Run 跨项目查询拒绝、终态重试重新执行模型且增加计划版本 | 不替代尚未实现的 HTTP 身份与 Origin 测试 |

执行结果：完整既有与新增回归 **172 项通过**（94 单元 + 78 PostgreSQL 集成）；随后新增终态重试、提交事务回滚、项目边界三项单独通过，共 **175 个不同用例**。Ruff 检查/格式与 mypy（39 个源码文件）通过。完整回归有一条既有 Starlette/httpx 弃用警告，未引入替代依赖。

本地原始证据不入 Git，SHA-256：

- `.agentgit/evidence/S4-scheduler.xml`：`3e90aa8bcaaa0d1900083d83f3441c129a5abfbd6a1cb50c0e0f5838c647b95d`。
- `.agentgit/evidence/S4-scheduler-extra.xml`：`10e07d6599ae353bfbedf854bcaf8321f71b176d012099cb4d97bd3954bacf48`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

完整回归命令为 `uv run --locked pytest tests/unit tests/integration --junitxml=.agentgit/evidence/S4-scheduler.xml`；补充证据为 `uv run --locked pytest tests/integration/test_jobs.py -k 'terminal_retry or enqueue_failure or repository_scope' --junitxml=.agentgit/evidence/S4-scheduler-extra.xml`。GitHub Actions 的 Linux 运行尚未触发，本地通过不代表 Linux CI 已运行。

## 下一步与未完成项

继续实施方案 S4 第 3 步：Tool Policy、精确审批、一次性消费、独立 fake effect server、未知写结果核对；随后实现 API/CLI、鉴权/Origin、分页、下载和错误映射。

S4-T09–T20 尚未验收。`terminal_exec / code_exec` 的真容器、固定镜像、独立采集身份、可信 PID 1 截止时间、资源限制、有界导出与整容器清理均为必选后续工作；不存在宿主执行回退。当前 `doctor --sandbox` 仍为 `execution_ready=false`。全部 S4 退出用例通过后才能进入 S5。
