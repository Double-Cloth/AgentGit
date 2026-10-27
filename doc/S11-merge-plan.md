# S11 三方产物与结构化状态合并实施计划

依据设计 5.5、5B.3 和实施方案 S11。用户暂缓环境/人工验收并要求继续工程实现；按实施方案 1.2 先推进开发，D2 前置、原 S8 定位未达标和最终验收不作通过处理。S11 全部验收后才进入 S12。

## 交付顺序

1. 实现有界共同祖先与只读产物候选。复用 `StateSnapshot.artifacts`、CAS BLOB、路径校验、AGJSON 和现有 `ResearchReport`；先支持 UTF-8 Markdown 章节及 `sources.json` 的明确 Schema。未知类型明确拒绝，不能把通用 JSON 递归拼接作为语义合并。
2. 增加 Fact、TaskNode、Constraint 和效果审计投影。现有研究状态的事实对应 `(framework, dimension)`，证据对应 `Evidence.source.id`，计划对应 `PlanNode.key`；合并后重新检查正文摘要/引用、时间、DAG、完成前置、预算及权限。调用轨迹和未知外部动作不能拼接为新的可执行轨迹，容器/进程不合并。
3. 用新迁移持久化提案、冲突、修订、验证和精确审阅。绑定项目、base、两分支 HEAD/version、候选摘要、验证器与政策；编辑后旧验证/审批失效。API/CLI 共用服务，修改带身份、幂等键及预期版本。
4. 在批准事务中锁定双分支并重查上述绑定，创建稳定顺序 `(ours, theirs)` 的双父 Commit，仅 CAS 更新目标 HEAD；拒绝、过期、竞争失败不更新历史。合并不启动 Run 或执行工具。
5. 升级 Log/DAG/Fork/Diff/恢复和显式 Recorded 路径兼容。多个最佳共同祖先必须显式选择其中之一，不凭路径距离任选，也不递归构造虚拟 base。
6. 按 DESIGN.md 接入 Merge Review、受限 IPC、候选编辑/重新验证、精确批准/拒绝及重开只读；生成新 Windows 候选。冻结 10 无冲突、10 事实冲突、10 计划/约束冲突样例、人工预期和基线，执行 T01–T06 后逐项归档。

## 首增量的有限产物协议

共同祖先内核最多读取 10,000 个不同 Commit，每节点至多两个不同父；缺失、非法摘要、环或超限明确失败。返回全部最佳共同祖先，按摘要排序仅为了稳定展示；多个时返回 `ambiguous_merge_base` 及完整候选集合，等待显式选择。读取器从同项目索引读取并核对 manifest，不改变既有单父写入约束。

Markdown 只支持严格 UTF-8、以列首 `## ` 分隔的唯一二级标题及其前言；代码围栏内的标题保留为正文。不解释嵌套章节或任意 Markdown AST；重复/空标题、未闭合围栏、已有章节重排明确不支持。原章节按 base 顺序保留，新章节按标题稳定排序。章节是完整原字节块，不逐行调和双侧修改。相同修改或单侧修改/删除可选取；删除与编辑、同章不同编辑返回 `WRITE_WRITE`，冲突时不输出可用文件。

`sources.json` 仅接受项目 `ResearchReport` v1：固定 fixture、evaluation_time 和 frameworks；claims 以 `(framework, dimension)`、sources 以 `source.id` 对齐并整体三方选择，保留完整 Evidence，重复身份拒绝。事实双侧异改报 `FACT_CONTRADICTION`，时间不一致报 `TEMPORAL_MISMATCH`，来源异改保守报冲突；不拆开同一条证据拼接字段。合并结果按稳定身份排序并重新通过 Schema。原始缺失与 JSON null、bool 与 int 均按项目严格编码区分。

每个输入文件上限 1 MiB、Markdown 上限 1,000 章节。候选保留三方 BLOB 摘要作为证据；只读预览从 `HistoryService.restore` 与 CAS 获取真实字节，重算 BLOB 域摘要。即使产物无冲突，也始终标记结构化状态、任务/来源核验和人工审批待办，不能据此落地 Merge Commit。首增量不提供批准、双父写入或外部执行权限。

## 冻结历史兼容

S8/S10 的正式运行源码仍是 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`。增加 S11 产品代码后，旧 release 的源码 guard 应拒绝当前环境；复现必须使用各自冻结快照和已记录 Git 基线，不能改写旧 release 来接受新代码。旧报告/成本/成功率不重算为 S11 结果。

首增量[共同祖先与只读产物候选](acceptance/S11-artifact-preview.md)已通过 43 新单元、全部 743 单元、3 新真实集成及 7 版本历史集成核对，两个原始包独立重审；没有改历史单父约束或执行模型/工具。下一步按上述第二项实现结构化状态/约束/来源硬验证，再接持久审批与双父等完整交付。
