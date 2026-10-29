# AgentGit 产品上下文

<!-- impeccable:product-schema 1 -->

本文仅整理用户已确认的 [产品设计](doc/AgentGit_设计文档_v1.0.0.md)、[实施方案](doc/AgentGit_实施方案_v1.0.0.md)及当前实现事实，不新增产品承诺。技术与阶段要求仍以这些项目规范为准，视觉继续遵守 [DESIGN.md](DESIGN.md)。

## Platform

web

## Stack

既有后端为 Python 3.12、FastAPI、Pydantic、PostgreSQL、SQLAlchemy、Alembic、Typer 和 uv。S6 的用户指定方案为 Electron + React/TypeScript、React Flow、Electron Forge，交付 Windows x64 安装包与便携 ZIP；渲染界面使用 Web 技术，桌面主进程承担受限系统集成。已建立 `desktop/` 初版并固定 Node.js 24.19.0、Electron 45.0.0、Forge 8.0.1 等版本；初步真实 Electron 边界/四页测试通过，安装交付尚未验收。

## Users

管理和调试 AI Agent 执行历史的开发者。需要查看一次运行发生了什么、从安全检查点创建独立分支、比较客观差异，并在后续阶段通过受控实验验证修复，而不是凭一次模型回答判断因果或成功。

## Product Purpose

提供执行状态的版本管理、可审计的 Replay 与隔离实验，逐步完成故障候选定位、有界最小修复搜索、独立复测和受约束合并。D1 是中间桌面演示里程碑；完整目标还包含 S7–S11 及 S12-A/B/C/D，不能把 D1 或 D2 当作全部交付。

## Positioning

把版本化状态、类型化干预、隔离重执行和验证证据串联起来。Recorded 验证已选历史的受控状态转换；Live 从独立输入、预算和授权重新执行。恢复状态、复现历史、重新答对和完成修复是不同事实，需要分别展示。

## Operating Context

首版是 Windows x64 本机桌面应用，连接用户已准备的回环 API、独立 Worker、PostgreSQL 与 Docker Linux 固定镜像。桌面退出不取消任务、不关闭外部服务，不自动安装或迁移后端。Agent 的 Shell/Python 只在既有固定容器执行，不转交给 Electron、宿主 Node.js 或系统 Shell。

操作者通过 `AGENTGIT_API_TOKEN` 预置本地 API 身份；主进程读取并使用，渲染进程、IPC 返回、前端存储、日志与安装包均不包含令牌。连接和窗口偏好可以保存在用户数据目录，业务数据库、CAS 与 Run 产物不搬进安装目录。

## Capabilities and Constraints

- S1–S5 已完成本机验收：五个工具、耐久执行与审批、未知结果核对、版本历史、Branch、Recorded/Live 和结构化 Diff；当前完整回归 599 项通过，见 [S5 验收](doc/acceptance/S5.md)。
- S6 首批业务页面为 Runs、Timeline、Branch Experiment、Diff。连接配置留在现有窗口内，执行详情留在 Timeline 的详情区域，不新增独立终端页面。
- 状态/事件/产物使用现有服务及项目边界。修改请求防双击并保持幂等；轮询使用游标且退出页面取消请求；断连或重开窗口不能重发修改动作。
- 主进程 API 客户端只有固定回环端点、路由、方法和有界请求/响应；IPC 有业务白名单、sender/frame/参数/项目验证。渲染进程启用隔离、sandbox、CSP 和导航限制。
- 外部资料、Markdown、HTML、代码和日志均是不可信数据。产物仅按 ID 下载，由主进程的原生保存对话框确定导出目标，渲染进程不能传入任意宿主路径。
- 无外部模型密钥、无外网时离线 Demo 和确定性测试必须可用。S6 已接入固定 OpenAI Responses 模型，Runs/Live 显式选择并输入 USD 上限，Timeline 区分价格表估计、预留、历史逻辑和本次实际用量。密钥只在独立 Worker；真实供应商冒烟需明确开启、单列实际结果，不成为离线隐含条件。
- S7 后端已提供冻结验收器、A/B Live/Hybrid 实验、持久 Job/Run 和 API/CLI，以及[类型化干预与只读上下文](doc/acceptance/S7-interventions.md)。Hybrid 已接入 Worker、不可变观测账本、实际/逻辑计量、恢复与 Recorded 复验；synthetic 与模拟结果显式标为 test_only。[精确统计](doc/acceptance/S7-statistics.md)和[桌面受控实验视图](doc/acceptance/S7-desktop.md)已实现并进入 [Windows 0.1.4](doc/acceptance/S7-package-014.md)。新 ZIP 中的真实 Live 10 trial 得到 9 通过及 1 取消，Hybrid 各 5 项全部通过，录制/合成来源、原键确认及重开查询均实测。[S7 本机退出核对](doc/acceptance/S7-gates.md)齐备，S6/D1 前置待补，S7 未整体验收。

