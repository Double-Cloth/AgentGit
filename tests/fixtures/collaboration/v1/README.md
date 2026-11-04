# 同进程四角色 v1 fixture

固定 `offline-four-role-research/v1`、内部 `agentgit-collaboration/v1` 和 collect→verify→write DAG；write 同时显式依赖 collect。规格见[支持计划](../../../../doc/S12-C-collaboration-plan.md)。`program.json` 为角色权限/预算和 DAG，`cases.json` 为公开输入、路由及失败注入/核对项；UUID/事件地址在单元 fixture 中为虚构标识，不是数据库记录或通过证据。

实际集成必须为四角色新建独立 Run/任务/私有上下文并采集真实 CAS、父事件、消息收据及共享版本。相同消息去重、共享版本比较等纯契约不替代数据库原子性；跨角色私有输入隔离、取消、崩溃恢复、Recorded 和桌面仍须按计划逐项接入。

执行增量将 Supervisor 默认模型额度校准为 2，分别对应派发和汇总；旧准备阶段额度 1 的程序仍保留在原冻结中，并作为实际预算拒绝用例。其他角色额度不变，运行时不自动扩大额度；fixture 规格不等于正式验收结果。
