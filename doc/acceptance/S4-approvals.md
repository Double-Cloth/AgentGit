# S4 工具策略与精确审批进展

- 日期：2026-10-21。
- 状态：S4 进行中。本记录覆盖精确审批与未知结果阻断，不表示 S4 或 D1 已完成。
- 前置实现：`c90f4d0`，见 [调度与恢复进展](S4-progress.md)。当前 Schema head 为 `0007_exact_approvals`。
- 环境：Windows、Python 3.12.14、真实 PostgreSQL 17.6；无新增依赖，uv.lock 未改变。

## 实现与约束

新增本地 `ToolPolicy / ToolRule / BoundAction`，调用方必须匹配登记的 provider、签名和效果分类。缺省拒绝，外部写/不可逆效果不能通过 preauthorized 绕过精确审批；现有本地三工具与离线模型由 Registry 生成预授权规则。sandbox 预授权契约已保留，真实执行后端仍未实现。

`ApprovalService` 将 Run/Branch、checkpoint、occurrence/attempt、请求摘要、工具签名、效果、策略摘要/版本、actor 与响应上限绑定为动作摘要；请求原文与策略进入 CAS/项目引用，审批本身带截止时间。等待时保留分支绑定，Run/Job 以操作元数据停在 waiting_approval，不修改对应逻辑 checkpoint；已持久 pending decision 和预算继续作为恢复依据。

批准和拒绝需要预期状态版本；同 actor 的同决定幂等，相反决定冲突。批准只重新排队，真正消费与调用意图在同一受租约保护的事务内提交。等待和恢复使用同一 Run、新租约 token；到期扫描有界，不自动批准。取消审批等待可以到安全点完成。

调用意图事务再次检查暂停/取消，修复控制请求在 Runtime 检查与真正发送之间到达的竞态；未发出的计数/预留撤回，不产生第二个 Run 或额外调用。

## 真实服务验证

fake effect 服务由独立 Python 进程监听随机回环端口，通过 HTTP 与 Adapter 通信，不是进程内 mock。它记录请求、幂等命中和效果条目，先 fsync 临时测试目录中的效果记录，再回复或断开连接。Adapter 只允许显式登记的回环端点，禁用代理/重定向，校验有界回执中的 operation_id、请求摘要和结果。

| 用例 | 实测范围 |
|---|---|
| S4-T04 | 审批前请求数 0；批准后请求/效果各 1；重复调用读取已保存结果，不再请求；拒绝、批准前到期和批准后到期均为 0 |
| 精确绑定 | 改参数、checkpoint、策略版本或响应限制后必须重新申请；改签名、效果分类或 provider 直接拒绝；原审批不会被消费 |
| 事务与竞争 | 在 consumed 更新后、调用意图事务内注入失败，消费和意图全部回滚；并发消费者只发出一次 HTTP 请求；决定重复幂等、冲突决定拒绝 |
| 调度与隔离 | managed Job 等待时不可领取，checkpoint 不变；批准后同 Run/new token；旧租约被拒绝；过期回收和等待时取消有效；其他 actor/Repository 不可决定 |
| S4-T05 已覆盖部分 | 服务落地后断连：效果已 fsync；网关标为 outcome_unknown、result_ref 为空、审批已消费；重复调用不发送请求；查询得到绑定原请求的回执，总写动作仍为 1 |
| S4-T06 增补 | 模型/工具意图前注入暂停，或工具意图前注入取消；不发出新调用、不保留虚假预算消费；暂停后同 Run 可恢复完成 |

S4-T05 的“核对后继续”尚未通过：目前有可信查询 Adapter，仍需实现耐久核对服务、预算结算与 Runtime 继续推进，不能把查到回执直接视为 Run 已恢复。

## 验证与剩余范围

测试命令：`uv run --locked pytest tests/unit tests/integration --junitxml=.agentgit/evidence/S4-approvals.xml`。**211 项通过**（108 单元 + 103 PostgreSQL 集成），耗时 186.32 秒；新增审批/策略 33 项、意图前控制竞态 3 项。完整迁移往返和全部既有回归均包含在内。Ruff 检查与 71 个文件的格式检查通过，mypy 44 个源码文件通过。仅有既有 Starlette/httpx 弃用警告，未引入替代依赖。

本地原始证据不入 Git，SHA-256：

- `.agentgit/evidence/S4-approvals.xml`：`c254ea36f559c627812655397deb2dc437f6015ae92c99007d87687a01c91805`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

后续依次完成未知结果核对后的继续执行、业务 API/CLI、鉴权/Origin、分页/下载/错误映射，以及 S4-T09–T20 的全部真实容器执行验收。审批服务尚无 HTTP 入口，fake effect 不接入普通 Demo；`doctor --sandbox` 保持 `execution_ready=false`。没有启动或实现用户代码的宿主执行回退；Linux CI 尚未运行。