S8 已提供行为图、依赖对齐、失败硬断言及[不可变静态 Debug Report](doc/acceptance/S8-static-reports.md)，API/CLI 支持创建、读取与发现，未运行干预时保持 static_only。单连接池与并发发布边界已修复、验证。[20 例及 12/8 划分](tests/fixtures/diagnosis/v1/README.md)、排名及评估脚本先后冻结；[暂定全量评估](doc/acceptance/S8-evaluation.md)无漏例，保留集主要注入点 Top-3 为 4/8，未达 6/8，不能用拟修复决策的 8/8 替代主要指标。已增加[候选与 S7 单变量干预的只读关联](doc/acceptance/S8-candidate-experiments.md)，并接入[桌面报告、精确事件跳转与显式干预表单](doc/acceptance/S8-desktop.md)。真实 Mock 示例 A 0/5、B 5/5 通过，桌面可保存单项干预并带入 A/B 表单，静态报告及不确定项保持；该示例不替代全量定位验收。功能已进入 [0.1.5 分发候选并完成包内复验](doc/acceptance/S8-package-015.md)；[人工标签](doc/S8-dataset-review.md)、其余适用故障效果证据和环境/原生前置仍待补，不宣告 S8、D2 验收或定位优势；后续可按方案用完整候选显式降级验证搜索工程。

S9 已提供[基础与台账](doc/S9-repair-plan.md)、[原子派发/结算](doc/acceptance/S9-trial-dispatch.md)、[自动搜索、模板 API/CLI 和 repair-worker](doc/acceptance/S9-search-loop.md)，完成[本机后端退出核对](doc/acceptance/S9-gates.md)。最新 569 单元 + 32 真实集成通过（不与历史重复累加）；五类安全拒绝均有关联审计且零新 trial，比较表完整保留失败/未知与实际成本，终态墙钟冻结。新 Mock 字节策略按预算摘要冻结、保护确认/回归额度；原生产 Mock 默认入口在两个 Prompt 失败后自动找到 Plan，总筛选 99,753 字节、最终试验 47,109 字节，来源不变。候选来源仍为完整有限模板降级，状态仅为待确认，不输出 verified 或最小性结论。[S10 实施计划](doc/S10-repair-plan.md)已建立；S6/D1 和 S8 待补项保持，尚无本轮桌面分发包，继续 S10–S12 的必选完整目标。

S10 已实现[耐久缩减](doc/acceptance/S10-reduction.md)、[预注册/一次性开启](doc/acceptance/S10-pools.md)、[实际六加四](doc/acceptance/S10-confirmation.md)、[三原子](doc/acceptance/S10-three-atoms.md)、[字段缩减](doc/acceptance/S10-fields.md)、[精确提案/审批后端](doc/acceptance/S10-reviews.md)及[随机拒绝/实际观测伪修复后端](doc/acceptance/S10-falsefix.md)。原子、字段和必要性复查共用原预算；确切派生内容在最终锁后独立验证。建议展示 before/after、任务/环境、用量和范围；票据绑定配置版本，批准只创建新版本，Plan 模板由新任务真实执行，保存实验只落定义。随机确认失败保留拒绝；观测补丁即使六加四通过也不能采纳为正式配置。仅在隔离测试库验证，业务配置未采纳；合成验证仍标 `verified_under_fixture`，预选随机向量不构成无偏成功率研究。当前测试范围见独立记录，旧版本结果不累加；S9 默认深度二和 reduce=6 不变。继续 Repair Lab、30 例/四基线和 D2，S10 尚未整体验收。

Repair Lab 的[原子准备与桌面桥接基础](doc/acceptance/S10-prepared.md)及[可见页面与只读流程](doc/acceptance/S10-lab.md)现已实现：搜索和十张预注册票据同事务发布；第五页接入来源/预算、缩减、锁后复测、配置对照、精确票据与决定。原键确认、观测禁采纳和断连保留有明确 HTTP fixture 覆盖；新增[同次真实设施联调](doc/acceptance/S10-lab-real.md)经 PostgreSQL/API/独立 Worker/CAS 完成联合 Prompt/Plan、两项字段缩减、六次确认/四项回归、真实版本竞争和精确批准，来源不变、批准无新 Job/Run、重开只读。真实字段收据展示已修正，结论仍为固定 Mock 条件验证。继续其余真实桌面边界、候选交接、30 例/四基线和新包复验，不能据此宣告 Repair Lab/S10/D2 退出。

