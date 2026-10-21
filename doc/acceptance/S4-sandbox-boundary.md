# S4 真容器基础边界进展

- 日期：2026-10-21。
- 状态：S4 进行中；本批验证镜像与内部容器生命周期，尚未接入生产执行工具。
- 前置：c914a58，见 [服务入口](S4-service.md)。数据库 head 仍为 0008_call_resolutions，无新增依赖或迁移。
- 准备方式与完整边界：[sandbox/README.md](../../sandbox/README.md)。

## 实现与冻结

新增 `adapters/execution`：Docker 本机管道/Engine API、有界多路输出、独立 UID 的 PID 1/采集桥接进程、有界文件协议、固定配置的新容器及核对清理。宿主仅以固定参数数组连接本机 Docker 管道；用户 Shell/Python 在 Linux 容器内执行，不提供宿主执行回退。

镜像的固定内容地址为 `sha256:8b6a5f3823eb4901c524704e8a2721d9e5e9f96f9038c87a877d83eb298b8d7b`；锁文件同时保存基础镜像 digest、Python 3.12.14、固定 Debian 快照、五个可信源码文件和 Dockerfile 的摘要。完整依赖清单为 `sandbox/packages.lock`，SHA-256 `7e6fc440f1d27570607a7000cd0e46e0d50d88cee8484f2df13f3283064f833d`。运行不按 tag 查找、不自动下载。

`sandbox/prepare.py` 实际进入固定限制的检查容器，核验镜像内源码、Python 和完整软件包，确认清理后才允许显式记录新摘要；已修复 Windows 默认 GBK 读取含中文 Dockerfile 的问题，统一使用 UTF-8。构建上下文白名单防止发送项目数据和本地凭据。没有将镜像包、原始测试报告或运行数据加入 Git。

UID 10001 负责可信采集，UID 10002 执行命令；控制 socket 检查内核提供的对端 UID。PID 1 独立计时，退出终止整个容器进程命名空间；管理面另行停止、删除、核对消失。只读根、全部 capability 移除、no-new-privileges、默认 seccomp、无网络/宿主挂载/设备、CPU/内存/PIDs/tmpfs 的固定限制均实际检查。

输入和导出只接受普通文件，逐帧校验路径、长度、hash、数目和完整回执。导出前后检查执行身份残留进程，读取前后核对 inode/权限/大小/mtime/ctime；逐项 scandir 限制遍历成本，不预先创建大目录清单。stdout/stderr 各保留 1 MiB 原字节并标记截断；日志驱动为 none。真实退出码来自 Engine exec inspect，资源证据来自可信采集读取的 cgroup。

## 真实用例证据

| 要求 | 本批底层验证 |
|---|---|
| T09/T10 | 管道、重定向、可执行输入脚本、临时 Git 仓库；Python 参数/stdin/JSON 计算；非零退出、缺命令、语法/运行异常 |
| T11/T19 | 两容器同路径写入和删除互不影响；穿越/Windows 特殊名、软硬链接、FIFO、权限不足拒绝；输出字节与文件内容核验 |
| T12 | 实际非 root 身份、seccomp、NoNewPrivs、零 capabilities、私有 namespace、无 host bind/device；外网与本机探测地址连接失败、根不可写；宿主合成秘密和 Docker context/host 环境覆盖不进入执行环境/管理目标 |
| T13 | 700 MiB 分配触发 OOM 与可信证据；进程数触发 EAGAIN 与 pids.events；工作区/临时目录实际 ENOSPC；CPU 密集子进程触发 cgroup throttling |
| T14 | stdout/stderr 各超过 1 MiB、非法 UTF-8 原字节；单文件/总量/文件数/空目录遍历上限明确拒绝 |
| T15/T16 部分 | 后台进程阻止导出；PID 1 到期停止；真实宿主 Worker 被杀后，主命令和脱离会话的后代仍按独立截止时间终止 |
| T17 部分 | 固定摘要缺失返回真实 Docker 404，无下载/宿主回退；另有单元检查证明远程 endpoint 和超时/镜像 tag 请求在内部边界拒绝 |
| T20 部分 | 无权终止采集身份；控制连接被拒绝，用户打印“成功”不改变 Engine 的非零退出码；采集进程消失后不能导出，残留后台进程与权限错误失败 |

这些是基础设施证据，不能替代生产 Tool/Job/CAS 事务、完整 T09–T20 或 D1 验收；未实现项见下节。

## 检查结果

环境为 Windows + Docker Desktop Linux Engine 29.8.2、cgroup v2、Linux/amd64 固定镜像。没有声称 Linux 宿主 CI 已运行。

1. `uv run --locked pytest tests/unit tests/integration --junitxml=.agentgit/evidence/S4-sandbox-regression.xml`：286 项通过（132 单元 + 154 真实 PostgreSQL 集成），312.74 秒。
2. 导出改为逐项扫描后，`uv run --locked pytest tests/unit tests/sandbox -q --junitxml=.agentgit/evidence/S4-sandbox-boundary-final.xml`：163 项通过（132 单元 + 31 真实容器），83.83 秒。没有跳过缺 Docker/镜像的用例。
3. Ruff、95 个文件格式检查、mypy 54 个源码文件通过；镜像内容核验通过，测试结束无遗留 managed 执行容器。只有既有 Starlette/httpx 弃用警告。

两批去重覆盖 317 项；报告是上面两个命令的实际范围，不宣称同一个命令运行全部套件。原始报告保留在本地，SHA-256：

- `S4-sandbox-regression.xml`：`4454861a232b2a608c3b701997ba01c71cf35f68d7c43276e8d2d698ad505ea4`。
- `S4-sandbox-boundary-final.xml`：`29b531b70fad31b767e140741bc967d5dbd4eb983ee2c13c353f16b62827431b`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

## 剩余门槛与下一步

接下来实现生产 `terminal_exec/code_exec` 请求/结果 Schema 与固定策略、`sandbox_executions` 迁移、授权 manifest/CAS、Job 租约下 intent 先落库再创建容器、取消/失租约/恢复清理、完整结果录制复用及新 attempt 预算。只有完整正常成功且清理确认的 manifest 才能推进 checkpoint；失败保留原状态，操作计量不写入逻辑 AgentState。

需要把两个工具接入原 Registry、计划/配置与 Worker，补齐真实业务入口下的 T09–T20，核验不支持语言、权限/参数篡改、取消、fencing、未清理禁止重发和保存结果后崩溃不重复启动。随后才能验收 S4 并进入 S5；`doctor --sandbox` 保持 `execution_ready=false`。S5–S10 与必选 S12-A/B/C/D 的完整目标不变。
