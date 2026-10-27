# S10 合法起点、独立回归及只读故障环境

2026-10-27，分支 `codex/s10-repair`。57 项契约及 7 项真实 PostgreSQL/Worker 集成分批通过，共 64 个不同节点；最终归档 9 个独立 Job/Run，6 正常通过、3 故障失败。原始响应、硬验收、实际计量和公开起点已独立复核。此处没有保留案例或五方法修复成绩。

## 起点与四项回归

`repair_benchmark_origins.py` 只接收仓库、来源 Run 与冻结验收器，不接收标签、注入位置、方法或正确补丁。根规则为 first-act-else-first-plan；局部规则在首个公开错误/失败硬验收之前，选择最近的合法模型请求。排除待执行工具、未知/不可继续状态及没有剩余节点的 FinalDecision 检查点，再由原 InterventionService 核验所属录制、检查点、配置、状态及冻结输入。

选择结果单列继承历史用量。正常 Run 不伪造局部异常；早期失败的根/局部可能相同，晚期失败从自己的真实局部点生成两项候选并通过原方法选择器保留，没有只过滤根空间而把局部候选排空。首个 act 前即失败时回退到首个合法 plan，均有真实 Worker 证据。

`tests/fixtures/repair/regressions-v1.json` 固定四套六 Source/九事实的独立输入，与三十例全部来源 ID 不同。仍是原三框架/三维领域，四种框架顺序和精确分数分别覆盖普通日期、生效时刻包含、失效前一秒及采集时刻相等。静态原硬验收通过，另四个生产 Mock Worker Job 均生成两份产物并通过执行前冻结的验收器。本轮正常通过不能替代后续补丁的四次实际回归；完整驱动还须在筛选前同事务登记四项来源，确认后不能更换。

## 只读环境与空搜索

`repair_benchmark_environment.py` 在原 ToolRegistry 上应用固定搜索响应，保留权限/参数检查、工具接口签名、工具计数、规范语料、工作区及硬验收。环境为规范排序、分组排序，以及全量查询为空而框架子查询正常；不读取方法、阶段、序号或成败。ObservationWorker 仅在原 Worker attach 后装配适配器，继续继承租约、恢复、Live/Hybrid、派发及结算。正式运行 release 仍须绑定此评估封装和每例环境身份，不把它声称为生产工具服务的变化。

原型发现空列表会先被 Runtime 以 tool_empty 拒绝，不能虚构 search_results 后直接读取。最终机制在真实重规划后，依据 Goal 既有公开目录退到单框架子查询，把首项误用于六个读取槽；实际读取仍满足原来源身份检查，最终因 source_coverage/claim_coverage 不足失败。同一模型在规范观测下通过。两者在独立回归输入上验证，没有实例化 R21–R30。

其他保留机制只在独立回归输入的构造契约中核对引用归属、外部提纲、精确数值和未授权工具决策，没有保留成绩。完整观测补丁搜索/确认/回归尚待驱动执行；后续即使通过，也不能解除 test_only 或采纳禁令。

## 原始证据及剩余工作

最终九个 Job/Run 共 93 次模型、73 次工具调用，873,894 Mock UTF-8 字节，归档原字节 3,202,901，不跨仓去重。这些都是准备/控制成本。重规划第二次 search 的 attempt=2、origin_call_id=null，作为真实新调用保留。独立审计重算 CAS 类型域摘要/归属、Job 终态、冻结 Goal/语料、硬验收、每条模型响应和用量；同时从公开事件重新推导根/局部选择并校验确切前置快照。

忽略目录 `.agentgit/evidence/` 保存：`S10-origin-contract-v3.xml`（57）、`S10-origin-jobs-v2.xml`（5）、`S10-origin-late-v1.xml`（1）、`S10-origin-initial-v1.xml`（1）、`S10-origin-proofs-v2/`、`S10-origin-code-v2/`、`S10-origin-audit-v2.json` 及 `S10-origin-summary-v1.json`。旧原型报告/控制及失败的空搜索假设另存，不累计；旧开发构造继续使用各自冻结源码解释。

产品身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head `0023_repair_proposal_reviews`；产品、依赖和 0.1.6 包字节未变。下一步接入新数据驱动并冻结完整运行 release，再实际完成开发/保留与四基线、失败/无解/伪修复及 D2。十二例可修复声明仍待验证；S11/S12 和用户暂缓项保持。
