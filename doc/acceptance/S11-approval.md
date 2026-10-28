# S11 原子批准与双父 Commit 增量

日期：2026-10-28。分支 `codex/s11-merge`，基线 `4317839`。依据设计 5.5、5B.3、实施方案 S11 及[合并计划](../S11-merge-plan.md)。本记录只确认批准后端及历史浏览增量，S11-T01–T06、桌面/30 组/新包和完整阶段验收仍待完成；用户暂缓的独立 Windows 与 S8 人工审阅不删除。

## 实现

新增 `ApproveMergeProposal`、`MergeApproval`、独立合并历史发布器和 `0025_merge_approval`，当前 71 张产品表。批准请求绑定确切修订/ref、票据 ID/ref 和候选摘要；操作者须与票据相同。仓库和双分支锁内重查来源、通过的精确验证、验收器、候选、两个 HEAD/version 与活动写入者，再重新运行已有硬规则，失配拒绝。

同一事务发布 CAS 归属/Commit/双父/产物索引、不可变批准和目标 HEAD/version CAS。父顺序固定 `(ours, theirs)`；theirs 不动。数据库限制两个不同父且序号从 0 开始，双父必须有精确批准且与来源/票据/当前修订和目标更新匹配，已发布合并不能补写父边。批准、拒绝和编辑追加相互排斥；成功原键重试返回原收据，不重复合并。

合并 Commit 不属于任何 Run，不修改原 Run/checkpoint 或创建 Job/调用。StateSnapshot 保存完整 MergedResearchState 及候选文件；环境标识 `agentgit-merge/v1`、模型为 `none:merge/v1`、工具签名为空，输入为独立 `merged_input/v1`。一条 CAS `merge.approved` 审计事件记录批准身份和候选，未伪造执行事件或合并调用轨迹，继续执行仍需新 Run。

Log/ancestor 读取改为有界唯一节点图（最多 10,000），核对实际父边、manifest 和顺序，稳定拓扑输出中每个父都在其子之后，不展开菱形路径乘积。结构化 Diff 返回全部最佳共同祖先，歧义时不任选。Recorded 仍要求显式 Run 路径，逐边只接受单父连续录制；选择一个来源 Run 不能把合并 Commit 冒充其终点。独立合并的 Fork/继续执行与 Recorded 完整适配、实际进程崩溃、桌面等后续范围不能据本记录宣告通过。

HTTP 新增 `POST /merge-proposals/{id}/approve`，CLI 为 `merge-approve`，共用服务。成功查询为 `merged`，附不可变 `approval`，旧票据保留但不再 current。查询只读，不重新发布 CAS。接口参数见 [API.md](../API.md#s11-耐久合并提案)。

## 验证记录

最终全部 784 单元通过；批准/迁移组 13 通过（159.92 秒），相关组 78 通过（642.93 秒），无失败/跳过。两组重复三个迁移节点，合计 88 个不同集成节点；新增节点为九个批准流程及一个父序边界。Ruff/format 通过，mypy 的 173 个源文件通过，未增加依赖或修改 UI。相关组在收紧父序前通过，最终 13 项在收紧后的迁移重新执行，旧原型不重复累计。

九个新增节点覆盖成功/恢复/Log/Diff、四个事务注入点（Commit、批准、HEAD 更新后及事务提交后回执丢失）、两个操作者竞争、非法票据/候选/修订/操作者、未经批准双父、旧版票据、源 HEAD 改变和真实 HTTP/CLI。四个事务注入测试是受控异常，不称为操作系统进程强杀证据。只有输入准备使用真实 Mock/工具，合并服务禁用执行入口后仍可完成流程。

补充父序测试在实际 PostgreSQL 提交时拒绝只有序号 1 的单父；成功批准流程还验证已发布双父不能补写。相关回归覆盖前增量提案/验证、未知效果、版本历史、冻结验收器、迁移往返及 RecordingReader/Recorded/Live/耐久 Replay 入口；没有据此声称独立合并状态已具备全部 Fork/Recorded 适配。

初版迁移冒烟有两项回退失败：创建 check 约束时用了短名称，实际名称与元数据不一致；修正完整名称后空 Schema 的往返/正式回退保护三项通过。原失败 XML 保留；未影响业务库、未将迁移失败计作 Runtime 结果。所有迁移只在独立测试 Schema 执行，不改写历史迁移。

源码身份为 `source-sha256:db3eb33e012d97d61d75d224eb0da9b440da597be7c2739136e55fe8a907142d`，锁为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。正式核对归档使用 `.agentgit/evidence/S11-approval-v3/`，运行前冻结 215 文件；v1 成功冒烟、v2 九项及父序收紧前的相关回归保留各自原始目录，不覆盖或重复累计。v3 使用相同产品源码，增加数据库序号保护和对应驱动，再完整复跑批准与迁移。旧 S8/S10 冻结源码/指标仍不重算。

## 原始证据与独立复核

最终九组共 9 提案、10 修订、9 验证、10 票据、7 批准；两组因过时/非法绑定保留未批准。实际输入准备 117 次 Mock 模型、81 次工具、1,104,867 个 Mock UTF-8 字节，费用 0。合并服务零新调用/Job/Run。主动推进源 HEAD 的夹具操作单独核对，其余源执行行完全不变；重开/CLI 原键查询只读。

离线审计重算 CAS 域 hash、候选/请求、来源读取和真实祖先/原文硬规则，再检查批准/票据/验证/修订、双父顺序、目标版本、完整候选状态/文件、无执行权限的环境及合并审计事件。四个内存副本分别篡改父顺序、批准 Commit、目标版本或 CAS 字节，全部拒绝；原包未改动。

| 忽略目录中的文件 | SHA256 |
|---|---|
| `S11-approval-v3/summary-v1.json` | `15ec476335648ea1463f0327bc31045a20421bd2707c47b211a99e9da790bce7` |
| `S11-approval-v3/audit-v1.json` | `0490038f38101319e83ee540ac3a6883082a5fd4aa066c4703ba0482e8e8f9d7` |
| `S11-approval-v3/frozen/snapshot.json` | `dfe02314457433009d2ae30f47ddd015d9562d9cc9b61bebe09090a169c21fe0` |
| `S11-approval-v3/results.xml` | `5721b37a2e4229009b1df0fc8aa34ac973a0dacac7b6516163b067b04c1673f4` |
| `S11-approval-related-v1.xml` | `ed5bae00f90c42e5f2ed71c58056c06b0c0fb503e411527d7e4db5892bfb0f62` |
| `S11-approval-all-unit-v1.xml` | `65bb81b6a81a723b38475b9d5427eea701fb92991d71533de7864bfa7070989d` |

全部原始表/CAS 位于 `cases/*/{source,final}/evidence.json`，215 文件快照逐字节保存于 `frozen/`；运行结束后再修改记录文档，产品、锁、迁移及驱动保持冻结。复核命令（输出文件须不存在）：

```powershell
uv run --locked python tests/integration/merge_approval_verify.py .agentgit/evidence/S11-approval-v3 --destination .agentgit/evidence/S11-approval-v3/audit-recheck.json
```

已实现本轮批准后端；未合并 main，业务库未迁移。继续独立合并状态的 Fork/新 Run/Recorded 与实际进程中断，再完成桌面、30 组及基线、新包和正式前置；本增量不替代完整 S11，S12 尚未开始。
