# AgentGit：面向 AI Agent 的执行版本控制、故障诊断与反事实修复平台

> **产品与技术设计文档 · v1.0.0 · 2026-10-20**
> 项目定位：Git for Agent Runs + Counterfactual Debugger + Minimal Repair Search

> **当前状态：仅完成仓库初始化和方案设计，尚无程序实现。** 详细执行顺序、数据契约、阶段测试及退出标准见 [AgentGit_实施方案_v1.0.0.md](AgentGit_实施方案_v1.0.0.md)。本文说明产品目标和技术边界，实施方案将其细化为 S0–S12；涉及界面时继续遵守根目录 [DESIGN.md](../DESIGN.md)。

**v1.0.0 设计基线：**统一优先级和阶段编号；将版本存储前置于可恢复 Runtime；明确 Recorded Replay 的起止范围、调用匹配与失败行为；补充持久化 Job、审批绑定和未知副作用结果处理；以数据契约约束分阶段迁移，不将固定 12 周作为全部研究功能的交付承诺。

## 0. 一页摘要

**一句话介绍：**让开发者像使用 Git 一样管理 Agent 执行历史，并在任务失败后自动定位可疑决策、从历史检查点展开受控实验，搜索能够恢复任务成功的最小修复方案。

**需要解决的问题：**Agent 输出具有非确定性；发生失败时，开发者很难回答“哪一步最值得怀疑”“只改变哪一个决策就能修好”“这个修复是否稳定、是否增加成本”“两个版本的优点能否安全整合”。

**核心能力：**围绕 Agent Runtime、Git 式版本管理、反事实实验、决策故障定位和语义合并，重点设计 **Minimal Intervention Search（自动最小修复搜索）**，打通“检测失败 → 候选定位 → 生成干预 → 隔离重执行 → 稳定性评估 → 推荐修复”的闭环，并明确搜索算法、持久化模型、API、验收指标和开发节奏。

**项目的四层能力：**

1. **版本控制（基础设施）**：Commit / Branch / Checkout / Log / Replay；不单独宣称首创。
2. **受控反事实调试（实验基础）**：从相同检查点修改 Prompt、计划或工具响应，建立可追溯实验分支；不将相关性直接宣称因果关系。
3. **故障定位与最小修复搜索（核心差异化）**：基于事件依赖寻找候选故障点，自动生成、缩减并筛选尽量少改动的修复方案，以成功率、变更成本和鲁棒性验证。
4. **可验证语义合并（后续研究方向）**：合并结构化状态与有证据的产物，显式检测事实、计划、约束与副作用冲突。

**MVP 成功标准：**一个简单 ReAct Agent 能检索/读取资料、写出报告，并通过隔离容器实际执行终端命令与 Python 代码；执行后可查看时间线、从历史节点创建分支、完成“录制响应重放”和“真实重新执行”、比较两个分支的结构化差异；全过程不重复执行真实外部副作用。

**非目标：**任意 Python 程序的字节级确定性重放、通用多 Agent 操作系统、任意自然语言推理的严格因果归因、任何状态的无损自动合并，以及对任意任务都保证找到修复解。

## 1. 核心概念与使用场景

### 1.1 Git 概念映射

| Git | AgentGit | 注意事项 |
|---|---|---|
| Repository | Agent 项目仓库 | 管理运行历史与对象 |
| Commit | 不可变执行状态版本 | 不是每个 Token 一个 Commit |
| Branch | 指向某 Commit 的可移动引用 | 分支执行上下文必须隔离 |
| Checkout | 选择 / 恢复历史状态 | 恢复不等于重新触发副作用 |
| Diff | 状态、轨迹与结果比较 | 区分客观差异与模型判断 |
| Merge | 受约束的状态 / 产物合并 | 不拼接两条聊天记录 |
| Tag | 成功运行或里程碑标签 | 可用于回归基线 |
| Replay | 录制回放 / 真实重执行 | 必须明确两种语义 |

### 1.2 演示案例

研究 Agent 接到“生成某领域框架对比报告”。它搜索资料、生成结论，却引用了过时页面。AgentGit 根据失败断言“关键结论必须有现行官方来源”定位候选决策，尝试“要求官方来源”“增加来源验证步骤”“替换过时工具响应”等干预，在相同检查点隔离执行对照实验；随后剔除多余干预、复测稳定性，推荐改动最少且通过验证的修复方案。开发者仍可人工创建 `verify-sources` 分支，检查 Diff，合并经过核验的章节；任何真实写操作均需遵守副作用策略。

## 2. 需求优先级与范围控制

| 级别 | 需求 | 验收方式 |
|---|---|---|
| P0 | 事件采集、可恢复 Snapshot、Commit DAG | 运行一次任务并查询完整历史 |
| P0 | Branch、Checkout、从历史节点重执行 | 两分支互不覆盖原始历史 |
| P0 | Recorded Replay（受控录制回放） | 明确起止 Commit，相同受控输入和录制依赖得到相同逻辑状态序列 |
| P0 | 外部副作用保护 | Replay 不会重复发送 / 写入 |
| P0（必选） | 终端命令执行 `terminal_exec` | S4 在隔离 Linux 容器中实际执行 Shell、管道、重定向和本地脚本；输出/文件变化可追溯 |
| P0（必选） | 代码执行 `code_exec` | S4 支持 Python 3.12 片段/脚本、argv/stdin、异常和产物；实际执行、隔离及资源限制通过测试 |
| P0 | Structural Diff | 清楚显示状态字段和调用变化 |
| P1 | 轨迹对齐与行为差异 | 找出首个显著分歧节点 |
| P0 | Web 时间线及分支 DAG 可视化 | 作为可演示 MVP 的一部分，可点击节点查看详细事件 |
| P1 | Prompt / 模型 / 工具实验 | 相同检查点至少两个实验分支 |
| P1 | 故障断言与候选定位 | 能结合失败断言给出可点击候选节点及证据 |
| P2 | 自动最小修复搜索（核心创新） | 自动提出并缩减候选干预，在预算内输出带复测报告的修复建议 |
| P2 | 语义 Diff + 证据引用 | LLM 解释有来源、可复核 |
| P2 | 结构化 Semantic Merge | 能检测冲突并产生可审计合并提案 |
| P3（必选后续阶段） | MCP / A2A / 多 Agent / LangGraph 适配 | S12 四个子项全部实现并独立验收；不作为 D1/D2 前置依赖，但属于完整交付 |

## 3. 技术架构

```text
CLI / React Web（公开 Python SDK 后置）
           │
           ▼
FastAPI（REST，WebSocket 后置）
           │
     AgentGit Core
   ┌───────┼────────────┬────────────┬────────────┐
   ▼       ▼            ▼            ▼            ▼
Commit   Branch        Replay     Diff/Merge   Repair Search
 Engine  Manager      Controller     Engine       Engine
   │       │            │            │            │
   └───────┴────────────┴────────────┼────────────┘
                      ▼
             Instrumented Runtime
         ┌───────────┼──────────────┐
       LLM Adapter  MCP Adapter（S12）  Tool Adapter
                      │
        PostgreSQL + 本地 Object Store
```

**首版技术栈：**Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、Typer、React、TypeScript、React Flow、Docker Compose。采用单一 Python 包内的模块划分，不拆微服务或多个独立发布包；大对象先存本地文件系统，后续按需换 S3/MinIO。API 与单 Worker 分进程运行，以 PostgreSQL `jobs` 表承担持久化队列和租约，不引入 Redis/Celery。具体依赖版本在 S1/S6 通过兼容性验证后锁定，本轮不安装依赖。

**必选执行环境：**复用 Docker，在 S4 由 Worker 创建短生命周期 Linux 容器，统一承载终端与 Python 工具；Windows 使用 Docker Desktop 的 Linux 容器模式。命令/代码只能接触本 Run 授权文件，不直接执行宿主 PowerShell/cmd，不开放宿主挂载或外网。容器管理属于后端固定能力，不暴露 Docker socket 给模型和执行代码。容器技术降低执行风险，但首版不是公网恶意代码托管服务。

