# S7 Hybrid 运行接线方案与实施状态

这是接在类型化干预和纯匹配选择器之后的实现方案。2026-10-24 已接通下述后端路径，阶段证据见 [Hybrid 运行验收](acceptance/S7-hybrid-runtime.md)。继续复用现有 Runtime、持久 Job/Worker、调用网关、Recorded 和审批/预算/容器边界，不新增框架；统计和桌面部分尚待完成，S7 未整体验收。

## 冻结输入与模式

1. `NewExperiment` 增加显式 Hybrid 策略：来源终点、选择的源调用 ID、冻结 miss 策略；每组有序干预仍绑定共同安全起点、配置和 validator。只选择受控只读工具；响应补丁必须指向被选中的调用。
2. 从指定项目/Run 的完整来源范围加载一次录制并核验选择，冻结 `FrozenHybridPolicy`、原请求/响应和替换响应。目标不能在起点之前；未选择的调用继续经正常 Live 路径，模型永不消费旧答案。
3. 使用独立 `HybridReplaySpec`，真实 Run 的 mode 为 hybrid；不要用 live 标签掩盖混合模式。通过新迁移扩展 `replay_requests` 和 trial 约束，不能回改已提交的 0013/0014。
4. Job 拥有 policy 及所需只读对象的引用，Worker 领取/恢复时核对归属、语料和冻结配置；不得凭任意 hash 读取其他项目的数据。

## 耐久观测与恢复

1. 网关对选定调用先验证当前工具权限，再调用纯 `HybridSelector.resolve`。已知历史/替换/模拟响应不访问外部服务；其字节预先进入 CAS。
2. 选定观测的全新调用意图、开始/完成事件、结果与来源账本在同一短事务发布。使用本次新 checkpoint 的 CallIntent，不冒用源 intent；来源指针留在观测账本。
3. 只有真正的模型/工具调用继续走“保存 intent → 退出事务调用 Adapter → 保存结果”原路径。不要将模拟、历史响应或 synthetic 结果计为真实外部调用。
4. 观测账本区分 recorded、synthetic、fixture、miss，绑定 selected_call_id、policy_ref、响应和干预来源，标记 test_only。miss 仅记录确定性匹配/模拟失败；缺失、损坏、越权和未知输入直接拒绝，不伪造成功观测。使用不可变表及项目复合外键；不要修改恢复专用 origin_call_id 的既有语义。
5. 结果提交后中断沿用现有恢复消费，不再次解析/发送真实动作。取消、暂停、截止时间和 fencing 继续生效；所有未知结果如实保留。

## 结束、Recorded 与计量

1. 在最终验收之前检查所有必需选定依赖均已到达；不能在已写入 completed 后才发现缺选定调用。可为 Runtime 增加可选的验收前检查，旧 Live/Recorded 保持原路径。
2. Hybrid 的冻结 policy 引用进入受控环境输入，并由 Recorded 的环境兼容性验证绑定。纯 Recorded 从本次新录制恢复模型/工具结果，不能重新调用模拟器或真实模型；对未到达依赖的失败也须重现相同验收前检查。
3. `RecordedBundle`/复合 Recorded 的来源元数据须传播所需 policy，缺对象或越界时失败。不能只验证成功 Hybrid 的历史，遗漏 miss/取消/恢复分支。
4. 原逻辑用量和新逻辑差额继续保留；实际调用账本排除历史响应/替换响应，受控模拟器另列次数。恢复消费不能重计费；未知、预留和价格表估计不当作零或供应商账单。
5. 报告展示已用和未到达的选择、实际命中/模拟/synthetic 来源、miss 策略与限制，再接入统计和桌面。不得把 test_only 的成功解释为真实外部世界已修复。

## 验证顺序

先用真实 PostgreSQL 验证命中、改参 stop、改参 fixture、synthetic 替换、未到达依赖；再验证调用结果落盘后的中断恢复、取消/预算和来源不变，以及 Recorded 零调用复验。后端专项及 Live/Hybrid 各十个独立 CLI Worker 验证已通过；扩大回归证据见验收记录。之后完成统计和桌面流程，按实施方案逐项核对 S7-T01–T06。真实供应商冒烟仍需显式开启并单列，当前不调用付费模型。
