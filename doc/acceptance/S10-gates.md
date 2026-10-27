# S10 交付与退出证据矩阵

2026-10-27，依据实施方案 S10 的八项步骤、T01–T08 和 6.2–6.4。当前产品身份为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，Schema head `0023_repair_proposal_reviews`。本矩阵区分已经存在的工程证据与仍在执行的完整评估；S10/D2 尚未整体验收，不合并 main。

## 交付位置和范围

| 要求 | 实现与证据 |
|---|---|
| 原子删除、字段缩减及必要性复查 | `repair/reduction.py`、`repair/field_reduction.py`、study/field 控制器及 `0019/0022` 历史迁移；[耐久删除](S10-reduction.md)、[三原子](S10-three-atoms.md)、[字段](S10-fields.md)。每次变化用新试验复查，默认 reduce=6 共享，不能挪用六加四预留 |
| 最终锁、预注册与独立复测 | `services/repair_pools.py`、`repair_confirmation.py`、`repair_confirm_trials.py`、普通 Worker 和 `repair/confirmation_worker.py`；[池](S10-pools.md)、[实际确认](S10-confirmation.md)。先预注册再筛选，锁后仅开启一次，结果由真实 Job/CAS 形成 |
| 限定统计、拒绝及未知 | `repair/verification.py`、[确认/字段边界](S10-fields.md)、[随机拒绝与观测](S10-falsefix.md)。六次至少五次且无未完成、四回归全部通过，安全零违规；未知不重发、不释放完整预留，不冒充全局最小 |
| 精确提案、版本和人工决定 | `services/repair_reviews.py`、`0023` 迁移及既有 API/CLI；[建议与审批](S10-reviews.md)。票据绑定候选/报告/配置版本，批准只创建新配置；观测只能保存实验 |
| Repair Lab 与候选交接 | [真实联合](S10-lab-real.md)、[候选交接](S10-candidate-handoff.md)、[取消/未知](S10-lab-boundaries.md)、[观测](S10-lab-observation.md)。第五页保留失败、用量、Diff、限制和精确决定，断连重开不重复修改 |
| 包内可运行交付 | [Windows 0.1.6](S10-package-016.md)：35 单元和 17 包内 Electron 流程通过，三条真实修复原始链独立审计；安装/原生专项按用户指示保留待补 |
| 三十例、四基线及复现 | [完整 release](S10-evaluation-freeze.md)、[开发全量](S10-development-evaluation.md)、[执行/复核说明](../S10-evaluation-reproduction.md)。开发二十例×五方法结束；保留十例×五方法正在执行，完整原始结果及 D2 报告待补 |
| 原始安全及计量核对 | [开发全量补充计数](S10-evaluation-safety.md)：100 个方法预算/验收器绑定、19,899 实际调用和 2,470 产物记录核对；60 次非法路径完整拒绝，归档范围零成功越界写/正式采纳，八项分析反例通过 |
| 数据恢复 | [D1 当前版本补验与 D2 研究记录](S10-backup-restore.md)：两次停止自己的写者后备份 PostgreSQL/CAS、恢复到空测试库；核对引用/hash/HEAD/checkpoint，未决写保持待核对、零重发 |

## T01–T08

| ID | 具体行为证据 | 当前结论 |
|---|---|---|
| T01 | `test_real_three_atom_deletion_removes_c_and_retests_a_b_under_shared_frozen_budget`，五次真实删除、十二个配对 Job；[原始记录](S10-three-atoms.md) | 删除冗余 C 后重新测试 A/B；确定性原子语法下 1-minimal，未测试字段或全局最优。历史独立源码证据不与当前版本累计用例数 |
| T02 | `test_seeded_rare_screen_pass_then_six_confirmation_failures_stays_rejected`；[固定随机向量](S10-falsefix.md) | 筛选通过、确认 0/6 仍 rejected；预选故障向量只证明拒绝机制，不估计无偏算法成功率 |
| T03 | `test_insufficient_frozen_confirmation_quota_does_not_open_or_expand_budget`、`test_five_passes_plus_incomplete_confirmation_cannot_be_promoted_to_verified`、回归预算/未知控制用例；[确认](S10-confirmation.md)、[字段](S10-fields.md) | 配额不足不开放、不扩容；五通过加一未完成不能 verified。实际 n、未确认和完整预留保留 |
| T04 | `test_nonmonotonic_deletion_restarts_after_success_and_does_not_reuse_old_failure`、`test_partial_deletions_exhaust_budget_without_minimality_and_lock_exact_remaining_candidate`、字段变更后复查；[字段](S10-fields.md) | 非单调重新执行；部分删除/预算不足保持未证明，顺序冲突拒绝。当前 179 项相关纯契约已重验 |
| T05 | `test_real_observation_passes_six_plus_four_but_can_only_save_exact_experiment`；[固定观测](S10-falsefix.md)及[包内实际链](S10-package-016.md) | 六加四仍 test_only，正式配置批准在 UI/API/数据库被拒绝，只保存确切实验定义 |
| T06 | `test_changed_proposal_ticket_actor_or_config_rejects_until_new_review_then_reject_is_idempotent`、`test_two_reviewers_compete_for_one_config_version_and_one_decision`；[后端](S10-reviews.md)和[包内版本竞争](S10-package-016.md) | 修改提案/配置使旧审阅失效，原键幂等；最多一份新配置，批准无新模型/工具/Job/Run |
| T07 | 三十例 release、逐例五方法原始归档/独立 auditor、冻结预算；[开发结果](S10-development-evaluation.md) | 二十例完成；保留十例执行中。最终三十例及安全/原始证据全量核对尚未完成 |
| T08 | 运行前冻结模型/策略/家族、开发完整门禁；`repair_benchmark_analysis.py` 和逐任务 JSON | 保留集执行中；成功率/区间、改动量、成本/时延、回归和失败分析待实际全量结果，不预填 |

本次 `S10-gates-contracts-v1.xml` 的 179 项来自九个相关单元文件，覆盖有序候选/权限、预算预留、独立来源、非单调删除、字段、最终锁和确认/回归判定，全部通过。它们是当前协议层证据，不替代真实数据库、Worker、桌面和三十例评估。历史文档各有对应的源码身份、JUnit 及原始审计摘要，不能将不同源码的通过数相加后宣称当前全量回归。

## 退出仍需满足的条件

完整保留结果与四基线必须全部执行并经原始重审，任何失败/未知/未确认保留在任务分母；汇总成功率、确认率、回归失败率、改动规模、成本、时延和证据覆盖，列出未达项及可复现限制。开发主方法在预声明十二例为 12/12，达到十例目标；完整开发 12/20 与无排序相同、低于不缩减 14/20，不能宣称定位或全面基线优势。

S6/D1 独立 Windows、原生保存/DPI/环境专项和 S8 人工标签按用户授权暂缓，前置保持未完整验收；S8 注入点保留 Top-3 为 4/8，搜索工程使用完整候选降级。以上均不能用开发目标通过替代。完整 D2 报告与阶段前置核对完成后再进入必选 S11；S12-A/B/C/D 继续保持完整目标范围。