[故障候选到 Repair Lab 的显式交接](doc/acceptance/S10-candidate-handoff.md)已通过真实设施核对：只预填已核验来源、起点、screen 验收器及请求事件，不启动搜索或继承授权，confirm/regression 样本拒绝；回归来源、预算及授权须由操作者填写。该项补齐上文候选交接待办，其余真实桌面边界与新包仍待完成。

[真实桌面取消和未知边界](doc/acceptance/S10-lab-boundaries.md)已核对：排队取消后零试验，确认未知在取消/重开后保持一项预留和一次调用，不自动重发或标为通过。界面明确显示未知计量和等待安全结算；真实观测边界、新分发包及完整研究评估继续。

[真实观测桌面闭环](doc/acceptance/S10-lab-observation.md)现已通过：实际六加四仍限 test_only，按钮禁用且 API 拒绝正式批准，只保存确切实验定义，不增加配置/Job/Run；独立原始审计通过。继续新分发候选和完整评估，不宣告 S10/D2 完整验收。

[同预算评估方法接线](doc/acceptance/S10-benchmark-methods.md)现已完成 19 契约与五种方法的真实集成核对，来源是明确的旧工程例，不计入新数据集或 10/12。主方法保留完整候选降级，不制造对无排序基线的优势；局部不适用和不缩减未证明最小性均保留。方法资产已冻结，新的 30 例、20/10 家族划分、全量四基线和 D2 继续。

[三十套公开输入规格](doc/acceptance/S10-input-assets.md)已固定 20/10 划分、五类配额与 13 个拟议家族；52 项契约及 20 个开发输入的真实正常 Run 通过，原始 CAS、冻结验收器和实际用量独立复核。当前冻结范围仅为公开输入，故障运行协议尚未绑定，保留集没有真实 Run 或修复成绩。正常通过不计为修复成功；继续故障材料化、语义回归、正式起点协议和五方法完整评估。

[二十个开发故障构造](doc/acceptance/S10-fault-construction.md)已接通既有 Runtime，37 项契约及 20 项真实集成分批通过；最终四十个 Run 为 20 失败、20 正常通过。两类越界写被拒绝且零产物，原始响应、硬验收及用量独立复核。此处仍无修复搜索或保留成绩；运行适配器是待补齐的构造草稿，完整评估协议与 D2 继续。

[合法起点与四项独立回归](doc/acceptance/S10-benchmark-origins.md)补齐公开根/局部选择、首个 act 前失败的回退及原 Worker 上的只读观测环境，64 个不同节点分批通过。九个真实 Job/Run 的原始响应、硬验收及计量独立核对；晚期局部空间确实生成两项候选。仍须冻结完整运行 release 并执行五方法，不代替修复/保留成绩。

[新数据驱动原型与故障 v2](doc/acceptance/S10-dataset-driver.md)已在一个开发联合例实际执行五方法：四种完成六加四，固定全局 Prompt 在其选定空间无解，原始 CAS 独立核对。冻结前发现同源指令跨划分，v2 将全部指令例归入保留家族、开发路径改用独立导出缺陷，原输入和旧证据保留。当前 73 契约及两个新开发机制源/对照通过；仍无正式 30 例或保留成绩，完整运行 release、全量评估与 D2 继续。

[三十例五方法运行协议](doc/acceptance/S10-evaluation-freeze.md)的 85 项契约通过，逐例声明与方法/输入/预算/源码共同绑定；二十个开发任务结束并审计后，才执行十个保留任务。现已完整执行及审计 150 个方法结果。迁移清单遗漏已披露，并从运行前已归档 Git 基线补核 26 文件；原快照不回写，完整复现需要该 Git tree。

上述各增量保留历史范围；当前完整结果见 [D2 研究报告](doc/D2-research-report.md)和[S10 矩阵](doc/acceptance/S10-gates.md)：主方法开发目标 12/12、全开发 12/20、保留 6/10；保留局部/固定全局各 4/10、无排序 6/10、不缩减 8/10，失败/待审阅不删，不宣称定位或全面基线优势。当前十三项关键数据库复验、三条原始链重审和[两次备份恢复](doc/acceptance/S10-backup-restore.md)通过；恢复中断写保持待核对、零重发。分析 v2 正确区分最终字段候选的最小性，未修改执行条件。完整 D2 前置、S8 其余适用证据、用户暂缓项与 S11/S12 继续，未合并 main。

