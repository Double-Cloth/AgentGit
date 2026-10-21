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

以上只证明版本与录制基础；测试把读取到的源快照作为校验材料，**尚未证明 Runtime 重算后的每个 state hash 与历史一致**。

2026-10-21 本机验证（Windows、Python 3.12.14、PostgreSQL 17.6、Docker Desktop Linux）：当前共收集 434 项，采用下列有重叠的分批执行覆盖，不声称是一次 434 项完整运行。

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

## 尚待完成

1. 用纯 Runtime 和内存工作区重算逻辑状态与产物，重建逻辑时间等输入，并逐个对照来源 state/产物摘要；分离逻辑用量和新运行的实际计量。
2. 将 Recorded/Live 作为耐久任务接入 Worker、HTTP/CLI，保存来源及范围；Recorded 禁止 override，禁止真实客户端或执行后端初始化。
3. Live 从指定检查点建立独立工作区，验证配置/计划/参数变化，启用新预算、独立执行容器和重新授权；不继承源调用结果或审批。
4. 完成 S5-T01–T09 的端到端验收，特别是完整断网重算、执行工具 Recorded 零启动和 A/B Live 代码/同名产物隔离。
