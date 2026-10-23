# S7 桌面受控实验增量

本页保留 `14e79ed` 的源码视图增量。后续新视图已进入 [Windows 0.1.4 候选](S7-package-014.md)并重新完成包内全部实验流程，阶段总核对见 [S7-gates.md](S7-gates.md)；以下“未打包”等表述仅描述本页对应的历史增量。

2026-10-24 接通实施方案 S7 第 6 步。按用户授权继续暂缓 S6/D1 的独立 Windows 用户与原生专项；本记录不把 S6 或 S7 整体标成已验收。Windows 分发候选仍为此前 0.1.3，本轮验证源码桌面，新视图尚未进入已审计的安装包。

## 实现范围

Branch Experiment 保留单次 Fork/Recorded/Live，新增按能力标识启用的受控 A/B 入口。流程为来源 Run/安全起点 → 显式冻结共同验收器 → A/B 完整 Prompt、计划或有序干预 ID → 默认各 5 次 → 原子提交 → 统计/trial/时间线与控制。两组共用模型、权限、split、验收器和每 trial 预算；外部模型须显式输入每 trial USD 上限。

只读上下文接入正整数事件 ID，明确区别调用 UUID。Hybrid 可选受控只读调用与 stop/fixture 策略；TOOL_RESPONSE_PATCH 只引用同项目完整、同参同签名录制，冻结后显式加入 B 组。合成观测显示 synthetic observation/test_only，不提供应用正式配置动作。

报告区分全部消耗与完整试验分布，显示样本覆盖、均值、样本方差、Mock 字节、供应商 Token/估价/预留。分数与微美元用有界 BigInt 格式化，近似值带 `≈`，缺失费用和单样本方差保持未知。验收器不兼容时保留已确认报告并显示刷新错误，不自动重新解释历史判定。

提交和控制保留固定请求与幂等键；未知结果只能原键继续。暂停/取消受理后等待 Worker 安全点，不能提前显示落实。控制前重新读取当前 Run 版本。短暂断连保留工作区并禁用操作，恢复后只读刷新；切换单次/A/B 入口也保留待确认请求。按当前项目列举实验元数据，重开后通过列表或 UUID 查询，不自动 POST。列表按 UUID 排序，读取不依赖重验旧 manifest。

涉及 `desktop/src/renderer/{App,Branch,Experiments,InterventionContext,ExperimentStatistics,ExperimentTrials,ui}.tsx`、既有样式，以及固定 bridge/preload/routes；未新增框架、依赖、导航页或数据库迁移。后端能力和实验发现接口已提交为 `e1bf354`。

## 验证事实

| 核对 | 实际结果 |
|---|---|
| 后端回归 | 416 单元 + 81 集成去重共 497 项通过；其中 Hybrid/恢复/独立 API 重启与 CLI Worker 专项 31 项，不与上轮 764 累加 |
| 桌面单元 | 26 项通过：精确分数/大整数/微美元、事件 ID、预算、条件互斥、列表路由和 IPC 边界 |
| 既有 Electron 边界 | 两条通过，旧能力后端只禁用 A/B，原三种单次模式保留 |
| 新 UI HTTP fixture | 费用未知、空完整样本、不兼容验收器、断连保留、控制原键/原版本重试通过；明确不是实际 Worker 任务 |
| 真实 Electron/API/独立 PostgreSQL/Worker | 真实五工具来源后提交 Live 10 trial；A1 暂停/恢复（attempt=2），B5 取消，最终 9 passed + 1 incomplete/cancelled |
| 未确认实验提交 | 真实 API 已提交后注入一次响应丢失，切换入口后双击原键继续；仅一个实验、恰有 10 个独立初始 Run，两次 POST 使用相同键 |
| Hybrid 与响应干预 | 只读事件上下文 → 冻结工具响应补丁 → 加入 B → 各 5 trial 全部 passed；A 合计 5 次 recorded，B 合计 5 次 synthetic，始终 test_only |
| 来源、清理与重开 | 来源指纹不变；40 条容器执行记录清理确认且容器实际不存在；重开查询无新增 POST；pageerror/console error 均为空 |

真实来源为 `a832bf56-f1a6-4528-a98a-05a8d2c439ee`，项目 `feeee6b1-c61a-44d5-a3bd-e7eed834e8d9`；Live 实验 `993776cd-c81f-4a14-a52d-65ae896a91c0`，Hybrid 实验 `b3e4066b-fa6f-4ecc-943e-e42286b3fc8d`。冻结验收器 `cf481ec59cee88fef92842577cf7a449b3b8ee3c172b5ce3f261e58558c8bcd0`。来源前后指纹均为 `f76c12f61fbd0deb1102360b84cffe42429615c945f462497a44f7b4b12e79d9`。