S8 的[其余适用效果工程证据](doc/acceptance/S8-candidate-effects.md)现已补齐：原二十规格统一另建持久来源，每例同故障条件五加五，四例正差、十六例零差均经原始响应/硬验收重算；原报告与排名不变。早期两种来源构造拒绝保留，没有改产品或放宽 Replay 规则。人工标签和原 Top-3 4/8 未达标、独立 Windows/原生及整体阶段前置仍待补。

S11 已按用户继续工程实现的授权进入[只读首增量](doc/acceptance/S11-artifact-preview.md)：共同祖先、有限 Markdown/研究报告 JSON 三方候选和同项目真实 CAS 预览已实现，全部 743 后端单元、3 新集成及 7 版本历史集成通过。候选始终保留状态/来源/任务约束/审批待办，不改变 HEAD、不执行外部动作；尚无双父 Commit、Merge Review、30 组正式评估或新合并桌面包。阶段前置保留，完整 S11/S12 主链继续。

S11 后续[结构化状态与完整候选验证](doc/acceptance/S11-state-validation.md)已通过当前 764 单元与 18 不同集成节点：计划/事实、权限/预算、真实来源及效果独立校验，不拼接调用轨迹或授权；伪引文与未知写入实测阻断，预览只读。完整验证限既有研究双文件，保留审阅待办；耐久提案/审批、双父历史、Merge Review、30 组/基线和新包仍待完成。

S11 第三增量[耐久提案与精确审阅](doc/acceptance/S11-proposals.md)已实现不可变根/修订/验证/冲突/票据/拒绝及 API/CLI；完整编辑定义重新构造候选，旧版验证失效，重开/票据分页只读。当前 784 单元及 25 不同集成节点通过，六组原始 CAS 与四项篡改拒绝核对通过；合并服务零新调用/Job/Run，不更新历史。准备审阅不表示批准，正式批准/双父 Commit/CAS、历史兼容、Merge Review、30 组与新包继续，S11 整体验收及 S12 仍未完成。

S11 第四增量[原子批准与双父后端](doc/acceptance/S11-approval.md)已实现本人精确票据/候选重查、独立合并 Commit、固定双父顺序和目标 HEAD CAS 同事务；重开/原键查询返回原提交，不启动 Run 或外部动作。Log/祖先支持稳定拓扑图，Diff 保留歧义，Recorded 仍核对显式单一来源。当前 784 单元及 88 不同集成节点通过，最终九组原始审计和四个篡改拒绝通过。独立合并状态的 Fork/新 Run/Recorded 完整兼容、实际进程崩溃、Merge Review、30 组/基线和新包待完成，未迁移业务库、未进入 S12。

S11 第五增量[合并后的独立新任务](doc/acceptance/S11-merged-runs.md)已实现明确批准来源的分支、显式新配置/预算/当前验收器、零执行基线初始化、Worker 接管及只读来源查询。合并点缺少录制路径明确拒绝，新 Run 初始化→终点 Recorded 通过且零新增调用；四处实际解释器终止完成回滚/原键查询/新 Run 接管验证。当前 796 单元与 147 不同集成节点通过，最终九组原始重审及四项篡改拒绝通过；七个显式新任务发出 133 次新调用，全部为 Mock/本地工具、零费用。再次语义合并、更多双父/歧义、30 组/基线、Merge Review 和新包继续，完整 S11/S12 仍未完成。

S11 第六增量[再次合并与交叉历史](doc/acceptance/S11-remerge.md)已实现批准根作为 base、用量 null/未执行审计、有界来源重核和显式 `selected_base_run_id`。连续两代再次批准、真实 Live 副本构造双最佳共同祖先及歧义选择通过；804 单元、99 个不同集成节点、两组原始审计和六个篡改拒绝通过。早期相同 HEAD 及资源不足/数据库退出失败保留，恢复环境后相关 97 项分批全过。30 组/基线、Merge Review、新包和完整阶段前置继续，未进入 S12 或合并 main。

S11 第七增量[三十组真实输入准备](doc/acceptance/S11-merge-inputs.md)已完成三类各十组、90 个实际 Run 及预览控制，修正单侧补读完整 Evidence 的三方选择。806 单元、30 项构造及 23 相关集成通过，30 组原始输入和同类来源替换反例已审计；准备成本为 478 模型、358 工具、4,808,743 Mock 字节，另有一条经精确审批的本地未知付款模拟，无外部付款请求。准备与正式成绩分开；正式三方法、完整候选/批准后执行、人工预期复核、桌面和新包继续，S11/S12 仍未完成。

