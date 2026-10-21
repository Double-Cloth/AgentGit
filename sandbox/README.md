# 固定执行镜像与底层隔离验证

本目录属于 S4 的镜像准备和内部基础设施验证。持久执行记录与受管后端已接入调用网关，见 [执行记录](../doc/acceptance/S4-execution-ledger.md)；生产 Tool Registry/计划/预算观察及完整恢复接入仍未完成。不能以本套件通过代替 S4 验收，也不开放通用执行 HTTP/CLI。`doctor --sandbox` 继续报告 `execution_ready=false`。

## 已冻结内容

`image.lock.json` 保存本次实际预置的 Linux/amd64 镜像内容地址、基础镜像 digest、Python 版本、Debian 快照及可信源码摘要。`packages.lock` 是镜像内完整 dpkg 清单，包含 Git `1:2.39.5-0+deb12u3`、ripgrep `13.0.0-4+b2`、coreutils `9.1-1`、dash `0.5.12-2`、findutils `4.9.0-4`、grep `3.8-5`、sed `4.9-1+deb12u1` 和 tar `1.34+dfsg-1.2+deb12u1`；Python 为 3.12.14，无额外 Python 包安装。

构建上下文只传入 Dockerfile 和五个可信源码文件；不复制项目其余源码、配置、CAS 或数据。Debian 使用固定签名快照，Git/rg 的直接版本显式指定，构建时允许访问镜像仓库和 Debian 快照。运行阶段只接受本地已有 `sha256:` 内容地址，不按 tag 查找、不自动 pull、不安装依赖。

## 准备与核验

在仓库根目录执行：

```powershell
uv run --locked python sandbox/prepare.py
uv run --locked pytest tests/sandbox
```

缺少锁定镜像或能力时检查失败，不以 skip 通过。该套件不需要 PostgreSQL；默认 `pytest` 只运行单元测试，必须显式选择 `tests/sandbox`。当前实测为 Windows 宿主 + Docker Desktop Linux Engine 29.8.2/cgroup v2；本地通过不代表 Linux 宿主 CI 已通过。

需要重新构建时显式执行：

```powershell
docker build --provenance=false -f sandbox/Dockerfile -t agentgit-sandbox:v1 .
docker image inspect agentgit-sandbox:v1 --format '{{.Id}}'
uv run --locked python sandbox/prepare.py --record --image-id sha256:<上一步的完整摘要>
uv run --locked pytest tests/sandbox
git diff -- sandbox/image.lock.json sandbox/packages.lock
```

`--record` 只接受显式内容摘要，核对实际镜像内源码、Python 和完整包清单后更新锁文件。tag 只用于准备阶段查找构建结果；执行器不使用它。构建器版本/存储模式和构建元数据可能影响本地镜像内容地址，不保证不同机器重新构建得到相同 digest；新摘要必须作为新策略版本审阅提交，不能静默修改已冻结 Job 的镜像或降级到 tag。已经冻结的调用必须预置它所绑定的原镜像。

可用 `docker save --output <本地受控文件> <锁定的 sha256 地址>` 保存预置镜像，再以 `docker load --input <该文件>` 恢复；镜像包不得入 Git。恢复后仍运行 `prepare.py` 核验锁定内容地址与内容，不能只看 tag。

## 内部执行边界

PID 1 与采集桥接进程为 UID 10001，命令为 UID 10002，共享 GID 10003。抽象 Unix socket 的对端 UID 必须为 10001；命令 UID 无法终止采集身份或伪造控制响应。命令环境通过固定 `/usr/bin/env -i` 清空，仅提供固定 PATH/HOME/LANG。基础镜像的公开版本环境变量只留在镜像/可信 PID 1，不继承宿主环境。

根只读、capabilities 全移除、no-new-privileges、默认 seccomp、network none、私有 IPC/cgroup namespace、无宿主 bind/device/额外权限；内存与 swap 上限均为 512 MiB，CPU 1 核，PIDs 64。工作区为 128 MiB tmpfs，显式 `exec,nosuid,nodev` 支持工作区脚本；临时目录为 32 MiB `noexec` tmpfs。PID 1 从启动独立计时，最大 120 秒，到期退出使内核终止整个 PID namespace；管理面另行停止、删除并核对容器消失。

输入文件采用采集身份所有、共享组 `0770` 的固定模式；支持直接执行带 shebang 的脚本。执行身份可读写/替换文件，但不能 chmod 采集身份所有的原文件；需要调整权限时创建替代文件。新增文件必须让采集组可读；用户收紧权限造成无法采集时拒绝导出。

传输不是 tar 解包：每个 JSON 帧最多 4096 字节，文件按长度携带原始字节和 SHA-256，复用跨平台路径规范，最多 1000 文件、单文件 8 MiB、总计 32 MiB。导出前核验无执行身份残留进程，拒绝软/硬链接、FIFO、设备、非法名称、权限不足、遍历/文件超限及前后元数据变化；宿主再次检查所有路径、大小、hash 和完整回执。空目录不形成版本化产物，但同样检查路径与权限，不可用于绕过检查。

Engine API 的非 TTY 多路流分别采集 stdout/stderr，各保留 1 MiB 原字节并持续有界排空；退出码来自 exec inspect。容器日志驱动固定 `none`。OOM/pids/CPU 证据取自可信采集身份读取的 cgroup 文件，不凭退出码或用户打印的 JSON 断言成功或资源原因。

## 剩余 S4 接入

`sandbox_executions`、请求/结果 Schema、授权输入 manifest/CAS、Job 租约下的 intent 先落库、取消/丢失租约清理和完整结果复用已由 `services/executions.py` 与调用网关接入。`SandboxContainer` 只供受管内部执行服务及本套件使用。生产 Registry/配置/计划、结果观察与工作区推进、恢复后的新 attempt 和预算仍待接入；全部 S4-T09–T20 通过后才能开放两个生产工具并验收 S4。

协议依据：[Docker Engine API 1.44](https://docs.docker.com/reference/api/engine/version/v1.44/)、[Moby 25 的冻结 API 定义](https://github.com/moby/moby/blob/v25.0.0/api/swagger.yaml)、[Docker Engine API 概览](https://docs.docker.com/reference/api/engine/)。固定使用已有全部隔离字段的 1.44，避免要求较新的 1.51；仍需实际 cgroup v2/资源能力检查。Engine 的本地管道由固定参数的 `docker system dial-stdio` 连接，stdin 半关闭与非 TTY stdout/stderr 多路帧已由真实容器验证；不新增 Docker SDK 依赖。
