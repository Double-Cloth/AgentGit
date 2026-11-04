# S10 同预算方法协议与真实接线

2026-10-27，分支 `codex/s10-repair`。19 项契约与五方法真实 PostgreSQL 接线共 24 个不同测试节点通过，无失败、错误或跳过；Ruff/格式和 diff 检查通过。只增加评估辅助代码、方法资产和文档，产品包、依赖及 0.1.6 桌面运行文件未变。新 30 例、全量四基线和 D2 尚未完成。

## 方法与同预算

方法输入只有共同冻结 RepairSpace、预算、逐项干预 ID 和公开轨迹的首异常映射，不接收 split、标签或 trial 结果。首版明确离线 Mock 与 full_candidates_fallback，拒绝只凭 static_ranking 字样宣布定位有效。主方法和无排序枚举保留生产原顺序，可以等价，不能人为制造排序优势。固定全局 Prompt 只使用 prompt-verified-sources/v1 提议一次；局部方法没有适用目标就保留空集合，不换成已知可修复位置。

不缩减方法将六个缩减次数预先移到筛选，为 20/0/6/4，其余为 14/6/6/4。max_trials=30，单 trial/总调用、Mock 字节、费用、时间、并发和独立六加四保护额度相同，共同预算摘要为 `8dc9f4116e1f56d1b4944e04de497d2148a475c0fb8b395f56bc69bb97545edf`。恢复、失败均计台账，额度未用不冒充实际支出。

## 实际结果和边界

五方法分别在独立 schema 构造同一个旧工程 Prompt 例，经真实原子发布、控制器/普通 Worker、Job/Run/CAS 和锁后复测。该例含预定锚点，仅证明接线，不是五个独立任务，不计入新的 30 例或 10/12。正式任务的起点和候选只能从公开输入及预先固定规则选择，不能读取标签。

| 方法 | 实际结果 | screen/reduce/confirm/regression | 六加四新 Mock 调用 / 字节 |
|---|---|---|---:|
| repair | verified_under_fixture | 2/4/6/4 | 56 / 603048 |
| first_exception_local | not_found，局部目标不适用 | 1/0/0/0 | 未执行，不作为成功 |
| global_prompt_once | verified_under_fixture | 2/4/6/4 | 56 / 603048 |
| unordered_enumeration | verified_under_fixture | 2/4/6/4 | 56 / 603048 |
| combination_without_reduction | verified_under_fixture，最小性 not_proven | 2/0/6/4 | 56 / 607752 |

screen 均包含一次 Recorded 恢复检查。四种适用方法各有十个不同新 Run，共 24 次确认、16 项回归、224 次新 Mock 模型调用、2,416,896 Mock UTF-8 字节；没有供应商费用或正式配置审批，来源轨迹均不变。不缩减方法没有删除或字段试验，不能称严格最小。独立生成允许相同文本，不为摘要不同而挑选响应。

局部方法从首个显式失败前最近模型请求获得目标；它不属于共同可用空间，只运行恢复检查并保留 not_found，未打开确认。该负例只有真实台账/轨迹快照，没有完整 CAS 导出；正式 30 例必须为成功、失败和无解全部导出原字节，不能拿本例宣称已满足全量证据覆盖。

四份成功归档有 1416/1416/1415/1321 个对象，原字节为 3482878/3477010/3481187/3208548。独立审计重算选择/身份、对象类型域摘要、硬验收、原始请求响应、用量、任务/语料/配置、不同执行身份及预注册/锁/开池顺序。所有新调用核对协议、driver 和确认适配器摘要，来源另核对其专属注入器。初版审计器错误要求通用回归也携带未使用的来源注入器摘要，纠正角色范围后通过，没有放宽产品检查。

## 冻结与后续

`tests/fixtures/repair/methods-v1.json` 只冻结方法/预算/源码，不是数据集，dataset_release_ref=null、heldout_execution=not_started。首轮原型五项虽通过，但规范化排序偏离生产顺序；修正并拒绝静态排序标记后，用新前缀重新执行。旧原型源码/结果保留，不与最终 24 项累计，不冒充同一策略。

最终证据在忽略目录 `.agentgit/evidence/S10-benchmark-method-smoke-v2/`、两份 v2 XML、`S10-benchmark-method-audit-v2.json` 和 `S10-benchmark-methods-summary-v1.json`。自己的 schema 在 fixture 结束时删除，业务库未迁移；产品身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 为 `0023_repair_proposal_reviews`。

```powershell
uv run --locked pytest tests/integration/test_repair_benchmark_protocol.py
$env:AGENTGIT_S10_BENCHMARK_SMOKE_DIR='.agentgit/evidence/S10-benchmark-method-smoke-new'
uv run --locked pytest tests/integration/test_repair_benchmark_execution.py
```

下一步按[30 例计划](../S10-benchmark-plan.md)建立任务家族、故障机制、hard oracle、四项回归和 20/10 划分，再冻结完整协议、执行五方法及保留集。10/12 目标、完整四基线和 D2 未验收；S11/S12-A/B/C/D、独立 Windows/原生和人工标签均保持，不宣告阶段或项目完成。