S11 第八增量[完整三方法报告](doc/S11-merge-report.md)已归档 30×3 候选及十个批准后新任务：主方法完整内容 10/10、安全拒绝 20/20（19 冲突、1 Schema 拒绝），两种有限基线错误接纳 15/20、20/20，均未发布无效候选。30 正式与 15 协议节点通过，457 文件执行冻结，全量原始候选/来源/批准/初始化及四类候选、五类统计反例重核完成。正式成本共 607 模型、477 工具、6,477,472 Mock 字节，费用 0；独立人工预期、Merge Review、新包和整体前置继续，未退出 S11 或进入 S12。

## Brand Commitments

产品名称为 AgentGit。界面继承根目录 DESIGN.md 的视觉体系、允许的字体替代、组件和断点；不另外发明品牌配色或装饰体系。字体和其他资产须有授权并满足安装包离线使用要求；已取得本地 Inter、EB Garamond、JetBrains Mono 及 OFL，记录上游提交和摘要，没有把未提供的商业字体视为已具备。

## Evidence on Hand

2026-10-23 用户明确授权暂缓当前环境/原生验收、先继续 S7 开发与测试；S6/D1 保持未完整验收，最终交付前补验。具体范围见[实施方案第 1.2 节](doc/AgentGit_实施方案_v1.0.0.md)。

当前候选为 [0.1.6](doc/acceptance/S10-package-016.md)，36 个运行构建文件、字体与授权审计、35 单元和 17 条打包 Electron 流程通过，包含受控实验、故障报告和 Repair Lab。联合修复、候选交接、取消/未知及观测禁采纳均从新 ZIP 实测；原始 CAS 独立审计通过。旧 0.1.5/0.1.4 四件套按原摘要保留。本轮未安装/升级/卸载；0.1.3 的 MSIX 安装生命周期只保留为历史证据，[D1 操作指南](doc/D1-desktop-demo.md)及[独立 Windows 清单](doc/S6-windows-acceptance.md)已更新到新候选。以下旧版本及包内容修正保留历史事实；原生保存和独立 Windows 验收仍未完成。

真实 PostgreSQL、固定 Linux 容器、完整五工具来源、CLI 与 API、逐检查点 Replay 证明、A/B Live 文件与预算隔离均有本机记录。原始运行证据在忽略的 `.agentgit/evidence/`；可提交的验收说明位于 `doc/acceptance/`。真实 Electron 与从 ZIP 解压的程序已通过独立测试库/Worker/容器五工具→Fork→Recorded/Live→Diff 联调，应用进程没有构建用 Node/npm 的 PATH。Windows x64 0.1.1 开发候选安装程序和 ZIP 已构建、均未签名；Codex MSIX 上下文中实际覆盖升级/卸载/重装及业务数据保留已验证。导出测试仍替换了原生对话框返回值，尚无独立 Windows 用户安装/升级/卸载证据，各项不能互相替代。

2026-10-23 修正：此前候选的 ASAR 含测试状态文件与构建配置，原包审计结论覆盖不足；当前仅接受[分发修正记录](doc/acceptance/S6-package-hygiene.md)的新 Setup/ZIP 摘要。根白名单审计及新包便携/安装生命周期复验通过，产品运行构建文件未改变。独立用户环境按用户指示稍后提供；原生保存和执行环境缺失的桌面专项继续保留未完成。

## Product Principles

1. 展示客观状态、来源与证据，明确未知、失败、验证范围及能力边界。
2. 未知副作用不自动重发，Live 不继承旧授权，实际计量与历史逻辑用量分开。
3. 用已有服务形成可操作闭环，保持简单，不加入未经要求的框架或复杂抽象。
4. 离线、隔离和安装后可运行必须实际验证，不用模拟成功或开发服务器代替交付。

## Accessibility & Inclusion

S6 要求完整键盘操作、可见焦点、有名称的控件和文字状态；支持长中文与长结构化内容。桌面最小内容宽度 768px，验证 768/1024/1440px 及 100%/150%/200% DPI，360px 是补充渲染检查。对比度须实际检测；现有 token 不达标时先记录并同步修订 DESIGN，不能静默改色或宣称已通过。

S11 的[Merge Review 桌面闭环](doc/acceptance/S11-desktop.md)已接通第六页、提案发现和完整基点查询，沿用既有设计。完整修订、本人精确票据、硬阻断拒绝、双父批准、显式独立新任务和 Recorded 已在真实设施/Electron 核对；历史票据重开不激活批准，断连草稿保留，未知只原键确认。当前尚为开发构建，新 Windows 候选、人工预期和阶段前置继续；不将界面例并入既有 30×3 研究成绩，也不提前进入 S12。
