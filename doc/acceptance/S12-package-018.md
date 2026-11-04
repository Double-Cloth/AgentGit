# S12 最终桌面候选 0.1.8（本机工程通过，正式验收待补）

2026-11-02，Windows x64，分支 `codex/s12-adapters`。候选包含原有六页、受控实验／故障报告／Repair Lab／Merge Review，以及MCP、固定LangGraph、四角色协作和A2A工作区。**本机分发和功能工程核对通过；S12／D3正式验收仍未通过，不合并main。** 完整前置见 [D3就绪报告](../D3-readiness-report.md)。

## 候选与运行环境

四个文件在 `.agentgit/packages/desktop-0.1.8/` 独立归档，未签名，后端不嵌入桌面包。由操作者准备Python3.12、PostgreSQL、API、Worker、固定Docker Linux镜像和身份；桌面不会安装或迁移后端。Python／API版本0.1.0，Schema `0033_a2a_runs`，79业务表加版本表共80。

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `AgentGitSetup.exe` | 156166144 | `b186f5ea191e3888408f8051276c2b1fe4007da1f57191638da41586f783397d` |
| `AgentGit-0.1.8-full.nupkg` | 155408968 | `c43cbc00bf614530a369cab690ad272be1f50e88ded990dd54453945ed5de4f5` |
| `RELEASES` | 79 | `2c69575e2dab1e1edfcb96bea0d669858987da01b188df3f4e6ac7b7a01a4c14` |
| `AgentGit-win32-x64-0.1.8.zip` | 159387450 | `d1ca17fdf6498524d98fd0fba061f7321c821e229f9ae10704ed8a2e21d779a1` |

ASAR SHA256为 `1babc4e030f4bd1fbec8171798a24daf9de78496c5f91ebaad062443d89c0a31`：107入口、44运行构建文件、26授权／字体文件，2,963,185字节。根白名单、构建字节、字体及OFL、26项依赖授权、ZIP全部路径与文件字节、Nupkg内ASAR、RELEASES的SHA1／长度均已核对；额外根文件／错误版本篡改副本被拒绝。73个实际便携文件逐一重读，与已归档ZIP对应。没有将开发源码、测试、构建配置、令牌或后端写入包。

固定构建工具为Node24.19.0／npm12.1.0；首轮误用Node26导致cross-zip失败的原日志保留，未升级依赖或修改node_modules。任务内固定Node完成make，prebuild增加工具链检查，全局Node不变。桌面运行进程使用归档ZIP中的实际exe，确认isPackaged、版本／页脚及路径，PATH仅System32且没有NODE_PATH／NODE_OPTIONS；测试后端仍由开发侧准备，不夸大为整个宿主未安装Node。

## 实际测试与范围

产品源码 `source-sha256:dca8f8b5e4400e05ce0a6bc49a26560322638fea315922c1f1e55ff5fce7ba7d`，锁 `13df29416247b05eb1ab2a97b9ef2d6035af2903b1e52e8562d3ef1aaa45ed3a`。修复及原失败见 [协作发布边界](S12-C-publication.md)、[Live预算映射](S12-live-budget.md)和[测试环境记录](S12-test-environment.md)。当前 **2,855个不同技术节点具有分批通过证据**，其中88项真实execution、32项sandbox；另50项历史研究按原源码冻结审计，不重标为当前测试通过。不是单进程全量一次全绿，原失败、诊断、准备错误及源码归属均保留。

47桌面单元及三组noEmit类型检查保留其原实际报告：本轮运行前120份桌面输入和69个构建文件与原报告逐字节一致。没有把这些旧报告描述为重新执行。实际包内34项在新 `S12-package-018-v8` 批次**全部通过，无失败／跳过**：

| 专项 | 不同节点 |
|---|---:|
| 连接降级、资源及IPC边界 | 2 |
| 真实设施、生命周期／强制缩放、五工具与模型接线 | 4 |
| 受控实验 | 2 |
| 故障报告及证据跳转 | 2 |
| Repair Lab、取消／未知／交接及观测禁采纳 | 7 |
| Merge Review及硬拒绝 | 3 |
| MCP HTTP／stdio | 2 |
| LangGraph HTTP／stdio及原生边界控制 | 6 |
| 四角色Live／Recorded和原键恢复 | 2 |
| A2A Live／Recorded、原键确认及本地／远端状态 | 4 |

实际命令使用仓库既有Playwright／Electron配置，唯一前缀 `S12-package-018-v8`、固定工具链和归档可执行文件，未加载开发服务器；完整命令、报告和输入位于同名证据根。供应商接线使用本地协议替身，不调用付费供应商。公网供应商冒烟仍为独立可选项，不纳入离线必过门槛。

## 原始核对与可见结果

- MCP／固定图：18份后端成功轨迹／Recorded／控制包，以及8个实际桌面项目的原始记录重核；桌面合计8次实际工具、36次协议请求，Recorded前后计数保持。stdio程序、解释器／脚本摘要、最小环境与进程退出均有原始依据。
- 协作：来源四角色72 Commit，Recorded新增四角色后共144 Commit、72步映射；消息／收据／共享版本、调用来源与CAS域摘要一致，来源行和指纹不变。台账记录含来源与复用，不作为新增物理调用数。
- A2A：四份桌面原始包共163条对象记录、5条Live调用台账，其中4个完整结果、1个未知；9次实际协议请求、3次接受消息。成功来源与Recorded各3个检查点独立重算状态／产物，Recorded消费一次来源调用且零额外请求。
- 既有页面：联合修复、观测禁采纳、未知结果、合并及拒绝的5组只读审计，114个原始文件和5份基础报告核对。既有读取器及当前契约共同验证，不声称全新独立算法。日志标记、遗漏嵌套读取器和旧runtime字段三次准备错误及部分副本保留；只修证据读取，没有改产品、测试或原始报告。
- 已实际查看时间线1440／768、故障候选1440、协作1440／360及A2A1440截图，内容／焦点可见、无错误覆盖；360px协作表格在自身容器横向滚动，属于最小768px之外的补充渲染证据。原用例还覆盖1024、768、1440及强制1／1.5／2缩放。应用error／pageerror记录为空；warning覆盖按各用例保留，Node的NO_COLOR／FORCE_COLOR为启动器提示，不扩大为所有路径warning全采集。未替代独立Windows原生DPI验收。

截图位于仓库外 `C:/Users/DC/.codex/visualizations/2026/11/02/agentgit-s12-package018-v8/`，已查看截图及摘要见 `review/viewed.json`。`.agentgit/evidence/S12-package-018-v8/analysis/` 保存报告、协议、旧页面、协作和A2A审计；运行前911输入／副本及15驱动核对。后续正式文档在全部Runtime终态后更新，字节差异另核对。最终索引与独立重读结果采用 `.agentgit/evidence/S12-package-018-final-v1/`，具体摘要以该目录实际记录为准。

## 正式待补

0.1.7四件套再次按原摘要核对；升级基线Setup为 `8ca050f02fafc3d41f67eb6ecbc1978bdb78da655a03bb431675e93920e4d912`，未用新out目录冒充旧包。**本轮未安装、覆盖升级或卸载0.1.8。** 独立用户安装／升级／卸载、原生保存／DPI和无开发环境专项按 [S6清单](../S6-windows-acceptance.md)待补；S8人工标签、Top3原4/8未达6/8、S11人工oracle及整体前置仍保留。包内通过不表示D1／D2／S11／S12／D3已正式退出。
