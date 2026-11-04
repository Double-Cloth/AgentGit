# S11 结构化状态、来源与完整候选硬验证

2026-10-28，基于[实施计划](../S11-merge-plan.md)。本增量继续开发分支工程，不表示 D2 前置、完整 S11 或三十组正式评估已验收。

## 当前能力

`core/merge.py` 增加独立 `merged_research_state/v1`：goal、按稳定 key 合并且重新检查 DAG 的计划、工作记忆、权限/预算交集、完成声明、真实读取证明、效果及来源逻辑用量审计。它不能解析为 `AgentState`，不拼接 messages、不继承 pending_tool/执行容器/审批，也不把两侧完成声明当作新 Run 已执行；后续运行仍需显式创建。

`merge/state.py` 重新检查完成前置、已完成节点未改写、事实身份与冻结正文/引用/时间、已支持维度、授权交集、预算上限及完整计划的最低调用量。相异事实、DAG 环、删除/编辑、缺来源、未知/未决效果或新外部不可逆动作均阻断。原用量按三方来源分列，不相加为新 Run 的预算；静态检查不保证未来 Token、费用、墙钟一定足够，执行层继续负责耐久额度。

`services/merge_inputs.py` 从同项目明确 Run/Commit 路径、真实 state/environment 和实际调用索引读取，逐项核对 CallIntent、occurrence、登记签名、请求、完整响应和来源正文。关键修正：调用意图的前检查点还必须在选定 Commit 的祖先内，字节相同的分叉后原 Run 调用不能成为新分支的证明。未决调用可能尚未进入 messages，仍从来源链意图中检查；本地容器结果还须关联受管理执行的清理记录。

`services/merge_validation.py` 将三方状态与全部受支持产物合并，使用同项目冻结验收器的输入/语料重跑既有报告硬规则；验证绑定三方来源、源码、锁、验收器、状态及产物摘要。此处检验合并产物，未伪造 completed Run 或 FrozenAssessment。通过后仍标 `approval_required=True`，没有提案写入、批准、HEAD 修改或工具执行权限。

完整研究验证目前只支持 `report.md/sources.json`，额外文件明确 unsupported；通用 Markdown 仍只能做前一增量的未验证预览。Fact 当前限既有文本值和三维度，不从任意自然语言推断数值/单位转换。JSON 产物顺序改为保留 base 中已有实体、新实体稳定追加，解决原按身份排序会改变 Markdown 渲染顺序的问题；新增不变报告回归，未修改旧验收器。

## 当前验证

| 范围 | 结果与边界 |
|---|---|
| 全部后端单元 | 764 通过；本轮新增 20 个状态向量及 1 个 JSON 顺序回归已包含，不重复累加旧 743 项 |
| 新真实研究预览 | 3 通过：两个完整来源/产物场景及跨项目验收器拒绝 |
| 真实未知效果输入 | 1 通过：选择实际写入意图前检查点，快照没有该工具消息，仍加载后续未知写入并阻断，零重发 |
| 相关真实集成 | 当前源码复验 14 通过：3 个首增量预览、7 个版本历史、4 个冻结验收器用例 |
| 静态检查 | Ruff 检查/格式通过，全部 168 个产品源文件 mypy 通过 |

上述为 764 单元与 18 个不同集成节点，不表示三十组、双父历史、并发批准或桌面安装包验收通过。单元中的读取证明和效果为已核验输入向量，真实来源能力由上述独立数据库用例证明。

新完整研究场景都经实际 Runtime/Worker：正常来源先完成；正例从计划检查点显式建立两条 Live 分支，独立重读六份来源并完成报告；反例从完成点将第一条引文替换成不在语料中的文本，两个产物同次带入，坏分支真实失败。候选验证从原始字节重算，正常例通过、伪引文明确 `FACT_CONTRADICTION`，没有采信分支上报的 evaluation。预览执行于 PostgreSQL 只读事务，观察 SELECT/有界只读 CTE，CAS put、Model/Tool invoke 均禁止；前后各表计数、全部 CAS 文件及 main/ours/theirs 完整分支行不变。

