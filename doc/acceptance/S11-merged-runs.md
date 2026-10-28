# S11 合并后的独立新任务与实际进程中断

日期：2026-10-28。分支 `codex/s11-merge`，基线 `5c54277`。依据设计 5.5、5B.3 和实施方案 S11，延续[原子批准](S11-approval.md)。本增量补合并后 Fork、新 Run、明确 Recorded 路径及实际进程中断；完整 S11 仍需后续样例/基线、桌面和新包，用户暂缓的原生/人工前置保留。

## 实现范围

`0026_merge_runs` 新增两个不可变来源表，当前 73 张产品表：`merge_branch_origins` 绑定批准/Commit 与独立分支，`merge_run_requests` 绑定新 Job、配置版本、验收器及 CAS 初始化状态。使用同项目 FK、归属和不可变保护；每个合并分支只能发布一次初始化任务，后续查询或从实际新 Run 再显式 Fork，不把合并 Commit 填入旧 Run 路径。

`merge-fork` 只创建分支和来源，不启动 Run。`merge-run` 必须显式选择分支 HEAD/version、配置 ID/version、当前冻结验收器与预算。读取已批准候选的真实状态/文件，核对当前语料和硬规则；工具、预算不能扩大合并约束，配置模板不能暗改合并计划。新 Task/Run/工作区从计划、核验记忆和产物初始化，completed、messages、pending_tool、用量、attempts、执行状态和审批均清空；历史完成/用量/效果继续只留在合并审计。

Job 使用独立 `merged` 输入，不能与 Replay 混用；空字段从旧 Job 编码省略。标准 Job/同步研究入口拒绝从合并分支绕过显式初始化。Worker 每次加载都重核不可变请求、配置/预算、批准、初始化 CAS 及文件归属；新 Run 首检查点为 `merge.initialized`，零预算基线，之后沿用现有 Runtime、预算、审批与恢复。恢复使用新 Run 身份和相同初始化输入，不重用旧来源的调用。

合并 Commit 没有录制路径，Recorded 明确返回 `recording_missing` 并要求选择新 Run 的执行检查点。新 Run 初始化检查点→终点的 Recorded 已实际核对，状态逐点证明、零新外部调用。HTTP/CLI 同服务、同键确认与只读重开；Run/Job 查询附 `merged_input`，恢复后的当前 Run 通过 Job 查询，原提交收据不改写。

## 已执行证据

最终九个新真实 PostgreSQL/CAS 节点通过（231.16 秒），覆盖完整 Fork/新任务/Recorded、预算/目标/版本/项目拒绝、标准入口防绕过、并发同键/竞争键、事务回滚、HTTP/CLI 与只读来源查询，以及四处实际终止子进程。全部 796 单元通过，包含 12 个新增输入/兼容契约；138 项相关集成通过（1207.44 秒），与新增九项合计 147 个不同节点，无失败或跳过。相关范围包括原批准/提案/状态验证、版本/迁移、Recorded/Live、Job 租约与独立 API/CLI 进程，旧原型不重复累计。Ruff/format 及 177 源文件 mypy 通过。

四个实际终止点为批准事务内目标 CAS 后、批准已提交回执前，以及新 Run 首初始化提交前、提交后。测试使用 Windows `TerminateProcess` 终止实际 Python 解释器，核对启动器/解释器父子关系、退出码 73 及源码身份；没有把 Python 异常或关闭页面当作进程退出。批准中断回滚或原键找回同一合并；新任务在租约到期后由新恢复 Run 接管，两个被终止 Run 的调用计数均为零。

初版 v2 的 5 项普通流程通过、4 项进程测试失败：Windows venv 有启动器和实际解释器两层 PID，旧测试在身份断言提前退出，未完成终止验收。保留该失败 XML 和原始来源，不改写为成功。修正为核对父子关系并持有实际进程句柄终止；v3 重新冻结并完整运行九项，四个真实终止均通过。初次单条 v1 成功冒烟也单独保留，不重复累计。

最终源码为 `source-sha256:145a71278e6dbb446fb049db91084606d2e968d8dfcc4866159cfcacba94758d`，锁仍为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。v3 在执行前保存 442 文件，包含完整产品/迁移及测试 Python/JSON、实际子进程助手和驱动。没有新增依赖、修改 UI 或迁移业务库。

九组最终原始包独立重审通过：保留原执行集合后，分别核对批准/双父，合并分支/Job 请求/初始化归属和零基线、实际新调用及唯一来源、Recorded 逐检查点状态相同、进程退出证据。篡改初始化绑定、批准绑定、Recorded 来源路径或进程退出码的四个内存副本全部被拒绝。总计 187 模型、144 工具、1,865,375 Mock UTF-8 字节，费用 0；其中 133 次调用属于七个显式新任务，其余为三方输入准备。8 个独立合并分支、7 个合并新 Job、1 个 Recorded Job、4 次实际终止，原来源不变。

原始包位于忽略目录 `.agentgit/evidence/S11-merged-runs-v3/cases/*/{source,final}/evidence.json`；快照为 `frozen/`，独立审计为 `audit-v1.json`，反例为 `negative-v1.json`。复核输出须为新文件：

| 原始证据 | SHA256 |
|---|---|
| `S11-merged-runs-v3/summary-v1.json` | `fe092fb90e1bf018be8fa6dc4469833bfc03400829fd5d692414488baac131e7` |
| `S11-merged-runs-v3/audit-v1.json` | `d98487e87f5eebb833914e6e941df2174dc066e6137436c3b795867d60f16b9d` |
| `S11-merged-runs-v3/negative-v1.json` | `6ab4825615176e9e8556afc95d77c13dac34621ef601b363236e3c5624f96ea1` |
| `S11-merged-runs-v3/frozen/snapshot.json` | `5a61d5b06928b5bf2585cab466c609fe40fc4e93be6d72423043da263536a9f1` |
| `S11-merged-runs-v3/results.xml` | `6f4382ba5c72954006db02c4917ba6f0505c54d6278af7adb74db6ef8e251bf6` |
| `S11-merged-run-related-v1.xml` | `0b16179b70e57a2ba713fea777bcde2894fa17b2e12c5df7b5650447c55c659a` |
| `S11-merged-run-all-unit-v1.xml` | `bec34222705ae6caca668e332b1df7facfc3914e709d3c96085c522cd167b02d` |

```powershell
uv run --locked python tests/integration/merge_run_verify.py .agentgit/evidence/S11-merged-runs-v3 --destination .agentgit/evidence/S11-merged-runs-v3/audit-recheck.json
```

后续仍需核对合并根作为共同祖先的再次语义合并、更多双父/歧义范围，以及正式 30 组、基线、Merge Review 和新 Windows 包。本记录不把当前工程范围替代完整 S11，也不进入 S12 或合并 main。
