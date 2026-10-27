# S11 首增量：共同祖先与只读产物候选

2026-10-27，依据 [实施计划](../S11-merge-plan.md)。当前只是 S11 的只读首增量，不表示 D2 前置、S11-T01–T06、三十组样例或双父历史已经验收。

## 已实现范围

`core/merge.py` 固定冲突 taxonomy、阻断级别、三方 BLOB 证据和共同祖先选择结果。`merge/ancestry.py` 在最多 10,000 个不同节点内计算全部最佳共同祖先：交叉合并保留歧义，选择必须属于最佳集合，不按摘要或路径距离自动挑选；非法父关系、环、重复和超限明确失败。纯图支持两个父；当前数据库读取器仍使用原单父 Commit Schema，不提前放开写入约束。

`merge/artifacts.py` 支持有限 UTF-8 二级 Markdown 章节和既有 `ResearchReport` v1 的 `sources.json`。独立章节/实体变更合并，原始字节与引用绑定；同章异改、删除与编辑、互斥事实、评估时钟或元约束变化保留冲突。来源整体选择，不把两个不同来源对象的字段拼成“证据”。未知文件/Schema、重复身份、章节重排、非法 UTF-8、摘要不符和超限均拒绝。无冲突的候选仍包含 `state/provenance/task_constraints/approval` 待办；候选不代表事实已被硬验收器认可。

`services/merge.py` 从同项目 Commit 索引、manifest、真实快照和 CAS 读取，重算字节摘要；提供基点查询与单文件预览。没有提案批准、HEAD 写入、工具调用或自动运行入口。

## 验证

| 范围 | 当前结果 |
|---|---|
| 新共同祖先/产物单元 | 43 通过；含交叉合并、旧祖先捷径、1,200 节点非递归解析、80 个固定生成 DAG 与独立可达 oracle、严格 Schema、代码围栏、CRLF、删除/编辑、缺换行导致的章节黏连、事实/时钟/来源冲突 |
| 全部后端单元 | 743 通过，包含上述 43 项，不重复累计；现有 Starlette/httpx 弃用提示保留 |
| 新真实 PostgreSQL/CAS | 3 通过：独立章节、同章冲突、跨项目不可访问；初次及临时归档目录修正后复验均通过，不按执行次数累计节点 |
| 现有版本历史集成 | 7 通过：Fork、来源范围、只读浏览、隔离及历史行为保持 |
| 静态检查 | 新文件 Ruff 检查/格式通过；全 165 个产品源文件 mypy 通过 |

真实预览的 SQL 逐条检查仅为 SELECT，同时禁止 CAS `put`；所有表记录数、CAS 文件/字节摘要和 main/ours/theirs 三分支 HEAD/version/活动写入标记在前后完全相同。源历史由测试 fixture 经既有提交服务创建，没有 Runtime 或实际模型/工具执行；这三项不能冒充正式双父 Merge、真实研究任务或桌面操作。

初次两项的三方原始 CAS 与结果在忽略目录 `.agentgit/evidence/S11-preview-v1/{independent,conflict}`，每项 13 个物理对象。独立核对各文件 SHA256、三方 Commit/State/BLOB 域 hash、父关系、候选证据与确切正文：独立例只合入左右各一章，同章冲突的候选内容为 null。原始补审 `S11-preview-raw-audit-v1.json` SHA256 为 `7989fedce723d07f41d0f75be3069643a102582b7d2928846c60b0476dfc8e1b`。

本增量产品身份为 `source-sha256:4aaab4a37f713b9a2bbad1b7a60c9dd6bc2e55279d099321a4f7ed4fbf5dc227`。审计另外实际验证旧 S8 effects v3、S10 evaluation v1 的 guard 拒绝新环境；没有改旧 release、原始结果、预算或旧源码快照。初版集成测试将证据目录固定为只创建一次，现已改为每次测试自己的 `tmp_path/preview-evidence`，日常回归可重复运行；初次原始包继续保留。

```powershell
uv run --locked python -m pytest tests/unit/test_merge_ancestry.py tests/unit/test_merge_artifacts.py -q
uv run --locked python -m pytest tests/integration/test_merge_preview.py tests/integration/test_versions.py -q
```

JUnit 为 `S11-kernel-unit-v2.xml`、`S11-existing-unit-regression-v1.xml`、`S11-preview-integration-v2.xml`、`S11-version-regression-v1.xml`。后续继续 Fact/TaskNode/约束/副作用投影与硬验证，再接耐久提案/审批、双父兼容、Merge Review、三十组及基线、分发候选；完整 S11 验收后进入 S12。
