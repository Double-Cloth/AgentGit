# S10 新三十例输入资产及开发正常控制

2026-10-27，分支 `codex/s10-repair`。生成 30 套新公开输入及 13 家族的拟议故障规格，冻结输入边界与 20/10 划分。52 项契约、20 项真实 PostgreSQL 正常 Run 共 72 个不同节点通过，无失败、错误或跳过；Ruff/格式及 diff 检查通过。当前仅输入准备，不宣称故障集、四基线或 D2 已验收。

## 数据与标签隔离

输入文件位于 `tests/fixtures/repair/v1/`。单点/联合/观测伪修复/空间无解/副作用约束为 10/8/4/4/4；开发/保留为 6/4、6/2、3/1、3/1、2/2。13 家族按完整组划分，不能将同模板改名跨 split。R01–R20 开发、R21–R30 保留；开发集 R01–R12 只是预先声明的可修复假设，尚未经故障/修复执行验证，分母不能按结果改。

三十个公开 profile 与故障标签独立排序，每项材料化六个新 Source、九条事实、固定时间和自然语言 Goal，公开输入摘要全部不同。仍在既有三合成框架的比较领域内，不冒充三十种真实业务领域。新文档、来源 ID 和正文不复制旧 S8/工程 fixture；共同的生产 Mock、报告结构和硬验收器继续复用。

`task_for_case` 返回严格 TaskInput，只包含 Goal、时间、Sources；id、split、family、pattern、category 和 declared_repairable 不进入 Agent 状态。变更标签/划分的变形测试保持公开输入相同；缺例、重复输入、家族/模板跨划分、伪造人工 gold、按已知注入点选起点、预填 passed 均拒绝。源码/字节、资料摘要或划分变化使冻结校验失败。

这些家族目前是 agent_proposed/unverified 规格。结构层隔离不能单凭家族名证明语义新颖；绑定注入器时须再审查共享变换、因果结构与标签隔离。正常输入未包含真实故障；如故障条件派生语料，必须在新运行 release 中冻结其输入/语料身份并对所有方法保持一致，不覆盖基准输入或临时放宽 hard oracle。

## 实际构造验证

30 套输入的静态构造均可形成符合原硬验收的九条结论，日期、引用/框架/维度和正文摘要核对通过。该层由构造工作记忆验证，不当作真实 Run。另仅对 20 个开发输入，在各自独立 test_* schema 和 CAS 中运行正常 MockResearchModel；经过实际研究 Runtime、Gateway、模型/工具录制、持久 Run/检查点和两份产物，再用执行前冻结的原验收器检查。

每项真实控制为 11 次新模型调用、9 次工具调用；共 220 模型、180 工具、2,193,854 Mock UTF-8 字节，单项 108,754–110,614 字节，低于既定单 trial 200,000 上限，没有扩大预算。实际请求的 Goal 与公开 Goal 完全相同，没有编号或故障模板。二十个不同成功 Run、二十个不同输入分别归档，每份 211 个 CAS 对象；原字节按仓库求和为 8,068,914，不宣称跨仓去重。

独立审计从原字节重算类型域摘要、归属、原始请求响应用量、冻结 Goal/语料/配置、最终状态和硬验收。归档沿用既有证据编码器，其 failed/reference 槽都明确标为成功控制 Run 的只读别名，没有真实失败 Run 或故障注入，不假装生成了修复证据。这里直接执行研究 Session，没有声称新派发了修复 Job 或独立 Worker 搜索。

保留 R21–R30 没有真实 Run、故障、候选搜索或成绩，只有输入契约检查。20 个正常通过不证明开发 12 例可修复，更不等于 10/12。测试 schema 正常清理，没有迁移业务库、调用供应商或改变产品/分发包。

## 冻结与后续

`inputs-release.json` 的 SHA256 为 `1f4f6a9d960bb067cb260ccf01266a01c69213f8881ca0b1be0d3d25f1b5e0a0`，范围为 public-inputs-only/not-fault-runtime-or-evaluation，fault_runtime_ref=null、heldout_evaluation=not_started。绑定 cases、profiles、builder、方法资产及全部 train/heldout ID；完整故障运行 release 仍未建立。

最终证据为忽略目录 `.agentgit/evidence/S10-input-assets-v1.xml`（52）、`S10-input-controls-v1.xml`（20）、`S10-input-controls-v1/`、`S10-input-controls-audit-v1.json` 和 `S10-input-assets-summary-v1.json`。早期 46 项构造检查不与最终重复累加。产品身份保持 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 仍为 `0023_repair_proposal_reviews`，0.1.6 包字节未变。

```powershell
uv run --locked pytest tests/integration/test_repair_input_assets.py
$env:AGENTGIT_S10_INPUT_CONTROL_DIR='.agentgit/evidence/S10-input-controls-new'
uv run --locked pytest tests/integration/test_repair_input_controls.py
```

下一步实现并冻结故障材料化、四项语义回归和正式五方法运行协议；局部基线必须从公开首异常的合法局部起点生成实际候选，不能把旧工程共同空间的不适用结果推广为正式基线零分。随后按冻结规则执行开发与保留、完整报告四基线、失败/无解和 D2。S11/S12-A/B/C/D、用户暂缓的独立 Windows/原生验收及人工标签保持必选/待补，不宣告阶段整体完成。
