# S10 Windows 0.1.6 分发候选

2026-10-27，分支 `codex/s10-repair`。候选已构建、审计并完成包内复验：35 桌面单元与 17 条打包 Electron 流程全部通过，无失败、错误或跳过。包含 Repair Lab、原子准备、精确审阅、候选交接和未知结果提示，继承 S7/S8 和既有四页。package.json/package-lock.json 只调整根版本，没有更换依赖；产品 API 协议仍为 0.1.0，后端身份仍为 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，schema head 为 `0023_repair_proposal_reviews`。

本轮只构建、审计和运行便携程序，未执行安装、覆盖升级或卸载。用户暂缓的独立 Windows/原生验收及人工标注仍待补，不以包内验证代替。

## 分发文件及包审计

构建前逐个核对旧 0.1.5 四件套与[原记录](S8-package-015.md)摘要一致，旧包保留于 `.agentgit/packages/desktop-0.1.5/`；新四件套独立保存于 `.agentgit/packages/desktop-0.1.6/`。通用 Setup/RELEASES 文件名不能识别历史版本。

| 文件（相对 desktop） | bytes | SHA256 |
|---|---:|---|
| out/make/squirrel.windows/x64/AgentGitSetup.exe | 156140544 | `c08cb49a2c765601b15d29b420212d51de2f1c44cc9f75a2a0a16bebf279ff97` |
| out/make/squirrel.windows/x64/AgentGit-0.1.6-full.nupkg | 155384140 | `4a62cc38e9a10877ccb6748d0b2dadf7210505489c0f86c1aac7452bb7e7ddae` |
| out/make/squirrel.windows/x64/RELEASES | 79 | `4b2925263f3b73ef3f2c885e5c8022d720573a31eb05d1b9da6ba8695a2e26d0` |
| out/make/zip/win32/x64/AgentGit-win32-x64-0.1.6.zip | 159361689 | `71be203d7a9274541f27591a5472282bd4f2e5c3c8d0a7b9566af471806b1830` |

Setup 和主程序均为 NotSigned。ASAR 为 99 项、2,866,929 bytes，SHA256 `29c96a89c48946909612757cad961ac3544fe5ae0cc1cc63bee70b74c32b646b`；36 个运行构建文件与本地构建逐字节一致。根白名单、禁止开发/测试文件、三套字体/OFL 与 26 项声明依赖授权通过；唯一生产 Node 依赖仍为 electron-squirrel-startup 1.0.1。临时 ASAR 副本分别加入额外构建文件、改成旧根版本，审计实际拒绝两个反例，交付包未被修改。

ZIP 安全解压到新临时目录 `C:/Users/DC/AppData/Local/Temp/agentgit-portable-0.1.6-1wqzvvfx`；73 个文件、376,853,683 bytes 与打包目录一致，Nupkg 中 ASAR 也一致。包内流程明确从该目录 AgentGit.exe 启动；新 S10 与 S7/S8 公共运行器要求 isPackaged、Windows x64、0.1.6/页脚及实际程序路径，应用进程只用 System32 PATH，移除 NODE_PATH/NODE_OPTIONS。基础 HTTP 边界沿用原独立启动器，不将其声称为另一项无 Node 环境证明。

## 包内流程矩阵

| 组 | 当前实测状态 |
|---|---|
| 桌面单元 | 35 项通过 |
| 四页与连接 | 2 条通过 |
| 既有真实设施 | 4 条通过：Worker/执行环境、生命周期/Chromium 缩放、五工具/Fork/Recorded/Live/Diff、模型接线/计量/未知 |
| S8 报告与候选 | 2 条通过：HTTP 边界及独立数据库/API/Worker 的真实诊断与 A/B 闭环 |
| S7 实验 | 2 条通过：HTTP 边界及真实 Live/Hybrid、控制与重开只读；Live 为 9 通过/1 取消，Hybrid 两组各 5 通过，40 条容器执行均清理 |
| Repair Lab | 7 条通过：真实取消/未知 2、preload 桥接 1、真实候选交接 1、明确 HTTP 页面 1、真实观测 1、真实 Prompt/Plan 联合修复 1 |

