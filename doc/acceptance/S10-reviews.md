# S10 精确提案与版本绑定审批后端

2026-10-26，分支 `codex/s10-repair`。依据设计 5B.4 与实施方案 S10-T05/T06，以及[实施边界](../S10-review-plan.md)。本增量只在隔离测试 Schema 批准配置，没有迁移业务库、修改本机业务配置或生成新桌面包。Repair Lab、真实观测伪修复、随机偶然筛选通过、30 例/四基线和 D2 仍待完成；完整目标继续包括 S11 和 S12-A/B/C/D。

## 精确建议和审阅事务

一个 study 对应一份不可变最终建议，具有独立于 S9 筛选候选的新身份。建议绑定原子/字段最终锁、实际六加四封存摘要、来源配置 ID/version/body ref、完整补丁、适用任务、验收器、环境、预算用量和必要性范围。before/after 展示正式配置原文与完整 Plan DAG，使用来源配置的授权和预算，不把私有验证配置的收紧预算误当正式来源。

审阅票据另冻结操作者与当时的项目配置版本计数器。任何配置版本推进都使旧票据失效；新幂等键可以刷新同一建议的审阅，旧建议和票据均保留。批准在同一项目行锁/事务中检查精确摘要、票据、版本与封存结果，只创建一个配置新版本并写不可变决定；不修改旧配置、已有 Task 或活动头，不启动模型、工具、Run 或 Job。重复原决定复用原记录，改参数/改决定冲突；并发操作者最多一个获准。

`TOOL_RESPONSE_PATCH` 的建议只能保存实验，`after=null`，服务和数据库均设有禁止正式配置采纳的约束。保存返回精确实验定义和 `execution=not_started`，不调用 `ExperimentService.submit`。本轮真实测试验证了合格 Prompt 建议的保存定义、事务回滚与查询；针对真实观测伪修复的完整研究用例仍单独待补，不据工程限制提前宣称 S10-T05 完整退出。

## 计划模板的实际行为

`AgentConfig.plan_template` 为空时保留原编码和首次模型规划行为；有模板时校验 DAG 与工具授权，在同一 Runtime 保存 `plan.accepted`、配置来源和模板摘要，并进入执行。新任务不继承旧 Run 的完成节点、记忆、消息、产物或副作用；模板不额外授权工具。

实际联合用例先进行原子/字段缩减和六加四，再批准确切的 196 字节 Prompt 与缩短后的 Plan：源配置为版本 1，审阅时计数器为 28，批准只创建版本 29。随后显式创建新任务，10 次新模型调用均为 act，没有重新生成首次计划，完成固定验收；从初始化前/后检查点的 Live 以及 Recorded 均通过。批准时 Jobs/Runs/分支不变，新任务和三项重执行在批准之后单独创建，不计作原确认或回归。

另有四个实际持久新任务覆盖正常初始化、暂停/恢复，以及首次规划与模板登记检查点后崩溃。恢复后计划版本仍为 1、只登记一次，没有重复规划或借用旧任务状态。旧 Live/Hybrid 的预算、来源、恢复和计量另行回归。

## 存储与接口

迁移 `0023_repair_proposal_reviews` 新增 `repair_adoption_proposals`、`repair_review_sessions`、`repair_saved_experiments` 和 `proposal_reviews`，历史迁移/快照不改写。归属、源 study/锁/报告、配置版本、动作效果和对象引用有复合 FK/触发器及不可变约束；迁移完整往返和正式回退保护均实测。

API 提供 `POST /repair-studies/{id}/proposal`、`GET /repair-proposals/{id}`、`review/approve/reject`；CLI 提供 `repair-propose`、`repair-proposal-status`、`repair-review`、`repair-review-decision`。输入与现有认证、项目、Schema、大小和幂等边界一致。接口不接受用户上传 passed、after 或权限。详情、请求与版本语义见 [API.md](../API.md#精确修复建议与审阅s10)。

## 验证与复现

所有执行均为冻结 Mock 与真实 PostgreSQL 临时 Schema/CAS，没有付费调用。最终源码为 `source-sha256:92ca679e45828214f5c15473eff37085c0f12c44d1c45687768b5d299d33fb0f`。本轮原始归档为 `.agentgit/evidence/S10-review-adoption-v4.json`，包含实际建议、票据、决定、配置、Task、Job/Run 和全部 CAS 原始字节。独立 `verify_s10_reviews_v1.py` 重放字段前缀、核对实际六加四、重算调用字节，并检查来源配置、版本 29、初始化来源和批准后的新执行。

事务故障覆盖配置创建后、决定提交前、提交后丢失响应；未封存/版本/摘要/操作者错误不造成配置变化。两名审阅者竞争、失败确认不可采纳、只读无 CAS 写入、HTTP/CLI 保存实验定义及原键重试均有实际数据库检查。接口测试禁用了模型、工具和 Job 提交；批准没有副作用执行。

最终同源码 **746 项去重通过：696 单元 + 50 真实集成**，没有失败、错误或跳过。JUnit 分组为 `S10-review-core-template-v5.xml`（712，含单元与 16 项初始化/旧任务/迁移集成）、`S10-review-adoption-api-v4.xml`（2）、`S10-review-controls-v4.xml`（7）、`S10-review-compat-v4.xml`（25）。Ruff、格式和 mypy 160 个源码文件通过；历史源码结果不与本轮累加。

独立摘要 `.agentgit/evidence/S10-review-summary-v1.json` SHA-256 为 `55a15212da5f601e160d28f4e89f197d47f805d3446f2bdfb2c02f51983ee7ea`；原始归档 SHA-256 为 `1b50e8dfcd9fde4220e52b509015da15e14fda21f1101cb9e34df2457b7efa87`，共 1991 个对象、4,858,281 原始字节。脚本核对全部暂存 Python/JSON 源码与实测字节相同，并记录测试/适配器和规范摘要。完整测试收集为 1265 项，不表示本轮执行了全部 1265 项。

```powershell
uv run --locked pytest tests/unit tests/integration/test_plan_template_jobs.py tests/integration/test_kernel_migrations.py tests/integration/test_research.py
uv run --locked pytest tests/integration/test_repair_adoption.py tests/integration/test_repair_review_api.py
uv run --locked pytest tests/integration/test_repair_review_controls.py
uv run --locked pytest tests/integration/test_live_replay.py tests/integration/test_hybrid_runtime.py
```

探索失败均保留：首次测试错误地拦截不存在的 `JobService.submit`，已改为真实 `enqueue`；旧 study 省略字段摘要造成新生成器 KeyError，已修正为兼容读取；API 测试曾把缺失/未知 Schema 的既有 409 误写为 422，已按原协议修正，没有放宽未知字段断言。早期指向不存在测试文件的收集失败未执行测试，不计通过。旧源码结果不与最终结果重复累计。
