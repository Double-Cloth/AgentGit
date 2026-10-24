# S8 桌面报告与候选干预工程验收

2026-10-24，在 `codex/s8-diagnosis` 上接入现有 Diff/Timeline。保持四个主页面、DESIGN 的字体/色彩/断点及 Electron 主进程业务白名单；没有新增依赖或 Python 产品代码。本轮后端源码仍为 `source-sha256:7f76f71d0a4f84551e4b88f14540a0e29de2c844f5b93cccb9b7dda7f2ac3c9f`，与[候选干预验收](S8-candidate-experiments.md)一致。

## 可操作流程

1. “差异”页增加“结构化差异 / 故障定位”入口，原比较表单继续可用。定位入口核对 `static_debug_reports` 能力，可显式提交失败 Run 和可选成功参考；也可分页发现或输入 UUID 打开已冻结报告。
2. 展示硬断言、完整候选排名与并列、理由、特征、不确定项、支持事件、输入/工具异常及环境变化。证据不足明确称为完整候选回退，原静态报告始终与干预效果分开。
3. 展示冻结的行为对齐、最小分歧前沿、仅单侧出现、重复身份歧义及双方缺口。候选和证据按 50 项分页；完整报告、试验原文仅展开时生成 JSON 视图，避免每次键入都重建大型原文。
4. 点击事件进入 Timeline，独立请求 `after_seq=目标seq-1&limit=1`，交叉核对 Run、seq、数据库 ID，并把焦点移到目标证据标题。最近 2000 条事件窗口不限制精确跳转；缺 ID 不生成按钮，返回相邻事件或 ID 不符明确报错，没有补猜。
5. 核对候选上下文后，可在报告中显式保存一个 `PLAN_PATCH` 或 `PROMPT_PATCH`，填写完整内容和理由。前置条件、目标调用及候选证据来自已核验上下文；后端仍检查类型、安全检查点、权限和已完成节点。
6. 保存后“前往 A/B 表单”带入原来源、检查点、验收器、真实 split 和单项干预 ID，A 沿用来源，B 只预填该干预。交接仅初始化表单；重复次数、预算和提交仍由既有 S7 流程显式确认，不另行冻结验收器或启动 Job。
7. 返回报告输入已有 Experiment UUID，核对它与当前候选的单变量关系，显示全部 trial、精确统计、固定设置及限制。未完整判定时改善未知；完成的条件比较不改写原静态报告，不证明严格因果或 Top-k 定位优势。

报告与干预的未知响应固定原内容、幂等键并阻止双击；明确拒绝才解除锁定。报告 GET 失败可用相同 UUID 重试。切换页面和重开窗口只查询，不自动重发修改；项目/端点切换清除候选交接状态。未受信任的文本按 React 文本/JSON 展示，不执行 HTML、脚本或代码。

## 验证结果

本轮去重 **35 项通过**：30 项 Node 单元、2 项新 Electron 专项、2 项原四页/连接回归、1 项原受控实验边界。`npm run typecheck`、`npm run build`、`git diff --check` 通过。Python 产品源码未变，没有把此前 552 项后端结果重复计入本轮，也未重新运行冻结的 20 例排名。

| 检查 | 实际证据 |
|---|---|
| 页面身份及非空 | Windows x64 Electron，`agentgit://app/index.html`，标题 AgentGit；真实报告、候选和 trial 可见 |
| 框架覆盖层/控制台 | 无错误覆盖层；最终新专项 pageerror/console error 均为 0 |
| 交互 | 明确拒绝后可修正；未知 POST 原键确认；同报告 GET 失败后可重试；候选切换、对齐展开、精确事件跳转和键盘焦点均实测 |
| 真实闭环 | 独立 PostgreSQL Schema、API 和独立 Worker；来源 Mock 正常运行，再构造省略 `write-sources` 的失败分支；由桌面保存计划干预、带入 A/B 表单并显式提交 |
| 结果与隔离 | 10 个独立 Job/Run；A 0/5，B 5/5 通过；原报告完整内容、来源指纹不变；重开后没有多发报告 POST |
| 未完成及不确定性 | Worker 门控期间显示改善未知；HTTP fixture 覆盖取消状态、并列、缺口、无精确 ID、被替换事件及恶意文本转义 |
| 响应式 | 1440/1024/768px，另补充 360px；均无页面横向溢出，长内容在内部滚动；真实八候选逐项检查按钮内容未被压缩裁字 |
| 只读/白名单 | 所有新桥接绑定当前项目，仅固定路由；路径注入、多余字段、错误 UUID、布尔/非安全整数事件 ID 在请求前拒绝 |

