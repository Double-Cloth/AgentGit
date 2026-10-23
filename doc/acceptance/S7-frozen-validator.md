# S7 冻结验收器增量

本记录只覆盖 S7 第一步；S7 整体尚未验收。当前开发顺序依据用户 2026-10-23 的明确授权，S6 剩余环境/原生专项延后补验，见[实施方案第 1.2 节](../AgentGit_实施方案_v1.0.0.md)。

## 已实现

- `ValidatorManifest` 固定 `research-validator/v1` 的八组硬规则、任务输入、语料、UTC 评估时间、fixture split、代码版本、依赖锁摘要和报告 Schema 摘要。输入/语料/manifest 使用现有 AGJSON-v1 和带类型的 CAS 摘要；不新增依赖或修改历史 Schema。
- `ValidatorService.freeze/manifest/evaluate` 按项目归属访问；验收具体 Run/Commit 的历史快照及实际产物，不接受调用方提交“已通过”作为证据。结果保存为不可变 CAS 引用，重复评估相同证据去重。
- 重新执行既有的来源覆盖、有效期、事实引用与报告结构检查。当前代码、依赖或 Schema 与冻结环境不兼容时直接拒绝；语料/时间摘要损坏也拒绝，不算作成功或普通失败。
- 取消、预算耗尽、审批等待、结果未知、中断等状态只记 `incomplete`；即使残留正确产物或运行自行上报 `passed=true` 也不能通过。失败运行带有正确产物仍不能通过。

## 验证

首批 **37 项通过**：33 单元 + 4 真实 PostgreSQL 集成，21.764s，无失败或跳过；原始证据 `.agentgit/evidence/S7-frozen-validator.xml`。覆盖冻结重复性、split 参与摘要、非法规则/权限/时间拒绝、所有未完成状态、伪造 passed、错误产物、输入漂移、代码/依赖/Schema 漂移、原语料修改隔离及 CAS 损坏。

真实 PostgreSQL 中实际完成离线研究，然后独立复验历史报告并读取持久化结果；检查跨项目、错误 Run、未归属 CAS、任务输入不符和中间检查点。测试使用独立测试 schema，不修改业务库；不调用付费模型。

当前代码的扩大回归 **406 项全部通过**：355 单元 + 51 真实 PostgreSQL 集成，179.75s，无失败或跳过。执行范围为 `tests/unit` 以及 `test_validator_service.py`、`test_research.py`、`test_research_faults.py`、`test_model_runtime.py`；覆盖原报告运行、故障处理和模型计量接线。本次未重跑其余集成、Docker 沙箱或桌面套件，不将此前阶段的结果计入本次 406 项。唯一告警为现有 FastAPI/Starlette TestClient 的 httpx 弃用提示，没有新增依赖。

Ruff、格式检查、严格 mypy（94 个源文件）和 `git diff --check` 均通过。新增用例尚未接入 API/桌面，因此当前桌面包仍为 S6 的 0.1.3。

| 原始证据（忽略目录 `.agentgit/evidence/`） | SHA256 |
|---|---|
| S7-frozen-validator.xml | `4e5babeb17d2589e274c5893b12387094229110023273a350d9c2e57ea2667b7` |
| S7-validator-regression.xml | `ae13cbdee12984b6497add5051fc4ba47cbace9231fbf6b96cf897464cb96526` |

需求检查：产品设计摘要保持 `eda6eaa91399be15c85ac55fd14dcf4db8f9ce931f620193976fb99b4fb1cfdc`；实施方案仅加入用户授权的临时顺序调整后为 `ceec2a719990f61b4be0266eb3ba611f9d5c0c94a7a3d3b44a7236262aedf04b`；DESIGN 与 uv.lock 未改变。

## 尚未完成

实验迁移与 Runner、A/B 各五次独立 Run、三类干预 DSL、Hybrid、取消/恢复汇总、指标/不确定性报告及 Branch Experiment UI 都继续按[实施记录](../S7-experiments-plan.md)推进。本增量不能单独作为 S7-T01–T06 整体通过证据。

当前 split 是冻结标签，不代表已准备独立的确认/回归数据集；结果固定标记 `test_only`，不能据此声明真实外部世界已修复。
