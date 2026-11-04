# S10 耐久逐补丁缩减增量

分支 `codex/s10-repair`，接续纯协议提交 `8937874`。当前已把 S9 选定候选接到实际独立 Live/Hybrid Job、物理计量与原子候选锁；确认响应池尚未开启，不能标 verified、稳定修复、正式采纳、S10 或 D2 完成。S6/D1 独立 Windows/原生验收和 S8 人工标签仍按用户指示暂缓。

## 冻结和执行

`RepairStudyService` 只接受当前代码下已经排空、封存为 `needs_confirmation` 的 S9 搜索。每个来源只建立一个 study，绑定原 manifest、停止事件、选中提案/独立通过结果、screen 验收器及候选有序原子内容。幂等重试先返回旧绑定；同键异参、跨项目、扩大原 reduce 次数或另建后续 study 均拒绝。旧代码搜索保留只读，不借新版本重判历史结论。

`repair-study-worker` 领取独立控制租约；每次删除产生持久意图，再由 Replay 原子创建并绑定新的普通研究 Job/Run。模型、工具由普通 `worker` 执行，提交、查询、缩减控制及结算本身不调用 Adapter。Live 沿用原 checkpoint 和固定验收条件，Hybrid 重新冻结删除后集合的精确 policy，并以当前 reduction trial 自有 CAS 归属注册输入，不能借用其他提案的权限。

`RepairReductionService` 从实际 Job 终态、当前 Run、固定验收器、Replay 报告和原始调用账本生成收据。确定失败、执行错误、取消、预算与安全分别保留；`assessment` 必须绑定同一 Run 和验收器。已知物理计量、收据、协议步进和事件在同一事务提交；丢失确认后重试复用，不重复计费。未知效果/计量保存待核对标记及可用观测，不产生假零或新删除；旧未知观测不能重新挂起已经结算的试验。

每次删除成功后由既有内核重新检验新集合中的剩余单项。完成规定确定性 fixture 删除测试才声明该有序原子删除语法下的 `one_minimal_under_fixture`。非确定样本、未完成、异常和预算停止仍为 `not_proven`；字段缩减为 `not_run`，不能据此声明全局最小或文本级最小。

## 全阶段预算与停止

S10 不建立新的资源上限：准入读取原 S9 已消耗调用/字节/费用和原创建时间，加本阶段实际结算与完整未决预留。次数含取消的已预留意图，墙钟包含排队、控制停止和等待确认；不会因为 S9 查询中的终态墙钟已冻结就重开计时。reduce 继续保护原六次 confirm、四次 regression 的资源。外层共享预算不足时可锁定当前已筛选候选，但保留未测删除、`reason=search_budget` 和未证明最小性，不把并发占满误作预算终止。

锁定事务同时保存最终候选、全部选择结果/Run、缩减摘要和审计事件，进入 `awaiting_samples`。这个状态只表示候选已锁、确认池未开启；后续确认仍受最初墙钟和剩余预算限制，超期不得扩额。锁后不能改变缩减状态。取消单调；未派发意图只有证明没有实际 Job 才能用 `not_started` 零物理用量收据释放，已派发任务须由原 Worker 应用取消并按实际账本结算。

新迁移 `0019_repair_reduction_studies` 和冻结 metadata 增加八张自有表。数据库检查来源排空、独立 Job、顺序意图、租约、终态、锁及对象归属，历史行不可重写。迁移只在测试自建 Schema 演练，没有迁移业务库。

## 当前实测

当前源码 `source-sha256:0b36604d418aa35cc16d029b65fef83bb65159b6cdf6972fd0cd35333031bb99`。675 个不同测试通过：639 单元、13 项新缩减集成、3 项新接口/进程/Hybrid 集成、20 项共享 Replay/Hybrid 和迁移回归，无失败/跳过。ruff 检查、282 文件格式检查和 139 源文件严格 mypy 均通过；保留上游 FastAPI/httpx 测试适配弃用提示，未为此引入新依赖。所有实际试验执行期间包源码和 `uv.lock` 保持冻结。测试使用独立 PostgreSQL Schema 和临时 CAS，结束由 fixture 删除；未调用付费供应商。

| JUnit（`.agentgit/evidence/`） | 覆盖 |
|---|---|
| `S10-reduction-unit-v5.xml` | 639 单元，含十项新增收据和外层预算停止契约 |
| `S10-reduction-integration-v5.xml` | 13 项；双原子独立删除、原预算保护、单连接池并发幂等、未知保留、派发/结算事务故障、过期租约、取消和数据库不可变 |
| `S10-reduction-boundaries-v2.xml` | 3 项；真实 HTTP/CLI、拒绝伪造结果、只读不写 CAS、子进程派发提交后退出恢复、Hybrid 当前 trial 自有输入和物理/逻辑计量 |
| `S10-reduction-regression-v5.xml` | 20 项；完整迁移往返/正式回退保护、原 S9 物理派发/结算及 Hybrid、公共模板 API/CLI 和单连接池 |

双原子案例原 S9 五个筛选 trial 累计 148,201 个 Mock 请求/响应 UTF-8 字节。删除 Prompt 和删除 Plan 各运行新的 Job/Run，均硬失败；新增 73,857 字节，整个过程累计 222,058 字节、15 次实际模型调用和 9 次实际工具调用。确认和回归次数均为零，费用为本地 Mock 零费用，不解释为供应商 Token。最终保留两个必要原子，原 S9 决定和来源分支不变。

`.agentgit/evidence/S10-reduction-real-v4.json` 保存当前代码真实案例、433 个 CAS 对象和 853,731 原始字节；归档中的 reference 是同一来源的存档别名，不能当作独立对照。逐对象摘要、物理模型请求/响应字节、独立 Job/Run、收据、候选锁和共享总数均已从归档原字节重新核验。汇总 `.agentgit/evidence/S10-reduction-summary-v5.json` 的 SHA-256 为 `304df27aeadbfabc1b234d4790cca1458a9b6b5c55185fe07cda18ab8c39f45e`，绑定四份 JUnit 的全部不同测试、CAS 包、源码/规范/锁文件及静态检查输出。

前三次探索失败分别暴露 SQLAlchemy 键编码、真实 Run 字段及 Job kind 假设，已修正并保留早期报告；首次公共边界测试因漏传 JSON 内容而被 HTTP 层按规范拒绝，修正测试请求后通过。这些失败不算通过样本，也不重复累加 smoke 复验。

## 尚未完成

这次真实案例只有 A+B，不能代替 S10-T01 的真实 A+B+C 删除冗余 C；三原子和成功删除后重验已有纯协议向量，仍须接入受控实际执行案例。独立确认/回归池的原子唯一开启、六加四实际试验、任务层划分、字段缩减、精确提案/审批、Repair Lab、完整 30 例/四基线及 Windows 新包继续实施。既有 0.1.5 包不包含本次后端，未合并 main。
