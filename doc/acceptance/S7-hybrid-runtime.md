# S7 Hybrid 持久运行增量

2026-10-24 按实施方案 S7 第 4 步接通 Hybrid。S6/D1 的环境和原生专项继续按用户授权延后；统计和桌面实验列表仍待完成，S7 未整体验收。

## 实现与边界

实验请求增加可选 `hybrid`，包含同一来源范围的终点、显式调用 ID 和 `stop/fixture` miss 策略。提交时从完整录制验证并冻结所选只读调用、原请求/响应、替换响应、语料与验收器摘要；每个 Job 登记自身输入引用。模型、写操作、起点前调用、跨项目调用、截断或未知响应不能成为选定录制。

`HybridReplaySpec`、Run 及 replay request 都保存真实 hybrid 模式；新增 `0015_hybrid_observations`，不回改已提交迁移。网关仍检查权限、控制、租约与 HEAD：确定性选定观测在同一短事务保存新调用、开始/完成事件、结果及不可变来源账本；真正的 Adapter 调用继续在事务之外执行。恢复专用 `origin_call_id` 的含义不变，Hybrid 来源另存 `source_call_id`。

命中要求 occurrence、provider/签名、请求字节、效果和响应界限全部匹配。改参后按冻结策略停止或从固定语料生成新观测；工具/签名/效果改变不能借模拟器绕过，缺失/损坏/越权和未知输入直接拒绝。`recorded/synthetic/fixture/miss` 分开记录，均标记 `test_only`。miss 是已知匹配/模拟失败的观测，不是未知动作或成功替代。

Runtime 在最终验收前检查选定依赖是否到达，未到达不能先宣告 completed。结果提交后崩溃恢复消费既有录制，不再次解析选定响应，不重置截止时间。取消、预算不足和未知结果仍反映真实 Job 状态。

实际模型/工具次数排除选定观测及恢复消费；录制、synthetic、fixture 和 miss 次数单列。逻辑用量保留 Runtime 的消费和预算差额，不与物理调用混淆。Hybrid policy 进入受控环境输入，Recorded 从 trial 自己的初始化检查点消费本次 tape；复合 Recorded 继续保留 policy、synthetic/test_only 来源。Recorded 不调用模型、工具或 Hybrid 模拟器。

## 当前验证

修复后的最终复验 **756 项全部通过，无失败或跳过**：397 单元、342 集成（含真实 PostgreSQL、独立 TCP/CLI 进程）及 17 项真实 Docker 执行交叉验证。四个不重叠组分别通过 295、89、247、125 项，耗时分别为 716.62s、706.92s、648.34s、784.04s；全部绑定下述修复后源码，不累计早期专项。最终清理复核没有残留 AgentGit 托管执行容器。

以下过程性专项用于说明验证顺序与发现的问题，最终用例均已在四组中重新运行。

前期源码下的控制/恢复专项 **27 项通过，340.31s**：Hybrid 流程、既有 Live 实验 Runner 与迁移往返。另有选择/SQL 约束专项 **5 项通过，60.37s**，包含命中场景的重复验证，两者不能直接相加作为去重总数。新增用例明确覆盖：

- 改参 stop/fixture、未到达依赖、synthetic 来源和逻辑/实际调用差异。
- 原子观测落盘后中断、同 Job 新 Run 接管、保留截止时间与只产生一次原始观测。
- 取消、预算耗尽、未知模型结果及禁止自动重发；这些状态不可计为通过。
- 原失败报告保留，A 消费失效观测仍失败，B 消费明确的 synthetic 修正后通过；后续 Recorded 与双层 Recorded 可验证。
- 多条成功/失败终态的 Recorded 零调用复验；冻结请求对象缺失时拒绝复验，不重新访问外界。
- SQL 拒绝观测更新、删除和跨项目插入；非法录制选择整体回滚，不留下部分 trial。

真实 TCP/独立进程专项 **2 项通过，132.12s**。分别对 Live 和 Hybrid 提交 A/B 各五次：退出首个 API 服务后，以十个独立 CLI Worker 运行，重启 API 后与 CLI 查询一致、来源分支不变。Hybrid 每个 trial 为 9 次模型、7 次真实工具、1 个录制观测；两组各 5/5 通过。相同 Mock 的通过率不证明真实模型稳定性或干预效果。

真实 Docker 执行回归 **16 项通过，301.18s**，范围为 `test_agent_flow.py`、`test_live_execution.py`、`test_recorded_runtime.py`、`test_compound_recorded_execution.py`。覆盖五工具、真实取消/期限恢复、失败输出保护、Live 的终端/Python 独立容器与 Recorded/复合 Recorded。