**运行时策略：**先实现 CAS/Object Store、事件和 Commit 原子提交，再实现轻量 ReAct Loop（Plan → LLM → Tool → Observe → Update State），统一经过 Adapter；先使用离线语料和 Mock LLM 完成闭环，再连接一个真实模型。LangGraph、多 Agent、MCP/A2A 均为 S12 必选交付，四项完成才达到 D3 完整版本；S11 合并保持可选。

### 3.1 三层数据与职责

- **Event：**记录实际发生的事件，如 `llm.requested`、`llm.completed`、`tool.started`、`tool.completed`、`state.updated`、`effect.proposed`。
- **Snapshot：**保存可恢复执行状态，包含上下文、计划、工作记忆、待执行队列和环境引用。首版每个 Commit 均有完整状态对象，Snapshot 只作为恢复锚点元数据；增量快照和周期压缩后置。
- **Commit：**将某个一致性状态固定为不可变版本，引用事件范围、Snapshot/状态摘要、父 Commit 和代码 / Prompt / 工具版本；`Branch` 只保存 HEAD 指针。

建议 **每一次完整状态转移** 生成 Commit，Event 的粒度更细。长时间工具调用可产生多个 Event，但只有完成并验证状态更新后再提交新的 Commit。

## 4. 最小数据模型与数据库设计

数据库设计以如下数据契约和实施方案第 3 节为准，必须覆盖恢复、审批、作业、跨项目外键和事件完整性约束；DDL 与迁移待对应实现阶段再编写，不将不完整的示意直接作为迁移执行。

| 阶段 | 实体 | 必须成立的约束 |
|---|---|---|
| S2 | `repositories / objects / object_refs` | 对象内容不可变；所有引用可追溯到项目；对象下载不能只凭 hash |
| S2 | `branches / commits / commit_parents` | 父节点有序、同项目、无环；HEAD 按预期 hash 与 version 原子推进 |
| S2 | `runs / run_commits / events / snapshots` | 运行与 Commit 关联独立保存；每个 Run 的事件 seq 唯一连续；Commit 绑定事件清单摘要 |
| S2 | `artifacts / external_calls` | 路径为受控相对路径；调用按逻辑发生次序区分，不能仅靠参数 hash |
| S3 | `agent_configs / tasks / task_nodes` | 配置不可变；Task 是用户意图，Run 是一次执行尝试；计划版本化且无环 |
| S4 | `jobs / approvals / idempotency_keys` | Job 有租约与 fencing token；审批绑定精确动作和状态；幂等键绑定请求内容 |
| S7–S8 | `experiments / experiment_trials / interventions / debug_reports` | 验收器、输入、环境和干预均冻结；依赖边和定位证据可追查 |
| S9–S10 | `repair_searches / repair_proposals / repair_trials / proposal_reviews` | 每次独立执行独立 Run；分阶段预算；批准只创建配置新版本 |
| S11 | `merge_proposals / merge_conflicts` | 校验和审批绑定三方版本；HEAD 改变后须重新验证 |

**运行状态统一：**`queued / planning / running / waiting_approval / paused / interrupted / outcome_unknown / completed / failed / cancelled / budget_exhausted`。`interrupted` 表示进程中断待恢复，`outcome_unknown` 表示外部动作结果不明、等待核对；二者不能当作普通失败自动重试。任务状态是当前 Run 的投影，旧 Run 终态保留。同 Branch 最多一个活动写入 Run，暂停/审批仍保留绑定，恢复运行在事务内接管。

