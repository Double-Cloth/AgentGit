# S11 Merge Review 与真实桌面闭环

2026-10-29，`codex/s11-merge`。按[桌面实施计划](../S11-desktop-plan.md)接通第六页“合并审阅”。本增量是 Windows Electron 开发构建（包版本仍为 0.1.6，`isPackaged=false`），不是更新分发包验收。新包、S11 人工预期以及用户暂缓的原生/人工前置继续；未进入 S12，也不据此宣告完整 S11 退出。

后端身份为 `source-sha256:6bd747bcfae2111d2857a9a51f6ebeb0bd9e7726b6c8e117c29a9ccb784761ff`，API 协议仍为 0.1.0，迁移 head 仍为 `0026_merge_runs`。新 Python 产品代码仅增加提案元数据发现、完整共同祖先查询和能力声明；已有语义合并、批准和执行契约未改。此前[正式 30×3 评估](S11-merge-evaluation.md)仍属于原冻结 release/源码，不能把本次界面例计为新研究成绩或改写旧 release。

后续分发验证另见 [Windows 0.1.7](S11-package-017.md)：新合并三条及全部 20 个不同包内节点均有通过证据，失败尝试及原始审计单列；本文件继续保留原开发构建的范围，不追溯改写为打包执行。

## 可见流程与业务边界

提案列表只读元数据，按创建时间/ID 倒序分页，限制 1–100，跨项目或不存在的游标拒绝。`open` 表示尚无决定，打开详情后才核对 stale/validated 等当前状态。`GET /merge-bases` 返回全部最佳共同祖先，多基点由操作者明确选择；同摘要多执行归属仍须显式 base Run。`semantic_merge` 与 `merged_run_initialization` 分别声明审阅和后续任务能力，缺能力不影响旧页面。

页面呈现固定请求、三方 Commit/Run/环境/状态、两个研究文件的原文和导出、候选计划/工作记忆/约束、结构化冲突和验证结论。原文仅以文本显示，不执行 Markdown/HTML；批准后的 base 明确为“未执行”，不把 null 用量显示为零成本 Run。

编辑提供 Source ID 的 base/ours/theirs/delete 选择、明确 Plan 替换和两个研究文件的完整文本/删除；保存是整套编辑替换。不能编辑权限、用量、效果、来源原文或验证结论。单文件上限与完整 HTTP 请求体的 1 MiB 限制同时适用，后者包含转义及其他字段；桌面不截断、不拆分修订，也不放宽 API 的统一传输边界。

未保存草稿禁止验证/准备票据/批准。新修订后必须重验；只有本次明确准备并经只读核对、完整匹配当前提案/修订/验证/候选和两侧固定来源的本人票据才能启用批准，仍须明确勾选。历史票据只读，断连、编辑、版本变化和重开不能恢复批准能力；后端在事务内继续重查。

修改防双击、冻结原参数/幂等键；未知结果仅允许用户用原键确认。断连禁用修改并保留草稿和已确认详情，恢复/切页/重开不自动 POST。过时提案不能批准，仍可明确拒绝当前修订。批准仅创建双父 `(ours,theirs)` 状态根；继续执行是另一个流程：建立/选择独立合并分支，输入确切配置版本和预算，再明确提交新 Task/Job/Run，不继承完成、消息、轨迹或授权。

主进程/preload 只增加 12 个固定业务操作，限制普通对象、字段、UUID、摘要、版号和有界 JSON；没有任意 HTTP、路径、Node、Shell 或 renderer 凭据通道。源文件只通过现有固定产物 ID 入口读取/导出。详情响应保持有界，超过限制明确报错。

## 当前实测

| 范围 | 结果与实际含义 |
|---|---|
| Python 单元 | 806 通过；当前产品源码，未与历史重复累计 |
| 只读真实集成 | 1 通过：分页/关闭元数据、项目和游标、限额、共同祖先与能力；列表禁止 CAS 读取/写入及执行，运行历史指纹保持 |
| TypeScript | 类型检查、构建及全部 40 单元通过；含固定路由/字段、精确门禁、两文件与转义后的完整请求超限拒绝 |
| 明确 HTTP fixture + 真 Electron | 1 条通过：多基点显式选择、双击、丢失响应的原键确认、编辑使票据失效、断连草稿保持、版本竞争、批准未知、显式新任务及两类重开只读；恶意 HTML 保持纯文本 |
| 真设施 + 真 Electron 正例 | 1 条通过：真实三方来源、完整修订、硬验证/本人票据、双父批准、独立分支、新任务零初始化、独立 Worker 完成、显式 Recorded、DAG 两父边及重开只读 |
| 真设施 + 真 Electron 负例 | 1 条通过：错误引文硬阻断，禁止获取合格票据和批准，明确拒绝、重开禁改；无新 Task/Job/Run/调用或批准 |
| 既有桌面边界 | 2 条通过：连接降级、旧四页与固定桥接/CSP/隔离，证据前缀 `S11-merge-legacy-v1` |

