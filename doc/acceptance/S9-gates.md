# S9 本机后端功能退出核对

依据实施方案 S9 七项步骤及 T01–T07。S9 自身的后端功能逐项核对，不替代 S6/D1 原生与独立 Windows 验收、S7 整体前置、S8 人工审阅或定位优势。S8 注入点 Top-3 仍为 4/8，按方案显式使用完整有限模板降级，不能因此宣称 D2 或完整项目通过。

## 实施交付

| 项目 | 实现与范围 |
|---|---|
| 类型化补丁与固定模板 | `core/interventions.py`、`core/repair.py`、`repair/templates.py`；三种操作沿用 S7，目标、前置条件、效果策略、来源与可修改字段受限；不默认让 LLM 编写补丁 |
| 有限空间、有序应用与去重 | `repair/space.py`；默认八原子、深度二，先单点再兼容有序双点；同字段覆盖冲突拒绝。项目、环境、base、验收器、策略及有序效果摘要参与身份，提案与独立 trial 身份分开 |
| 耐久试验与预算 | `0017/0018` 冻结迁移、`repair_jobs.py`、`repair_trials.py`；原子预留和新 Job/Run 归属、Recorded 前置证明、Live/Hybrid 筛选、实际调用结算、未知完整预留、取消与租约 fencing；不在控制器中执行模型/工具 |
| 自动控制及停止范围 | `repair/worker.py`；单点失败仍探索联合；通过后排空已启动项；只有非空声明空间全部确定硬验收失败才称空间内无解，预算不足和非确定观测分别报告 |
| 候选排序与历史查询 | 停止决定冻结全部已探索项的 trial/结果、补丁数、字节变更范围、实际量及可用墙钟；按安全、验收、补丁数、变更量、计量单位、费用、墙钟、稳定 ordinal 排序。报告验证完整性、唯一性和实际合计，选中项必须是首个通过项。旧报告缺少比较表时为 `comparison=null`，不伪造历史排序 |
| API/CLI | `POST/GET /repair-searches`、取消、项目分页列表；`repair-search/status/list/cancel/worker`。从共同起点及目标事件生成全部适用有限模板，排除与不适用项冻结；原始请求/干预发布幂等，单连接池可用 |
| 默认预算与建议状态 | 新 Mock 字节策略按内容摘要冻结且不扩大来源授权，生产 Mock 默认公共入口可产生可审查 Plan 候选；次数和多维确认/回归预留保持。S9 状态最多 `needs_confirmation`，不改正式配置、不输出 verified 或最小性证明 |

## S9-T01–T07

| ID | 权威行为用例 | 结论与边界 |
|---|---|---|
| T01 | `test_automatic_prompt_joint_and_scoped_failure[prompt-needs_confirmation-1]` | 固定受控 Mock 的 Prompt 单点有效，另建独立 Run/结果；只能待确认。替身摘要冻结在任务文本，搜索器不读测试标签 |
| T02 | 同一用例的 joint 参数；`test_immutable_comparison_explains_failed_alternatives_and_terminal_time_stops` | 三个单点失败后继续有序联合，联合通过；四项比较表保留单点失败，未探索顺序及其他候选不冒称全局最优 |
| T03 | `test_rejected_unsafe_patches_have_correlated_audit_and_zero_trial_execution`；既有 Patch/Plan 安全单元及 S7 发布校验 | validator 注入、工具字段越权、任意路径、任务环均 422；借 confirm 验收器筛选为 403。五例响应关联编号与 JSON `http.request` 日志逐一对应；Run/Job/搜索/trial/派发/结算计数均未增加，不记录令牌或攻击正文 |
| T04 | 固定空间和身份单元；`test_single_connection_pool_and_six_concurrent_submissions_publish_once`；公共模板单池六路用例；原子派发并发/重试历史证据 | 相同提案复用，不吞掉不同有序语义或跨环境身份；筛选意图和确认 repeat 身份分开。同键异参拒绝，原搜索与新独立试验不混淆 |
| T05 | `test_phase_quota_is_not_borrowed_and_total_quota_is_hard_limit`、多维预留单元；持久原子拒绝用例；控制器资源停止四参数及阶段停止例 | 总/阶段次数、调用、字节、费用和时间均为硬上限，未知项占完整预算；不启动新 trial；保留已有证据、剩余空间及未确认原因。当前真实费用为 Mock 零费用，不冒充付费供应商实测 |
| T06 | 自动搜索 never、execution、阶段预算三个例；停止契约篡改测试 | 只有两项声明空间全为确定失败才称该空间内无解；执行异常即使已枚举完也为 `not_found`，预算停止保留未探索项；空空间不作真空无解证明 |
| T07 | 丢失派发响应、未知计量、取消、旧租约 fencing；真实子进程在派发提交后退出；并发排空用例 | 意图/Job 复用，未知不重发，不释放未知费用；取消后不扩展，原 Worker 落实已派发项；首个通过后仍等待已启动项结算，再冻结决定 |

