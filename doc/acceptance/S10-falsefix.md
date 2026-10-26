# S10 随机拒绝与实际观测伪修复

2026-10-26，`codex/s10-repair`。依据实施方案 S10-T02/T05，实际运行 PostgreSQL、搜索/缩减/确认控制器和普通 Worker；每例使用独立测试 Schema 与临时 CAS。未迁移业务库、调用付费模型或生成桌面包。Repair Lab、30 例/四基线、D2、S11 与 S12-A/B/C/D 继续实施；S6 独立 Windows 环境与 S8 人工标签按用户授权延后。

## 随机筛选通过仍须独立确认

新增 Mock 专用保守声明 `fixture_scope="stochastic_fixture"`，在搜索创建时冻结到请求摘要和 manifest，随后传入缩减候选。省略保持旧编码；真实供应商使用该声明被拒绝。该字段只降低证据结论，不修改模型调用、验收器或门槛。HTTP 实测涵盖默认和随机声明、首次派发前可见、同键省略声明冲突；内部直接提交也不能更改已登记请求。

实际适配器按 `random.Random(seed).random()<0.1` 决定能否写入正确产物；缺少必要 Prompt 规则始终失败。种子向量 `31,0,1,2,3,4,5,6`、概率与按 Job 发布顺序的分配策略，在源 Goal 中随适配器文件 SHA 冻结。模型只读取任务、Prompt 和预先分配样本，不读取 trial 阶段或验收结果。相同 Job 恢复沿用原种子，不凭运行次数推进样本。

筛选使用首个种子，实际通过；删除必要 Prompt 的新 Job 失败，缩减仅标 `empirical`。最终候选锁定后才开启预注册池；六个新确认 Job 全部硬验收失败，四个不同回归任务通过。最终为 `rejected`，确认 `0/6`，协议计算的 95% Wilson 区间为 `[0, 0.390334287902165383]`。所有确认具有新模型调用、不同 Job/Run 和独立结果，旧筛选结果没有计入确认。再次开启同一池返回原开启记录，不重置拒绝、不产生第七次确认；正式采纳被拒绝，源 S9 决定保持不变。

这是专门预选的故障种子向量，用于复现“偶然筛选通过、确认失败”的拒绝边界；不是无偏抽样研究。区间是既定协议输出的数值，不能据此估计真实模型或随机修复算法的成功率。

## 观测通过只能保存实验

源故障适配器对搜索结果顺序敏感；正常排序时写错产物路径。另一实际 Run 录制相反顺序的完整只读 `corpus_search` 响应，六个 SourceID、正文、固定时钟及验收器不变。替换调用和原调用拥有相同参数摘要及工具签名。公共模板入口只生成该 `TOOL_RESPONSE_PATCH`，从新模型决策前的共同检查点开始 Hybrid 执行。

筛选通过；删除观测补丁的新 Job 失败，字段阶段为 `no_applicable_fields`。最终锁后六个 Hybrid 确认均通过，每个各含一次明确的 synthetic observation；四项不同任务的 Live 回归均通过，投影说明该观测仅适用于源任务。所有验证结果保留 `test_only`。统计可以是 `verified_under_fixture`，但 `has_observation_patch=true`、`production_eligible=false`，建议 `after=null`、`adoption_scope=save_experiment_only`。

实际尝试批准被服务层拒绝，直接伪造批准写入也被数据库约束拒绝；配置数量保持不变。保存返回完整确切补丁的实验定义和 `execution=not_started`，没有新增配置或排队执行。桌面上的按钮状态和原因展示仍随 Repair Lab 验收，不以本后端用例替代 UI。

首次观测用例将工具已暂存参数的检查点用作确认起点，按既有新决策约束被拒绝；修正为初始共同检查点后完整通过。失败报告保留，没有放宽独立样本入口。

## 证据与复现

原始证据位于忽略目录 `.agentgit/evidence`；归档含原始 CAS 字节及归属索引，不向仓库提交运行数据。独立复核器 `verify_s10_falsefix_v1.py` 逐对象核对类型域摘要/长度，从原始请求响应重算调用、字节、计量与六加四统计，核对采样先后、不同执行身份、预注册与锁/开启顺序、观测来源和保存定义。原始失败也保留，不与最终通过重复累计。

冻结源码为 `source-sha256:c9cdda8b35a53c31bdb8320dc908a32085a2ca8f0171939146fe416abc82f475`，Schema head 保持 `0023_repair_proposal_reviews`。二十次验证均从最终状态/产物和冻结语料重新执行硬验收；不只核对报告自述。

| 原始归档 | SHA-256 | 对象数 / 原始字节 | 六加四新模型调用 / Mock 字节 |
|---|---|---|---|
| `S10-stochastic-first.json` | `8ca37c3b8ebeb38e62bbcfe2450deac6354d6c0a60be01de926cc5b3e6c74691` | 1366 / 3,354,638 | 56 / 602,368 |
| `S10-observation-v2.json` | `68709bbb502ab04ce8f64dcba8c4aac8b526efb80a7f376eca1d0fbac49fae89` | 1445 / 2,656,212 | 110 / 948,940 |

对象数和原始字节按两个独立仓库记录相加，不宣称跨仓去重；Mock 费用为零，字节数不是供应商 Token 或实付费用。

最终汇总 `S10-falsefix-summary-v1.json` 的 SHA-256 为 `6777bfd89f197a601034e334e419ac6079fb193a8131f5761b0b559768428647`。同源码去重通过 746 项：698 单元与 48 真实集成；四份最终报告分别为 `S10-falsefix-core-api-v1.xml`（733）、`S10-falsefix-compat-v1.xml`（11）、`S10-stochastic-first.xml`（1）和 `S10-observation-v2.xml`（1），均无失败、错误或跳过。698 项早期单元结果不重复累加。Ruff、格式、mypy 160 个源码文件、工作区/暂存区差异检查及完整测试收集通过；收集数量不当作执行数量。暂存区 162 个包内 Python/JSON 文件与实测源码逐字节一致。

```powershell
uv run --locked pytest tests/integration/test_repair_stochastic_gate.py tests/integration/test_repair_observation_falsefix.py
uv run --locked pytest tests/unit tests/integration/test_repair_api.py tests/integration/test_repair_worker.py tests/integration/test_repair_studies.py tests/integration/test_repair_pools.py
uv run --locked pytest tests/integration/test_repair_adoption.py tests/integration/test_repair_review_controls.py tests/integration/test_repair_review_api.py tests/integration/test_repair_confirmation.py tests/integration/test_repair_confirmation_variants.py
```