所有结果均是本候选实际运行，不累加源码或历史版本结果。新增真实修复仍使用固定 Mock；预先声明的未知故障和明确 HTTP fixture 与真实设施范围分别保存，不用于统计研究成功率。旧供应商接线测试使用固定 HTTP 替身，估价不等于供应商账单，没有实际付费调用。

联合修复实际接受两项字段缩减、六加四通过，真实版本竞争后仅创建新配置 29，没有增加 Job/Run，来源和重开 POST 不变。观测六加四仍为 test_only，按钮禁用且实际绕过批准得到 403；仅保存实验定义，不增加配置/Job/Run。未知调用在取消/重开后仍有一项预留、没有响应或验证通过结论，实际调用一次；排队取消没有派发试验。候选交接不执行搜索，实际 confirm 输入拒绝。S8 真实 A/B 为 0/5 与 5/5，原静态报告与来源保持，不替代冻结 20 例或人工标签。

| 包内原始归档 | CAS 对象 / 原字节 | 六加四新 Mock 调用 / UTF-8 字节 |
|---|---:|---:|
| S10-package-016-joint-archive.json | 1723 / 4497546 | 58 / 655324 |
| S10-package-016-observation-archive.json | 1445 / 2694896 | 110 / 967266 |
| S10-package-016-unknown-archive.json | 890 / 2121889 | 一次结果未知，预留保持，不能作为完整复测样本 |

独立审计重算类型域摘要、硬验收、原始调用/用量、任务/语料/配置绑定、采样先后和精确决定；未知请求的原始 SHA-256 与唯一调用意图相符。对象数按各自归档统计，不宣称跨仓去重或文本相异就是统计独立。三项审计通过后才形成上述结论。

导出仍替换原生保存对话框返回值，Chromium 1/1.5/2 强制缩放不是 Windows 原生 DPI。实际依赖异常只操作测试自己的 Worker/context/镜像查询，没有停止外部服务或删除共享固定镜像。包内试验使用独立 test_* schema 和自己的 CAS/Worker，不迁移业务库。

本次既有真实流程配置增加显式证据前缀路径，保留原默认路径；新复验不会覆盖旧 S6 原始 XML/trace。此前记录的历史报告覆盖问题没有被掩盖。临时负向审计首次用绝对目录加载 ASAR 包未解析其 exports；改为从既有审计脚本位置使用 Node 包解析后两个反例通过，不涉及交付代码或放宽审计。

## 复验与门槛

静态检查、make、包审计后设置 AGENTGIT_DESKTOP_EXECUTABLE 为新 ZIP 内程序绝对路径，为每组设置不同 AGENTGIT_ELECTRON_EVIDENCE_PREFIX。分别执行 playwright.repair.config.ts、playwright.config.ts、playwright.real.config.ts、playwright.diagnosis.config.ts、playwright.experiments.config.ts。真实流程需仓库测试数据库和固定镜像；运行中保持产品 Python 包、fixture/helper、uv.lock 和构建文件不变。

`.agentgit/evidence/S10-package-016-*` 保存分发清单、审计/反例、签名状态、35 单元、17 包内流程及逐组 XML，由 `S10-package-016-summary.json` 冻结。完整复验后再次逐字节核对新四件套、解压程序及旧 0.1.5 归档未被运行改写；运行数据和测试配置均不进入分发包。源码、真实 fixture、构建/依赖与最终文档摘要一并封存。

继续 30 例/20-10 划分/四自动基线及 D2；S8 标签待人工审阅，原注入点 Top-3 4/8 不被本示例替代。S6/D1 的独立 Windows/原生验收仍暂缓，S11/S12-A/B/C/D 保持必选。没有宣告 S10/D2 或完整目标完成，也没有合并 main。
