# S7 受控实验实施记录

2026-10-23 用户授权延后当前 S6 环境/原生专项，先推进 S7；范围见[实施方案第 1.2 节](AgentGit_实施方案_v1.0.0.md)。S6/D1 仍未完整验收，剩余证据最终交付前补齐。

## 既有实现与边界

复用 `ResearchRuntime`、`ResearchService`、持久 Job/Worker、`prepare_live`、精确 occurrence、项目限定的 CAS 和历史服务。运行/审批/预算/容器边界保持不变，不把实验执行放进桌面进程，不复用旧 Run 的授权或写入工作区。Recorded 继续禁止 overrides、网络和执行容器；实验 Hybrid 单独接线，不能改变 Recorded 语义。

报告检查继续使用已实现的 `research-validator/v1`：框架/来源覆盖、有效期、证据及引用、产物结构和 Markdown 一致性。新实验在开始前冻结输入、语料、时间、规则、Schema、代码和依赖版本；任务自身的 `evaluation.passed` 仅作为运行记录，实验需要独立检查历史快照中的实际产物。

## 实施顺序

1. **冻结验收器（已实现）：**`ValidatorManifest` 与输入、语料分别保存为不可变 CAS；项目内按明确归属读取。评估绑定具体 Run/Commit/state_ref；代码、依赖或 Schema 不兼容时阻断。未完成、取消、预算耗尽、审批等待、结果未知和中断都不能计入通过。
2. **实验数据与调度：**新增阶段迁移保存 experiment、分组和 trial；每个 trial 都通过现有新 Run/Job 流程建立独立分支、输入、预算和工作区。冻结共同起点、配置、环境、种子、重复次数及有序干预，支持取消/恢复并保留未完成状态。
3. **最小干预 DSL：**只允许固定消息的 Prompt、尚未完成且通过 DAG/权限检查的计划，以及测试环境下精确匹配的只读工具观测替换。前置条件绑定原状态、配置、验收器、目标事件和 occurrence；未知字段、改判分规则、改 split、扩大权限一律拒绝。
4. **Hybrid：**显式选定的录制调用在签名、请求、逻辑步骤、发生次序及效果完全相符时才能使用；miss 严格遵守冻结的停止/受控模拟器策略。发生变化的模型调用重新由 Mock 生成，不套用旧答案。响应替换必须保留 synthetic observation 和 test_only 来源。
5. **A/B 与报告：**同 checkpoint 各 5 个独立 trial；记录控制变量、每次实际状态和 trace，统计任务/硬约束通过数、Token、时延、实际/估计费用及方差。录制逻辑计量与本次实际计量分开，未知费用不视为零。
6. **桌面与验收：**扩展既有 Branch Experiment 页面、固定 API/IPC 业务契约，展示 manifest、分组、trial、取消和恢复。真实后端/桌面验证后逐项核对 S7-T01–T06，再判断阶段退出。

## 当前冻结验收器范围

`fixture_split` 是创建时固定的 screen/confirm/regression 标签，标签参与 manifest digest；当前增量没有引入 S8/S9 的故障数据集，也不宣称不同标签就具备独立保留样本。语料实际内容另由 `corpus_digest` 绑定，后续数据集仍须显式检查分组和样本隔离。

冻结验收器已接入 Python 用例服务、实验调度及 API/CLI，桌面待接入。它只验收项目中明确归属某次 Run 的历史检查点，不开放凭 hash 读取任意 CAS 的接口。冻结结果记录是 test_only，不能推断真实外部世界已经修复。

## 实验数据与调度增量

`0013_branch_experiments` 新建不可变 `experiments/experiment_trials` 索引，分组条件保存在 manifest。每个 trial 原子关联一个独立 Live Job、初始 Run 和分支；SQL 约束检查来源、分组、序号及新排队 Job 的对应关系。Job 中断后继续由原 Worker 恢复流程建立接管 Run，trial 继续关联同一 Job，同时展示初始和当前 Run；不会把中断重新包装为新 trial。

当前分组只开放固定 Prompt 和完整计划，权限、模型、验收规则、split 和预算不能通过分组改写。剩余计划继续经过既有安全 checkpoint、DAG、已完成节点保护及工具授权检查；这部分是条件比较基础，仍需第 3 步的目标事件、payload_ref、前置条件与 provenance DSL。

默认 A/B 各 5 次，可显式设置各 1–10 次，所有 trial 采用同一明确预算。当前本地 seed 固定为 0，如实披露 Mock 的确定性与供应商的不确定性，不伪造不同种子已传给供应商。报告直接投影 Job/Run，读取期间状态变化时有界重读；只有真实 completed 且独立冻结验收通过才计入通过。

API/CLI 创建只排队，不在请求线程执行模型或容器；同一幂等动作覆盖整个实验，任意 trial 排队失败回滚全部可见数据。示例及边界见 [API.md](API.md) 的 S7 小节。当前没有扩大桌面 IPC 白名单，也没有替换 S6 桌面包。
