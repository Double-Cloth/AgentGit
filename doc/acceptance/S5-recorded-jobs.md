# S5 耐久 Recorded Job 实施记录

状态：本机 Recorded 子批已验证；S5-T01–T09 总体验收及最终完整回归现已汇总于 [S5.md](S5.md)。后续 Live 与复合来源结果见 [S5-live.md](S5-live.md)、[S5-compound.md](S5-compound.md)；完整 S0–S12 与 Electron 交付范围不变。

## 实现与边界

- `POST /replays`、`GET /replays/{job_id}` 与 `agentgit replay` 复用管理服务、身份/项目边界及幂等事务。Recorded 明确来源 Run、起止 Commit、新分支，不接受干预字段。
- 提交验证来源完整性和环境后，原子保存独立 Task/Job/Run/分支/不可变请求及幂等响应，不创建工作区、执行 Runtime 或访问模型/工具。
- `RecordedJobRunner` 作为现有 Worker 的独立分发路径，不构造真实模型、执行服务或工作区。保留原逻辑状态，逐检查点重算；Commit、快照、证明与 HEAD 同事务提交。
- `0011_replay_jobs` 的证明约束来源顺序、起止范围、来源与目标状态摘要一致、目标 Run/Job 和父边归属；来源请求及证明不可修改或删除。
- 实际暂停/恢复、取消、到期不重写原逻辑状态或预算。普通暂停沿用 Run；崩溃接管创建新 Recorded Run，重新验证已保存前缀后继续；持久截止时间不重置。
- `replay` 报告分别呈现历史逻辑状态/用量、选定范围验证结果、实际零模型/工具调用与耗时。未完成计量的尝试明确计数，不把崩溃未知耗时当成零。完成一个非终态范围不等于原任务完成或修复成功。

等价分支测试发现版本内核会为已共享的 Commit 重插父边，被 S2 的已发布历史保护拒绝。现在仅对新 Commit 建立父边，对已有 Commit 严格比较原内容与父关系；没有放松数据库不可变/反补写/反成环规则。另修复预算读取随机选中共享 Commit 的其他 Run 快照的问题，按当前 Job 来源链限定查询。

## 验证进度

迁移往返/回退保护 2 项通过；首个耐久闭环通过，包含完整 21 个状态点的证明、原分支不变、幂等重试和零真实 Adapter/工作区构造。事务中崩溃、检查点提交后崩溃、终态提交后崩溃、暂停/恢复、取消和期限测试的首批结果通过。

等价分支的内核及耐久任务聚焦回归 2 项通过。Recorded 公共契约的干预字段与分支名校验已加入单元测试，首轮全单元 189 项通过。双分支最初暴露的父边重插失败已修复；最终回归包括该失败路径，没有绕过历史不可变保护。

2026-10-21 最终验证，Windows/Python 3.12.14/PostgreSQL 17.6/Docker Desktop Linux，引擎 29.8.2：

| 范围 | 结果 | 本机记录（不提交） |
|---|---|---|
| 单元及全部 PostgreSQL 集成 | **393 通过**，606.45 秒 | `.agentgit/evidence/S5-recorded-jobs-core.xml` |
| 全部 Sandbox/执行 | **106 通过**，795.36 秒，含真实五工具耐久 Recorded 及缺输出失败 | `.agentgit/evidence/S5-recorded-jobs-execution.xml` |
| 全仓收集 | **499 项**，上述两个不重叠组完整覆盖 | pytest collection |
| 实际 CLI | 来源五工具完成；Recorded 完成，28 个后续状态点验证一致；历史逻辑模型/工具调用为 15/13，实际为 0/0，源历史不变 | `.agentgit/evidence/S5-recorded-cli-smoke.json` |
| 静态与构建 | Ruff、141 个文件格式、mypy 的 74 个源码文件及 sdist/wheel 全通过 | `.agentgit/packages/` |

耐久专项覆盖零长度/非终态终点、幂等重试与异参冲突、跨项目拒绝、提交响应故障全回滚、证明事务回滚、提交后接管、终态后接管、实际暂停/同 Run 恢复、取消/期限、共享 Commit 的预算隔离、缺录制不回退、请求及证明不可变，以及 HTTP/CLI 入口。崩溃尝试的未知耗时保留为 `unmeasured_attempts`，没有将其伪装成零。

本机 Docker Desktop 曾停止，导致一次测试在连接数据库时失败；恢复引擎和已有数据库后重跑。开发库已前进至 `0011_replay_jobs`，原有 Job 数据保留。没有用 skip、SQLite 或 Mock 容器替代真实设施，也没有向远程推送或宣称远程 CI 已通过。
