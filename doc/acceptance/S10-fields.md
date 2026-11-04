# S10 锁前字段缩减与实际必要性复查

2026-10-26，分支 `codex/s10-repair`。依据设计 5B.4-C.6、实施方案 S10 第 1–3 项及[字段方案](../S10-field-reduction-plan.md)。本记录覆盖耐久字段阶段，不表示 S10、D2 或完整项目退出；正式配置尚未采纳。独立 Windows 和 S8 人工审阅继续按用户指示待补。

## 实现

新 study 默认冻结 `predefined-fields/v1`，在原子删除结束、唯一最终锁之前进入字段阶段。显式空策略和历史 study 保持旧协议，不改写原子状态、旧迁移或已封存决定。`reduction` 继续表示字段之前的原子范围；`field_reduction.summary` 分别描述最终候选的原子必要性和预定义字段变换范围。

Prompt 仅删除原提示后追加的完整子句；Plan 仅删除新增叶节点或新增且未完成的依赖边。原提示、源节点、已完成节点、工具授权、模型、验收器和输入均保持冻结；非法或不适用变换保留原因。ToolResponsePatch 不参加字段改写。接受任何字段变化后，以新实际 Job 重新删除当前原子；删除成功也会重新生成后继。旧候选的失败不会成为新候选的必要性证明。

继续使用同一 study 租约和原 S9 预算，原子删除、字段试验与复查共同消耗 reduce 次数，六加四预留始终受保护。`0022_repair_field_reduction` 增加七张不可变表及受约束的当前状态指针；意图、独立 Replay/Job、调用来源、收据、计量和状态推进具有事务边界。实际来源只接受本 Job 自身恢复链，新调用数与物理计量一致；结果保存后的普通 Worker 恢复不把复制响应算作新调用。

派生补丁保留根、父、完整效果、S7 发布和具体 study 的归属。不同搜索可以合法共享同一来源内容摘要，因此解析和确认投影显式传入 study ID；缺少 ID、兄弟 study 或项目不匹配不能借用变体。只读查询沿用传入连接，S7 内容读取无需在该连接内再领取连接。

实际单连接池测试在接受字段缩短后禁用 Model/Tool/CAS 写入，完整 study 查询、派生内容解析和配置投影均通过；错误项目被拒绝。数据库还实际拒绝派生补丁历史改写，以及字段初始化后向原子阶段追加试验。

## 实际闭环

所有执行均在独立临时 PostgreSQL Schema/CAS 中使用冻结 Mock，不调用付费供应商。源码为 `source-sha256:0535a21170cd4c54e856760d27866764ad980e45232fa24a4cf9fe8816f9e037`；设计、实施方案和 `uv.lock` 摘要未变化。

| 用例 | 原子删除 | 字段/原子复查 | 独立确认与固定回归 | 结论 |
|---|---:|---|---|---|
| Live Prompt | 1 | 缩短子句通过，删除原子失败，再删必要子句失败，共 3 项实际 Job | 6+4，全部使用缩短后的 Prompt | 规定变换下完成，`verified_under_fixture` |
| Hybrid Prompt | 1 | 同样 3 项实际 Job，各自拥有冻结 Hybrid policy 和新模型调用 | 六项 Hybrid、四项 Live，使用确切缩短内容 | 历史观测与物理调用分开计量 |
| Prompt + Plan | 2 | 6 项字段试验、4 项原子复查；必要叶节点删除失败，冗余依赖边删除通过 | 6+4，全部投影确切 Prompt 和 DAG | 两次字段变化接受，变化后均复查原子 |

联合 Plan 用例在首次筛选前明确冻结 reduce=16、总次数=40；实际使用 12 次 reduce。它用于完整覆盖 Plan 变换，不扩大产品默认 reduce=6、组合深度二。默认六次额度用例另行实测：额度覆盖原子、字段和复查总和，剩余字段明确 `not_proven`；额度为 0、2 的收紧，以及原子阶段总预算拒绝也不增加字段配额。预算停止仍可保留最后一个确实通过的候选，不能据此宣称字段最小。

