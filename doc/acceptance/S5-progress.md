# S5 实施记录：版本入口与录制基础

状态：进行中，尚未满足 S5 退出条件。完整 S5-T01–T09 仍以实施方案为准，本记录不缩减验收范围；S6 及 D1 尚未开始。

## 已实现

- 安全 Fork 绑定来源 Run/Commit，保存操作者与不可变来源；同名竞争和幂等响应采用同一事务。未决写入的检查点及后代拒绝 Fork，可信清理的容器丢弃点需核验精确停止证明。
- 只读 Checkout、分支列表和历史游标、单父共同祖先，以及 JSON Pointer 的字段/数组/产物/调用差异。A/B 分支可独立提交同名文件和计划，原 HEAD 不变。
- HTTP/CLI 共享服务、认证/项目边界和幂等语义；新增迁移 `0010_branch_forks`，历史 Schema 保持冻结。
- `RecordingReader` 校验来源路径、环境、事件边界、occurrence、参数/签名/响应和对象所有权。执行录制额外核验输入/策略/代码/命令/日志/输出 manifest，支持追溯恢复复用的原物理调用。
- `RecordedAdapter` 无网络/模型/容器客户端，只按顺序消费完整录制；同参数的不同 occurrence 保持不同响应；错配、缺数据和未消费项均失败，无 Live 回退。

## 验证范围

基础用例涵盖字段删除与 null 的区别、布尔与整数的区别、JSON Pointer 转义、数组重排、二进制产物、无共同祖先、分页、双分支隔离、同名竞争、跨项目拒绝，以及 Fork/来源/幂等响应故障回滚。

录制用例覆盖同参数不同响应、无网络消费、零长度范围、缺响应、环境不匹配、真实 fake 外部写录制读取不再访问服务。真实五工具任务录制后禁止构造 `DockerEngine`/`SandboxContainer`，验证全部调用引用；删除响应或输出文件须失败。另覆盖容器失败可信丢弃点的安全 Fork。

以上是 `0c5bf32` 的版本与录制基础，单独不能证明 Runtime 重算后的状态一致；随后新增的 Runtime 重算见下节。

2026-10-21 基础批次本机验证（Windows、Python 3.12.14、PostgreSQL 17.6、Docker Desktop Linux）：该批共收集 434 项，采用下列有重叠的分批执行覆盖，不声称是一次 434 项完整运行。

| 检查 | 实际结果 | 本机原始记录（不提交） |
|---|---|---|
| 单元 + PostgreSQL 集成全量 | 337 通过，370.20 秒；此后新增 4 项并补充录制边界检查 | `.agentgit/evidence/S5-versions-core.xml` |
| Sandbox + 执行全量 | 首轮 92 通过、1 失败；失败为租约接管测试的一次性领取断言 | `.agentgit/evidence/S5-versions-execution.xml` |
| 最终版本/录制/Jobs/Adapter/Diff | 60 通过，97.09 秒；含新增累计大小/事件上限、缺 Commit、显式 Job 行锁竞争 | `.agentgit/evidence/S5-versions-core-final.xml` |
| 最终真实执行录制 + 执行服务 | 28 通过，152.28 秒；含租约接管与旧写者拒绝 | `.agentgit/evidence/S5-versions-execution-final.xml` |
| Ruff、格式、mypy、sdist/wheel | 全通过；68 个源码文件，新版本/录制模块已进入 wheel | `.agentgit/packages/` |
| 实际 CLI + 开发库升级 | Schema head=`0010_branch_forks`；读取真实五工具 Run 的 29 个检查点，创建 A/B；幂等响应一致，源历史不变，111 项 Diff 正确分页 | `.agentgit/evidence/S5-versions-cli-smoke.json` |

首轮接管失败的原因：`claim()` 正确使用 `SKIP LOCKED`，与旧执行线程的短事务竞争时允许暂时返回空。测试现采用 3 秒有界等待，并增加显式持锁时不领取、释放后可领取的真实 PostgreSQL 用例；租约/fencing 生产逻辑没有放宽。最终回归覆盖该失败路径。录制累计内容限制为 64 MiB、事件最多 50000，超限明确失败；避免按不可信 seq 跨度创建巨大列表。

已升级本机开发库；未向远程推送，未执行远程 CI，不把本机结果视为原生 Linux 或远程 CI 验收。测试中现有 Starlette/httpx 弃用警告保留，未为此增加依赖。

## Runtime 状态与产物重算

`RecordedReplayer` 直接使用原 `ResearchRuntime`，只有起点来自恢复快照。随后每个状态转换均由录制调用结果驱动，使用独立 `MemoryWorkspace` 重建产物，并同时对比快照摘要与来源 Commit 的 `state_ref`；成功重算后才通知检查点回调。回调可供后续耐久 Job 保存新 Commit 和 provenance，目前尚未接通持久 Replay。

实际模型、工具、网络、宿主工作区/进程和容器入口均不调用；`ReplayResult` 分别返回历史逻辑用量与实际零调用事实。执行输入 manifest 根据内存文件重新计算，Shell/Python 输出从已验证的 CAS manifest 重建。中间终点立即停止，不继续运行至原终态；零长度范围只恢复起点。