新增 `test_hybrid_execution.py` **1 项通过，57.61s**：完整五工具来源后，A/B 各一个 Hybrid trial；每次为 13 个模型、11 个真实工具调用和 1 个录制观测。两个 trial 合计创建四个互不相同的真实容器，固定镜像、执行完成与清理确认记录可核对。随后禁用 socket、进程创建、容器、真实 Adapter 和 Hybrid 模拟器，独立重算两条 Recorded，均以零实际调用通过。该测试首次调用独立重算器时遗漏必需的冻结预算基线，被环境兼容性校验正确拒绝；补齐测试参数后完整重跑，未放宽产品校验。

扩大后端回归首次得到 **667 通过、1 失败**，发现新检查错误地要求所有来源输入为 JSON，破坏通用录制允许不透明字节的既有契约。修复为：先验证 CAS 完整性；通用来源保留不透明输入兼容，Hybrid 来源继续严格解析并核对 policy。缺失/损坏对象不进入兼容分支。修复后录制读取专项 **9 项通过，7.89s**，包含 Hybrid 禁止借用兼容路径的反向检查。

四组结果已按完整 testcase 身份去重，恰好覆盖最终收集的全部 756 项。此前 667 项通过和各专项不计入该总数。

## 证据与后续

原始证据位于本机 `.agentgit/evidence/`。最终源码为 `source-sha256:b80e6ee5bca4306f1a405ef6272f4e0d5ba3b64c43cf56d4ae731a3473b35506`；最终冻结验收器、两种模式的十 trial 明细及四组 XML 均在此版本生成。需求设计/实施方案、DESIGN 和 uv.lock 摘要核对未变，详细摘要与完整去重用例列表保存在汇总 JSON。

| 最终证据 | SHA256 |
|---|---|
| S7-hybrid-final-1.xml | `f9dfb395556550db2baba8e90707d788f6ba60b714794495b80bab365c5d34f7` |
| S7-hybrid-final-2.xml | `caabd4dd9751200729c4e0a6a8bdcaa519326c5881d863d0a94bf9c063574e91` |
| S7-hybrid-final-3.xml | `00b6c8861098993e76bd845c60f82cca73f0558e1b558e2cf843a432ffe822c4` |
| S7-hybrid-final-4.xml | `5c2c25845a9093004c591498da4a9e9b69995e4c516caac083e779010e005105` |
| S7-hybrid-summary.json | `e91fe39504aa8b46531b9efebf8e00a8665be8e2069862d95d1d352d910e8fc0` |
| S7-final-process-live-audit.json | `e3bdecfe426b7f6bbda906cfa28acd5d1a01c56832e04250c6dec63b5cc43efe` |
| S7-final-process-hybrid-audit.json | `e0e365553f3049f31f8f314f5a1864a476b71665514e8edb2cf4fae30b1f281e` |
| S7-final-validator.json | `0e9219e3b6fd089243d523d8032604c9cacf2d8acf26fb414f769b015c48b08a` |
| S7-hybrid-final-cleanup.json | `09fe3c79d72c4e18ea3344dcdf81845b0168d9b0166e875266b570286b5cb96e` |

下表仅为修复前源码 `source-sha256:b46b97a86cd00b8c84302497f225512bfb73dc6d509b6e5be7572175b78627bc` 的历史过程记录，不替代最终验证：

| 文件 | SHA256 |
|---|---|
| S7-hybrid-runtime-control.xml | `a845667b45ff8b4140879e910436e439b52ae35ad919a583095077e7cf911d6b` |
| S7-hybrid-selection.xml | `7b0d611580227c3bb1fb83c553b64ee17ff515eabd7936b08d55198308e25d6c` |
| S7-hybrid-processes.xml | `2ce44b7d6358a8ed38502d0f41fde8dd03857252f68e959011a87b0416d80277` |
| S7-process-live-audit.json | `08a69159684390b473ea21273f488718d8fa986faef7e9ca8f29944536f28d8d` |
| S7-process-hybrid-audit.json | `714e94ba7fbacee1d8df6a07a09017d94180edadd51a8828877439a501311cac` |
| S7-hybrid-execution.xml | `c4c09d4294f4eca3490e095f4f4a9d2bcf114fac56f62182da4bd1529f9da154` |
| S7-hybrid-real-containers-final.xml | `bcff89e6b68d41c90289b5767341b67ea6272adf9f5834169f01487b5fc7ccb2` |

静态检查：Ruff 检查、202 文件格式检查、103 个产品源文件的严格 mypy 及 `git diff --check` 通过。现有 TestClient/httpx 弃用提示保留，未因此新增依赖。迁移仅操作独立测试 schema；未自动升级业务库，未重建 S6 桌面包，未调用付费模型。

下一步补齐成功率/硬断言、Token、时延、费用和方差统计，以及 Branch Experiment 的 manifest/分组/trial/控制交互，再逐项核对 S7-T01–T06。synthetic 成功只能证明冻结试验条件下的结果，不能提供“已修复真实外部世界”的结论。
