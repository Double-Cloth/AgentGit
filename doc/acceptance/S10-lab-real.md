# S10 Repair Lab 真实设施联调

2026-10-27，分支 `codex/s10-repair`。真实 Windows Electron 经既有认证 API、独立 PostgreSQL schema、独立搜索/缩减/确认控制器及普通 Worker、CAS，完成一次联合 Prompt/Plan 修复。模型是固定 Mock，结论限于 `verified_under_fixture`；未调用付费模型、迁移业务库或采纳业务配置。此项补齐[前次页面验收](S10-lab.md)中同一设施链路的缺口，不代表 S10/D2 整体验收。

## 实际操作和结果

操作者在可见页面填写失败 Run、模型决策前起点、冻结验收器、有限事件、四个不同回归来源和完整预算，显式授权后双击准备；实际只有一次准备 POST。搜索保留失败试验，随后显式缩减、锁后开池、生成精确建议、审阅和批准。采用本例显式预算 reduce=16、max_trials=40，其余资源仍受来源授权约束；S9 默认深度二和 reduce=6 未扩大。

字段阶段完成 6 次字段试验和 4 次变化后的原子必要性复查，接受两项字段缩减、未移除原子，没有待测引用或排除的变换。原子结论为 `one_minimal_under_fixture`，字段结论为 `one_step_minimal_under_fixture`，均限于预先声明的有限范围，不声称全局最优。页面此前读取字段结果外层，误把真实执行用量显示为未知；现优先读取 `result.execution`，保留直接执行收据的兼容路径，真实页面断言覆盖此修正。

最终锁后六次确认和四项回归全部通过，十个不同新 Run 共 58 次新 Mock 模型调用、58 条响应记录、10 个不同响应引用，计量 655,324 Mock UTF-8 字节。每个投影的 Prompt/Plan 与建议 after 完全一致。独立审计从归档 CAS 复核 1,723 个对象、4,497,543 原始字节，重算对象类型域摘要、硬断言、阶段用量、票据/封存时序、派发归属和配置映射；不只相信页面计数或数据库状态。

首次审阅后显式在同一来源配置基础上创建一个测试版本，实际触发配置计数器变化；只读刷新禁用旧票据，重新核对后取得新票据。来源配置版本 1、重新核对的项目版本 28、批准创建版本 29；只允许 Prompt/Plan 改变，工具、权限、模型、限制和执行策略保持原值。双击批准只有一次 approve POST，决定为 `execution=not_started`。批准前后 Job/Run 数量不变，配置和决定各加一；最终为 27 Job、32 Run、29 配置、1 决定。来源运行指纹始终一致。重开同一窗口配置后只读打开搜索及既有决定，没有新 POST。

1440/360 请求视口的真实 Electron 截图已检查，配置对照按既有断点堆叠，长 JSON/表格在自身容器滚动，页面没有整体横向溢出。没有 pageerror、相关控制台错误或 Vite 错误遮罩。360 是补充渲染检查，不替代原生 DPI。测试结束后独立 Worker 正常退出、仅本测试 schema 被删除，CAS 归档保留。

## 复验和探索记录

当前选取的不同测试节点共 38 项：真实 Repair Lab 1、明确 HTTP fixture 的页面/桥接 2、既有真实诊断链路 1、桌面单元 34，全部通过，无失败、错误或跳过。后三组分别验证既有 UI 行为和共用联调进程控制兼容性，不将 fixture 计为新的真实修复案例。typecheck、Ruff/格式和 diff 检查通过；后端包源码未变，不把历史 700 单元重复计入本轮。

第一次真实运行已完成批准，但测试误读 createAgent 的 `config_version`，实际返回字段为 `version`，导致期望 NaN；修正断言。第二次在最终审计才读取共享用量，重复累计确认用量；改为开池前捕获不可变缩减快照。该次 helper 已异常退出，测试继续发送控制请求后等待不结束；已修复控制通道退出/错误时拒绝待处理及后续命令，并增加正常停止后命令必须拒绝的断言。仅核实并终止当次测试自己的进程树，未停止外部服务。v3 完整运行和其后既有诊断链路通过；早期失败/中断不计通过，记录随证据摘要保留。

包源码身份保持 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 保持 `0023_repair_proposal_reviews`。新测试 helper 自身摘要及既有 fixture 摘要冻结在任务 Goal；没有引入依赖或迁移。

忽略目录 `.agentgit/evidence/` 保存 `S10-native-real-summary-v1.json`、`S10-native-real-archive-v1.json`、原始 native 操作、两张截图和逐项 XML；摘要冻结本次文件、报告、构建和独立审计脚本。原始数据不进入提交或分发包。

```powershell
uv run --locked ruff check src tests migrations desktop/tests/support/repair_backend.py
uv run --locked ruff format --check src tests migrations desktop/tests/support/repair_backend.py
cd desktop
npm.cmd run typecheck
npm.cmd test
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S10-native-real-new'
npx.cmd playwright test --config playwright.repair.config.ts repair-real.spec.ts
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S10-native-ui-new'
npx.cmd playwright test --config playwright.repair.config.ts repair-bridge.spec.ts repair-lab.spec.ts
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S10-native-diagnosis-new'
npx.cmd playwright test --config playwright.diagnosis.config.ts diagnosis-real.spec.ts
```

继续真实未知/取消/观测边界、既有候选交接和新候选包复验；当前 0.1.5 分发包未包含 S9/S10。30 例/四基线、D2、S11、S12-A/B/C/D 均保持必选。按用户明确指示，独立 Windows 安装/升级/卸载等环境验收与 S8 人工标注审阅暂缓，仍记待验收。