终态 `usage.elapsed_wall_ms` 使用相同状态的冻结决定；没有算法决定的控制终态取对应 revision 的审计时刻。只读查询不会继续增加完成后的墙钟，不写 CAS，不覆盖历史。活动搜索的预算仍由数据库当前时刻计入编译、排队、等待与重启，终态显示修正不扩大执行额度。

## 当前证据

当前包内源码：`source-sha256:d2fcee1fe5b937171ac63761473a3fae12c3c3e5fe30fe5f9ed21b9f6e014d3a`。最新 601 个不同测试全部通过，569 单元 + 32 真实集成，无失败或跳过。当前 `ruff check`、249 文件格式检查及 131 Python 源文件 mypy 全部通过。六份报告保留重复复验，汇总按 classname/name 去重并为重复项选用后续专项，不叠加用例数：

| JUnit（`.agentgit/evidence/`） | 结果 |
|---|---|
| `S9-gates-unit-v1.xml` | 569 通过，含 13 项新排序/封存契约测试 |
| `S9-gates-integration-v1.xml` | 16 通过，含 3 项新安全/墙钟测试及公共默认、单点/联合、未知、取消、HTTP/CLI/Hybrid |
| `S9-gates-controls-v1.xml` | 10 通过；选择性运行租约、取消、预算和代码兼容控制，另 7 项未选中而非跳过 |
| `S9-gates-edges-v1.xml` | 6 通过；四项资源停止、并发排空、真实进程提交后退出 |
| `S9-gates-ranked-evidence-v1.xml` | 2 项已覆盖用例复验，另存当前源码联合/生产 Mock 原始包，不另加数量 |
| `S9-gates-safety-and-time-v2.xml` | 3 项已覆盖专项复验；恶意请求期间禁止调用 Mock/Tool Adapter，关联日志和终态只读查询核对 |

汇总 `.agentgit/evidence/S9-gates-summary.json` 的 SHA-256 为 `85cf512a06cf31a1f6c2c2e268b0786c64fdc4c173edcfb7d9e1025d532e8888`，绑定报告、逐测试选用记录、五例审计、854 条 CAS 对象记录和 1,844,080 原始字节（跨包可重复），及源码/规范/锁文件。逐对象摘要与模型原请求/响应字节重新核算均通过。

`.agentgit/evidence/S9-gates-safety-v2.json` 保存五例请求摘要、响应、关联审计和前后计数；审计使用已有 HTTP JSON 日志，没有新增安全业务表，也不把用户请求正文或认证头放入操作日志。`.agentgit/evidence/S9-gates-real-v1/` 保存当前源码的联合例及生产 Mock 例的完整比较/结果/CAS 原始字节。此前六例包及 620 项结果保留为 [上一增量证据](S9-search-loop.md)，不与本轮重复计数。

本轮完成 S9 本机后端功能退出核对，未修改 Schema、未迁移业务库、未调用付费供应商、未打新桌面包或合并 main。继续 [S10](../S10-repair-plan.md) 的耐久缩减、独立确认/回归、Repair Lab、审批与完整 30 例评估；仅通过本节测试不能代替前置补验、后续阶段、D2 或 S11/S12 的全部必选交付。
