# S12 当前兼容与历史冻结评估的核对范围

2026-11-02当前结果：[测试环境记录](S12-test-environment.md)及[最终0.1.8候选](S12-package-018.md)补齐实际证据。新增两个MCP就绪回归使当前技术入口为2,855，另50历史单列；同产品dca8f8…ba7d源码按20＋177＋1,357＋1,301逐节点去重覆盖。原v7三失租、图复验一个setup错误及诊断／准备／负例保留，不是单进程一次全绿。34个实际包内节点和原始审计已通过；正式[D3](../D3-readiness-report.md)仍待。以下2,853／2,903等数字保留其历史收集范围；当前完整命令会实际收集2,855技术节点，不修改两个历史入口的冻结guard。

2026-11-01，当前产品源码为 `source-sha256:dca8f8b5e4400e05ce0a6bc49a26560322638fea315922c1f1e55ff5fce7ba7d`。[Live有效预算错误映射](S12-live-budget.md)只改既有配置校验入口，未新增依赖／迁移或放宽预算。本文分列当前技术与历史冻结评估，保留全部交付和正式门槛，不把失败轮或旧源码成绩改写为当前通过。

## 原轮结果与完整范围

`S12-package-018-v4/backend.xml` 为 **1,680通过、1失败**。原十二组并发及两项发布延迟回归已通过；后续首个 S8 效果例 D01 在 `load_protocol()` 被冻结守卫拒绝，尚未构造来源、trial 或新结果。该轮不是2,899全过，也没有修改协议使其通过。

同b9源码的v5为296通过／1间歇失租失败；五项诊断复验通过，但根因未定位，不放宽租约／fencing。v6续跑为229通过／1非法Live预算原始Pydantic异常失败（2637.05秒），终态后才修复。连同3项拒绝守卫，旧b9分版按节点去重为2,208通过、尚有644未通过，保留历史拒绝／失租／错误映射三次失败；这些均不计为新dca8源码通过。

修复后的新预算回归由一个入口增加为两个参数，全部入口实际收集2,903。其中两组一次性冻结入口仍为 S8 `test_diagnosis_candidate_effects_v3.py` 的20例，以及 S10 `test_repair_frozen_evaluation.py` 的30例。它们绑定旧完整源码／锁／驱动，固定结果目录只创建一次；原方案要求在对应冻结工作树重现，不允许在新代码下覆盖或重标。

除上述50项历史执行入口外，**当前技术范围为2,853不同节点**，包括3项历史协议拒绝检查；S11三十组 `pilot`、其余所有单元／集成、88项execution与32项sandbox均保留。旧正式S11数据不被pilot替换。当前只完成新源码1,245单元及56不同定向集成，共1,301；其余须按精确补集继续执行和核对输入等价，不能把收集或旧源码记录计为当前通过。

当前完整技术范围可从仓库根目录执行：

```powershell
$env:AGENTGIT_MERGE_BENCHMARK_STAGE = 'pilot'
$env:AGENTGIT_MERGE_PROPOSAL_EVIDENCE = $null
uv run --locked pytest tests/unit tests/integration tests/execution tests/sandbox `
  --ignore=tests/integration/test_diagnosis_candidate_effects_v3.py `
  --ignore=tests/integration/test_repair_frozen_evaluation.py -q
```

两个忽略项仅用于这条当前技术命令；没有添加默认skip、修改旧研究驱动或移除研究交付。正式复现仍分别遵守[S8效果记录](S8-candidate-effects.md)和[S10复现说明](../S10-evaluation-reproduction.md)，使用原字节及新的独立输出工作目录。

## 历史结果的实际重核

在独立 detached 工作树 `0e4537eaf1204b1fa2c52e55bc95a2d50f0264d0`，叠加原201／189文件快照；两者共有文件无冲突，产品SHA恢复为 `f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，锁为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。两个加载门禁均实际通过。原 `bb04fa1d36f14b363272691d12e824c961c7f966` Git tree的25个迁移文件加alembic.ini共26文件，在审计前后逐字节复核；未回写原清单遗漏。

原case／冻结输入物理复制到独立目录，493文件共480,563,535字节，复制前后及审计后原件／副本摘要均相等。当前环境已安装的旧锁依赖版本无差异；此次是原源码下的只读重算，没有执行新的Agent任务或以当前源码重评旧保留集。

- S8：20例全部重算，四例正差／十六例零差、839模型／634工具和7,706,326 Mock字节保持；新摘要与原 `d66078cb354197e6e3e41477e0470557e49e1ad52736cf96067683bc63344f82` **逐字节一致**。
- S10：30例／150方法全部重算，缺例0；逐行结果、分组、12目标与全部历史计量和原analysis-v2一致。安全审计重核29,836条历史实际调用、150预算／验收器绑定及60次越界拒绝，零成功越界写或正式采纳；这些是历史计量，不是新执行成本。

重核记录在 `.agentgit/evidence/S12-frozen-study-recheck-v1/`，结果摘要SHA256 `29d4a4ae5a0d8e933c46c856375f82326359e1b89aff1cdaa58473bea01f2e81`。原协议、case和成绩未改，S8主要注入点Top-3仍4/8，人工标签仍待审。

## 当前版本拒绝检查

`test_historical_study_guards.py` 的3项真实隔离PostgreSQL用例调用 S8效果执行、S10冻结执行和 S11 formal入口。禁止来源构造、Worker、模型／工具调用和CAS发布，要求旧协议在这些操作前拒绝；前后所有业务表计数、CAS和协议字节不变，无新执行目录。

原b9源码的 `S12-historical-study-guards-v1/result.xml`：3不同节点全部通过（12.84秒，无失败／跳过），SHA256 `d114ba7bad7af704ea3c246fc37e568493daef2e4f28551598fc5d3b3cdc9b29`。这证明原版拒绝边界，不算历史50例在新源码通过；新dca8仍须执行这3项并归入其自身完整技术集合。当前2,853范围、34包内节点、最终封存与D3报告继续；正式原生／人工门槛和不合并main保持。
