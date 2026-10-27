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
| 三十例、四基线及复现 | [完整 release](S10-evaluation-freeze.md)、[开发全量](S10-development-evaluation.md)、[执行/复核说明](../S10-evaluation-reproduction.md)及[完整研究报告](../D2-research-report.md)。开发二十例及保留十例均完整执行五方法，150 个方法结果经原始核对；正式阶段前置仍待补 |
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
| T07 | 三十例 release、逐例五方法原始归档/独立 auditor、冻结预算；[完整报告](../D2-research-report.md) | 30×5 完整执行及原始审计通过；150 个方法预算/验收器绑定、29,836 实际调用核对，归档范围非授权 provider/成功非法路径写/effect 绕过/正式采纳均 0。迁移遗漏及 Git 基线补充核验明确披露 |
| T08 | 运行前冻结模型/策略/家族、开发完整门禁；`repair_benchmark_analysis.py` 和逐任务 JSON | 十例保留全部执行；主方法 6/10，95% Wilson 31.27%–83.18%，无排序 6/10、不缩减 8/10。改动、成本/时延、回归、配对和失败分析齐备，未确认保留分母，不调参或改标签 |

本次 `S10-gates-contracts-v1.xml` 的 179 项来自九个相关单元文件，覆盖有序候选/权限、预算预留、独立来源、非单调删除、字段、最终锁和确认/回归判定，全部通过。它们是当前协议层证据，不替代真实数据库、Worker、桌面和三十例评估。历史文档各有对应的源码身份、JUnit 及原始审计摘要，不能将不同源码的通过数相加后宣称当前全量回归。

在同一当前源码上，`S10-gates-critical-v1.xml` 的十三项真实 PostgreSQL/普通 Worker 集成全部通过：重新执行 T01 三原子、T02 随机确认拒绝、T03 配额不足/五通过加一未完成/回归预算、T05 观测禁正式采纳和 T06 全部审阅版本/并发/幂等/事务边界。当前三原子、随机、观测原始归档分别保存，独立重审摘要 `S10-gates-critical-archives-v1.json` SHA256 为 `0d2141dc9b712bfd097a0474419ea63fda42a1089e9957cb8e9a50f33f3acf00`；旧源码结果仅作历史，不累计当前通过数。

最终汇总 `S10-final-summary-v1.json` 的 SHA256 为 `8f98f7694431b1f075b7b467dfa6dae1b627c7867abec58102e7ea14269d1ba2`，绑定三十份清单/容器/审计、当前复验、官方分析 v2、安全计数、迁移来源核验、备份及未变的 0.1.6 四件套。150 个方法的筛选验收器均与各自源失败评估相同，没有重绑定；最终记录明确为研究和本机工程核对完成、完整 D2 前置待补。

## 研究结果与整体前置

完整三十例与四基线已执行并经原始重审，失败/未知/未确认均留在任务分母；成功率、确认率、回归失败率、改动规模、成本、时延、证据覆盖和可复现限制见完整报告。开发主方法在预声明十二例为 12/12，达到十例目标；完整开发 12/20 与无排序相同、低于不缩减 14/20，保留同样未胜过不缩减。最终最小性以分析 v2 的最终字段候选为准，不沿用字段变化前的旧结论。

S6/D1 独立 Windows、原生保存/DPI/环境专项和 S8 人工标签按用户授权暂缓，前置保持未完整验收；S8 注入点保留 Top-3 为 4/8，搜索工程使用完整候选降级。以上均不能用开发目标通过替代。完整 D2 报告与阶段前置核对完成后再进入必选 S11；S12-A/B/C/D 继续保持完整目标范围。