派发提交前/后与结算提交前/后中断均验证原 Job/结果复用、不重复实际费用；取消区分未派发的可证空意图与已派发的实际终态。真实未知模型响应在控制器重启、取消后仍占完整预留，不重发、不锁定候选。未授权执行器未被调用，结果为安全不完整；实际预算耗尽的字段候选不被接受。已保存模型响应恢复为新 Run，来源止于本 Job 初始 Run，复制记录不增加新调用数。

## 证据与核对

原始归档位于忽略目录 `.agentgit/evidence/`：`S10-fields-live-v2.json`、`S10-fields-hybrid-v2.json`、`S10-fields-plan-v2.json`，包含 CAS 原始字节和阶段索引。独立脚本 `verify_s10_fields_v2.py` 重算对象域摘要、完整字段前缀/后继、S7 发布内容、每次新模型请求/响应字节、物理工具调用、共享用量、最终锁及六加四统计。执行和测试报告摘要在该脚本生成的 `S10-fields-summary-v2.json` 中归档。

最终同源码 **770 项去重通过：686 单元 + 84 真实集成**，没有失败、错误或跳过；旧版本历史结果不累加。JUnit 分组为 `S10-fields-core-v2.xml`（686）、`jobs-v2.xml`（13）、`recovery-v4.xml`（4）、`legacy-v2.xml`（40）、`plan-v2.xml`（1）、`ownership-v2.xml`（1）和 `confirmation-regressions-v2.xml`（25），后六个名称均带 `S10-fields-` 前缀。Ruff、格式、mypy 157 个源码文件、Git 工作区/暂存区空白检查通过；完整收集 1242 项不等于运行了全部 1242 项。

三份闭环归档共 4637 条对象记录、11,607,732 原始字节、4393 个去重对象。独立校验同时确认全部暂存 Python/JSON 源码与实测字节相同。摘要 SHA-256 为 `43acf35b1e35aa1f554dd35c9126829e387c83343bdc4d7db01d34980985e9b7`；三份原始归档的 SHA-256 分别为：

- Live：`3ec0a300e3cf9494431f6f2249124fb271cde562dc1de1116f5388cd5253c536`。
- Hybrid：`b559d85a7d3dfe6afbe7d7fd07e1f38af8d38960a3b7670b84a94f694f8fc781`。
- Plan：`09bc535174a57237f79a3405370ce0afc050e40019d8bde665f9e4524cb72f79`。

复现单元、字段流程、完整旧缩减/池/迁移/干预回归：

```powershell
uv run --locked pytest tests/unit
$fieldTests = @(Get-ChildItem -LiteralPath tests/integration -Filter 'test_repair_field_*.py' | ForEach-Object { $_.FullName })
uv run --locked pytest @fieldTests
uv run --locked pytest tests/integration/test_repair_studies.py tests/integration/test_repair_study_boundaries.py tests/integration/test_repair_pools.py tests/integration/test_kernel_migrations.py tests/integration/test_intervention_service.py
uv run --locked pytest tests/integration/test_repair_confirmation.py::test_ten_new_jobs_confirm_and_regress_exact_locked_candidate_and_freeze_summary tests/integration/test_repair_confirmation_hybrid.py tests/integration/test_repair_confirmation_controls.py tests/integration/test_repair_confirmation_variants.py tests/integration/test_repair_frozen_corpus_boundary.py tests/integration/test_repair_confirmation_unknown.py tests/integration/test_live_replay.py
```

初次试跑 `S10-fields-confirmation-first.xml` 保留为探索，不与最终同源码结果累加。`S10-fields-recovery-v2.xml` 的预算向量先耗尽重规划而未触达调用预算，`v3` 的恶意节点又先被计划约束拒绝；已按预先冻结的新向量改正，保留两次失败报告。最终向量收紧原单次调用额度，并在保留已完成节点的有效重规划中请求未授权工具，没有放宽断言或产品权限。

## 范围

这些结果只支持所声明合成条件和有限变换的原子/字段必要性，不证明全局最小、真实供应商稳定性或线上有效性。随机偶然筛选通过、观测伪修复、精确提案与批准、Repair Lab、30 例/四基线和 D2 仍待完成。随后继续必选 S11 和 S12-A/B/C/D；本增量没有迁移业务库、生成新桌面包或合并 main。