当前代码/Python/依赖锁/模型/工具签名/Prompt、固定语料和输入均须兼容；改配置或环境先拒绝。初始/中间快照、录制响应被改或缺调用也拒绝。支持普通暂停/恢复、取消、步骤/工具预算、持久截止时间和当前版本墙钟停止事件；实际回放耗时独立于逻辑时间。恢复来源的已保存结果与停止丢弃见后续专项；其他未知结果核对路径仍需验证。

最终专项：17 个 PostgreSQL Runtime 用例通过，包含完整 20 次调用、逐检查点摘要、四种起止范围、起点/中间状态错配、缺数据、环境兼容性和六种控制/预算边界；另 2 个真实执行用例通过，完整五工具录制重算 28 次调用/28 个后续检查点，所有状态/产物摘要一致，缺输出对象返回 `recording_missing`。二者在禁止真实 Adapter/网络/容器/宿主进程的条件下执行；固定镜像源码与系统包核验仍通过。

本批最终回归：单元全量及 research/Recorded/录制读取/版本入口专项共 **213 通过**，149.05 秒，记录在 `.agentgit/evidence/S5-runtime-core.xml`；真实五工具重算最终 **2 通过**，40.80 秒，记录在 `.agentgit/evidence/S5-runtime-execution-final.xml`。Ruff、131 个文件格式检查、mypy 的 70 个源码文件及 sdist/wheel 构建通过。当前全仓共收集 **453 项**；沿用基础批次未受影响的验证结果，不声称这次运行了全部 453 项。镜像未改变；无遗留受管执行容器，未推送或运行远程 CI。

后续恢复专项：录制额外保留已验证 `call.reused` 事实；纯 Runtime 的缓存消费入口据此先结算已有完整结果，再处理取消/到期，普通录制仍走常规调用入口。真实执行在结果落库后、观察前崩溃，生成恢复 Run；正常继续、取消和到期三种来源全部离线逐检查点重算通过，**3 通过**、44.00 秒，记录 `.agentgit/evidence/S5-runtime-recovery.xml`。没有真实模型/工具/网络/宿主进程/工作区/容器初始化。通用 Adapter/录制读取/Runtime 专项最终 **39 通过**、93.80 秒，记录 `.agentgit/evidence/S5-runtime-reuse-core.xml`；静态检查、构建通过，全仓现收集 456 项。

## 停止丢弃来源重算

`RecordingReader` 对每个 `sandbox.execution_discarded` 核验独立检查点、原调用与物理执行身份、同一来源链、未伪造结果、固定输入/策略/镜像、唯一且已确认的清理证明及当前来源 Run 的丢弃引用。证明必须对应容器名称/ID、旧 fencing token 和更大的观察 token；清理标记必须为布尔值，预算必须为精确整数。全部核验后才提供 `RecordedDiscard`。

`rebuild_discard` 从原调用前状态重算，或精确归一化已计入预算的 `outcome_unknown` 状态；拒绝轨迹、计数、attempt、输入漂移及重复证书。重算保留已有未知调用轨迹，旧尝试只扣一次；后续工具调用严格消费新 attempt 的独立录制。不会为旧调用补响应，不启动容器或检查真实引擎状态。

首轮全单元 **180 通过**、11.38 秒（`.agentgit/evidence/S5-discard-unit.xml`）；真实恢复专项 **7 通过**、100.98 秒（`.agentgit/evidence/S5-runtime-discard.xml`），覆盖已保存结果三种走向及丢弃后的继续/取消/到期/工具预算不足。另一个真实 `outcome_unknown` 起点用例 **1 通过**、22.57 秒（`.agentgit/evidence/S5-runtime-unknown-discard.xml`），缺清理证明与替换输入证书均明确失败。

最终扩大回归：单元全量与 research/Recorded/读取器/版本入口共 **223 通过**、167.70 秒（`.agentgit/evidence/S5-discard-core-final.xml`）；真实恢复、完整执行重算、资产读取与安全 Fork 共 **15 通过**、272.42 秒（`.agentgit/evidence/S5-discard-execution-final.xml`）。其中连续两次结果丢失达到策略上限的来源也重算一致：保留两张停止证书、正确旧尝试计量，以预算耗尽结束，无第三次尝试。Ruff、134 个文件格式、mypy 的 71 个源码文件和 sdist/wheel 构建通过。全仓当前共收集 **472 项**，本批复用此前未受影响的验证，不声称一次执行全部 472 项。

## 尚待完成

1. 补全恢复专用来源路径的逻辑状态重算，并在持久任务中分离逻辑用量与新运行的实际计量。
2. 将 Recorded/Live 作为耐久任务接入 Worker、HTTP/CLI，保存来源及范围；Recorded 禁止 override，禁止真实客户端或执行后端初始化，验证崩溃接管仍可安全重算。
3. Live 从指定检查点建立独立工作区，验证配置/计划/参数变化，启用新预算、独立执行容器和重新授权；不继承源调用结果或审批。
4. 完成 S5-T01–T09 的端到端验收，特别是完整断网重算、执行工具 Recorded 零启动和 A/B Live 代码/同名产物隔离。