原始复核 `tests/integration/merge_research_verify.py` 不连接数据库或执行工具：对全部归档对象重算域 hash/长度，检查三方 manifest/state、真实读取及其祖先、完成声明、约束、效果审计、产物硬判定和候选摘要。最终正例为 12 条独立分支读取证明、零继承；坏产物例保留 18 条含来源身份的证明，其中 12 条来自合法继承的真实原 Run 读取，仍不能使假引文通过。复制证据后分别替换成分叉后原 Run 调用、伪造报告文本，审计确实在祖先与硬验收处拒绝，原件未改。

| 最终两场景计量 | 实际模型 | 实际工具 | Mock UTF-8 字节 | 对象记录 / 原字节 |
|---|---:|---:|---:|---:|
| 两条全新读取分支及正常来源 | 31 | 27 | 278,551 | 278 / 394,907 |
| 坏产物分支、对照及正常来源 | 13 | 9 | 122,719 | 239 / 448,124 |
| 合计 | 44 | 36 | 401,270 | 517 / 843,031 |

这里只累计最终两场景的准备与分支执行，零费用，不把继承或归档副本算成新调用/新对象；对象数跨仓可能重复。另一个未知输入 fixture 有一次已发送但客户端未确认的本地 Fake effect 请求，读取/合并前后仍为一次、零命中/重发，原始 51 个对象、31,859 字节独立核对。它保留 `outcome_unknown`，不据测试 Server 旁观结果自动结算，不加入上表。

## 原始记录及修正历史

最终产品身份 `source-sha256:3c11e13e61b42015e5764850477df03fedcf77bf210c9b2341f709526776cb9e`。新完整预览执行前的 203 文件快照、输入/产物及 source/validated 两份索引/CAS 保存在忽略目录 `.agentgit/evidence/S11-state-validation-v3/`。主审计 `audit-v1.json` SHA256 为 `3764d5aa131e8bff2e72dcb1d4ffd9cbd89890e8a37b2adb76be67d8a2f69d5a`；两个反例 `negative-audit-v1/summary.json` 为 `16b889abe29fbeeb990700afe8e63aa2be28aa973a3ec9f15fcd4aabb64bd602`。

未知效果位于 `.agentgit/evidence/S11-unknown-input-v2/`，冻结驱动、实际来源及结果均保留；`audit-v1.json` SHA256 为 `f4394cc2dd4da1f3dab59239489b7b4f5e435669b824bcb704f975d59ecb8e8a`。

早期失败全部保留，不计入最终通过或上表：v1 首次因 Docker Desktop/现有 PostgreSQL 均未运行而发生三项连接错误，零 Runtime；启动现有引擎/容器、确认 ready 后复验，两项因测试观察器误拒绝只读 CTE 而失败，只有 JUnit/冻结代码，无完整原始归档。v2 修正为只读事务后两项通过，正例发现 24 条证明，暴露原 Run 分叉后调用混入的实现缺陷；两场景的 source 原始包和已完成反例 validated 包保留，正例没有完整 validated 包。v3 增加真实祖先绑定后保持原 12 条断言，三项全过。未知输入首轮误选最终检查点，那里已有未知调用消息，测试前提不成立；保留失败 XML/驱动，改选真实意图前检查点后通过。没有通过降低来源断言或放宽副作用策略掩盖失败。

当前 JUnit：`S11-state-all-unit-v2.xml`、`S11-state-validation-v3/results.xml`、`S11-state-related-integration-v2.xml`、`S11-unknown-input-v2/results.xml`。

最终封存 `.agentgit/evidence/S11-state-validation-summary-v1.json` SHA256 为 `0ad6cdde55d2e3658b993eacc6ba1e2219335a7cf351b2c668487fc819205693`，绑定当前源码、四份无失败/跳过报告、203 文件快照、两场景计量、原始/未知审计和两个篡改反例；计划文档在运行后补充结果，冻结副本保持原字节。完整 S11 退出明确为 pending。

```powershell
uv run --locked python -m pytest tests/unit -q
uv run --locked python -m pytest tests/integration/test_merge_research_validation.py tests/integration/test_merge_unknown_input.py -q
uv run --locked python tests/integration/merge_research_verify.py .agentgit/evidence/S11-state-validation-v3 --destination .agentgit/evidence/S11-state-validation-recheck.json
```

正式三十组与基线、完整冲突编辑/重新验证、耐久提案及精确审批、双父 Commit/历史兼容、Merge Review 与新桌面包仍须继续。S6/D1 原生环境、S8 人工与原定位目标等前置保留；完整 S11 验收后才进入 S12。
