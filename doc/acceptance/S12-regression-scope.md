# S12 当前兼容与历史冻结评估的核对范围

2026-11-01，当前产品源码仍为 `source-sha256:b9dee51c0d966346a9deb0b41437fb953de25457433d919ac790023fa9729319`，无产品、迁移、依赖或预算修改。本文纠正最终候选最初把所有研究入口当普通兼容测试的执行安排，保留全部既定交付与正式门槛，不把失败轮改成通过。

## 原轮结果与完整范围

`S12-package-018-v4/backend.xml` 为 **1,680通过、1失败**。原十二组并发及两项发布延迟回归已通过；后续首个 S8 效果例 D01 在 `load_protocol()` 被冻结守卫拒绝，尚未构造来源、trial 或新结果。该轮不是2,899全过，也没有修改协议使其通过。

全部入口当前收集2,902节点。其中两组一次性冻结入口为 S8 `test_diagnosis_candidate_effects_v3.py` 的20例，以及 S10 `test_repair_frozen_evaluation.py` 的30例。它们绑定旧完整源码／锁／驱动，固定结果目录只创建一次；原方案要求在对应冻结工作树重现，不允许在新代码下覆盖或重标。

除上述50项历史执行入口外，**当前技术范围为2,852不同节点**，包括新加的3项历史协议拒绝检查；S11三十组 `pilot` 流程、所有其余单元／集成、88项execution与32项sandbox均保留。旧正式 S11 数据不被 pilot 替换。当前范围还未全部完成：依次核对v4的1,680通过、新守卫3通过及尚待执行的1,169项，并按实际节点身份去重和检查冻结输入等价，不能把收集或历史结果计为当前通过。

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

`S12-historical-study-guards-v1/result.xml`：3不同节点全部通过（12.84秒，无失败／跳过），SHA256 `d114ba7bad7af704ea3c246fc37e568493daef2e4f28551598fc5d3b3cdc9b29`。这证明拒绝边界，不算历史50例在当前源码通过。完整当前技术范围、34包内节点、最终封存与D3报告继续；正式原生／人工门槛和不合并main保持。
