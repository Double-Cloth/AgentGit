# S10 实际独立确认与固定回归增量

分支 `codex/s10-repair`，接续预注册/开启提交 `c25df52`。本增量把已开启的十张票据接到真实独立 Job/Run、原始调用来源核对、共享预算、物理结算与不可变统计摘要。仍不宣告 S10、D2、正式配置采纳或完整项目完成。

## 当前行为

`RepairConfirmationService` 只领取拥有不可变开启绑定的 study；缩减控制器排除这类任务，避免在待核对/取消状态互相领取。确认串行执行六项，随后执行四个预注册回归任务，所有结果保留。即使确认不足门槛，已知且无安全违规时仍执行固定回归；未知效果/用量、取消、安全和预算保持各自停止边界。

每项意图绑定唯一 sample、完整计划、最终候选锁、冻结执行投影和预算。Prompt/Plan 仅投影已锁定的确切内容，不生成新补丁或重新排序；目标计划不兼容时停为待处理。工具观测替换只在原来源的 Hybrid 条件生效，其他任务明确记录 `outside_source` 与不适用原子，保持 Live 回归，不能借新任务改写历史观测。观测补丁即使 fixture 验证通过也不具备正式配置采纳资格。

`RepairConfirmationTrialService` 在同一短事务发布普通研究 Job 并绑定独立初始 Run；实际模型与工具仍由普通 `worker` 执行。票据的输入、语料、模型参数、权限、代码/依赖、起点与固定验收器均核对。新的实际模型调用必须匹配这些条件；恢复只能消费该 Job 自身恢复链产生的调用，不能借用筛选、缩减、其他确认或回归的模型响应。原始请求/响应通过不可变调用引用保留，相同文本仍计为不同的实际调用，不按输出筛样。

收据来自实际 Job 终态、当前 Run、固定验收、Replay 报告及完整原始调用来源证明。零新模型调用或只重验已完成输出不能形成确定确认结果；执行错误不能当成允许的那一次硬失败。六次完整硬验收至少五次通过、四项回归全部硬通过、安全零违规、用量完整才可封存 `verified_under_fixture`；真实模型路径用 `empirically_confirmed` 并保留经验限制，本轮未实测付费供应商。

所有阶段仍共用原 S9 次数、实际调用/字节/费用和最初墙钟。未知继续占完整试验预留，核对后原子写入实际收据、统计证据、调用来源引用、计量和审计；重复结算不再次调用模型或扣账。取消的未派发意图只有证明没有 Job 才按 `not_started` 释放零物理用量，已派发项须由普通 Worker 应用控制并结算。已预留但尚未派发的第六次确认在重启后仍属于确认阶段，不能因计数达到六而提前转回归。

封存同时写入统计摘要/事件和终态控制；终态墙钟冻结。报告分别展示 confirm/regression 范围的统计成本和包含原 screen/reduce 的全流程用量。批准仍为 `adoption=not_reviewed`，本增量不创建正式 Agent 配置或启动采纳动作。

## 入口与存储

在原 `repair-validation-open` 成功后，启动 `agentgit repair-confirm-worker --repository-id ...` 和普通 `agentgit worker`，两者可用 `--once`。现有 `GET /repair-studies/{id}/validation`、`repair-validation-status` 返回不可变开启记录及当前 `execution`：实际 trial、投影、收据、来源引用、共享用量、暂定统计和最终封存统计。开启记录的 `execution_status=not_started` 是开启时的历史事实，当前状态以 `execution.status` 为准。查询及控制不会调用模型/工具，不接收用户上传的 passed 或成功回执。

新迁移 `0021_repair_confirmation_jobs` 增加五张不可变验证执行/摘要表；新 metadata 快照扩展控制状态，既有迁移与冻结快照保持不变。数据库约束正确样本/阶段/计划、独立派发、终态结算、空取消证明、摘要/审计和完整门槛。仅在自建测试 Schema 演练，业务库未迁移；旧代码来源只读保留。

## 实测范围

2026-10-25 至 2026-10-26 核对，最终源码 `source-sha256:6d5cc94ae6ecc54c24a90f9de0a68fe556d1f9daf421fb56cea20c860a71961f`。734 项不同用例通过：656 单元、24 项本增量真实集成和 54 项迁移/既有派发/缩减/确认池/Recorded/Live 回归，无失败、错误或跳过。不同源码的早期报告不混入本次计数。执行期间包源码、`uv.lock` 和正在使用的测试适配器保持冻结，提交前已按原字节核对暂存区与工作区。

环境为 Windows `10.0.26300.0`、Python `3.12.14`、uv `0.12.19`、PostgreSQL `17.6`；数据库容器使用 Compose 中固定的 `postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3`。只迁移/回退各用例自己的测试 Schema，未迁移业务库。设计、实施方案及 `uv.lock` 摘要与前增量一致。

