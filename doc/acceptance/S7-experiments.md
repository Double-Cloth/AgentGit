# S7 实验调度与入口增量

基于 `f3fcb57` 的冻结验收器，继续实现实验数据、Live Runner 接线与 API/CLI。S7 整体仍未验收；S6 独立环境/原生专项按用户授权延后，要求保持不变。

## 实现与约束

- 新迁移 `0013_branch_experiments` 保存不可变 experiment/trial 归属，分组、来源状态/环境/配置与验收器绑定在 CAS manifest。每次试验建立独立配置、Task、Job、Run、分支及租约工作区；相同内容的只读输入允许 CAS 去重，不能据此共享可写工作区或授权。
- 默认 A/B 各 5 次，显式次数各 1–10，预算相同且不得超过来源授权。当前只开放分组固定 Prompt 与完整计划；模型、工具、执行策略、validator、split 和预算不能被分组改写。已有完成节点、DAG 与工具权限检查继续生效。
- 所有 trial 与幂等响应在同一事务提交。部分排队失败整体回滚，重试同一幂等键可重新完成；并发同键只产生一个实验。同键异参和跨项目请求拒绝。
- 运行、暂停、取消、截止时间、租约和崩溃恢复复用现有 Job/Worker。trial 固定关联同一 Job，保留初始 Run；恢复后显示该 Job 当前 Run，不重复创建 trial、不重置截止时间。未完成状态不能通过，completed 仍须独立复验历史产物。
- 查询直接投影真实 Job/Run，读取期间状态变化有界重读，不能将旧 Run 状态和新 Run 结果拼接；持续变化返回可重试的版本冲突。
- 冻结源输入与语料摘要需复核。来源仅保留自定义语料摘要、又没有可恢复的冻结 Job 时拒绝，禁止默默改用默认语料。
- 新增 `POST/GET /validators`、`POST/GET /experiments` 及 `freeze-validator/experiment/experiment-status` CLI。固定规则只能从明确归属的历史输入创建；不接受任意 validator 代码/规则，不开放凭 hash 读取其他项目对象。提交仅排队，执行仍在独立 Worker。

## 运行证据

独立进程验证 **1 项通过，46.54s**：真实 TCP API 冻结来源输入，独立 CLI 提交实验；HTTP 用同键读取相同幂等响应，确认十个 Job 尚未执行。退出 API 后，由十次各自独立的 CLI Worker 进程完成试验；再启动全新 API 进程，HTTP 与独立 CLI 查询得到相同终态。两组各 5/5 通过，恰有十个独立 Run，本次实际模型调用共 30 次、工具调用共 20 次、未知结果为零，来源分支未改变。

当前代码扩大回归 **462 项全部通过，515.51s**：372 单元 + 90 真实 PostgreSQL 集成，无失败或跳过。与上述独立进程用例无重叠，合计 **463 项通过**。先前 71 项专项通过是增加来源摘要交叉核验前的增量证据，不重复相加。

回归范围为 `tests/unit` 及 `test_experiment_runner.py`、`test_experiment_entrypoints.py`、`test_validator_service.py`、`test_kernel_migrations.py`、`test_replay_jobs.py`、`test_live_replay.py`、`test_recorded_replayer.py`、`test_management_api.py`、`test_management_cli.py`。独立进程用例另在 `test_experiment_processes.py` 执行。当前迁移的空库往返/正式回退保护、既有 Recorded/Live 和管理入口均在此次范围内；其余集成、Docker 沙箱及桌面套件未重跑，不计入此次结果。现有 FastAPI/Starlette TestClient 的 httpx 弃用告警保留，没有因此增加依赖。

全项目 Ruff、格式检查、严格 mypy（96 个产品源文件）及 `git diff --check` 通过。

进程验证的冻结代码版本为 `source-sha256:81e4efe92f831fd2921bf77a00848776b5640093a0950f38a2e7c8b83771ac30`，已从该次临时 CAS 提取 validator 并与当前源码重新核对；依赖锁摘要仍为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。

| 原始证据（忽略目录 `.agentgit/evidence/`） | SHA256 |
|---|---|
| S7-experiment-regression.xml | `54c6c53d18938d03986c6fc9cd597fe0565e140574ed5aa4f72080d941912115` |
| S7-experiment-processes.xml | `85de0de326482d9d299ade839920bd1800b54e9a48faa5bcc60ad559409e691d` |
| S7-experiment-process-audit.json | `0c6962f15c8583f72191c9e2bd85b63e29d445385cb990fd5013dd2c7d312e82` |
| S7-experiment-validator.json | `d84ec42bc541e1a94f0af0e156655e3eeaf5dace1abbbb38e4d53122286e1d96` |

专项覆盖真实 PostgreSQL 中的 A/B 各五次、10 个独立 Run/Job、共同起点和输入摘要、配置/环境/预算/工作区隔离、物理模型/工具调用及零历史调用复用。B 的固定 Prompt 与剩余计划确实进入各自新运行；此处采用确定性 Mock，两组都通过，不能解释为 Prompt 提升了效果。

同时验证预算不足、取消、暂停恢复、调用结果落盘后 Worker 中断与新 Run 接管、原子回滚/同键重试、并发同键去重、SQL 不可变与分组归属约束，以及 API/CLI 的幂等一致性、项目/认证边界和 1 MiB 输入限制。重复实验对照新增逻辑用量和实际调用向量，不要求实际墙钟耗时相等。

## 阶段门槛核对

| 要求 | 当前证据范围 | 未完成部分 |
|---|---|---|
| S7-T01 | Live A/B 各五次的真实数据库、Run/Job 与工作区隔离 | Hybrid/DSL 接入后的整体验证仍需补齐 |
| S7-T02 | 固定验收器与分组白名单、split/权限/模型/预算越界拒绝 | 完整干预 DSL 的目标事件/前置条件与越界用例 |
| S7-T03 | Recorded 语义保持不变 | Hybrid 精确命中与冻结 miss 策略 |
| S7-T04 | 当前固定语料结果标记 test_only | 工具观测替换、synthetic observation 与来源链 |
| S7-T05 | Live 预算、取消、暂停、崩溃恢复及真实状态投影 | Hybrid 下的控制、未知结果与恢复交叉用例 |
| S7-T06 | 确定性 fixture 重跑的逻辑/实际调用向量一致，沿用实际/逻辑分列 | 完整指标聚合、方差、费用未知及不确定性报告 |

目标事件干预 DSL、Hybrid、工具响应替换、完整统计报告及 Branch Experiment 桌面列表继续推进；不得以本增量替代 S7 退出验收。当前未扩大桌面 IPC、未重建 S6 0.1.3 包，也未运行付费供应商冒烟。

测试迁移只在独立测试 schema 内往返，没有修改用户业务库；当前代码的数据库 head 已是 `0013_branch_experiments`，实际使用新入口前按既有方式升级后端。设计/实施方案、DESIGN 和 uv.lock 摘要核对未变。