该闭环是独立 Mock 工程示例，不属于冻结 20 例，不替代保留集注入点 Top-3 的 4/8。三个本地工具完成研究流程，没有调用外部供应商或执行宿主 Shell。实际干预内容、验收器、试验原始计量和状态保留于 `real.json`，不把 API 查询开销混成零成本修复。

视觉检查发现并修复了新表单的按钮基线错位，以及 768px 下八候选被受限 Grid 行高压缩的问题；最终 `grid-auto-rows: max-content` 保留按钮完整文字。没有更换颜色、字体或原页面视觉体系。

前期失败/诊断记录保留：项目创建后未等待选中确认、短暂连接/数据库就绪检查未确认、交接测试未等待来源核验，以及测试用递归 JSON 类型触发的 TypeScript 推导错误。测试改为等待明确 UI 状态、核验原生表单有效性；仅对有界只读轮询的临时连接状态重新查询并留记录，不重试 POST 或忽略验收错误。最终真实专项临时读取异常为 0；不据此声称所有运行都没有短暂连接问题。

构建仍保留既有 React Flow `use client` directive 提示和测试 `NO_COLOR/FORCE_COLOR` 提示；没有页面运行时错误。

## 复验命令

从 `desktop/` 运行，先准备既有测试数据库、Worker 所需设施和锁定运行时。禁止在真实 Run/Job 执行中修改 Python 产品源码或 uv.lock。

```powershell
npm.cmd run typecheck
npm.cmd run build
npm.cmd test
$env:AGENTGIT_ELECTRON_EVIDENCE_PREFIX='S8-new-verification'
npx.cmd playwright test --config playwright.diagnosis.config.ts
```

专项包含明确标注的 HTTP fixture 和真实 API/Worker 两条，均不 skip。旧四页回归用 `playwright.config.ts --reporter=line`，旧实验边界用 `playwright.experiments.config.ts experiments-boundary --reporter=line`；分别使用独立证据前缀，保留此前验收文件。Browser 插件不在本会话可用列表，依 frontend-testing-debugging 使用仓库现有 Electron/Playwright，无新浏览器依赖。

## 证据绑定及剩余项

最终原始 JSON 与截图在本机测试临时目录；摘要绑定 30 个桌面源码/资源描述文件、33 个构建文件、规范和 package-lock.json，以及每个截图原字节。

| 证据 | SHA256 |
|---|---|
| `.agentgit/evidence/S8-desktop-summary.json` | `930a433ca30e8f96ed75af5717d0d6def85c8b969486c4cabd830ca5e3a3572f` |
| `.agentgit/evidence/S8-desktop-release-check-diagnosis.xml` | `9e12ce8c2b58b4aecf5b9ccb881f1fd1408fd35e2fccde41d1f40acd374b7f4c` |
| `C:/Users/DC/AppData/Local/Temp/agentgit-s8-real-hzz9Hj/real.json` | `1e9a905043bfb27f266e9e7dbac234273198aead72246febf790a4664a756ca8` |
| `C:/Users/DC/AppData/Local/Temp/agentgit-s8-boundary-Qnz9JW/boundary.json` | `ade24cceba5f1e28cf864a0773fe09adfa1b050e5389b76f6b581ec7b186fdf6` |

截图位于上述目录的 `candidate-{1440,1024,768,360}.png` 与 `report-{1440,1024,768,360}.png`。临时测试 Schema 已清理，归档 UUID 不能当作业务库现有记录。

这是版本号仍为 0.1.4 的开发构建，未覆盖历史 0.1.4 分发候选，尚须生成新版本候选并实际打包复验。Windows 独立用户安装/升级/卸载、原生保存和原生 DPI 等按用户指示待补；标签仍待人工审阅，S8/D2 不整体验收。后续可继续新分发验证及 S9 显式全候选降级工程，不宣称已证明定位优势。完整目标的 S9–S11、S12-A/B/C/D 均保留。
