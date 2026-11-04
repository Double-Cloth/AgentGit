# S11 三十组真实输入准备与补读修复

日期：2026-10-28。分支 `codex/s11-merge`，基线 `c07450d`。依据设计 5B.3、实施方案 S11 和[三十组协议](../S11-benchmark-plan.md)。本增量建立各 10 组无冲突、事实、计划/约束输入及正常控制，并修正准备中暴露的读取记录选择问题。正式候选/基线评估、人工预期复核、Merge Review 和新包继续。

## 实现

样例在 `tests/fixtures/merge/v1/cases.json`，固定 M01–M30、三类各 10 例、阻断种类/允许的输入拒绝及三个方法名称。预期按现有设计规则推导，资产明确记录独立人工复核待办。构造器通过真实研究 Run、显式 Live 配置/计划/产物变化产生每例两个分支；来源错误保留实际失败状态。计划环/悬空依赖/完成语义变化从真实未完成计划检查点分叉，其他例从完整来源分叉，遵守已完成节点不可改写规则。

首个正常控制 M01 暴露：ours/theirs 各补读一个不同来源，来源正文相同、实际读取 occurrence 改变，状态原先固定优先 ours，导致 theirs 的新读取与三方来源报告不一致。修正为先按完整 Evidence 做三方选择，保留单侧更新的整条记录；来源双侧冲突、显式选择和真实读取证明检查继续生效。增加 ours/theirs 两个对称回归，不拼接读取字段。当前产品源码为 `source-sha256:f892cae025dc8ecea1bdba31e86399999758c30dcc8abc20ac4b5e30b44b3a66`；没有新增依赖或迁移，仍为 `0026_merge_runs`、73 张产品表。

M30 经测试操作者对确切 fixture 动作执行现有精确审批，再由没有外部客户端的本地模拟器抛出未知结果。实际调用保持 `outcome_unknown`、无结果 ref，审批为 approve/consumed，预览同时报告 pending/effect 冲突，不重发。原型先创建待审批动作再取消，取消正确关闭待办，尚无实际调用，因而不适合作为未知效果样例；该原型失败及来源保留，修订后的构造器在完整执行前重新冻结。

## 已执行证据

最终 v3 全部 30 项真实输入控制通过（478.11 秒），23 项相关真实集成通过（528.99 秒），合计 53 个不同节点，无失败或跳过。相关范围包含再次合并、完整来源验证、提案、批准与合并新任务。全部 806 单元通过，Ruff/format 与 177 源文件 mypy 通过；先前更广的历史回归保持原记录，不重复累计。

三十组原始输入均独立离线核对：每例一个真实研究根和两个 Live Run，共 90 个 Run；检查 CAS 域摘要/长度/归属、同项目索引、连续单父路径、不可变 Job/Replay 来源、配置、完整 Live 初始化与预算基线、实际调用意图/检查点/结果。预览前后全部索引及 CAS 集合相同。30 个替换 Replay 来源的内存副本均被拒绝，属于同一类来源替换反例。

最终三十组输入准备共 478 次 Mock 模型、358 次本地工具、4,808,743 Mock UTF-8 字节，费用 0；另有 1 条上述未知付款 fixture 记录，实际外部付款请求为 0。原型、单元及相关回归的调用未混入这一数据集准备成本。M20 的未知 unit 字段在有限 Schema 处拒绝，按输入拒绝单列。M26 因读取授权不足产生 `CONSTRAINT_VIOLATION`；2026-10-29 核对 v1/v3 原始包确认其一直为约束冲突，旧文字误写为 forbidden 输入拒绝，现更正，原始数据及预期未修改。

v3 执行前保存 448 个文件，产品/锁/迁移、资产和实际构造驱动在全部 Runtime 终态时逐文件核对不变。离线审计器在执行后另存于 `analysis-v1/`，原快照保留。审计范围为已发布输入及预览只读边界；正式运行还需保存完整候选状态/产物、独立重算候选摘要、执行三种方法及无冲突例的精确批准后新 Run。

失败记录保留：构造 v1 五项为三通过/两失败（M01、M30）；首次补读补丁插入计划循环的位置错误，静态检查与 24 项单元捕获，纠正后 24 项全过。构造 v2 仅重测 M01/M30 两项通过；之后 v3 重新冻结并完整执行 30 项。原始失败与中间控制不混入最终计数或正式研究成绩。

## 复核与后续

原始包在忽略目录 `.agentgit/evidence/S11-benchmark-construction-v3/cases/*/{source,final}/evidence.json`。`summary-v1.json` 的 SHA256 为 `bf616f954bf33fc330e4bf4cc41af8738d8f11fc988fc787134c6bb51738809e`，索引成功/失败 XML、执行前快照、30 组原始包、分析器和输入审计摘要。新复核使用新输出文件名：

```powershell
uv run --locked pytest tests/integration/test_merge_benchmark_construction.py
uv run --locked python tests/integration/merge_benchmark_verify.py .agentgit/evidence/S11-benchmark-construction-v3 --destination .agentgit/evidence/S11-benchmark-construction-v3/input-audit-recheck.json
```

后续完成正式三方法候选比较、无冲突例批准/双父/新任务、完整候选字节审计及人工预期复核，再接桌面和新包。本记录保持 S11-T01–T06 与阶段整体退出待办；用户暂缓的独立 Windows/原生、S8 人工项继续保留，不进入 S12 或合并 main。