`ruff check` 通过，`ruff format --check` 核对 311 文件，`mypy src` 核对 149 源文件，Git 空白检查通过。全部 unit/integration 收集 1,192 项成功；收集不计作执行通过。FastAPI TestClient 保留上游 Starlette/httpx 弃用提示，未为消除提示变更依赖。

最终 JUnit 均位于忽略的 `.agentgit/evidence/`，以类名和参数化用例名去重：

| 报告 | 范围 | 通过 |
|---|---|---:|
| `S10-confirmation-core-v5.xml` | 全部单元；实际确认；三种固定失败向量；完整硬验收的数据库门槛 | 661 |
| `S10-confirmation-controls-v5.xml` | 派发/结算事务中断、未知用量、租约、取消、第六次确认恢复、预算、安全与普通 Worker 恢复 | 11 |
| `S10-confirmation-boundaries-v5.xml` | 联合完整 CAS、独立进程退出、公共 HTTP/CLI 与 Hybrid 六加四 | 4 |
| `S10-confirmation-shared-v7.xml` | 迁移往返、旧派发、Hybrid 模板及搜索 API | 20 |
| `S10-confirmation-studies-v7.xml` | 旧缩减与边界 | 16 |
| `S10-confirmation-pools-v7.xml` | 预注册、开启与资源/输入限制 | 13 |
| `S10-confirmation-corpus-and-variants-v5.xml` | 两个旧自定义语料拒绝入口；四个语义不同的真实回归 | 3 |
| `S10-confirmation-replay-regression-v5.xml` | Recorded 独立任务/预算与 Live 恢复/部分范围 | 5 |
| `S10-confirmation-unknown-v7.xml` | 实际未知模型请求在重启和取消后不重发 | 1 |

上一轮旧流程回归没有最终报告且进程/句柄已不存在，未计入通过。2026-10-26 补跑 v6 时 Docker 引擎及 PostgreSQL 尚未启动，三个首例均在连接前报错；错误 XML 保留。恢复既有本地依赖、测试库诊断通过后补跑 v7，49 项全部通过；没有改测试条件、重置数据库或将设施错误计作成功。

固定失败向量在筛选前把策略及测试适配器字节摘要写入任务输入，实际 Worker 重新产生和保存响应：5/6 + 4/4 达标，4/6 + 4/4 拒绝，6/6 + 3/4 拒绝。它们是受控故障向量，不冒充随机分布上的算法效果验证。另有单 trial 仅允许两次模型调用的实际预算案例：确认可完成，而完整回归停止为不完整，最终 `needs_review`，没有扩额。

完整联合案例保留两原子，使用六个新确认 Run 和四个不同任务的新回归 Run；从 CAS 原字节重新核算每次模型请求/响应 UTF-8 长度、独立调用 ID、候选/计划/样本映射与全流程总数。Hybrid 案例六项确认各消费一条固定历史只读观测，模型继续实际调用，四项回归为 Live；历史/逻辑工具计量与本次实际调用分开，不据此声明外部工具世界独立或真实线上稳定。

完整归档包含源搜索、缩减、全部验证/池/开启/锁/审计/Job/Replay 索引和原始 CAS；reference 仍是来源存档别名，不能算独立对照。四个回归准备 Run 是固定资产来源，不算应用最终候选后的回归成功。旧 0.1.5 桌面包不包含本增量。

另外单独执行四个语义不同的固定回归：框架顺序反转、修改结构化事实及其来源引用、外部资料加入不可信指令、固定时钟前移一秒。它们各自由持久 Job 冻结，生成四个不同语料/时钟身份；确认控制器再应用同一锁定候选，实际完成六加四。此例是工程边界验证，不作为 30 例研究数据或随机总体效果。

新增边界覆盖如下：

| 场景 | 核对内容 |
|---|---|
| 物理派发/结算中断 | Job 创建事务内与提交后、结果提交前后中断；重试复用原绑定和收据 |
| 租约/独立进程恢复 | 已预留第六次确认不跳阶段；子进程真实退出后不重复派发；普通 Worker 恢复只消费该 Job 自己的已保存响应 |
| 未知/取消 | 模型实际返回 `outcome_unknown` 后，重启和取消均不重发、不结算成功、不释放预留；另测物理用量不明、陈旧等待及未派发/已派发取消 |
| 安全停止 | 回归模型尝试未授权终端工具，在 Adapter 调用前拒绝；保留实际模型费用并停止后续回归 |
| 不完整确认 | 五次通过加一次执行错误、四回归通过仍为 `needs_review`；数据库也拒绝直接提升为 verified |
| 公共边界 | HTTP/CLI/只读查询不调用 Adapter、不接受 passed 字段，跨项目不可访问，验证记录不可改删 |
| 回放输入一致性 | 自定义事实/时钟由实际 Job 保存；缺少一致冻结输入的旧来源在 Replay 和预注册两个入口均拒绝 |

