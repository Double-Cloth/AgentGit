# S4 生产五工具与完整工作区推进

- 日期：2026-10-21。
- 状态：S4 进行中；本记录接续 `86e63ee` 执行记录后端，不替代整个阶段验收。
- 前置：固定镜像、授权输入、耐久调用、Job 租约与清理已实现。
- Schema：仍为 `0009_sandbox_executions`，未修改历史迁移；无新增依赖。

后续已完成停止后新 attempt 与完整本机阶段验收，最新证据见 [S4.md](S4.md)。下文保留本批五工具提交时的范围和记录。

## 实现范围

`AgentConfig` 可显式授权 `code_exec`、`terminal_exec`，生产 Tool Registry 使用与执行服务相同的冻结签名和 sandbox 效果策略；模型只收到可用工具、本 Run 的文件清单和策略引用。默认配置仍为三个基础工具，读取配置/签名不连接 Docker。

后端从包内 `image.lock.json` 读取已审阅镜像，并检查平台、Python、依赖及五个可信源码摘要。`prepare.py --record` 在实际核验镜像之后同时更新准备目录和包内副本，普通核验检查两者相同。新配置不能选择任意镜像；源文件和 JSON 策略均进入 Job 固定代码摘要。运行时不 pull，不回退宿主 Shell。

容器工具任务只能提交持久 Job，由 Worker 将冻结策略绑定到 ExecutionService；同步三工具入口拒绝容器配置。研究计划通过 `artifact_write` 生成脚本，再真实运行 Python 统计及 Shell 文件整理。五工具任务共有 13 次工具、15 次 Mock 模型调用和 7 个最终产物。统计脚本只根据 `sources.json` 计算，不包含硬编码答案；独立测试在容器外核验 9 条结论、6 个来源、3 个框架及每框架 3 条结论。

Session 登记当前工作区字节与 MIME，工具意图仍绑定当前 Run/检查点。观察成功结果时要求当前调用存在完整录制，核对 occurrence、请求、响应、镜像/策略/解释器、输入 manifest、清理和完整性，再读取并核验所有输出文件。工作区先校验业务大小/数量限制，在同目录下分阶段物化完整新树；替换失败恢复旧目录。正常增删改可作为后续工具输入；异常、非法导出和业务预算不足不发布半工作区。

CLI 新增 `config --with-execution`，不能与 `--file` 同用；HTTP 使用相同完整 AgentConfig。两者仍调用同一管理服务和 Job 幂等机制，重复提交不会多启动容器。CLI/HTTP 操作命令及产物位置见 [API.md](../API.md#五工具容器流程)。

## 恢复结算修正

真实五工具故障测试复现：执行结果已保存、`tool.observed` 尚未提交时立即取消，旧流程会在恢复开头先取消，漏计已发生的工具调用。新增只消费录制的 `CallGateway.recorded()`：缺少已保存结果时返回 None，不创建意图、不启动 Adapter；存在结果时严格核验身份、策略、checkpoint 和响应上限，恢复文件并结算原步骤/调用/Token，然后落实取消或持久期限。

成功恢复不重复创建第一个 Python 容器；取消/到期恢复只保留这一个真实执行，工具计数为 11，并保留已保存的 `statistics.json`。正常继续最终为 13 次工具、两个真实容器，统计被 Shell 移至 `results/statistics.json`。原 Run 的检查点保持不变。

新增录制查询以 `run_id/logical_step_id/call_index/attempt` 定位，沿用既有唯一约束索引；项目归属另外核验。实际 Adapter 调用与工作区物化在事务外，未因此新增索引或改写历史 Schema。

## 验证

定向真实套件 13 项通过：双工具与单工具权限、完整结果保存后恢复/取消/到期、工具预算阻止第二个容器、Python 异常、非法链接导出、业务文件大小上限、HTTP/CLI 配置与重复提交/产物下载、未审阅镜像拒绝。工作区单元测试另验证增删改、超限不改变旧文件和目录替换失败回滚。

构建 sdist/wheel 成功；在隔离 Python 导入路径中直接从 wheel 读取代码及资源，包内锁文件与准备目录相同，`bundled_policy()` 实际通过源码摘要核验。固定镜像仍为 `sha256:37837ca77fde6fa5ab6e183ea9484888a107eb79359d2788069c295478fa8967`。

完整回归命令：

```powershell
uv run --locked pytest tests/unit tests/integration tests/sandbox tests/execution -q --junitxml=.agentgit/evidence/S4-five-tools.xml
```

最终 368 项通过，599.20 秒：146 单元、154 真实 PostgreSQL 集成、31 容器基础、37 真实执行用例。仅有既有 Starlette/httpx 弃用警告，无 skip。Ruff 检查、108 个文件格式检查及 mypy 59 个源码文件通过；sdist/wheel 构建与包内策略读取通过。测试结束按实际管理标签查询，无遗留执行容器。远程 CI 尚未推送/执行，不以本地结果代替其证据。

报告留在忽略的本地证据目录；`S4-five-tools.xml` SHA-256 为 `546365adfe23ae8f507d677933c60717e569b842ca6edf72e9dde03123d51ead`。未改变的 `uv.lock` SHA-256 为 `16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

提交前再次复核需求文档：设计 `c3a7fbee2e7852f77794dae96c37e74e7f609c5ead4cfb642e83c20f01296c3f`，实施方案 `c5c2305c98dfb6321bc7fe69f159dc4a03cd7dfbd850294cb6980cc51f15cd3d`。二者仍含 Electron 桌面与必选 S11，全文交付范围未缩减。

## 尚未完成

完整结果丢失时仍保守保持 `outcome_unknown`。下一步须在旧容器确认停止、原输入/策略不变且策略允许的条件下，建立可审计的新 attempt，并在继续前结算旧尝试、为新尝试重新计预算；不得把清理证据当作原调用成功回执。还须补齐全部生产入口下的 S4-T09–T20 证据后正式验收。

`doctor --sandbox` 仍为 `execution_ready=false`，S4 未退出，不启动 S5。总目标继续覆盖 S0–S11 与 S12-A/B/C/D，含 S6、S11、S12 的 Windows x64 安装及便携包验收。
