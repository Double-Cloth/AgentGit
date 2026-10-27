# S10 Repair Lab 真实观测补丁禁采纳

2026-10-27，分支 `codex/s10-repair`。真实 Windows Electron 经认证 API、独立 PostgreSQL schema、独立控制器/普通 Worker 和 CAS，完成观测补丁的搜索、缩减、六加四和仅保存实验。复用此前冻结的输入顺序故障及同参只读录制，并将 fixture、本轮两个 helper 的摘要写入来源和回归 Goal。使用免费固定 Mock，没有迁移业务库、采纳业务配置或生成新分发包。

## 同次链路证据

在实际 donor Run 中生成完整、不同顺序的 corpus_search 录制；失败来源沿正常顺序产生硬断言失败。操作者从页面显式填写失败来源、模型决策前起点、screen 验收器、具体工具事件、完整替换调用 ID、Hybrid 范围、四项固定回归、原预算和授权，再提交原子准备。没有修改来源语料、放宽权限或更换验收器。

搜索找到 TOOL_RESPONSE_PATCH；实际删除该唯一原子失败后保留。观测字段禁止修改，字段结果为 no_applicable_fields，明确列出 observation:field_change_not_allowed，不声称已执行不存在的字段试验。最终锁后六次确认和四项回归全部通过，十个不同新 Run 均有新模型调用并标 test_only。六次确认是带一个 synthetic 观测的 Hybrid，四项回归是来源之外的 Live；观测来源、确切补丁和适用范围完整保留。

建议 after 为 null，adoption_scope 为 save_experiment_only，production_eligible 为 false。可见页面显示观测限制并禁用“批准创建配置版本”；正常审阅后“仅保存为实验”可用。独立负向请求实际调用批准接口，被 API/服务以 403 forbidden 拒绝，没有新配置或决定；没有仅凭按钮禁用宣称后端安全。

随后从可见页面双击保存，只有一次成功修改请求；保存定义的 patches 与精确建议逐项相同，决定为 save_experiment、execution=not_started、new_config_id=null。保存前后 Job、Run 和配置数量不变，决定增加一条；最终为 13 Job、19 Run、13 配置、1 决定。配置数量包含此前筛选/验证的隔离 trial 配置，不能把它们误当正式采纳。来源运行指纹始终相同，窗口重开只读发现已存决定，计数和 POST 均未变化。后台所有 Job 都能归属筛选/缩减/验证派发，没有保存触发的执行。

## 原始审计和复现

本增量新增一条真实设施测试并通过（2.7 分钟），不是新增一项可正式采纳修复，也不与[上轮 40 个不同节点](S10-lab-boundaries.md)重复累加。typecheck、Ruff/格式与 diff 检查通过；产品界面构建沿用上轮相同字节，没有改动视觉或产品 Python 包。1440/360 请求视口截图批量检查通过：限制文字可读、长补丁/配置和表格在自身容器滚动，没有页面整体横向溢出、pageerror、相关控制台错误或 Vite 错误遮罩。Browser 插件不可用，使用既有 Playwright 原生 Electron。

独立复核从 1,445 个 CAS 对象、2,694,896 原始字节重算类型域摘要、硬验收、任务/语料/配置/请求来源、实际用量、预注册/锁/开池先后和保存定义。六加四共有 110 次新 Mock 模型调用、110 条响应记录、18 个不同响应内容引用，计量 967,266 Mock UTF-8 字节；独立生成可以得到相同文本，没有为摘要不同而筛选响应。观测使 fixture 达标不证明正式配置资格，Mock 字节不冒充供应商 Token 或账单。

原始证据在忽略目录 `.agentgit/evidence/S10-observation-native-archive-v1.json`、`S10-observation-native-summary-v1.json`、操作记录、截图及 XML，摘要冻结原始审计脚本、文件与构建字节。沿用独立 falsefix 审计器逐项核验，不只相信 UI/API 自述。自己的 Worker 正常退出、schema 删除，CAS 留作审计；业务服务未停止。

产品包身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 仍为 `0023_repair_proposal_reviews`。共用 bootstrap 只增加显式证据 case 名参数；旧默认行为保持，历史 helper 字节仍由各自提交和证据摘要固定。

```powershell
uv run --locked ruff check desktop/tests/support/repair_backend.py desktop/tests/support/repair_observation_backend.py
uv run --locked ruff format --check desktop/tests/support/repair_backend.py desktop/tests/support/repair_observation_backend.py
cd desktop
npm.cmd run typecheck
npm.cmd run build
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S10-observation-real-new'
npx.cmd playwright test --config playwright.repair.config.ts repair-observation-real.spec.ts
```

下一步更新 Windows 分发候选并做包内完整兼容复验，再继续 30 例/四基线和 D2。S11/S12-A/B/C/D 保持必选；独立 Windows/原生验收与 S8 人工标注按用户指示暂缓，不把本例当作这些验收或完整阶段退出。