联调中实际发现并修正事件 ID 桥接类型、断连卸载工作区导致请求丢失，以及控制受理与落实的呈现混淆。最初的测试错误假设排队暂停立即完成、固定 15 秒内领取指定 trial；另一次独立只读探测遇到暂时连接失败。最终等待桌面报告的真实状态，不用测试循环高频重复大报告请求，完整流程通过约 6.1 分钟。失败输出不计入通过数。

## 界面复核与范围限制

当前没有 Browser plugin，沿用仓库锁定的 Playwright Electron，直接检查 `agentgit://app/index.html`；没有引入或安装浏览器依赖。1440/1024/768 和补充 360 内容宽度均核对非空、无错误覆盖层、长内容约束和页面无横向溢出；表格在自身容器横向滚动。批量截图后集中修正字段间距、来源摘要和 synthetic 明示，再做一次界面复核；相邻字段被 datalist 分隔时也用计算样式断言 20px 间距。

使用既有 DESIGN 的字体、颜色、组件及断点。impeccable 手动 detector 只提示既有 Inter 和既有选择边框，均受项目设计约束，不据此改动视觉体系。React 检查确认新页按需加载、组件定义不位于 render 内、没有 effect 自动提交、没有新增状态框架。

最终显示层修正后重建并复验 UI 边界和键盘入口；未再次重复耗时的 20-trial 真实流程。该流程的 Python 源码与冻结验收器始终相同，最终显示层改动没有更换后端契约或提交载荷。

尚须构建和审计包含 S7 新视图的 Windows 候选，并复验包内流程、核对 S7 总退出条件。独立 Windows 用户安装/升级/卸载、原生保存/DPI/断网等仍按原清单待补；本机 viewport 不是原生 DPI，HTTP fixture 不是真实依赖缺失，Mock/离线替身不证明付费供应商访问。S8–S11 和必选 S12-A/B/C/D 继续保留在完整目标中。

## 证据绑定

后端源码 `source-sha256:1132d409a7824df2413c62eb852bbf800ef31c7466964d3d16e6922368acd885`；设计/实施方案、DESIGN 和 uv.lock 摘要保持上轮一致。原始报告、去重测试列表、变更实现文件摘要和截图在 `.agentgit/evidence/S7-desktop-*`。已核对冻结 manifest 的 code_revision 等于该源码摘要；桌面测试 Schema 已移除，CAS 和原始证据保留。Ruff、5 个变更 Python 文件格式、3 个变更产品源文件严格 mypy、桌面 typecheck/build 和 diff 检查通过；保留既有 TestClient 弃用、React Flow directive 与测试色彩环境提示。

| 文件 | SHA256 |
|---|---|
| S7-desktop-regression.xml | `ea84148ecedd9a404c4fb54dac0dc480f61241959e863900570ff6568f8d13d1` |
| S7-desktop-entrypoints-final.xml | `4df614abad444caaee5b586f8882c4bbf06c0081910bc10a29b4da04791d82f8` |
| S7-desktop-hybrid-regression.xml | `c5a7718e1a7d557b94656f2df142ccd8341e075512d60fc66fe97962a62802d9` |
| S7-desktop-unit.xml | `cd2a792a7493016224afa133c046f49b747e7d2bdcfc1574e81abb2ef795ac45` |
| S7-desktop-boundary.xml | `c7d1e263c63a63b5e2a415878d43840bddb627640c3875d2e76bf8738ce88e37` |
| S7-desktop-ui-confirm.xml | `2e846a2a4b06222d14c6aec7b31f445870e3fefc9114f1cb4700cf76a3042c7d` |
| S7-desktop-real.xml | `18431bb286b3939a1c1f4efba184044dd0a277672ff5ec44802ed335d356e0f3` |
| S7-desktop-real.json | `e64fd9f604d06e72edf3251f7a9e69839be15272baebff649fde9413baae8657` |
| S7-desktop-cleanup.json | `7efce39763d6b89163d14e30ad0417cd9a86b6185dec381534892f7e645c654d` |
| S7-desktop-summary.json | `4bfa525780e553fdf4a43bbd35ce1c88e10885815ff67a7e343ef9f48a9f92c7` |