本轮实际复现并修复三个生产问题：新迁移中的 SQL `CASE` 比较语法；数据库只检查通过数而遗漏一次不完整确认；旧自定义语料在缺少 Job 时静默回退到 bundled。初次失败报告保留，修复后使用最终源码重跑对应边界。测试辅助函数的参数名冲突也已修正，不把该测试错误算作产品缺陷。

四份最终源码归档均为确认 6/6、回归 4/4，确认 95% Wilson 区间为 `[0.609665712097834617, 1.000000000000000000]`；区间下界不作为 80% 的另一个门槛。各自使用独立测试项目，金额均为 Mock 的零费用：

| 实际案例 | 全流程模型/工具调用 | 全流程 Mock 字节 | 其中确认与回归 Mock 字节 |
|---|---:|---:|---:|
| 单 Prompt | 60 / 44 | 666,434 | 597,352 |
| 两原子联合 | 73 / 57 | 878,854 | 655,026 |
| Hybrid 确认、Live 回归 | 76 / 52 | 864,928 | 746,550 |
| 四个语义不同回归 | 73 / 57 | 880,686 | 656,858 |

这里的“全流程”包含对应 S9 筛选、S10 缩减和实际验证。固定回归资产的准备运行单列为来源，未纳入此表，也不作为应用候选后的回归结果。Mock UTF-8 字节不换算成供应商 Token。各 trial 的独立 Job/Run、预算、原始请求/响应及统计映射均可从归档重新核对。

四份归档为 `S10-confirmation-real-v5.json`、`S10-confirmation-full-v5.json`、`S10-confirmation-hybrid-v5.json` 和 `S10-confirmation-variants-v2.json`。汇总核验重算 5,775 条对象记录、13,782,816 原始字节，跨包去重 4,914 对象，检查每个对象的类型域摘要、长度、归属、实际 Job 输入/语料/时钟和每次调用来源，再独立重算用量与统计。

汇总为 `.agentgit/evidence/S10-confirmation-summary-v7.json`，SHA-256 `0f88578430f0b4b46b9989f1603364c61d4d6e4088af53d2684729833652c783`；保存逐报告、逐归档及迁移/测试适配器/规范摘要和静态检查结果。核验脚本为同目录 `verify_s10_confirmation_v7.py`，其自身摘要亦收入汇总。原始轨迹保持本地，不提交 Git。

## 复现

在已准备的独立测试数据库执行下面 734 项范围；测试适配器的字节摘要写入冻结输入，运行中不得改源码/锁文件/这些测试。可省略四个环境变量而仅运行断言；需另存原始归档时，先指定四个新的文件名，已有文件不会被覆盖。

```powershell
$cases = @(
  'tests/unit',
  'tests/integration/test_kernel_migrations.py',
  'tests/integration/test_repair_trials.py',
  'tests/integration/test_repair_template_hybrid.py',
  'tests/integration/test_repair_api.py',
  'tests/integration/test_repair_studies.py',
  'tests/integration/test_repair_study_boundaries.py',
  'tests/integration/test_repair_pools.py',
  'tests/integration/test_repair_pool_registration.py',
  'tests/integration/test_repair_frozen_corpus_boundary.py',
  'tests/integration/test_replay_jobs.py::test_recorded_job_enqueues_without_execution_then_worker_proves_all_hashes',
  'tests/integration/test_replay_jobs.py::test_two_recorded_jobs_share_content_but_keep_separate_budget_and_proofs',
  'tests/integration/test_live_replay.py::test_live_recovery_keeps_baseline_and_supports_recorded_of_its_new_history',
  'tests/integration/test_live_replay.py::test_recorded_partial_live_range_reports_remaining_quota_against_frozen_baseline'
)
$cases += @(Get-ChildItem -LiteralPath tests/integration -Filter 'test_repair_confirmation*.py' | ForEach-Object { $_.FullName })
uv run --locked pytest @cases --junitxml=.agentgit/evidence/S10-confirmation-rerun.xml
```

原始归档的环境变量分别为 `AGENTGIT_S10_CONFIRMATION_EVIDENCE`、`AGENTGIT_S10_CONFIRMATION_FULL_EVIDENCE`、`AGENTGIT_S10_HYBRID_CONFIRMATION_EVIDENCE` 和 `AGENTGIT_S10_CONFIRMATION_VARIANTS_EVIDENCE`。实际确认使用 Mock 研究 Worker，不需要付费模型，也不执行宿主 Shell/Python。

## 剩余必选工作

真实 A+B+C 冗余删除、预定义字段缩减、真正随机响应池的偶然筛选通过案例、观测伪修复的完整实测与采纳限制、精确提案/版本绑定审批、Repair Lab、30 例及四基线、D2 和新 Windows 包仍待完成。S6/D1 独立 Windows/原生及 S8 人工标签继续按用户指示暂缓，S8 主要 Top-3 4/8 未达标的事实保持。完整目标还包括 S11 和 S12-A/B/C/D，不以本次真实验证闭环替代全部交付，不合并 main。
