# S7 本机功能退出核对

2026-10-24 将冻结验收器、实验调度、类型化干预、Hybrid 持久执行、精确统计、桌面视图及 Windows 0.1.4 候选合并核对。S7 自身的本机功能退出条件已有证据；S6/D1 的独立用户与原生前置项仍按用户授权暂缓，S7 整体验收、D1 与完整项目不据此宣告完成或合并 main。

当前 Python 源码为 `source-sha256:1132d409a7824df2413c62eb852bbf800ef31c7466964d3d16e6922368acd885`。最近后端定向回归为 416 单元 + 81 集成共 497 项，见 [源码桌面增量](S7-desktop.md)及其去重清单；当前包另有 26 单元及 8 条打包 Electron 流程，见 [0.1.4 候选](S7-package-014.md)。此前 764 项全覆盖及其他执行专项保留历史证据，不与本轮相加。

## 实施交付核对

| 实施方案步骤 | 实现及权威证据 |
|---|---|
| 1. 冻结 validator | core/experiments.py、experiments/validation.py、services/validators.py；冻结规则、输入/语料、时间、split、代码和依赖摘要；API/CLI 与实际包内冻结/查询通过，不接收自定义判分规则 |
| 2. 独立 experiment/trial | 0013 迁移、services/experiments.py、持久 Job/Worker；十个新 Run/分支/独立工作区，共同预算不得扩大来源授权，原子排队、幂等、来源不变均实测 |
| 3. 三种基础干预 | core/interventions.py、services/interventions.py、0014 迁移；Prompt/Plan/ToolResponse 的目标、前置条件、应用顺序、来源与效果策略冻结，非法模型/权限/验收器/split 更改拒绝 |
| 4. Hybrid 精确匹配 | core/hybrid.py、experiments/hybrid.py、services/hybrid.py、0015 迁移；仅显式只读调用，完整参数/签名匹配，stop/fixture 明确，耐久账本与恢复不重发，Recorded 复验零调用 |
| 5. 比较与统计 | experiments/statistics.py 与实际物理调用计量；成功/未完成、整体冻结硬断言、均值/样本方差、Mock 字节、供应商 Token/估价/未知费用分开；报告明确限制 |
| 6. 桌面 | Branch Experiment → 来源/验收器冻结 → A/B → manifest/统计/trial → 当前 Run 时间线/控制 → 重开只读；0.1.4 ZIP 中实际运行，固定 IPC 与状态/费用边界通过 |

## S7-T01–T06

| ID | 对应当前测试/观测 | 结论与范围 |
|---|---|---|
| T01 | test_ab_five_trials_have_independent_runs_inputs_files_and_actual_usage；test_real_api_restart_and_separate_cli_workers_complete_ab_five；包内 Live/Hybrid | 默认 A/B 各 5，10 个独立初始 Run，同 checkpoint、模型、fixture、validator、预算及环境来源可核对；差异在冻结组定义中列出 |
| T02 | test_unknown_or_escalating_fields_are_rejected；test_prompt_payload_cannot_hide_other_changes；test_stale_preconditions_rejected_without_publishing_intervention；跨项目/错误验收器/借用 confirm 观测测试 | validator/权限/split/模型等更改拒绝，不发布半个干预或部分 trial；manifest 及引用摘要保持冻结 |
| T03 | test_changed_arguments_stop_or_simulate_without_old_answer_and_replay 的 stop/fixture 两参数；test_unreached_dependency_cannot_pass_and_its_failure_is_replayed | 改变参数不套用旧响应，分别停止或走固定模拟器并记录 miss/fixture；未到达选定依赖不能 passed，历史复验不产生实际调用 |
| T04 | test_synthetic_observation_is_visible_test_only_and_replayed_without_effect；test_synthetic_fixture_repair_improves_outcome_but_does_not_rewrite_source；包内 B 组响应补丁 | synthetic/test_only 可见，来源不改写；只说明试验条件下结果，不证明实时世界已修复 |
| T05 | test_unfinished_trials_are_not_passed_and_paused_job_resumes；test_worker_crash_keeps_trial_identity_and_reports_current_recovery_run；test_control_and_budget_do_not_turn_unfinished_hybrid_into_passed；原子观测崩溃恢复及未知模型结果测试 | 预算/取消/暂停/中断与未知结果如实投影，未完成不进入完整成功分布；恢复使用当前 Run、固定请求和已保存结果；包内实际暂停/恢复/取消通过 |
| T06 | test_repeated_fixture_experiments_reproduce_logical_and_actual_call_metrics；test_provider_statistics_keep_measured_estimated_and_unknown_cost_distinct；结果落盘前后与可信核对计量测试；包内未知费用/预留 | 确定性调用向量可复核，实际时延不要求相同；Mock 字节不冒充 Token，未知/保守费用不冒充实测或账单，固定 fixture 不证明真实模型方差或泛化能力 |

后端测试名称均可在 `S7-desktop-summary.json` 的分组用例中定位；包内的十 trial、独立来源、控制与 synthetic 观测另有新的 0.1.4 原始报告。未以 UI HTTP fixture 替代 Worker、PostgreSQL、容器或恢复证据，也未用旧版窗口证明新包。

## 退出结论与残余项

冻结、隔离、匹配与本机任务/桌面测试通过。每份实验报告保留 request、共同输入/环境/验收器摘要、分组定义、执行模式、固定 seed、初始/当前 Run、实际与逻辑用量及 limitations；报告是各 trial 的有界投影，不能当作整个实验瞬时原子快照。硬断言只报告整体冻结验收器，短路后的未执行细分规则不单列为通过。

Windows 0.1.4 已包含新视图并通过包内容及包内流程；签名、独立用户安装/升级/卸载、真实保存、原生 DPI/禁外网/依赖缺失等边界见 S6 待补清单，不能凭本机源码/便携测试补签。付费供应商未运行，不推断真实账户访问或费用。

完整目标仍包括 S8–S11 与 S12-A/B/C/D。后续阶段须保留自己的前置和退出门槛，S7 的功能实现不能代替定位优势、搜索最小性、独立复测、合并正确性或适配兼容性证明。
