# S8 首版故障样例

`cases.json` 包含 20 个确定性故障规格，D01–D12 为开发集，D13–D20 为保留集。排序实现及规则调参前冻结规格、划分和测试替身源码；冻结后修改注入或划分须建立新版本，不覆盖已经评估的版本。排序规则冻结后才执行保留集定位评估，不根据保留集结果修改规则并在同一集合重新宣布优势。

冻结清单是 `release.json`，核验原始文件 SHA256、注入器 SHA256 及逐项划分，换行遵守仓库 LF 规定。清单中的 ranking_status/annotation_status 记录冻结时状态，不随以后实现或审阅覆盖修改。

所有资料均为固定合成 fixture。每个样例在真实 PostgreSQL 独立测试 schema 中先执行正常研究 Run，再从其首个检查点建立独立故障分支，执行受控模型/工具替身。完整调用经过既有 Gateway 录制，历史/CAS/检查点均实际生成，原分支不变。不是只在 JSON 中写一个“失败”状态。采用三个基础工具，不需要外部模型、外网或容器；超时是明确标记的确定性错误替身，不声称测量了网络超时可靠性。

规格中的 `annotation` 是代理提出的标签草案，统一 `agent_proposed/pending_human`，**不等于人工 gold**。注入位置、首个可见异常和可修复决策分别记录；后者是待验证候选，不保证对应干预必然修复。人工审阅说明及逐例清单见 [标签审阅表](../../../../doc/S8-dataset-review.md)。后续人工反馈须另存附加审阅记录，绑定本版本摘要、真实审阅来源和逐例意见，不将代理自己填写的姓名或日期作为人工确认。

`model:<node>` 通过已录制模型请求中的规划状态与 ready 节点寻址，`model:plan` 限定规划阶段；`tool:<node>` 通过逻辑调用身份寻址，`corpus:<source>` 指向冻结输入。标签解析须唯一，并返回真实的 Run/seq/数据库事件 ID；源语料注入则同时保留冻结 input_ref。不能把注入点的来源 ID 当成事件 ID，也不能使用时间戳强配。

注入器位于 [diagnosis_dataset.py](../../../integration/diagnosis_dataset.py)，通过参数化 [测试](../../../integration/test_diagnosis_dataset.py)运行。该测试只核对故障构造、终态、固定硬断言、录制响应与标签寻址，不计算定位命中率。标签、期望错误、划分及审阅状态不进入模型请求或 Agent 工作区；替身只根据操作和目标生成故障。

```powershell
$env:AGENTGIT_DIAGNOSIS_EVIDENCE_DIR = '.agentgit/evidence/S8-dataset-local'
uv run --locked pytest tests/integration/test_diagnosis_dataset.py -q
Remove-Item Env:AGENTGIT_DIAGNOSIS_EVIDENCE_DIR
```

可选证据目录限定在工作区 `.agentgit/evidence/S8-dataset-*`。每例导出实际对照/故障 Run、共同祖先、两侧行为图、录制注入收据、实际用量、拟议标签的事件引用以及构造耗时；另存 `<ID>-evidence.json`，包含本项目所属 CAS 的类型、原字节（base64）及必要所属索引，逐一核对域隔离摘要、长度和事件引用，有界归档并保留文件 SHA256。数据属于固定合成 fixture，不包含真实凭据。

测试 schema 在 fixture 结束时销毁，离线审阅及后续评估可以通过证据包核对原始内容；这些导出不能当作仍可调用原数据库 Run 的持久项目，也不会自动导入业务库。后续命中率、失败数、调用/成本和时间基线须单独评估并保留全部 20 例；待人工审阅时只允许明确标注的暂定工程结果，不作为 S8-T04/T05 的正式结论。
