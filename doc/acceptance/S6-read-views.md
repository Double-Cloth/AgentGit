# S6 桌面只读 API 增量

本记录只验收桌面需要的后端增量，不替代 S6-T01–T12、D1 或安装包验收。S5 已验收代码由 `235bbdf` 合入 main；增量在 `codex/s6-desktop` 开发。

## 实现与边界

- 项目内 Run 列表采用既有索引和稳定复合游标；返回当前关联 Job 与目标，不泄露 Worker/租约字段。
- 历史产物只列所选 Commit 的版本，游标不能跨项目或检查点；同名文件下载保持相应原字节。
- 执行详情从逻辑调用追溯明确的物理来源，复用 RecordingReader 校验请求/策略/输入/响应/日志/输出归属。只读路径不能构造 Docker/执行服务；不是启动或重试入口。
- 内容类别固定为 command、code、stdin、stdout、stderr、metadata、input_manifest、output_manifest。有界分段保持二进制原字节，附摘要、总大小、偏移和后续偏移；HTML/脚本只作为下载内容。
- 失败、尚未发布、清理未知分别保留原事实；缺证据返回 recording_missing，不补成功结果或输出清单。恢复复用显示逻辑 Run 与物理 Run，不允许以来源调用 ID 越过当前 Run 边界。
- capabilities 提供既有固定配置及能力版本，保持认证；无数据库/引擎探测副作用，Worker/执行环境明确 unknown。没有新增迁移、Python 依赖或放宽认证/CORS。

## 本机证据

原始 JUnit 位于忽略的 `.agentgit/evidence/`，下列组有交集，不能相加作为唯一用例数。

| 证据 | 结果 | SHA256 |
|---|---|---|
| S6-read-views-core.xml | 264 passed，194.10s；单元、列表/API、版本入口、Replay Job 等；在 capabilities 增量前运行 | `6cece59f5747d9535847edd448b19738fd8f0433ce276ee5a028936966f49f6b` |
| S6-read-views-execution.xml | 14 passed，127.03s；实际 Shell/Python、恢复复用、未知清理、缺对象及 Recorded 入口 | `54723c79e87a955cf67a42c513304cf25e728bf93540c469ebe028c94dcbfc7b` |
| S6-read-capabilities.xml | 7 passed，3.30s；最终只读 API 及无副作用能力探测、五工具配置可提交 | `9c4387b4aac8cfb6041694f513826c8c454200e0a2a9d816d04d12a66c6709c2` |

首次专项执行有两个测试准备错误：同一分支创建多个活动写者违反既有约束。已将列表占位 Run 设为终态、另一 Run 放到独立分支后重跑通过，生产约束未放宽。

最终静态检查：Ruff check 与 167 文件格式检查通过，mypy 84 个源文件通过，sdist/wheel 构建通过；`sandbox/prepare.py` 实测固定镜像及包清单 verified=true。全目录收集 615 项（含 sandbox），本增量没有宣称全部 615 项已重跑。

镜像仍为 `sha256:37837ca77fde6fa5ab6e183ea9484888a107eb79359d2788069c295478fa8967`，packages SHA256 `7e6fc440f1d27570607a7000cd0e46e0d50d88cee8484f2df13f3283064f833d`。冻结镜像代码未修改。

桌面主进程/页面、真实模型 Adapter、真实 Electron、安装/便携包与独立用户运行证据尚须后续完成。
