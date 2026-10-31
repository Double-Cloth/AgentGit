# S12-C 开发版桌面接入记录

2026-10-31，按既有“工程先行、正式门槛保留”授权扩展运行页。此次仍为开发版 Electron 0.1.7，不更新或替换既有分发包，不合并 main。固定后端源码为 `source-sha256:914591221d30a2bacc034fae92b6f596fe0de4914550dd8617e05eca92e06078`，锁文件不变，head32/79 业务表加版本表共80；没有新迁移、依赖或业务库迁移。

## 操作范围

运行页内切换“单次运行／四角色协作”，保留既有六页导航和 DESIGN。支持有界协作列表、按 ID 打开、固定离线模板准备、显式开始、整组暂停／恢复／取消、成功 Live 来源的 Recorded 另组准备。准备表单可填写目标和每角色私有备注，预算／权限采用后端固定模板并可展开核对；不提供任意框架、动态 DAG 或自由权限编辑。

`GET /capabilities` 新增 `collaboration` 特性与 `collaboration_template`。包内 `adapters/fixtures/collaboration.json` 由严格 NewCollaboration 校验，测试保证它与公开演示 JSON 相同且私有输入为空。响应可由操作者复制修改，不会污染下一次模板。旧后端的普通运行兼容性要求不增加此特性；协作 IPC 在缺少特性时于 HTTP 前拒绝。

八个固定 IPC 操作绑定主进程当前项目，仍使用原认证、受信任 frame、CSP、长度／整数／UUID／请求大小限制；没有通用 HTTP 或 CAS 读取桥接。共享路径和版本游标必须成对，版本限定安全整数，组控制必须携带确切修订。

详情依次展示组状态、控制修订、实际静止状态、四角色独立 Run／逻辑用量／物理意图／复用／未知、预算权限、消息父事件和发送／接收地址、共享产物版本及发布者。角色时间线显示协作来源，可返回所属组；单 Run 的控制保持只影响本角色。物理意图不是已执行次数或账单，Mock 按 UTF-8 字节记量。

准备、启动、Recorded 和控制均不自动重发。结果未确认时保留原请求、控制修订和幂等键，只提供显式原请求重试；断连停止读取并保留当前范围已确认的数据，禁用操作。关闭／重开只发现已有协作，不自动恢复提交；未确认请求不写入本机持久队列。控制收据表示接受，不能代替静止或终态查询。

## 本次验证与失败归属

- 后端 1,106 单元与23集成通过：既有22个操作者入口，加1个公开模板／独立响应检查。实际 PostgreSQL、CAS 与 Worker；报告 `.agentgit/evidence/S12-C-desktop-v1/backend.xml`，原始包与完整输入同目录。没有把这些测试称为全量 C 或核心兼容。
- 桌面44单元、TypeScript 三配置、构建、Ruff（541文件格式）及 mypy（219源码）通过。桌面单元日志在 `S12-C-desktop-v2/desktop-unit.log`。
- 两个新增真实 Electron 用例通过。独立测试 Schema、HTTP API、独立 Worker 与临时用户数据目录，均 await 实际进程结束。第一例：准备不执行、整组暂停恢复、Live 完成、四角色／7消息／5共享版本、角色跳转、严格 Recorded 构造屏障、另组执行、来源指纹不变、重开无 POST。第二例：prepare/start/control 在实际提交后返回503以模拟响应未确认，原键手动重试、断连保留并禁用、取消零调用、重开只读。503 是受控响应丢失模拟，不声称覆盖所有 OS 网络断开。
- 首轮 v1 第一例通过，第二例在 `getByLabel` 定位禁用 textarea 时失败；错误快照明确显示该 textbox 已禁用。v2 改为按角色／可访问名称定位，并补齐读取暂停时保留已确认数据；两例全部复跑通过。v1 失败证据保留，不能计为通过。
- 同一 v2 构建另通过13个既有 Electron：基础边界2、LangGraph HTTP／stdio及原生控制6、MCP HTTP／stdio2、合并审阅3。报告前缀 `.agentgit/evidence/S12-C-desktop-v2-core-*`。它们与新增2例合计15个不同桌面节点；这不等于完整 S2/S4/S5/S6/S11 后端／容器／安装包兼容。

