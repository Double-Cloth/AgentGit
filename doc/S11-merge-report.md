# S11 三十组双分支合并评估

日期：2026-10-29。执行提交 `2ffec49`，产品源码 `source-sha256:f892cae025dc8ecea1bdba31e86399999758c30dcc8abc20ac4b5e30b44b3a66`。依据设计 5B.3、实施方案 S11 和[冻结方案](S11-benchmark-plan.md)，协议摘要为 `a325636d6ee2485ccb11482a5dc384909847ffecf335d0d4e498c31c60ce29ce`。

## 结果及适用范围

固定 30 例（三类各 10）已完整执行三种方法，共 90 份候选记录。完整语义方法符合预声明预期 30/30：无冲突十例均保留完整内容及证明，经精确票据批准产生双父 Commit 后，十个零基线新任务全部通过；阻断二十例均未发布合并。预期由设计规则推导，独立人工复核仍待完成，S11 整体退出还需 Merge Review、新包及完整前置。

| 固定指标 | semantic | artifact_only | choose_ours |
|---|---:|---:|---:|
| 无冲突例完整内容整合 | 10/10 | 0/10 | 1/10 |
| 无冲突例双文件原字节正确 | 10/10 | 10/10 | 4/10 |
| 阻断例明确报告冲突 | 19/20 | 4/20 | 0/20 |
| 阻断例在 Schema 处拒绝 | 1/20 | 1/20 | 0/20 |
| 阻断例安全拒绝合计 | 20/20 | 5/20 | 0/20 |
| 阻断例错误接纳候选 | 0/20 | 15/20 | 20/20 |
| 全部预期符合 | 30/30 | 5/30 | 1/30 |
| 未完成/异常分析结果 | 0/30 | 0/30 | 0/30 |

完整内容包含目标、按稳定 ID 合并的计划、完整 Evidence、工具交集/预算最小值、两份产物及两侧当前读取证明集合。`artifact_only` 是既有产物层的消融方法，没有状态整合能力；`choose_ours` 直接保留一侧的状态/配置/文件，完整内容仅 M09 通过。其 M04/M05 文件正确但证明集合不足，M10 文件正确但丢失另一侧计划节点；这些情况保留为内容整合失败。

这些错误接纳是隔离候选的质量指标，基线均未获得发布权。计划/约束不合格候选分别为 0/10、9/10、10/10；未知效果不合格候选分别为 0/1、1/1、1/1。实际发布的合并均经过完整服务批准与硬验证，实际外部付款请求为 0。基线的批准后任务完成率未测，不能使用主方法的十个新任务代填。自动化测试产生 10 张审阅票据；独立人工审阅次数为 null。

## 逐例结果

下表“候选”表示基线接受了未经完整语义验证的内容；阻断例出现候选计为错误接纳。方案文件保留每例完整变化和预期，原始包保存具体冲突种类、路径及全部候选字节。

| ID | 主方法 | 仅产物 | 选 ours |
|---|---|---|---|
| M01 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M02 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M03 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M04 | 整合/新任务通过 | 候选，缺状态 | 证明不完整 |
| M05 | 整合/新任务通过 | 候选，缺状态 | 证明不完整 |
| M06 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M07 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M08 | 整合/新任务通过 | 候选，缺状态 | 内容不完整 |
| M09 | 整合/新任务通过 | 候选，缺状态 | 内容符合预期 |
| M10 | 整合/新任务通过 | 候选，缺状态 | 计划不完整 |
| M11 | 事实/写冲突 | 冲突 | 错误候选 |
| M12 | 事实/写冲突 | 冲突 | 错误候选 |
| M13 | 时间冲突 | 冲突 | 错误候选 |
| M14 | 来源冲突 | 错误候选 | 错误候选 |
| M15 | 来源冲突 | 错误候选 | 错误候选 |
| M16 | 无证据结论冲突 | 错误候选 | 错误候选 |
| M17 | 缺来源冲突 | 错误候选 | 错误候选 |
| M18 | 结论/证据冲突 | 错误候选 | 错误候选 |
| M19 | 来源摘要冲突 | 错误候选 | 错误候选 |
| M20 | 未知 unit 字段拒绝 | Schema 拒绝 | 错误候选 |
| M21 | 计划环冲突 | 错误候选 | 错误候选 |
| M22 | 悬空依赖冲突 | 产物冲突 | 错误候选 |
| M23 | 工具预算冲突 | 错误候选 | 错误候选 |
| M24 | 模型预算冲突 | 错误候选 | 错误候选 |
| M25 | 步骤预算冲突 | 错误候选 | 错误候选 |
| M26 | 读取权限约束冲突 | 错误候选 | 错误候选 |
| M27 | 同节点异改冲突 | 错误候选 | 错误候选 |
| M28 | 写权限约束冲突 | 错误候选 | 错误候选 |
| M29 | 完成语义冲突 | 错误候选 | 错误候选 |
| M30 | 未知效果冲突 | 错误候选 | 错误候选 |

