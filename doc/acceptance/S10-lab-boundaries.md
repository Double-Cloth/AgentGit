# S10 Repair Lab 真实取消与未知结果边界

2026-10-27，分支 `codex/s10-repair`。本增量在真实 Windows Electron、认证 API、独立 PostgreSQL schema、独立控制器/普通 Worker 和 CAS 上核对取消与结果未知。使用固定 Mock 和预先声明的首次确认故障注入，没有付费调用、业务库迁移或业务配置采纳。延续[联合修复流程](S10-lab-real.md)和[候选交接](S10-candidate-handoff.md)，不替代完整 S10/D2 验收。

## 实测边界

第一例只暂停自己测试 Worker 的领取循环；从页面原子准备搜索后双击取消，再恢复该 Worker。取消落实为终态，搜索 trial 为零、没有新 Job；前后均为 0 Job、5 Run、1 配置、0 决定，来源指纹相同。实际只有一次取消 POST，缩减动作禁用。没有暂停外部 Worker、数据库或 Docker。

第二例先完成真实 Prompt 搜索和字段缩减，再由可见页面锁后开启原预注册池。固定策略在第一次确认模型调用已发出后返回 OUTCOME_UNKNOWN；策略、既有 fixture 以及两个 helper 的摘要均已事先写入来源和回归 Goal。首次确认只有一个新 Job/Run，关系索引和 CAS 中有模型请求意图，没有响应或验证收据；模型调用一次，没有将未知标为失败、成功或零消耗。

页面明确显示“结果未知”和“预留继续保留”，该试验调用/费用均为未知，未结算试验为一；没有可用验证通过结论，生成精确建议禁用。显式取消后研究为“取消中，等待安全结算”；关闭再打开窗口、只读打开同一搜索后仍有一项 active 预留、一个确认 trial、同一未知调用，cancel_requested 保持，verification 为 null。页面的 needs_review 暂定统计不是终态通过。取消和重开不释放未知预留、不自动重发，不伪造取消成功。最终 7 Job、12 Run、7 配置、0 决定；重开前后这些计数、来源指纹和所有 POST 完全相同。当前会话只发出一次研究取消 POST。

界面在试验行使用 waiting.reason 展示结果未知，阶段状态补上中文的等待核对/取消中说明；保留原始收据与执行入口，沿用现有视觉体系。1440/360 请求视口的实际截图已批量检查，没有页面整体横向溢出、相关控制台错误、pageerror 或 Vite 错误遮罩。窄视口是补充渲染证据，不替代原生 DPI。

## 复核与范围

两条边界、既有真实候选交接一条、明确 HTTP fixture 的页面/桥接两条、35 桌面单元，共 40 个不同测试节点通过，无失败、错误或跳过；不累加前次重复执行。typecheck、构建、Ruff/格式及 diff 检查通过。既有 React Flow 的构建指令提示仍是客户端包的历史提示，不是运行期错误。

独立复核器从保存的 CAS 原字节重算类型域摘要，核对唯一未知模型请求的原始 SHA-256、调用意图/Job/Run/确认 trial 关系、冻结策略 Goal 和空响应；保留前后原始索引、调用次数和截图。证据在忽略目录 `.agentgit/evidence/S10-edges-summary-v1.json` 与 `S10-edges-unknown-archive-v1.json`，正文和运行数据不进入安装包。测试结束时自己的 Worker 正常退出、各自 schema 删除；这属于隔离测试清理，不表示未知业务结果已被核对或预留已在业务库释放。

共用联调 bootstrap 只增加显式 seed/命令扩展点并以实际入口文件启动自己的子进程，默认联合用例保持；真实候选交接兼容复验通过。Python 产品包身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 仍为 `0023_repair_proposal_reviews`。旧记录冻结的 helper 字节对应其历史提交，当前 helper 修改不篡改旧归档或伪装成旧运行。

```powershell
uv run --locked ruff check desktop/tests/support/repair_backend.py desktop/tests/support/repair_boundaries_backend.py
uv run --locked ruff format --check desktop/tests/support/repair_backend.py desktop/tests/support/repair_boundaries_backend.py
cd desktop
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S10-edges-real-new'
npx.cmd playwright test --config playwright.repair.config.ts repair-boundaries-real.spec.ts
```

真实观测补丁桌面边界、新分发包与包内复验、30 例/四基线和 D2 继续。S11/S12-A/B/C/D 仍为必选；独立 Windows/原生验收和 S8 人工标签按用户指示暂缓，没有宣告项目或阶段完整完成。