两版完整 Git 基线、840份工作区输入与40份实际构建文件分别在 `.agentgit/evidence/S12-C-desktop-v1`、`S12-C-desktop-v2` 冻结。后端包字节两版相同；v2 的 UI／驱动另行冻结，不把 v1 截图当作 v2 结果。各实际 Runtime 存续时不修改产品源码、测试或构建。

## 原始核对

标准库独立读取器 `S12-C-desktop-v2/analysis/raw_audit.py` 不导入产品实现，核验 CAS 类型化 SHA256、对象元数据、事件序列、Commit／状态／事件范围、消息父地址与收据、共享头／发布者及 Recorded 自身调用来源。三个原始快照分别为：

| 快照 | 组／角色 | 对象／原始字节 | 调用／来源复用 | 消息／收据／发布 | 映射检查点 |
|---|---:|---:|---:|---:|---:|
| 成功 Live | 1／4 | 374／358,167 | 20／0 | 7／7／5 | 0 |
| 同库追加 Recorded | 2／8 | 716／750,843 | 40／20 | 14／14／10 | 72 |
| 响应重试与整组取消 | 1／4 | 28／14,931 | 0／0 | 0／0／0 | 0 |

第二行包含第一行的来源，不把重复快照相加当作独立成功样本。原来源所有表行均仍存在且未改写，来源运行指纹一致。独立审计证明上述字节／台账／因果一致性，不冒充完整状态机重新计算；严格重算仍由原 Recorded 服务及既有专项证据负责。

23份后端原始包另核对2,230条对象记录的类型化摘要，属于包／对象完整性核对，不声称完整重算。最终封存入口 `.agentgit/evidence/S12-C-desktop-v2/summary-v1.json`，SHA256 `4169e22c799b4417a89ea4c82f7dc4672bb065f4187d4db4781b1f2832c91086`；1,857份文件索引 SHA256 `52883c1d1c01776d3d3c4b2174595d1d5fc86dca4d38a5c70feafa715af35cc3`。索引包含两版输入／构建、后端与桌面报告、失败与成功 QA、原始快照和独立审阅记录，原路径保持。

672份运行相关输入保持冻结工作区原字节；670份同时与 Git 暂存字节完全相同。仅两个既有字体／授权 provenance manifest 的工作区 CRLF 经原 `.gitattributes` 规范为 LF，文件本身未修改，逐项摘要在 `analysis/stage-proof.json` 中单列。所有后端包源码与 JSON 的暂存聚合仍为914591…6078，40份实际构建字节不变，不把这两个资产元数据行尾例外隐去。

## 界面完成核对

Browser 插件不可用，采用项目锁定 Playwright 1.64.1／Electron 45.0.0。QA 位于 `C:/Users/DC/.codex/visualizations/2026/10/31/agentgit-s12c-desktop-v1/run-v2`：prepare、workspace、group 1440／768／360、messages、shared、recorded-reopened、offline-retry 共9张 PNG及逐张 capture metadata。原生窗口可见、聚焦且字体 loaded；这些是 CSS 视口覆盖，不能代替独立 Windows 或原生 DPI 验收。

方向契约位于 `.impeccable/surfaces/desktop-src-renderer-runs-tsx.md`：Operate、code-led 精确扩展，`seed=not-run-precise-extension`，无批准效果图或新增 shipping raster。两个独立新上下文角色分别完成截图审阅和设计文档核对，完成审阅五节结论为 **ship**，限本增量。手工 detector 仅提示既有 Inter 与历史选中边线；未作无关全局修复。DESIGN SHA256 保持 `af6166c1dd80a7965f1cc3b7cc1d6846ec43287a4891808cd1f89d16e13fb33b`；`.impeccable/design.json` 原本不存在，本轮保持缺席，未生成。既有营销描述与操作界面的历史差异只记录，不据此改设计体系。技能环境提示更新和未设置 buildPath，本轮不修改技能或配置。

## 继续推进与正式门槛

更多组／单角色控制交错、规模与完整 S2/S4/S5/S6/S11 兼容仍需继续；本次开发版界面不等于最终四适配安装包。独立 Windows 安装／升级／卸载、原生保存／DPI、人工标签／预期、S8 Top-3 4/8 未达6/8、正式 C/T05/D3 保持原状态。C 本机协作稳定后才进入 D/A2A，不合并 main。