M11 的单位是互不归一化的文本值，M20 的 unit 是未支持字段；本实现不提供数值/单位自动转换。M14/M15 是报告中声称的过期/未来来源元数据，与实际冻结读取不一致，按来源证据阻断。M26 在准备 v1/v3 和本次正式数据中一直为 `CONSTRAINT_VIOLATION`；旧准备文档误写成输入拒绝，已按原始包更正，数据和预期未改。

## 调用、成本与审阅

| 阶段 | Mock 模型 | 本地工具 | Mock UTF-8 字节 |
|---|---:|---:|---:|
| 三十组输入准备 | 478 | 358 | 4,808,743 |
| 三种候选方法 | 0 | 0 | 0 |
| 十个批准后新任务 | 129 | 119 | 1,668,729 |
| 本次正式执行合计 | 607 | 477 | 6,477,472 |

另有 1 条精确审批后产生未知结果的本地付款模拟调用，没有外部客户端；主方法未重发。Mock 费用为 0，字节数不当作供应商 Token。原型和历史准备运行未混入上述成本。每例方法耗时保存在 `elapsed_ns`；顺序固定，受本机负载和缓存影响，不据此宣称规模化性能优势。

本数据集没有开发/保留划分，使用固定合成研究语料及确定性 Mock，三十个场景的覆盖范围有限。基线用于比较产物层和单侧选择的不足，不代表任意完整状态求解器或真实 LLM。独立人工预期、真实资料与模型泛化能力均未由本评估证明。

## 原始审计与复现

30 例正式节点分成两个隔离批次各 15 项，全通过；15 项评分/冻结协议测试通过，五个原型另存且不重复累计。457 文件在所有 Runtime 终态时核对未变。原始 CAS 重核覆盖输入、候选摘要、读取证明/家族/祖先、实际批准/双父、初始化及零预算、新调用、拒绝不改 HEAD。四类候选/批准篡改及五类统计反例均拒绝，统计增补绑定后输出字节不变。首版独立分析仅检查新增读取，在 M05 遗漏另一侧仍依赖的旧证明；最终按冻结的两侧当前证明并集核对，运行评分未变。

原始包在 `.agentgit/evidence/S11-merge-evaluation-v1/cases/M*/{source,methods,final}/evidence.json`，完整索引见[验收记录](acceptance/S11-merge-evaluation.md)。当前文档包含执行后的更正和结论；正式协议仍绑定提交 `2ffec49` 的确切文档/代码字节。完整运行使用该提交的隔离完整工作树和锁定依赖，将本报告版本的 `merge_benchmark_capture.py`、`merge_benchmark_evaluation_verify.py`、`merge_benchmark_report.py` 复制到其 `tests/integration/`。这三个复现/分析工具不修改冻结的 Runtime 驱动。准备全新绝对目录后，按同一奇偶批次产生统计所需的两个 XML：

```powershell
$mergeEvidenceRoot = '<新的绝对证据目录>'
uv run --locked python tests/integration/merge_benchmark_capture.py --destination $mergeEvidenceRoot
$env:AGENTGIT_MERGE_BENCHMARK_STAGE = 'formal'
$env:AGENTGIT_MERGE_PROPOSAL_EVIDENCE = Join-Path $mergeEvidenceRoot 'cases'
$mergeOddCases = ((1..30 | Where-Object { $_ % 2 -eq 1 } | ForEach-Object { 'M{0:D2}' -f $_ }) -join ' or ')
$mergeEvenCases = ((1..30 | Where-Object { $_ % 2 -eq 0 } | ForEach-Object { 'M{0:D2}' -f $_ }) -join ' or ')
uv run --locked pytest tests/integration/test_merge_benchmark_evaluation.py -k $mergeOddCases -q --junitxml="$mergeEvidenceRoot/results-odd.xml"
uv run --locked pytest tests/integration/test_merge_benchmark_evaluation.py -k $mergeEvenCases -q --junitxml="$mergeEvidenceRoot/results-even.xml"
uv run --locked python tests/integration/merge_benchmark_evaluation_verify.py $mergeEvidenceRoot --destination "$mergeEvidenceRoot/audit-recheck.json"
uv run --locked python tests/integration/merge_benchmark_report.py $mergeEvidenceRoot --audit "$mergeEvidenceRoot/audit-recheck.json" --destination "$mergeEvidenceRoot/statistics-recheck.json"
```

仅分析现有包时，将原始忽略目录带入隔离工作树或使用对应绝对路径，并使用新的分析输出文件名。捕获工具已实际在 `2ffec49` 导出的完整代码中验证，457 文件快照内容与原包一致；当前更正文档后的工作树按门禁拒绝捕获，拒绝时不会创建目标目录。重跑得到新的 Run/项目标识及计时，按固定内容/调用规则复核，保持原包不变。完成 Merge Review、新包及人工/环境前置后，再核对 S11 整体退出和后续 S12。
