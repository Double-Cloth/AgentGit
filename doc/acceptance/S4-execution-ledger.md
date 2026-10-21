# S4 执行记录与受管工具后端

- 日期：2026-10-21。
- 状态：S4 进行中；受管容器后端已接入耐久调用网关，尚未接入研究 Agent 的生产 Registry/配置/计划。
- 前置：08102e8 真容器基础边界；最新规划 179afc6，S6 Electron 桌面与 S11 必选合并已纳入完整主链。当前工作仍按未变的 S0–S4 契约推进。
- Schema：新增 0009_sandbox_executions，历史迁移快照保持冻结；无新增运行依赖。

后续生产 Registry/配置/计划、工作区推进和恢复结算已继续接入，最新证据见 [S4-five-tools.md](S4-five-tools.md)。下文保留本次后端提交时的实现范围与测试记录。

## 实现

`core/execution.py` 冻结断网策略、终端/Python 请求、授权文件清单与可重放执行结果。策略签名绑定固定镜像、依赖、解释器、资源与请求 Schema；请求不能指定镜像或 Docker 参数。输入在读取 CAS 前与意图保存时两次核验 Run/检查点归属，额外数据和文件大小均有界。

`RecordedCallGateway` 为两个容器 provider 增加受管动作扩展。外部调用意图、固定容器身份和执行记录在同一 PostgreSQL 事务保存，确认提交后才启动真实容器。容器工具没有受管服务时拒绝执行，不能落入普通 `invoke`。动作期间不占用 SQL 事务；Job 租约、取消与持久截止时间持续检查。

清理后原子保存日志、成功文件清单、操作 metadata、执行记录与完整调用结果。正常成功才有输出 manifest；非零退出、异常导出、超时、取消和超限保留旧 checkpoint。未确认清理保持 outcome_unknown/result_ref 空，不把“命令打印成功”作为完整证据。容器 ID、实际耗时与资源计量存操作证据，不进入可重放结果。

Worker 接管在心跳下清理当前及合法恢复来源链的旧容器，绑定旧 Run/call/attempt/fencing 标签；新租约只确认物理清理，不伪造丢失输出。完整保存后、Commit 前的故障可直接消费录制，无需 Docker；缺结果仍禁止重发。

## 用例范围

真实 PostgreSQL + Docker 服务套件覆盖：Shell/Python 片段/脚本、cwd/argv/stdin、意图先提交、日志/产物核验、跨授权引用在读取前拒绝、意图/结果事务回滚、完整结果复用、非零/超时/非法导出、OOM/文件超限、二进制日志截断、取消、fencing、管理连接故障注入、镜像缺失、不支持语言、响应预算不足、已完成记录不可修改/删除，以及真正杀死执行进程后接管清理仍在运行的旧容器。

管理连接丢失使用显式传输故障注入；前面的命令和容器实际运行，另用独立真实管理连接完成测试清理，不冒充真的关闭整个 Docker 引擎。进程崩溃助手仅运行可信测试 Worker，用户命令仍在固定容器中执行。

## 检查结果

1. `uv run --locked pytest tests/unit tests/integration tests/sandbox tests/execution --junitxml=.agentgit/evidence/S4-execution-ledger.xml`：350 项通过，439.82 秒；143 单元、154 真实 PostgreSQL 集成、31 容器基础与 22 真实持久执行服务用例。
2. 收尾复现并修正“采集阶段 PID 1 已到期却报 export_invalid”的分类问题后，`uv run --locked pytest tests/unit/test_execution_contracts.py tests/execution -q --junitxml=.agentgit/evidence/S4-execution-ledger-boundary.xml`：35 项通过，62.82 秒；11 契约 + 24 执行服务用例，新增采集超时及用户普通退出 124 的区分。仅可信 PID 1 的退出状态用于超时判定。
3. 两批去重覆盖 352 项；不宣称同一命令运行最终全部 352 项。Ruff、104 个文件格式检查与 mypy 58 个源码文件通过；只有既有 Starlette/httpx 弃用警告。
4. 开发库已实际升级至 `0009_sandbox_executions`；固定镜像源码/依赖核验通过，测试结束无遗留 managed 执行容器。Linux 容器 CI job 已配置，尚未推送/远程执行，不将本地结果作为其通过证据。

本轮因可信源码增加错误码和资源证据而重新准备镜像，当前内容地址为 `sha256:37837ca77fde6fa5ab6e183ea9484888a107eb79359d2788069c295478fa8967`；Python、系统包清单与隔离上限未改变。Engine 连接固定 API 1.44，已按 [Moby 25 冻结定义](https://github.com/moby/moby/blob/v25.0.0/api/swagger.yaml)核对所需字段并在本机实测，避免无必要依赖 1.51；所有能力仍实际检查。

原始报告留在本地，SHA-256：

- `S4-execution-ledger.xml`：`014604d068f4b2deab5a9080d9a4027b25b820957b0d8bbdabfd596e3c8f38b4`。
- `S4-execution-ledger-boundary.xml`：`9e6503f3f704ea9fb98ef805ce8d7505adb7538144690943fab5283ed7ca2394`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

提交前复核最新文档内容摘要：设计文档 `c3a7fbee2e7852f77794dae96c37e74e7f609c5ead4cfb642e83c20f01296c3f`；实施方案 `c5c2305c98dfb6321bc7fe69f159dc4a03cd7dfbd850294cb6980cc51f15cd3d`。后续编辑以实际文件为准，不以这些摘要阻止用户修改。

## 剩余门槛

继续接入生产 Registry、Agent 配置/计划、结果观察与 CAS 工作区推进、有效预算和安全点控制，以及旧执行已停止且输入/策略一致后的新 attempt 计费与续跑。需要补齐生产 Worker/HTTP/CLI 下的全部 S4-T09–T20；现在不能用内部服务测试替代完整 Agent 五工具能力，`doctor --sandbox` 保持 false。

S4 完整验收后进入 S5；最新完整目标为 S0–S11 和 S12-A/B/C/D，包含 S6/S11/S12 的 Windows 桌面包验收。后续阶段开始前及提交前继续复核最新需求文档。
