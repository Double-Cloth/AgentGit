# S10 故障候选到修复实验室的显式交接

2026-10-27，分支 `codex/s10-repair`。补齐故障候选页面到 Repair Lab 的交接；沿用既有组件、设计和受限桥接，不新增依赖、接口、迁移或自动修改动作。此前时间线入口及[真实联合修复流程](S10-lab-real.md)保持。

候选先通过既有只读上下文核对来源及模型决策前检查点，才显示“在修复实验室核对候选”。点击时重新读取冻结验收器，验证来源 Run、候选证据、请求事件 ID、原验收器引用和 screen 划分。交接使用上下文的请求事件，不把候选模型输出事件当作干预目标；缺失、跨 Run、非法引用或 confirm/regression 样本拒绝。切换项目/连接清除交接，时间线入口不继承候选预填。

Repair Lab 只预填失败 Run、起点、screen 验收器和一个明确事件。四项回归、预算和执行授权没有预填；缺少必要输入时搜索保持禁用。没有继承候选排序为因果结论，没有自动创建搜索、Job、Run 或审批。使用完整有限模板的降级说明和 S8 定位未达标提示保持。

真实 Windows Electron 连接独立 PostgreSQL/API/Worker/CAS，以固定 Mock 产生实际失败 Run 和静态报告，经上下文核对后操作交接按钮。前后均为 0 Job、5 Run、1 配置、0 决定，唯一 POST 是此前显式创建静态报告；来源指纹一致。随后显式冻结 confirm 验收器，在同一报告实际核对上下文后再次点击，页面显示 screen 限制并停留在原报告，前后数据库计数和修改请求完全相同。该控制用例不执行搜索，也不计为新的修复成功案例。

1440/360 请求视口批量检查来源按钮及目标表单截图，长 JSON 在自身容器滚动、没有页面整体横向溢出；运行身份、非空内容、相关控制台错误、pageerror 和错误遮罩均检查。浏览器插件不可用，使用项目既有 Playwright 原生 Electron。保留第一次已通过的交接记录，随后为新增实际 confirm 拒绝覆盖再执行，不累计重复运行。当前 35 桌面单元、真实交接 1、Repair Lab fixture/桥接 2、既有真实诊断链路 1 共 39 个不同测试节点全部通过；typecheck、构建和 diff 检查通过。React 检查沿用按需页面、稳定 Hook 顺序及事件中发起的只读查询，没有渲染时网络副作用或新持久存储。

忽略目录 `.agentgit/evidence/` 中 `S10-handoff-summary-v1.json` 冻结选取 XML、原始操作、四张截图、源文件和构建摘要。Python 包身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 仍为 `0023_repair_proposal_reviews`。测试自己的 Worker 正常退出、schema 删除，业务库未迁移。

复现：先在 `desktop` 下执行 `npm.cmd run typecheck`、`npm.cmd test`、`npm.cmd run build`，设置唯一 `AGENTGIT_ELECTRON_EVIDENCE_PREFIX`，再运行 `npx.cmd playwright test --config playwright.repair.config.ts repair-handoff-real.spec.ts`；需仓库测试配置和 PostgreSQL，运行中保持 Python 包及 fixture/helper 不变。fixture 及旧诊断兼容复验分别显式选择其 spec，并使用其他证据前缀。

当前 0.1.5 分发候选尚不包含 S9/S10。其余真实未知/取消/观测边界、新分发包、30 例/四基线及 D2 继续；S11/S12-A/B/C/D 保持必选。独立 Windows/原生验收及 S8 人工标注按用户指示稍后补验，不据此宣告阶段退出。