**内容编码：**首版使用受限 `AGJSON-v1`，严格限定整数范围、排序规则和无歧义的哈希封装，禁止浮点数、重复 key、非法 Unicode；小数用约定单位的整数或十进制字符串表示。保留字符串原始 Unicode，不做静默归一化；其编码规则不冒称完整 JCS 实现。二进制保留原字节。Commit 覆盖有序 parents、完整 state、environment、事件清单摘要、schema/codec 版本；数据库 ID、创建时间、可变 HEAD 不参与内容哈希。见 [RFC 8785 对原始字符串的要求](https://www.rfc-editor.org/rfc/rfc8785.html#section-3.2.2.2)。

**提交事务：**先将对象写入同卷临时文件，校验并原子发布到最终 CAS 路径；之后在一个短数据库事务中插入对象引用、状态转移事件、Commit、Snapshot 和运行关联，并 CAS 更新 HEAD。事务失败不留下可见的半个 Commit；已发布但未引用对象属于可回收孤儿。外部调用的意图和结果各自先落盘，防止崩溃后盲目重发；它们不能与外部系统组成一个数据库事务。运行时不持有跨网络调用的数据库事务。

## 5. 核心算法与实现细节

### 5.1 Commit

1. Agent Runtime 执行一步并记录细粒度 Events。
2. 对状态按固定 Schema 序列化，保存至 Object Store。
3. 首版每步保留完整状态对象，并生成 Snapshot 锚点；待有真实容量数据后再考虑周期快照。
4. 构造不可变 Commit 清单，计算 ID 并持久化。
5. 使用乐观锁原子推进分支 HEAD；失败则报告冲突，不能覆盖别人更新。

### 5.2 Checkout 与分支

`branch(name, commit_id)` 创建新分支引用；`checkout` 只改变用户会话当前分支或打开只读历史视图，不擅自修改外部系统；`resume(commit_id)` 则恢复该 Commit 所代表的 Agent 状态并创建**新的 run**。分支环境须隔离临时目录、工作文件、工具缓存和授权上下文。

### 5.3 Replay：两种模式

**录制回放（recorded）：**请求必须指定 `source_run_id + from_commit_id + until_commit_id`，恢复起点的完整状态，只消费起点之后、终点之前（含终点）的原录制调用。不能只给一个终点 Commit 就从结束状态继续执行。匹配键包含逻辑步骤、调用次序、工具/模型签名和请求摘要；重复参数的两次调用不得串用响应。LLM 与工具均不得访问网络；缺记录、请求不匹配、代码不兼容时明确报错，禁止自动回退 live。Recorded 不接受改变 Prompt/模型/计划的 overrides。

时间、随机数、外部文件如影响状态，必须由受控输入提供；首版串行执行。比较受控逻辑状态 hash 和产物 hash，新的 Run ID、实际回放耗时和费用独立记录，不要求新的 Commit ID 等于原 Commit。Recorded 表明历史恢复能力，不作为修复成功证据。

**真实重执行（live）：**恢复历史状态，从新分支再次调用 LLM / 工具；新运行必须拥有独立 `run_id`、事件序列和外部调用记录。对非只读动作使用 sandbox、dry-run 或人工审批，不复用历史提交的副作用。

**Hybrid（S7）：**只有明确指定的调用使用录制响应；发生干预后，请求必须仍匹配才能命中。未匹配依赖按冻结策略调用受控模拟器、沙箱只读服务或停止，不能静默使用原答案。所有干预从目标决策前的安全 checkpoint 应用，不支持修改任意进程栈或工具调用中间状态。

### 5.4 Diff：三层逐步实现

- **Structural Diff (MVP)：**JSON Pointer 路径差异、计划任务列表变化、输入输出 Blob Hash、工具调用序列及 Commit DAG 的最近共同祖先（LCA）。
- **Behavioral Diff (P1)：**按事件类型、工具名称、调用参数归一化和依赖关系对齐轨迹，识别第一处分歧。并行事件不能单纯按时间戳逐项对齐。
- **Semantic Diff (P2)：**用 LLM 生成解释，但要求显式引用证据（事件 ID、文档段落、工具输出），并将“推断”与“可验证事实”分离。

### 5.5 Merge：从安全的场景开始

**先做 Artifact Merge：**Markdown 报告按章节 / 文件合并，JSON 按 Schema 合并，代码文件可以采用传统三方合并。

**再做 State Merge：**以最近共同祖先为 base，对 `base / ours / theirs` 做三方比较；字段按策略分类：

| 字段类型 | 示例 | 允许的策略 |
|---|---|---|
| 可累加集合 | 经过核验的资料引用 | 去重、保留来源 |
| 单值状态 | 当前任务负责人 / 下个动作 | 冲突则人工决策 |
| 任务计划 | 待执行任务 / 前置依赖 | 合并后重新验证 DAG |
| 事实性记忆 | “某酒店离地铁 200 米” | 检查证据、时间及互斥关系 |
| 外部副作用 | 已下单 / 已付款 | 不自动合并或重复执行 |

**Merge Proposal：**系统输出 proposed_state、conflicts、evidence、validation_results；只有通过 Schema 校验、任务约束、来源核验及必要人工审批后，才生成双父 Merge Commit。LLM 只提供建议，不拥有最终提交权。

### 5.6 关键决策定位（实验驱动）

先在两个分支中寻找最早的可观测轨迹分歧，再按该节点恢复并逐一控制变量进行实验。将质量度量拆成确定性测试（约束是否满足）、规则测试（引用是否有出处）与模型评估（描述质量）。多次采样并报告均值与波动，不能把时间上的先后等同于因果证明。

## 5A. 完整 Agent 产品能力设计（新增重点）

### 5A.1 产品中的 Agent 不是一个聊天机器人

AgentGit 首先实现**可执行、可恢复的研究报告 Agent**，以有限领域验证执行闭环，再扩展适用任务。最低能力为：接受目标 → 结构化计划 → 选择工具 → 校验输出 → 更新记忆与任务状态 → 有界重规划 → 产出文件 → 评估结果 → 请求人工确认 / 结束。终端命令和 Python 代码执行是首版必选能力，均须在隔离容器内真实实现；所有状态转换必须进入事件链。首版不承诺通用自主 Agent、直接操控宿主系统或自动适应所有任务。

**单 Agent 必须具备的功能（P0/P1）：**

| 子系统 | 功能 | 技术实现 | 最低验收 |
|---|---|---|---|
| Task Manager | 创建、暂停、恢复、取消任务；子任务依赖图 | Task DAG + 状态机 + cancel token | 任务中断后恢复，已完成步骤不重复 |
| Planner | 根据目标拆分步骤，观察失败后重规划 | JSON Schema 结构化规划 + 版本化 plan | 工具失败后能修改计划并继续 |
| Executor | 模型决策、工具选择、参数校验、并发限制 | 显式 ReAct loop、统一 Tool Registry | D1 五个内置工具均可实际使用 |
| Tool Layer | 本地资料/产物工具、必选终端与 Python 执行，后续 HTTP/MCP | Tool Adapter + 容器执行适配器 + policy engine | 功能、隔离、超时、退出结果及文件变化有明确测试和事件 |
| Memory | 首版会话短期和带证据的任务工作记忆，长期检索后置 | message log + structured memory | 恢复后记忆不丢，来源可查 |
| Artifact Manager | 报告、JSON、代码、附件 | 工作区文件系统 + CAS + manifest | 产物可下载、Diff、Merge |
| Human-in-the-loop | 对高风险工具调用审批 / 拒绝 / 修改 | durable interrupts + approval record | 审批前不会执行真实写操作 |
| Reliability | 退避重试、预算、循环检测、降级 | retry policy + limits + circuit breaker | 预算耗尽能可解释地停止 |
| Evaluation | 任务成功、约束满足、工具正确率 | deterministic validators + optional LLM judge | 每次 Run 产出评估记录 |
| Observability | Token、延迟、成本、异常、step traces | 首版 typed events；OpenTelemetry 后置 | UI 能追溯一步执行的所有调用 |

**D1 必选工具清单：**

| 工具 | 能力 | 实现阶段与范围 |
|---|---|---|
| `corpus_search` | 检索研究资料 | S3，固定离线语料 |
| `source_read` | 读取来源正文及来源信息 | S3，固定离线来源 |
| `artifact_write` | 保存报告、JSON 与其他文件产物 | S3，仅当前 Run 授权目录 |
| `terminal_exec` | 执行 Shell 命令、管道/重定向、工作区脚本及本地测试/临时仓库 Git | S4，固定 Linux 容器的 `/bin/sh`，非交互式、有超时，无宿主终端回退 |
| `code_exec` | 执行 Python 3.12 片段或脚本，接收 argv/stdin，计算和生成文件 | S4，固定解释器/依赖；首版不支持的语言明确拒绝 |

执行工具统一采用断网、非 root、只读容器根目录、有容量上限的独立临时工作区及 CPU/内存/进程/时间/输出限制。命令执行与可信采集身份分离；主命令结束后确认无残留执行进程，校验输出，再清理容器并提交正常成功的文件 manifest，后续调用从 manifest 恢复。仅有超时参数或 Python subprocess 不算隔离实现；详细输入、默认上限、容器生命周期和验收见实施方案第 3.5.1 节与 S4-T09–T20。

命令/代码、输入、退出状态、stdout/stderr、输出文件和执行镜像均留有版本化证据；Recorded 只恢复录制结果，不启动容器或解释器，Live 才在新容器执行。固定断网策略可由 Run 预授权，不要求每条合法命令反复确认；改变权限/资源须按冻结策略处理。失败、取消或导出不完整时保留旧 checkpoint，不把半产物计为成功；硬性验收器和答案数据不进入代码工作区。

**迭代推荐：**P0 构建单 Agent 任务执行闭环和版本能力；P1/P2 完成受控实验、定位和修复搜索；P3 必须完成 S12 的多 Agent、MCP/A2A 与 LangGraph 适配。优先级表示实施顺序，不表示 S12 可以省略。不要在 Agent 尚不能稳定完成任务时先投入高级 Merge。

### 5A.2 Agent 的可恢复状态与生命周期

状态示例仅展示业务字段（实现时须用 Pydantic 固定 schema_version，完整状态枚举以第 4 节及实施方案为准；运行标识、审批记录 ID 和实际成本账本与可重放逻辑状态分开保存）：

```python
class AgentState(BaseModel):
    schema_version: int = 1
    goal: str
    messages: list[MessageRef]
    plan: list[TaskNode]
    pending: list[str]
    completed: list[str]
    working_memory: dict[str, MemoryItem]
    artifact_manifest: dict[str, str]
    pending_approvals: list[str]
    budgets: BudgetState
    tool_context: dict[str, str]
    step_count: int
    status: RunStatus
```

状态机遵守 `queued → planning → running → completed/failed`，并支持第 4 节定义的审批、暂停、中断、结果不明、预算耗尽和取消。暂停只在安全边界生效；调用已发出时不能假称取消成功。暂停恢复沿用 Run，崩溃恢复、历史 Fork 和已终止运行重试创建新 Run 并引用原 checkpoint。每次逻辑状态转换记录 before/after hash、原因和操作者；已发生外部副作用不得通过 Checkout 撤销。

### 5A.3 工具协议：安全执行（S4）与 MCP（S12）

安全执行机制在 S4 建立，MCP Adapter 在必选的 S12-A 实现列出工具、schema 缓存、参数 JSON Schema 验证、工具调用、超时和错误标准化；取消能力按远端支持程度报告，资源与提示词接口进一步后置。每次记录 `server_identity`、`tool_name`、`schema_digest`、`arguments_digest`、`response_ref`、`latency`、`effect_class`。第三方 MCP Server 的写入工具默认需显式授权，不能仅靠模型自报“只读”。外部凭据仅在运行时注入，不写入 Snapshot / Event / Blob；日志做敏感字段脱敏。

工具按 `READ_ONLY / IDEMPOTENT_WRITE / NON_IDEMPOTENT_WRITE / IRREVERSIBLE` 分类，重放使用 RecordedAdapter 返回历史响应；live branch 在隔离工作区内执行，写操作采用 dry-run 或审批。重试写工具需要真正的幂等键，网络超时且结果未知时必须进入 `outcome_unknown` 而不是盲目重试。

### 5A.4 多 Agent 与 A2A（P3 / S12）

设置 Supervisor、Researcher、Verifier、Writer 四种角色；Supervisor 根据任务 DAG 派发子任务，Researcher 收集资料，Verifier 核查来源，Writer 生成可审计产物。每个子 Agent 有独立 `agent_run_id`、上下文、预算和权限；协作消息记录 `message_id / sender / receiver / task_id / correlation_id / payload_ref / parent_event_id`。跨 Agent 通过消息形成因果 DAG，而不是将多条时间线强行合成一条线。A2A 适配重点是 agent capability 描述、任务发起、状态查询、消息/产物传递以及身份认证；A2A 版本差异以实现时的官方规范为准。

并发约束：任务必须声明依赖；共享产物使用乐观锁；合并知识时记录来源。多 Agent 不属于首版运行引擎的硬依赖，但 S12-C 同进程协作和 S12-D A2A 均为完整交付的必选项；先完成 supervisor + workers，再支持跨进程远程协议。

### 5A.5 端到端演示任务

**研究报告 Agent**：输入“比较三个框架，生成带来源的 Markdown 报告”。首版在固定离线语料中检索、读取来源、生成 `report.md` 与 `sources.json`，为事实记录来源 ID、采集时间和摘要；验收器检查框架覆盖、证据存在、fixture 的有效期和输出结构。语料中的“当前”以固定 `evaluation_time` 判定，不冒充实时互联网事实。之后可选接入真实只读资料服务；MCP 是 S12 适配项。人工注入过时结果后，从错误决策前的 checkpoint Fork 并比较；合并章节属于 S11 扩展。

**D1 执行工具演示（必过）：**在上述资料任务中加入固定本地数据，Agent 用 `code_exec` 计算可核验的对比指标并生成 JSON，再用 `terminal_exec` 整理产物或运行固定本地测试；最终检查报告、计算结果、退出码和文件差异。Recorded 回放须在容器引擎不可用时仍能恢复齐全的历史结果；Live 分支才创建新的执行容器。此演示是必选验收，不以模拟执行响应替代真实工具实现。

## 5B. 创新点的可执行技术方案（新增重点）

### 5B.1 创新 A：可复现实验分支（受控反事实执行）

**通俗价值：**不是“再运行一次”，而是在同一个历史节点上只改变一个变量，检验结果为什么变化。

**研究问题：**如何控制模型随机性、外部工具变化及历史副作用，从而比较不同 Agent 决策？

**方案：**(1) 记录环境清单：模型/参数、Prompt digest、工具 schema digest、代码 commit、输入状态、时间与随机种子（仅记录种子不保证托管模型确定）；(2) 通过 `fork(base_commit, overrides)` 生成全新 Run；(3) 支持 `recorded`、`hybrid`（指定工具录制，其余实时）、`live` 三类模式；(4) 使用工具响应替换（response patch）、Prompt 替换、模型替换和计划节点替换；(5) 实验 manifest 绑定评估标准、重复次数和共享录制环境；(6) 输出分支 DAG、指标及偏差来源。

```python
def fork_experiment(base_commit, intervention, repeats=5):
    baseline = restore(base_commit)
    assert intervention.target in allowed_intervention_points(baseline)
    jobs = []
    for seed in range(repeats):
        jobs.append(start_new_run(
            state=clone_isolated(baseline),
            parent_commit=base_commit,
            override=intervention,
            replay_policy='hybrid',
            trial_seed=seed,
            effects='sandbox_or_approval',
        ))
    return evaluate_and_compare(jobs)
```

**度量：**任务成功率、约束满足率、Token、时延、外部调用数、恢复成功率、不同 seed 的方差。**验收：**从相同 Commit 派生 A/B 两组，每组重复 5 次；能复核干预字段、控制变量和原始 traces，且没有外部写操作重放。**边界：**这属于有控制条件的反事实实验，不自动证明严格因果关系。

### 5B.2 创新 B：失败决策定位（Causal Debugging）

**通俗价值：**告诉开发者“不只是哪里不同，而是哪一个步骤最值得怀疑”。

**输入：**失败运行 F、参考成功运行 S（或成功断言）、共同祖先 Commit、轨迹 DAG。**处理流水线：**

1. 先对齐事件：基于 `task_id / logical_step / tool / argument_fingerprint / dependency_edges`，用 DAG 依赖而非仅时间排序匹配节点。
2. 寻找**首个可观测分歧**与后续失败断言的依赖路径；生成候选关键决策集合，不能把它直接当成原因。
3. 对候选节点执行可控干预：替换 Prompt、工具响应或策略，其余录制依赖尽量保持不变。
4. 按效果大小、复现比例、干预成本排序；报告失败样本数和不确定性。
5. 输出可点击 Debug Report：候选节点、证据 Event IDs、试验设置、结果比较、可能的混杂因素。

可设计 `candidate_score = w1 * divergence_proximity + w2 * failure_dependency + w3 * intervention_improvement - w4 * uncertainty`，但权重需要用标注测试集校准，不能作为客观因果分数。失败样例：搜索结果过时 → 规划未验证来源 → 报告结论错误。系统应优先定位“未验证来源的决策”并展示替换实验，而不是简单把最早错误页面标成根因。

**验收实验：**制作 20 个可控故障注入用例（错误检索、空工具响应、Schema 变更、超时、计划遗漏、冲突记忆等），记录人工标注的故障注入点，统计 Top-1/Top-3 命中率、平均定位步骤与实验开销。与“按时间查首个失败事件”的基线比较。

### 5B.3 创新 C：可验证语义合并（后续研究方向）

**通俗价值：**两个 Agent 分别做对了一部分，系统能合并它们的有效成果，同时识别哪些内容不能直接拼接。

**创新区别：**不是让 LLM 总结两份报告；而是把可合并内容转为**有类型、可追溯、可验证的状态变更**，经过三方比较、冲突检测、验证后才生成 Merge Commit。

**结构化表示：**

```json
{
  "fact_id": "hotel-42-distance",
  "subject": "hotel-42",
  "predicate": "distance_to_station_m",
  "value": 200,
  "valid_at": "2026-10-20",
  "source_event_ids": ["evt-102"],
  "confidence": 0.88,
  "verification_status": "verified"
}
```

其他状态按类型划分：`Fact`（事实证据）、`TaskNode`（任务及依赖）、`Decision`（计划选择）、`Constraint`（预算/时间/权限）、`ArtifactSection`（文档章节）、`EffectRecord`（外部副作用）。关键是每一类有明确的**合并策略与不变量**，而不是一个通用 merge prompt。

**三方语义合并算法：**

1. 计算两个 HEAD 的最近共同祖先 B，提取 B→A 与 B→C 的结构化变更集合。
2. 使用稳定 ID、schema path、entity ID 和证据归一化对齐变更；对内容 hash 相等或单侧修改直接合并。
3. `Facts`：同一 subject/predicate 下如果值互斥，或时间范围/单位不同，标为事实冲突；优先要求证据验证，不以 LLM 置信度直接覆盖。
4. `Task DAG`：合并节点与依赖边后验证无环、任务状态前置条件、资源与预算约束；不合法则冲突。
5. `Artifacts`：章节级三方合并，重建引用关系和证据索引；同一段落双侧编辑则产生合并候选，不直接覆盖。
6. `Effects`：只合并审计记录，不重复执行；付款、订单、删除等不可逆动作禁止自动合并执行语义。
7. 由规则引擎先做硬约束检验，再让 LLM 生成可解释的合并建议；明确列出来源和未解决冲突。
8. 通过 Schema + validator + policy + 必要人工审批后，落地 `MergeProposal → ApprovedMergeCommit(parent_a,parent_b)`；失败保持原分支不动。

**冲突类型：** `WRITE_WRITE` 同字段双侧修改、`FACT_CONTRADICTION` 事实互斥、`TEMPORAL_MISMATCH` 时间有效期不一致、`PLAN_DEPENDENCY` 任务 DAG 冲突、`CONSTRAINT_VIOLATION` 超预算/时长、`EFFECT_CONFLICT` 不可逆副作用、`PROVENANCE_MISSING` 缺失证据。每个冲突包含 `paths / branches / evidence_refs / severity / proposed_resolutions`。

```python
def propose_merge(base, ours, theirs):
    ca, cb = typed_delta(base, ours), typed_delta(base, theirs)
    candidate, conflicts = three_way_merge(ca, cb, policies=FIELD_POLICIES)
    conflicts += validate_plan_dag(candidate)
    conflicts += validate_constraints(candidate)
    conflicts += validate_provenance(candidate)
    return MergeProposal(candidate, conflicts, status='needs_review'
                         if conflicts else 'ready_for_validation')
```

**可衡量的新意：**定义冲突 taxonomy + 合并协议 + 可审计 evidence graph；相比“LLM 直接合并文本”，在结构化任务上测冲突召回率、误合并率、约束违规率、人工审查次数、任务完成率与成本。**验收：**准备不少于 30 组双分支样例（10 无冲突、10 事实冲突、10 计划/约束冲突）；所有有害副作用冲突必须被阻断，合并产物通过定义的约束测试；报告 baseline 对照结果。不得宣称系统能自动合并任意自然语言记忆。

### 5B.4 创新 D（核心新增）：自动最小修复搜索（Minimal Intervention Search）

**一句话价值：**AgentGit 不只指出 Agent 哪一步可能错了，而是在明确验收条件与安全边界内，**自动寻找使失败任务恢复成功所需的尽量小的可验证改动**。

**与现有模块的关系：**5B.1 负责建立受控分支实验，5B.2 负责缩小值得干预的候选决策范围，本节负责搜索和筛选修复方案，5B.3 的语义合并则是后续按需集成成果的功能；最小修复搜索并不依赖语义合并才能成立。

#### A. 问题定义与边界

给定失败运行 `failed_run`、可恢复的共同检查点 `base_commit`、候选决策节点集合 `N`、允许干预操作集 `Ω`、固定的任务验收器 `V` 和有限执行预算 `B`，搜索干预集合 `I ⊆ Ω`。理想目标是：在干预成本与改动程度尽量小的情况下，最大化经过重复试验验证的通过率，并满足副作用与安全约束。

\[
I^* = \arg\min_{I \in \mathcal{F}} \left[\lambda_1 C(I) + \lambda_2 D(I) + \lambda_3 \widehat{R}(I)\right]
\]

其中 `C(I)` 是执行和资源成本，`D(I)` 是相对基线的变更规模，`R̂(I)` 是在测试分布下的经验失败率，`𝔽` 是通过硬性安全、权限和预算检查的可行集合。实际实现优先采用**字典序目标**：安全通过 → 验收通过率达到门槛 → 干预数量最少 → 变更幅度最小 → Token / 时延开销最小，而非过早调整不透明权重。以上“最小”只相对于已定义的干预语法、搜索范围和预算，并不保证全局最优。

**前置条件：**必须存在可恢复 checkpoint、稳定输入、明确定义的验证器或失败断言、可隔离的工具环境。对仅凭主观 LLM Judge 判定的任务，结果标为“候选修复”，不得声称已经证实正确。

#### B. 干预操作语法（Intervention DSL）

每个干预是**类型化、可审计、可回滚的变更对象**，至少包含 `target_event_id`、`target_commit_id`、`operator`、`payload_ref`、`precondition`、`effect_policy` 与 `provenance`。

| 操作符 | 修复示例 | 限制 |
|---|---|---|
| `PROMPT_PATCH` | 增加“只引用已验证的官方资料”约束 | 白名单消息层级；不得更改固定验收规则 |
| `PLAN_PATCH` | 在“撰写结论”前插入“核验来源”节点 | 合并后 Task DAG 必须无环且满足前置依赖 |
| `TOOL_RESPONSE_PATCH` | 在实验中替换过时搜索结果为已录制的可信结果 | 只可修改受控测试观测，不可当成真实外部世界已改变 |
| `TOOL_POLICY_PATCH` | 为搜索结果添加引用校验门槛 | 不扩大权限、不绕过审批或安全过滤 |
| `MODEL_CONFIG_PATCH` | 调整模型、温度、采样参数 | 属于条件对比；价格、延迟和可用性须单独记录 |
| `MEMORY_PATCH` | 修正带证据的过期事实记录 | 禁止无来源捏造事实；变更需保留证据链 |

第一版只开放 `PROMPT_PATCH`、`PLAN_PATCH` 和**测试环境下**的 `TOOL_RESPONSE_PATCH`，并为每个类型配置可修改字段白名单。不同干预可能不交换（例如 Prompt 变化改变后续计划结构）；组合实验必须按照稳定顺序应用，做前置条件检查，并明确记录冲突。

#### C. 两阶段搜索流程：先找到，再缩小

1. **发现失败（Failure Oracle）：**从用户声明的任务约束、JSON Schema、引用检查、单元测试或沙箱规则构建验收器。将硬性约束和可选 LLM 评分分开，锁定版本并保存在 experiment manifest，避免搜索过程修改评分规则。
2. **候选节点提取（Fault Localization）：**利用 5B.2 的依赖 DAG 和失败断言，优先考虑处于失败依赖路径上的决策。未能可靠定位时扩展搜索，而不是认定第一处异常就是根因。
3. **修复候选生成（Proposal）：**第一版用类型化模板生成局部单点改动，补丁经过 Schema、权限策略和路径白名单验证；自动 LLM 提议后置。默认最多 8 个原子补丁、组合深度 2。
4. **粗筛（Screening）：**先用 Recorded 检查恢复链，再用受控模拟器或 Hybrid 执行变化后的决策；任务硬断言不通过或副作用违规的候选淘汰。Recorded 不接受干预，也不以旧答案证明修复成功。
5. **组合搜索（Search）：**首版先单点、再按固定顺序枚举可兼容双点，不立即引入 Beam Search。去重键包含 base、ordered patch set、environment、validator 和策略版本；保留非交换干预的顺序语义。
6. **最小化（Reduction）：**对已通过的组合做逐补丁删除测试，必要时进行预定义字段级缩减；每次删除均有独立执行记录。确定性 fixture 只有在完整测试所有单点删除后才能称相应条件下 1-minimal；真实模型只报告经验缩减和不确定性，不能用一次随机通过声明严格最小。
7. **稳健复测（Confirmation）：**先锁定一个最终候选，再使用预留独立样本确认并运行回归；默认 6 次确认至少 5 次通过、安全零违规，同时报告 95% Wilson 区间和小样本限制。不能看完确认结果后继续用同一保留集筛选候选。真实模型的随机条件与时间敏感性单独说明。
8. **输出修复报告（Repair Proposal）：**展示精确改动、关联故障节点、可复核 traces、被淘汰的替代方案、验证条件、成功率、Token/时延成本、残余风险；默认只生成**待审批修复提案**，不自动修改生产配置。

#### D. 第一版搜索执行顺序

1. 预检 checkpoint、允许操作、冻结验收器、环境兼容性和各阶段预算，任何必要条件缺失立即返回可解释结果。
2. 按失败依赖候选产生有限模板补丁，验证白名单和前置条件，再依次测试单点与双点；每个试验有独立 Run。
3. 只使用筛选额度探索；默认总计 30 个 trial，分配 screen 14、reduce 6、confirm 6、regression 4，且有额外总 Token/时间/费用约束。
4. 在筛选结果上选择并缩减一个候选；删除测试不完整时不声明 1-minimal，更不能声明全局最小。
5. 锁定最终补丁后运行独立确认和回归；预算不足、样本缺失或硬安全失败都不能输出 verified。
6. 输出精确补丁、测试适用条件、证据、计量、不确定性与停止原因；只读提案待人工审查，批准只创建配置新版本。

**重要实现细节：**`budget` 同时限制 Token、货币成本、模型/工具调用数、墙钟时间、最大干预数和最大并发数；如果预算耗尽，返回已获得的证据与“未确认”，而不是错误宣告无解。试验使用新的 `run_id` 和隔离工作区，严禁跳过审批、调用真实不可逆工具或复用原分支外部副作用。

#### E. 研究对照、指标及验收数据集

| 维度 | 指标 | 解释 |
|---|---|---|
| 修复有效性 | `repair_success_rate`、复测通过率 | 原失败任务是否真的变好，以及复测是否稳定 |
| 修复幅度 | `patch_count`、变更 token 数、修改状态字段数 | 是否比其他成功候选更小 |
| 搜索效率 | 完成搜索的 trial 数、Token 成本、耗时 | 自动搜索是否具备实际可用性 |
| 定位辅助效果 | 故障候选 Top-1/Top-3、搜索空间缩减比例 | 定位结果是否帮助更快找到修复 |
| 安全可靠性 | 未授权写操作次数、误通过率、回归失败率 | 必须优先考虑安全和误判 |
| 可审计性 | 有效证据引用率、修复可复现率 | 用户是否能逐步复核实验 |

**数据集建议：**设计 20 个故障注入样例与至少 30 个可判定修复案例，覆盖“单点干预足够”“两个决策联合修改才有效”“工具结果伪修复”“无可行修复”“副作用受限”等类别；固定任务验收器，留出独立复测样例。

**基线比较：**至少四类自动基线：(1) 首异常节点局部补丁；(2) 一次性全局 Prompt 补丁，离线用固定模板、真实模型实验可用 LLM 提议；(3) 无定位排序的有界枚举；(4) 不做缩减的同预算组合搜索。人工调试可作为额外观察。各方法使用相同预算、验收器、独立确认和回归规则，报告成功率、变更规模、成本与失败案例；所有提议调用也计入预算。

**可量化的阶段验收：**完成 30 个可判定修复样例与至少 4 类基线对照；每个成功推荐必须可追溯到 checkpoint、干预集合、原始 traces 与验证版本；所有未授权写操作测试为 0；没有通过独立复测的结果必须标注为“未经确认”。这些是**验收设计目标**，不是已完成的实测数据。

#### F. 最小可演示场景

研究报告 Agent 误引用过时网站。固定失败断言：`report.md` 中核心结论必须引用当前有效的来源。系统定位“采信搜索结果但未核验”的决策；生成三个候选：A 为 Prompt 加一句约束、B 为 Plan 加一步验证、C 为 A+B。系统分别从同一 checkpoint 生成试验分支，对比有效引用率与成本；若 A 单独达标则优先 A，若只有 C 达标再试着移除其中一个补丁。最终展示**“最小改动、成功复测、成本增量、仍存在的风险”**，并允许人工查看 Diff 后决定是否采纳。

#### G. 创新性边界

Delta Debugging、组合搜索、因果调试、Agent Replay 等思想均有现有研究与工程先例；本方案的差异化潜力在于**针对 Agent 决策/工具/计划的类型化干预 + 安全重放 + 有预算的最小化搜索 + 独立复测 + 可审计修复提案**这一端到端组合。尚不能宣称算法学术首创、严格因果证明、对所有 Agent 框架普适，或保证全局最小修复。

### 5B.5 创新 E：执行过程的可移植 Commit 格式（S12 必选范围）

将 `event / snapshot / commit / artifact / environment / provenance` 定义为框架中立 schema，研究 LangGraph、自研 ReAct、A2A 子 Agent 的映射适配。目标是同一 Diff/Replay 界面能够消费不同 Agent 框架的受支持轨迹；不声称可以无损恢复任意框架的内部执行状态。最小规格包含 `schema_version`、因果事件边、状态恢复边界、effect_class、工具/模型签名、敏感信息脱敏策略。

## 5C. 开发接口、测试与里程碑补充

### 5C.1 Agent 核心服务接口

| API | 功能 | 关键约束 |
|---|---|---|
| `POST /agents` | 创建 Agent 配置（模型、权限、工具集合） | 配置版本不可变 |
| `POST /tasks` | 目标输入与预算配置 | 幂等键 |
| `POST /tasks/{id}/runs` | 触发执行 | 创建 Run 和持久化 Job |
| `POST /runs/{id}/pause` | 暂停 | 在安全中断点生效 |
| `POST /runs/{id}/resume` | 恢复 | 暂停沿用 Run；崩溃恢复派生新 Run，不复制副作用 |
| `POST /approvals/{id}/decision` | 批准/拒绝工具动作 | 审批人身份与审计 |
| `GET /runs/{id}/artifacts` | 列出运行产物 | 按项目权限控制，文件内容通过产物 ID 受控下载 |
| `POST /experiments` | 指定历史节点、干预与重复次数 | 预算上限、隔离执行 |
| `POST /repair-searches` | 从失败运行创建最小修复搜索 | 必须指定冻结的验收器、checkpoint、预算及副作用策略 |
| `GET /repair-searches/{id}` | 查询搜索进度与候选排行榜 | 展示试验数据、剩余预算和不确定性 |
| `POST /repair-proposals/{id}/approve` | 人工批准修复提案 | 版本校验、审批记录，不直接执行未授权写操作 |
| `GET /debug-reports/{id}` | 返回定位证据与分支实验 | 列明限制与置信度 |
| `POST /merge-proposals/{id}/validate` | 验证待合并状态 | 不执行外部副作用 |

### 5C.2 补充数据契约与迁移要求

实体引入阶段统一见第 4 节。迁移必须逐阶段增加，不能在 MVP 一次性建立所有研究模块。特别要求：

- `external_calls` 增加逻辑调用 ID、attempt、请求摘要、幂等键引用、审批摘要、状态和未知结果记录；同请求多次调用不能被误去重。
- `jobs` 保存租约到期时间、worker、递增 fencing token 和恢复 checkpoint；旧 Worker 失去租约后不可再提交。
- `approvals` 绑定 Run、checkpoint、精确工具参数摘要、策略版本、有效期和一次性消费状态；改动任何绑定项都需重新审批。
- `experiments` 必须先于 `interventions / experiment_trials` 存在；事件的数据库 ID 为 BIGINT，但可移植证据引用同时保留 Run 与 seq，不能与 UUID 混用。
- `repair_trials` 每条对应一次独立 Run，包含 `screen / reduce / confirm / regression` 阶段；删补丁测试不得冒用已有候选的试验结果。
- Patch 原文和报告存不可变对象；将数据移入 Blob 不等于加密，凭据仍禁止入库，私有内容需受控访问。
- 所有引用验证同项目归属、FK、唯一约束和删除策略；历史对象默认不可删除，GC 先做 dry-run 和恢复验证。
- S1/S2 冻结基础字段与哈希向量，S7 以后再加入实验/修复实体；详细字段、索引及迁移验收见实施方案。

### 5C.3 执行节奏

不再按固定 12 周同时交付 Runtime、研究搜索、语义合并和跨框架。统一按第 8 节推进；S6 为 D1 可演示 MVP，S10 为 D2 核心修复研究闭环，之后必须完成 S12-A/B/C/D，达到 D3 完整必选版本。S11 合并独立可选，不阻塞 S12。每阶段必须先通过对应测试门槛，再启动依赖它的阶段。工时估算和降级规则见实施方案第 7 节。

### 5C.4 不可省略的测试矩阵

- **Agent 功能：**规划准确性、任务依赖、空响应、工具 JSON 错误、超时重试、模型切换、预算限制、人工审批、重启恢复、产物一致性。
- **终端 / 代码执行（必选）：**Shell/脚本/Python 的真实执行、参数/标准输入、非零退出/异常、断网和文件隔离、资源/日志/产物上限、整容器取消与子进程清理、崩溃核对、半产物拒绝；Recorded 不启动执行环境，Live 从独立文件状态开始。
- **版本语义：**Commit 内容哈希稳定性、并发 Branch HEAD CAS 冲突、历史不可变、CAS GC、迁移兼容。
- **Replay：**录制工具调用严格不出网、相同受控输入输出一致、未录制依赖清晰报错、未知结果的写工具不得自动重试。
- **Diff：**分支 DAG LCA、事件乱序与并发对齐、证据关联、只读输出可复核。
- **Merge：**三方无冲突、写写冲突、过时事实、单位冲突、任务环、预算超限、付款冲突、缺失来源、人工拒绝后的无状态变更。
- **Repair Search：**验收器不可篡改、补丁类型校验、搜索去重、预算硬停止、单点/联合修复、删补丁缩减、无解/预算耗尽、复测样本隔离、同预算基线对照、修复报告可追溯。
- **S12 必选适配：**MCP Schema/权限/超时，LangGraph 支持图的恢复与 Fork，多 Agent 消息因果/预算/隔离，A2A 身份/幂等/断连/取消；四项录制回放及核心兼容性均须通过。
- **安全：**Prompt Injection（外部文档诱导调用写工具）、工具白名单、密钥脱敏、路径穿越、跨任务数据隔离、审批绕过。

### 5C.5 开题/简历表达与边界

**产品贡献：**设计实现可暂停、可恢复、可审计的 Agent Runtime，并提供版本化执行 DAG、Checkpoint Fork、工具级录制重放。

**创新贡献：**构建受控反事实执行框架和基于事件依赖的故障候选定位方法，重点研究**类型化干预 + 有界搜索 + Delta Reduction + 独立复测**的 Agent 最小修复流程；以成功率、修复规模、搜索成本、回归率量化效果，并将有证据的 Semantic Merge 作为扩展研究。

**避免过度承诺：**Replay 只保证受控边界内复现；因果定位输出候选和实验证据；“最小修复”仅针对限定干预空间和预算，不能保证全局最优；语义合并只支持明确 Schema 与验证规则的状态/产物。所有功能应以实际测试结果评估，而非仅靠 Demo 展示。

## 6. API、CLI 与代码组织

### 6.1 REST API（建议）

| 方法 | 路由 | 作用 |
|---|---|---|
| POST | `/repositories` | 创建项目仓库 |
| POST | `/tasks/{id}/runs` | 创建一次执行及持久化 Job，与 5C.1 保持一致 |
| GET | `/runs/{id}/events` | 获取事件序列 |
| GET | `/commits/{id}` | 获取 Commit 元数据 |
| POST | `/branches` | 从历史 Commit 建分支 |
| POST | `/replays` | 创建带明确录制范围的 recorded 或新分支 live 任务；hybrid 从 S7 开放 |
| POST | `/repair-searches` | 提交自动修复搜索任务（P2） |
| GET | `/repair-searches/{id}` | 查询搜索状态、候选与证据（P2） |
| GET | `/repair-proposals/{id}` | 查看精确补丁、验证和成本（P2） |
| POST | `/repair-proposals/{id}/approve` | 审批修复建议（P2） |
| GET | `/diff?base=...&head=...` | 查询差异 |
| POST | `/merge-proposals` | 生成合并提案（P2） |
| POST | `/merge-proposals/{id}/approve` | 批准已验证且 HEAD 未变化的合并（S11） |

长耗时任务返回 HTTP 202、`job_id` 和 `run_id`，通过 `GET /jobs/{id}` 与 `GET /runs/{id}/events?after_seq=...` 查询；MVP 前端轮询，WebSocket 作为后续优化。API 只提交持久化 Job，不能把 FastAPI BackgroundTasks 当作可恢复调度器。所有修改接口要求幂等键与请求摘要绑定，HTTP 接口在 S4 加入本地身份令牌和项目边界校验；默认仅监听回环地址，不包含公网多用户部署能力。

### 6.2 CLI（拟设计，不是现有命令）

| 命令组 | 设计语义 | 阶段 |
|---|---|---|
| `init / task / run / status` | 创建执行 Repository、提交任务、查询 Job；不同于初始化源码 Git 仓库 | S4 |
| `pause / resume / cancel / approvals / artifacts` | 显示控制请求状态、精确审批和受控下载 | S4 |
| `log / branch / checkout` | 只读历史、从安全 checkpoint 创建分支、选择当前分支 | S5 |
| `replay` 的 recorded 模式 | 必须提供来源 Run、from Commit、until Commit；不接受干预 | S5 |
| `replay` 的 live 模式 | 提供 Fork 点、新分支、配置与预算；效果策略重新授权 | S5 |
| `diff` | 同项目 Commit/分支的结构化比较 | S5 |
| `experiment / debug / repair search / report / review` | 冻结实验、定位、搜索和人工审阅 | S7–S10 |
| `merge` | 受约束的合并提案与验证，不直接执行副作用 | S11 |

精确参数格式和 JSON 输出在各阶段随接口契约冻结；本轮不生成 CLI 实现或可执行脚本。

### 6.3 目录结构（S1 以后逐步创建，本轮不生成）

```text
AgentGit/
├── src/agentgit/
│   ├── core/                 # 对象、事件、Commit、Branch
│   ├── runtime/              # 状态机、计划、任务执行
│   ├── adapters/             # 模型与工具边界；S4 execution/ 承载终端/Python 容器执行
│   ├── storage/              # 数据库、CAS、事务
│   ├── services/             # 用例服务、Job、预算、审批
│   ├── replay/               # S5：录制和真实重执行
│   ├── diff/                 # S5：结构差异，S8：行为对齐
│   ├── experiments/          # S7：冻结验证器和独立试验
│   ├── diagnosis/            # S8：故障候选与证据
│   ├── repair/               # S9–S10：搜索和复测
│   ├── merge/                # S11：可选
│   ├── api/                  # FastAPI，薄路由
│   └── cli/                  # Typer，调用同一服务
├── web/                      # S6：React + TypeScript + React Flow
├── migrations/
├── examples/research_agent/  # 固定离线任务与演示说明
├── tests/                    # 单元、集成、故障注入、端到端
├── doc/                      # 版本化设计、实施方案和界面规范
│   └── acceptance/           # 分阶段验收记录
├── compose.yaml             # S1：本地 PostgreSQL
└── README.md
```

## 7. 前端页面规划

S6 首版只做四个页面，保持产品闭环。色彩、字体、间距、圆角、组件状态和断点遵守 DESIGN.md；图和长表只在容器内滚动。涉及审批和暂停的控件放在 Timeline 详情区，避免增加独立功能页。

1. **Runs：**按仓库列出任务、状态、耗时、Token 与成本。
2. **Timeline：**时间线与 DAG；点击 Commit 查看状态、Prompt、模型、工具输入输出和关联 Artifact；执行工具详情显示命令/代码、stdout/stderr、退出码、截断、耗时和文件变化，沿用既有详情布局。
3. **Branch Experiment：**选择历史 Commit 和修改项，创建实验分支；显示 Replay 模式和副作用权限。
4. **Diff：**并排比较两个分支的状态路径变化、工具调用列表及最终产物。

S7 扩展 Branch Experiment 的重复试验与验证器配置；S10 增加 **Repair Lab**：失败断言、可疑决策、试验列表、修复方案、成本、复测样本和审批；S11 可选增加 **Merge Review**。修复补丁的语义和适用环境必须可见，测试响应替换不可提供“应用正式配置”动作。

## 8. 开发里程碑与验收

| 阶段 | 明确目标 | 核心测试门槛 |
|---|---|---|
| S0：当前轮 | Git 初始化、设计修订、详细方案 | 无源码、无依赖安装、文档互链有效 |
| S1：工程与契约 | 环境、分层、Schema、测试入口 | 空库迁移、格式校验、协议黄金向量 |
| S2：版本内核 | CAS、事件、完整状态、Commit、HEAD | 崩溃一致性、并发 CAS、跨项目隔离 |
| S3：离线 Agent | 计划、工具、记忆、产物、预算 | 离线研究任务、重规划、循环停止 |
| S4：可靠服务与必选执行 | Job、恢复、审批、API、CLI、终端/Python 容器执行 | 崩溃恢复、未知结果不重发；两工具真实执行、隔离/限额/子进程清理全过 |
| S5：版本调试 | Branch、Recorded/Live Replay、Structural Diff | 无网络回放、调用错配阻断、分支隔离 |
| S6：可演示 MVP | 四个页面、单一真实模型适配、五个必选工具 | 浏览器完整操作；终端/Python 真实执行及回放必过；真实模型冒烟另列 |
| S7：实验 | 冻结验证器、Hybrid、A/B、独立 Run | 相同起点与预算、验收器不可改、重复试验可查 |
| S8：定位 | 依赖对齐、20 个注入样例、Debug Report | Top-k 测量、证据完整、无参考运行也可工作 |
| S9：搜索 | 类型化干预、有界组合、预算、去重 | 单点/联合/无解/预算耗尽场景正确 |
| S10：研究闭环 | 缩减、独立复测、Repair Lab、30 个修复案例 | 无证据不标 verified、保留集隔离、同预算基线 |
| S11：可选合并 | 有证据的三方 Artifact/State 提案 | 30 组冲突样例、拒绝不更新 HEAD |
| S12：必选适配（D3） | MCP / LangGraph / 多 Agent / A2A 四项全部完成 | 专属测试、Recorded 零网络请求、核心恢复和隔离兼容全过；四份验收记录及 D3 汇总齐全 |

阶段的具体任务、测试 ID、输入与预期结果及验收证据统一见实施方案第 5 节。S0 规划基线已归档；后续实际实现状态见 [PROGRESS.md](PROGRESS.md)。工时是排期参考，不是已有完成度或交付承诺。

### 8.1 必须编写的自动化测试

- **DAG 正确性：**禁止循环、父 Commit 存在、相同输入得到稳定 Hash。
- **隔离性：**分支 A 的修改不能覆盖分支 B 的 Snapshot 或文件。
- **重放一致性：**在相同受控环境下回放得到相同状态 Hash；工具接口不会被真实调用。
- **并发一致性：**两个并发更新 HEAD 只有一个 CAS 成功。
- **副作用安全：**录制回放含发信 / 删除文件请求时不产生真实写操作。
- **执行安全与完整性：**终端/Python 只能操作本 Run 容器工作区；资源限制实际生效，无宿主执行回退；超时/取消清理整个容器，失败输出不能冒充成功 manifest，Recorded 不访问执行引擎。
- **合并安全：**相互冲突的事实 / 计划不允许静默合并。
- **修复安全与有效性：**失败的硬约束不可由补丁关闭；测试工具响应不冒充线上事实；预算中止不宣称无解；搜索结果在独立复测中验证；未审批补丁不修改正式 Agent。
- **安全：**敏感凭据不进入可分享日志；跨项目 API 不泄露历史对象。

## 9. 风险与工程取舍

| 风险 | 影响 | 应对 |
|---|---|---|
| LLM 随机性与模型升级 | “相同输入”结果不同 | 录制回放与真实执行区分；保存模型和参数 |
| 外部副作用不可逆 | 可能重复发信、修改文件 | 沙箱、审批、幂等键、效果分类 |
| 终端/代码越界或资源耗尽 | 影响宿主、其他运行或产生半产物 | 固定断网容器、无宿主挂载、独立采集身份、资源上限、整容器清理与输出校验；环境缺失阻断交付 |
| 上下文和状态过大 | 存储膨胀 | 首版完整状态 + 引用去重 + 大小上限；有容量证据后再做周期/增量快照 |
| Event 与 Snapshot 不一致 | 恢复错误 | 顺序号、事务边界、状态校验 Hash |
| Agent 差异难对齐 | 对比结果误导 | LCA + 事件依赖对齐 + 不确定性标记 |
| LLM 合并出现幻觉 | 错误事实混入状态 | Schema、证据绑定、约束校验、人工批准 |
| 搜索空间爆炸 | 试验次数、Token 和成本失控 | 候选定位、有界搜索、去重、逐层扩展和硬预算 |
| 自动修复过拟合验证器 | 在评估样本通过但真实任务退化 | 冻结验收器、保留独立复测样本、加入回归测试 |
| 修复候选绕过安全规则 | 看似成功但未经授权地执行操作 | 类型化补丁白名单、效果分类、沙箱、审批、拒绝策略绕过 |
| 凭据与隐私数据泄漏 | 安全与合规问题 | 字段脱敏、密钥引用、加密、RBAC、保留策略 |
| 接入过多协议 | MVP 失焦 | 先自研轻量 Runtime，再 MCP，最后 A2A |

## 10. 推荐学习路径及协议分工

- **Agent 基础：**ReAct、Tool Calling、状态机、短期记忆、上下文管理、终止条件与失败恢复。
- **MCP：**理解工具 / 资源 / 提示的接入方式；MCP 是工具能力连接边界，不承担版本控制。
- **A2A：**理解 Agent 间任务委派与通信；多 Agent 作为后期适配层，不应作为 MVP 前置条件。
- **OpenTelemetry：**Span / Trace ID 与运行事件关联；便于观测，但 Trace 不自动等价于可恢复 State。
- **Event Sourcing：**append-only 事件、投影、重建、一致性与幂等。
- **自动修复搜索：**Delta Debugging（ddmin）、Best-first / Beam Search、故障定位、实验设计、回归测试、统计置信区间。
- **Git 内核：**DAG、内容寻址、三方合并、引用更新、垃圾回收。

可优先阅读的官方资源：

- MCP：<https://modelcontextprotocol.io/>
- A2A：<https://a2a-protocol.org/>
- OpenTelemetry：<https://opentelemetry.io/docs/>
- LangGraph：<https://docs.langchain.com/oss/python/langgraph/overview>
- Git Internals：<https://git-scm.com/book/en/v2/Git-Internals-Plumbing-and-Porcelain>

## 11. 最小可演示版本（Demo Script）

1. 启动离线 Research Agent，输入“比较三个 fixture 框架并撰写报告”，界面明确语料与固定评估时间。
2. 运行结束后打开 Timeline：可以看到 `C1 → C2 → C3 → C4` 和对应工具事件。
3. 选择 C2 创建 `verify-official-docs` 分支，修改 Prompt 要求只采用官方来源。
4. 点击 Live Replay，生成 `C3' → C4'`；原历史仍在。
5. 在 Diff 页面看到新增验证工具调用、状态变化、结果评估及成本变化。
6. 选择来源 Run 和 C1→C4 范围，点击 Recorded Replay，确认模型/工具网络请求为 0，并重建相同逻辑状态和产物。
7. 人为注入一个“来源过时”故障并冻结验证器，在 Repair Lab 点击“Search Minimal Repair”。
8. 系统在同一个 checkpoint 上试验 Prompt Patch、Plan Patch 和二者组合；展示分支、试验次数、Token 成本及失败断言结果。
9. 对通过粗筛的组合运行缩减测试并做独立复测；输出“修复补丁集合、是否已复测、改动量、剩余风险”，人工批准后才允许写入正式配置。
10. 后续展示 Merge Proposal：选择两个分支的 Markdown 章节，出现一个事实冲突，审批前不能生成合并 Commit。

**项目完成的判断：**不只是页面做得像 Git，而是能证明“历史可恢复、分支可隔离、实验可比较、失败可定位、候选修复可搜索且独立复测、外部动作不误触发”。Semantic Merge 作为可验证的后续扩展。

---

*文档边界说明：CLI、API、数据库设计与时间安排均是 AgentGit 的建议实现方案，不代表现有第三方产品已提供这些接口。所述创新性属于项目差异化判断，并非经系统文献检索认定的学术首创；自动最小修复搜索只在明确的干预语法、验证器、环境与搜索预算内成立。*
