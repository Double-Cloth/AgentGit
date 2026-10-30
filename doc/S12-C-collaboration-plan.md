# S12-C 同进程四角色支持范围与实施计划

2026-10-30，接续 B 本机工程核对提交 `9a05f60`，按既有“工程先行、正式验收后补”授权进入 C。设计/实施文档摘要保持 `8297ef…f37 / a338f0…0e5`。不将 S11/D2/B 或用户暂缓项改为通过，最终四适配 Windows 包及 D3 仍为必选。

## 固定流程与边界

首批为 `offline-four-role-research/v1`，内部消息契约 `agentgit-collaboration/v1`；复用 Python 3.12、现有离线语料/Mock、History/CAS/Job/Run/预算/权限/审批，不引入新协作框架或依赖。Supervisor 按明确 DAG 派发 collect（Researcher）、verify（Verifier，依赖 collect）、write（Writer，依赖 collect/verify）；Writer 向 Supervisor 回报。四个角色各有独立 agent_run_id、task_id、检查点、私有上下文、预算及权限，不能用一个 Run 的角色标签代替四个 Run。只执行声明的固定 DAG，不接受任意子 Agent、代码、循环、动态扩权或公网任务。

Researcher 用既有 corpus_search/source_read 收集固定语料；Verifier 核对正文摘要、引用及冻结有效时间；Writer 消费明确导出的证据/验证结果，通过既有产物边界写 report.md/sources.json。Supervisor 负责委派、依赖与子任务控制。子 Agent 不继承操作者令牌、其他角色的私有消息/权限/预算、外部写批准或执行后端身份；首批不授予容器工具。未来如支持 Shell/Python，必须仍走 S4 策略，不能走宿主执行。

耐久准备使用协作域的真实角色任务索引，绑定现有 Run/Job；不把 RolePolicy 存为普通研究 AgentConfig，也不借用 S3 研究 Task 身份。每个角色有独立新分支，准备时 Run/Job 暂停并按既有触发器持有自身分支写锁，main 不变。普通恢复/取消入口不能提前激活 prepared 组；后续协作调度器负责开始、依赖与组控制。初始私有输入只通过后端调度器持有的有效 Lease 读取，模型/IPC/envelope 不得自报 Lease 或任意 CAS 摘要。

## 消息、因果与共享产物

不可变 envelope 包含 repository_id、collaboration_id、message_id、sender/receiver（实际 Run）、接收任务 task_id、correlation_id、kind、payload_ref 与 parent_event_id。parent_event_id 明确为已有耐久事件的地址：agent_run_id、commit_id、seq、payload_ref；服务必须读取原始事件核对归属，不能相信 envelope 自报。消息指向先前已保存的发送角色事件，发送事件再引用 envelope，避免 Commit/CAS 的循环摘要。跨角色因果 DAG 独立保存，不把四条时间线强行串成一个 Run。

路由固定为 Supervisor 的 dispatch/cancel、Researcher→Verifier/Writer 的 evidence、Verifier→Writer 的 verification、Writer→Supervisor 的 result。correlation_id 固定为本协作 ID；接收 task_id、来源/目标 Run 和项目必须匹配冻结绑定。只导出有界类型化 payload，CAS 摘要本身不是跨角色读取授权。相同 message_id 与相同完整输入只生效一次；同 ID 换内容拒绝。消息状态/收据、发送 outbox 和接收推进需具备原子性，实际边界在集成测试证明，纯去重函数不等于耐久去重。

共享 evidence.json 可由 Researcher/Verifier 在明确权限下提交，verification.json 只由 Verifier，report.md/sources.json 只由 Writer。每个共享路径保留版本、对象来源及发布角色事件，提交必须带确切期望版本；并发旧版本拒绝，重试不覆盖其他 Agent 的结果。共享导出与私有上下文分别授权，知道对象摘要不等于可读取其字节。最终产物仍进入相应 Run 的现有 StateSnapshot/ArtifactEntry/CAS 及预算。

## 推进与证据

1. 先固定本目录计划及 `tests/fixtures/collaboration/v1/`，实现严格程序/角色/任务/绑定/envelope/共享版本契约和纯核对；不据此宣布真实隔离或 T03 通过。
2. 接入现有数据库与独立 Run：原子创建四角色/任务、绑定不可变程序/环境和独立预算/权限；服务按可信当前角色授权，私有上下文只供所属 Run。迁移只在隔离测试 Schema；不迁移业务库。
3. 同进程调度固定 DAG，沿用租约/fencing/HEAD/CAS 与网关，耐久 outbox/inbox/控制/共享产物。验证子任务失败/取消、重复/冲突消息、依赖未完成、跨 Agent/项目读取与共享版本竞争；实际进程终止后接管不重复效果。
4. 成功轨迹严格 Recorded 固定各角色来源范围、消息/控制/共享版本与依赖；禁真实模型/工具/网络/进程，重算逻辑投影和产物。缺字节或伪造因果失败，无 Live 回退；每个角色历史/实际用量分列。
5. 提供操作者演示、真实桌面四角色轨迹/因果/来源查询，执行 S2/S4/S5/S6/S11 相关兼容、独立原始审计和 `acceptance/S12-C.md`。本机协作稳定后才开始 D；最终新包和 D3 汇总保留，不以开发模式替代包内/原生验收。

S12-T03 必须有实际数据库/Worker 的失败、取消、消息去重、乐观锁和私有上下文隔离证据；T05 必须有实际 Recorded 零外部效果/缺依赖反例；T06 保留真实核心/容器/合并/桌面兼容。所有输入在 Runtime 启动前冻结，期间不修改源码/驱动/锁/构建；原始 CAS/事件/父边/收据/消息/共享版本及 PID 记录由独立读取器核对。失败尝试与成功报告分别保留，不把 fixture 的虚构 ID/摘要当作真实耐久依据。

## 执行首增量进度

2026-10-30，准备之后已接通真实角色 Worker、耐久 outbox/inbox/收据、版本发布和恢复，证据见[工程记录](acceptance/S12-C.md)。当前产品源码 d8e7de…a34b，head30/78 表；1,081 单元和分批 47 协作节点通过，包含六处实际进程终止、父子领取顺序无关的接管、权限缩减、独立模型/文件预算、失败/取消及消息/共享旧版本拒绝。默认 Supervisor 从准备占位的一次模型额度校准为派发/汇总两次，保留额度一次的明确失败用例；其他角色额度不扩大。

后续调度增量已将 100 组窗口限制为确有失败需传播、停止需唤醒或依赖/消息齐备的暂停 Job，核对旧停止/人工暂停组不挤占窗口，并补回暂停检查点→Job 释放之间的漏唤醒。最新 ef15cc…0ed7 源码 1,081 单元、34 集成通过；测试主动把窗口缩为 1，用两个真实组证明阻塞结构，不宣称完成百组压力测试。

2026-10-31，组暂停/恢复/取消后端已接通本人身份、原键和事件版本；实际四角色控制与因果、保留单角色人工暂停、在途结算/未知、墙钟不重置和并发/事务恢复已核对。最新 5a74cf…8732 源码 1,081 单元、94 集成全过，无新依赖或迁移。接受请求不等于全部角色已安全停止；查询分别显示实际角色/Job、静止和未知事实，不能用取消覆盖未知。

下一步仍属 C：成功轨迹的严格 Recorded、规模压力及更多跨角色/项目/持久边界继续，补操作者入口与真实桌面/兼容。现有来源和作用域守卫保持，不从缺录制回退到 Live。C 尚未稳定或正式退出，不能据此开始 D，也没有新分发包。
