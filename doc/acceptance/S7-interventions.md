# S7 类型化干预与 Hybrid 匹配增量

本增量继续实现实施方案 S7 第 3–4 步；S7 整体仍未验收。上一轮 A/B Live 与独立进程证据见 [S7-experiments.md](S7-experiments.md)，S6 环境/原生专项按用户授权延后。

## 已实现的范围

`PROMPT_PATCH / PLAN_PATCH / TOOL_RESPONSE_PATCH` 使用固定 Schema：目标事件、源 Run、安全起点和目标检查点、payload、原状态/配置/validator/occurrence 前置条件、效果策略及证据来源。服务验证目标调用的完整录制、事件与检查点关系、项目归属和冻结验收输入，再将 payload、前置条件、provenance 与定义分别保存到 CAS；`0014_frozen_interventions` 建立不可变索引、项目复合外键和目标事件/调用约束。

Prompt 只改 `agent_config.prompt`，计划仍经过已有 DAG、完成节点保护及工具授权校验；两者必须在目标模型决策前的检查点应用。实验分组可引用最多八个有序干预 ID；同一 Prompt/计划/工具观测的重复补丁拒绝，不做静默覆盖，也不能与旧式直接分组条件混用。

工具响应补丁只能引用本项目已提交的完整、同参、同签名只读录制，不能上传任意“成功响应”或修改真实外部世界。响应字节另外登记到干预自身的只读引用，且不能从不同冻结 split 的实验 trial 借用观测。当前 split 仍只是标签，不据此宣称已建立独立确认数据集。

Prompt/计划干预已接入 Live 实验调度，创建和读取有 API/CLI。工具响应定义可冻结和审阅，但在缺少完整 Hybrid 执行上下文时明确拒绝排队，不能假装已应用。

`intervention-context` 提供项目内只读的完整目标调用、原配置和前置条件，API/CLI 返回一致；创建时仍独立重新核对，不把读取上下文当作审批票据。

Hybrid 选择器已实现纯匹配逻辑：只有显式工具 occurrence、签名、规范请求、效果及响应界限全部匹配时才读旧响应；参数 miss 依冻结策略停止或由固定语料重新生成结果。更换工具/签名/效果不能借模拟器绕过；缺失或损坏的原录制也不能被 fallback 掩盖。模型录制和写工具均不可选；synthetic、历史录制与模拟结果分别保留来源，输入语料及错误对象在构造时隔离调用方的可变引用。

## 验证状态

当前代码扩大回归 **457 项全部通过，307.40s**：397 单元 + 60 真实 PostgreSQL 集成，无失败或跳过。范围为 `tests/unit` 及 `test_intervention_service.py`、`test_experiment_runner.py`、`test_experiment_entrypoints.py`、`test_validator_service.py`、`test_kernel_migrations.py`、`test_management_api.py`、`test_management_cli.py`。覆盖目标/陈旧前置条件/项目/计划权限拒绝、两种干预的独立 trial、不可变定义、同键复用、API/CLI 上下文与创建一致性、同参响应约束、确认观测不能借给筛选，以及迁移往返与正式回退保护。

全项目 Ruff、格式检查（201 个文件）、严格 mypy（101 个产品源文件）和 `git diff --check` 通过。唯一告警是既有 FastAPI/Starlette TestClient 的 httpx 弃用提示，未新增依赖。

本轮未重跑独立 TCP 进程、其余集成、Docker 沙箱或桌面套件；旧轮次结果不计入当前 457 项。首批 24/34 项及加入上下文前的 457 项仅保留为过程证据，不重复相加。上下文曾因测试期望省略了契约默认的 schema_version 而比较失败，修正期望为正式契约后完整重跑通过；未删减行为检查。

原始证据 `.agentgit/evidence/S7-intervention-context-final.xml`，SHA256 `c91d0d3ac5913a37bf15800004e7fce3a5c4dea037da5f4662bf3501271f4858`。产品源码版本 `source-sha256:17538d60d39e91917b217b01a8d29c47e5a99fcd4fd407351d1f5449fb2bdb48`。需求设计/实施方案、DESIGN、uv.lock 和冻结执行协议未改变。

## 继续工作

Hybrid 选择器尚未接入 Worker、耐久观测账本与实际/逻辑计量；工具响应替换后的完整成功报告、停止/恢复、未知结果与 Recorded 交叉复验仍待实现。因此本增量不能单独证明 S7-T03/T04 已通过。

后续还包括完整统计和方差/未知费用报告、Branch Experiment 的桌面交互及 S7-T01–T06 整体核对。未运行付费供应商冒烟，未重建桌面包，未自动迁移业务库；所有迁移验证使用独立测试 schema。
