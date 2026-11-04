# S7 精确分组统计与桌面固定桥接增量

2026-10-24 实现实施方案 S7 第 5 步，并准备第 6 步所需的固定桌面操作。S7 整体仍未验收；S6/D1 环境与原生专项继续按用户授权延后。

## 实现与解释范围

实验报告新增 `statistics`，原 `groups` 计数保持兼容。A/B 分别列预定、通过、失败、未完成数，成功率分母包含所有预定 trial；另列已独立验收样本的通过率。整体冻结硬断言结果、错误码和再次领取 Job 的恢复结果可复核，不把短路后未执行的细分规则计为通过。

`consumption_to_date` 统计截至查询时的全部消耗，`assessed_trials` 只含明确通过或失败的完整试验；取消、暂停、预算耗尽和未知结果不混入完整执行分布。各指标给出样本、缺失和不适用数量，未知合计为 null、已知小计另列。均值与样本方差按整数精确计算，合计、极值和分数分子/分母以十进制字符串表示，避免浮点和安全整数截断；单样本方差未知。

`actual.model_usage` 从原始物理调用账本读取计量，排除恢复复用和 Hybrid 观测。读取的意图、结果及供应商计量响应须有本项目归属，可信核对结果沿其自身归属读取。所需对象缺失或摘要损坏会报错；有效对象中的计量封装不合契约时，费用保持未知和预留。输入字节数使用已登记对象元数据，Mock 输出使用耐久结果的字节计数，统计不重新读取请求或 Mock 响应正文，也不代替 Recorded 的完整性验证。检查点尚未推进时，已保存的结果也进入实际用量；恢复和可信核对后不重复计数。

Mock 字节与供应商 Token 分开，Mock 明确标为本地免费。供应商区分实测、保守上界、未生成和缺失计量；未核验费用保留预留，完整估价与已知小计分开。没有供应商账单，因此存在供应商调用时账单费用为 null；原 `actual.cost_micro_units` 仍是检查点已结算增量，可能滞后。实际模型调用数指网关保存的 Adapter 调用，不等同于 Adapter 内的 HTTP 请求数。

新增七个固定桌面 IPC：验收器冻结/读取、实验创建/读取、干预上下文/创建/读取。继续强制固定路径、项目头、UUID/digest/样本标签验证、JSON 上限与幂等键；未提供任意 URL、认证头、宿主执行或对象存储读取接口。统计分数字符串在桥接中保持精确。

## 本轮最终验证

后端 **764 项全部通过，无失败或跳过**：416 单元、347 集成及 1 条真实 Hybrid 五工具容器交叉验证。四个不重叠组分别为 220、289、148、107 项，耗时 662.61s、645.13s、652.79s、645.49s；早期 37 项专项不重复计数。

覆盖超大整数和精确方差、全未完成/全失败/混合样本、缺失/截断计量、未知与未核验费用、检查点滞后、崩溃恢复、可信核对来源、跨项目证据拒绝、Live/Hybrid 重复试验、API/CLI 一致性、独立 TCP 与各十个 CLI Worker。固定 fixture 的重复确定性指标相符，墙钟耗时不要求相等。冻结规则/干预/迁移往返和既有审批/Recorded/模型路径也在最终集成套件中覆盖。

本轮没有重跑上轮其余 16 条真实 Docker 执行专项，不将上轮 756 项和本轮 764 项相加。执行器/容器协议未修改；本轮真实 Hybrid 交叉用例保留实际容器、清理证据与离线 Recorded 复验。最终 Docker 复核没有残留 AgentGit 托管执行容器。

桌面类型检查及 **21 项单元测试通过**，另有 **2 项真实 Electron 边界/交互测试通过，12.7s**。Browser plugin not available；使用仓库已锁定的 Playwright Electron 工作流，不安装浏览器依赖。验证路径为：启动源码构建 → 检查本地协议和沙箱桥接 → 拒绝非法实验标识且不发送 HTTP → 既有四页查询和受控提交。

| 桌面核对 | 结果 |
|---|---|
| 页面身份、非空与框架错误界面 | `agentgit://app/index.html` / AgentGit 正确，既有四页可操作，截图无错误覆盖层 |
| 运行错误 | 测试收集的 pageerror/console error 为空 |
| 隔离与固定业务入口 | renderer 无 Node/process；contextIsolation/sandbox/webSecurity 保留，七个方法实际在 preload 导出；非法实验 ID 在 HTTP 前拒绝 |
| 交互与布局 | 四页查询、幂等提交、键盘图选择及宽度回归通过；Runs/Timeline 检查 1440/1024/768/360，分支页截图为 1280 宽 |
| 证据 | 新前缀 `S7-statistics-*` 保存截图/观测，不覆盖旧 S6 文件 |
| 尚未验证 | S7 表单、统计/trial 列表、试验控制的桌面视图尚未接入；本轮不算 S7 UI 验收，也未重建分发包或补齐原生专项 |

Ruff、207 文件格式检查、106 个产品源文件严格 mypy、桌面 typecheck 和 `git diff --check` 通过。保留既有 TestClient 弃用、React Flow directive 和测试色彩环境提示；未新增依赖、迁移或付费模型调用，业务库未自动升级。

## 证据绑定

Python 源码为 `source-sha256:00dadc90a7e59206dbc92e2109c67e697c7231e9c68bc497d93f4492ad881879`。汇总 JSON 保存完整去重用例、桌面变更文件摘要及未变的需求设计/实施方案、DESIGN 和 uv.lock 摘要。原始文件位于 `.agentgit/evidence/`。

| 文件 | SHA256 |
|---|---|
| S7-statistics-final-1.xml | `1bd474ee93f06bd7bea238cd51bbd178d6361c78bc4acd51b64907780c5152bb` |
| S7-statistics-final-2.xml | `2a1f72d6247015a22fdd10d2c857aae402f66175645da609a1f2831d4dd27f86` |
| S7-statistics-final-3.xml | `1df2679997a71a8f2c74840522fb6812bebcdfb719e08a24f7f82423f6043dcc` |
| S7-statistics-final-4.xml | `ee84292b921a76edd827437cb3ec977ef2fd93a428e96bd24d3fc8b7de740fed` |
| S7-statistics-desktop-unit.xml | `13108b707a4c98eeac4ec6b806a09644100dd579ffb208a2faca8f840b95cac6` |
| S7-statistics-electron.xml | `de98aebd3492b91f4912f66cec48a27808f11b351dd362ea49208c9ed5ba16c7` |
| S7-statistics-summary.json | `3466a6aa33a986e6b085cb397f1a94779b0968ebd6e67889740876faf56510f9` |
| S7-statistics-process-live-audit.json | `6990ffd3817d01a899f3c13428c8dd3f781040d90ce53f660939f3b2b77a1ef4` |
| S7-statistics-process-hybrid-audit.json | `142458e94441971959f87d5a7963b5e7fa6425f36a3bbfe55241a8bc4bd44e70` |
| S7-statistics-cleanup.json | `01505cb398b653e849886dabc5b9a0a40a60c3ea1d6bd42eab6bf4a312368ba5` |

下一步按 [桌面接线方案](../S7-desktop-experiments-plan.md)完成实际实验表单、manifest/分组/统计/trial 展示和控制，再核对 S7-T01–T06 整体退出条件。
