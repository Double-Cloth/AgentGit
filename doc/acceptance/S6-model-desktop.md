# S6 模型选择与桌面计量接线

状态：本批界面、业务桥接及独立后端联调通过，S6 / D1 仍未最终验收。后端模型基线为 `3c7650c`；这批未访问供应商、未产生真实模型费用，不能证明账户权限或供应商任务能力。此前 `c41635b` Windows 候选包不包含本批能力；后续 b649bf3 新候选已重建并[复测](S6-model-packages.md)。

## 可操作行为与边界

- Runs 默认 Mock，模型列表来自受认证的 capabilities。选择固定 OpenAI 模型后明确告知上下文外发，USD 上限必须另填且最多 6 位小数；无预填费用、无密钥输入或账号可用性假设。金额按整数 micro-USD 解析/显示，拒绝预算 JSON 中的冲突费用字段。
- Live 核对来源 Run 后才允许提交；继承外部模型或显式切换时要求本次 USD 上限。`config:null` 继承、完整空配置默认 Mock，匹配后端语义。请求受理不明时仍保留冻结的请求与幂等键。
- Timeline 展示已结算价格表估计、未释放/未核价预留、剩余预算及计量依据。缺失值为未知；Mock 字节、供应商 Token、混合累计分别标注。Recorded 历史逻辑与本次实际用量分开，结果未知明确禁止自动重发。
- 主进程启动渲染器前同时移除供应商环境密钥副本。供应商凭据只供独立 Worker 使用，API 令牌继续保存在主进程客户端闭包，不下发 renderer。
- 真实联调发现已有新建表单固定 `expected_version=0`，导致项目创建第二个配置时冲突。复用项目只读接口返回的 `config_version`；新 `getRepository` IPC 只读取当前已选项目、不接受任何调用方参数。提交前冻结版本，未知 POST 后不改变原请求体。

修改涉及 `management.py` 的能力/模型/项目版本查询，桌面 `main/preload/shared` 白名单、`model-config.ts`、Runs/Branch/Timeline、`BudgetSummary.tsx`、既有样式与对应测试。没有新框架、依赖、路由、数据库迁移或宿主执行接口；根 DESIGN、冻结容器源码和镜像不变。

## 验证环境与结果

Windows x64、Electron 45.0.0，固定 `agentgit://app/index.html`，无 Vite 服务。Browser plugin not available；使用仓库已有 Playwright Electron 流程，覆盖真实 main/preload/renderer。独立 PostgreSQL Schema、API 与 Worker；五工具使用固定 Docker 镜像。模型用 Worker 内的确定性 HTTP 替身经过真实 Adapter/运行时/计量路径，不构造公网请求。

| 检查 | 实际结果 |
|---|---|
| Node 单元 / 类型 | `npm test` 20 passed；`npm run typecheck` 通过，新增精确金额/旧能力/配置预算/Live 继承测试；路由测试确认当前项目及拒绝越界参数 |
| Electron 边界 | 1 passed，9.3s；四页、本地资源/授权、隔离、注入/导航、令牌、双击提交；供应商密钥不继承 |
| 真实五工具 | 1 passed，47.0s；历史/Fork、Recorded 零模型/工具/容器、A/B Live、Diff/受控导出、来源不变，4 容器清理确认 |
| 模型桌面联调 | 1 passed，28.6s；显式选型/金额校验/双击、正常完成、Recorded/Live、第二/第三任务、缺 usage、未知预留 |
| API / Python 静态 | 相关 inspection API 7 passed，3.22s；Ruff 通过，mypy 90 个源文件通过 |
| 页面身份 / 非空 / 叠层 | 标题 AgentGit、自定义协议页面；四页有业务内容，实际截图无框架错误叠层 |
| 控制台 | 本批模型界面 pageerror / console.error 均为 0 |
| 响应布局 | 表单 1440/768，未知状态 360 补充视口无页面横向溢出；修复分支按钮文字裁切及 Live 空状态误用 Recorded 说明，截图确认 |

正常来源共 11 次模型调用、9 次工具调用，实测 1,320 Token、USD 0.001518；Recorded 不新增供应商尝试（入口 guard 为 0）；从完成检查点 Live 新增一次模型、120 Token、USD 0.000138。缺 usage 保守估计 USD 0.036939 并停止；发送后未知保留 40,960 Token / USD 0.061440，界面不把已结算估计 0 当作实际免费。14 次计数和 14 次生成均为确定性替身，不是供应商账单。

交互路径：Runs 新建运行 → 默认 Mock → 选择固定外部模型 → 空/无效金额拒绝 → USD 1.00 双击提交 → Timeline 完成；随后 Branch Recorded → 历史/实际计量分开 → Live 显式 USD 0.50 → 新任务缺 usage / 未知终态。检查发现并修复的来源读取竞态与配置版本冲突均由此路径实际复现。

截图保存在仓库外 `C:/Users/DC/AppData/Local/Temp/agentgit-model-qa-v5ji7i/`：表单 1440/768、完成计量、Recorded、360 未知状态及 trace。沿用 DESIGN 的奶油底/珊瑚主操作/字体/焦点；未变更设计体系。视觉检查为一次成批检查、一次修正确认，没有无界润色。

原始证据在忽略的 `.agentgit/evidence/`；首次开发测试的默认 JUnit/QA 名称后来被脚本覆盖，下表改用独立命名、同语义的新包复验证据。后续完整包/生命周期矩阵见[新候选记录](S6-model-packages.md)：

| 文件 | SHA256 |
|---|---|
| S6-model-ui-api.xml | `d1eafa9c87d5b9e71f7d35393a0a330518198200035bd971f3957f1f0ab6ad67` |
| S6-model-portable-real.xml（后续同语义便携 2 项复验） | `0a6bf68d7f318c8bee7851968085b31f44e6a8eca4a9652064817909598f10b4` |
| S6-portable-acceptance-model-ui.json（后续包复验） | `8bb97c155ad2018056b3e97162507e079329d9f00aa27d7c657900773eed7297` |

## 尚未完成

后续新版本候选已复测并补充四页缩放/键盘/生命周期，见新候选记录；独立 Windows 用户安装/升级/卸载与实际原生保存仍需继续；本批导出依旧替换 SaveDialog 返回值。S6-T06 真实供应商冒烟未明确开启且无已验证账户/费用授权，单列未运行，不影响离线测试计数。不将本批检查等同于全后端回归；完整 S6 与后续 S7–S12 均保持原退出条件。