最终三条原生流程使用前缀 `S11-merge-freeze-v11`，全部通过，无失败、错误或跳过。每个真实用例使用 `agentgit_test` 中独立 `test_desktop_<uuid>` Schema、独立 CAS/API/Worker，完成后正常停止自有进程并删除自有 Schema；没有迁移业务库、调用付费模型或修改来源执行。332 个文件在种子执行前冻结，包含产品、迁移、两个锁、规范、桌面构建、配置与实际测试驱动；所有 Runtime 终态前不修改，归档时逐文件再次核对。

正例批准前后 jobs/runs/tasks/external_calls/configs 均不增加；随后仅一个新合并 Job/Run，初始化完成/消息为空、pending_tool 为 null、用量为零。新任务实际新增 10 次 Mock 模型、9 次本地工具调用，共 108,644 UTF-8 字节；Recorded 从该 Run 的首个实际初始化检查点到末端，调用数不增加，直接从合并状态根 Recorded 得到 `recording_missing`。三条原来源指纹保持。完整原始审计为 436 对象/978,518 字节、总 23 模型/18 工具；这些总数含来源准备，不与新任务计量混用。负例为 251 对象/519,606 字节，1 冲突、0 票据/批准、1 拒绝，仅保留来源的 13 模型/9 工具。

原始源/终态、CAS 字节、冻结文件、XML、操作收据和截图保存于忽略目录 `.agentgit/evidence/S11-merge-desktop-v1/`，完整文件级摘要见 `summary-v1.json`。此前失败和 v5/v7/v8/v9/v10 的不同范围保留，不累加成更多不同用例。最初两次真实设施失败分别是驱动误把多个配置当单行、误从按 Job 索引的请求表读取 run_id；均在停止自有 Runtime 后修正，再完整重跑。界面测试发现的断连清空草稿已修复。最初仅对 HTTP 替身测试的大请求设想不适用后端统一上限，已撤回，实际接口没有扩大额度。

另一个 Python 进程只读取复制后的原始包，分别重跑 `verify_run_pair` 和 `verify_pair`，两项通过；没有沿用执行进程内的分数作为审计输入。`summary-v1.json` SHA256 为 `9425fcb99217f44520189aa5fdb811b482a194dbf627a8593076ffcaf74eecf4`，`offline-audit-v1.json` 为 `f4a8172caf83083fef4e871d7c53d6ff0cac097d7b7c1841ccfa9190eb0f9a76`。

## 视觉及保留项

遵守既有 DESIGN、字体、暖白/珊瑚与焦点规范；1440/360 整页、首屏、三方与决定视口均核对，无整页横向溢出。旧 Timeline 两父边实际存在；该页面没有为本增量重设计。第一次长元素截图出现视口外固定 skip link 的捕获伪影，已改为滚动到区块顶部后捕获真实视口，旧图片不作为最终局部证据。

impeccable 检测器仅运行一次，3 条均涉及既有字体/旧 Diff 边框，遵守项目规范保留。独立新上下文审阅结论为 `ship`、无 material fixes；文档复核为 `No changes`，保留 DESIGN，不新增设计 sidecar。当前 harness 没有专用 agent 类型，使用独立通用审阅/文档代理执行技能清单；未把代理审阅冒充用户人工验收。视觉结论覆盖新增页面和已提供截图，批准后新任务只核对源码及功能试验，不声称该状态已获得截图视觉批准。

用户已明确暂缓独立 Windows 安装/升级/卸载、原生交叉验收及 S8 标签，继续工程实现。S11 的人工预期、新 Windows 候选及完整阶段退出核对仍须继续；本增量未合并 main、未发布远端。

```powershell
uv run --locked pytest tests/unit -q
uv run --locked pytest tests/integration/test_merge_discovery.py -q
uv run --locked mypy src
uv run --locked ruff check src tests desktop/tests/support/merge_backend.py
cd desktop
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S11-merge-new'
npx.cmd playwright test --config playwright.merge.config.ts
```
