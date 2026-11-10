# AgentGit

**为 AI Agent 的执行历史提供版本管理、回放与受控调试。**

AgentGit 将执行状态、事件、检查点和产物保存为可追溯历史，支持隔离分支、重放、差异比较、故障定位、修复实验与结构化合并。

> 项目仍处于开发完善阶段，尚未全部完成，具体缺口见[验收收尾清单](doc/acceptance/MAIN-integration-2026-11-04.md)。

当前面向 **Windows x64 本机桌面使用**，桌面候选版本为 **0.1.8**，Python/API 版本为 **0.1.0**。

[功能概览](#功能概览) · [环境要求](#环境要求) · [快速开始](#快速开始) · [启动与运行](#启动与运行) · [配置](#配置) · [常见问题](#常见问题) · [开发验证](#开发验证) · [文档导航](#文档导航)

## 功能概览

| 工作区 | 已有能力 |
|---|---|
| Runs | 创建与查询执行任务，查看状态、预算和审批 |
| Timeline | 查看事件、检查点、产物和来源，回溯执行过程 |
| Branch Experiment | 从安全起点 Fork，运行 Recorded、Live、Hybrid 及受控 A/B 实验 |
| Diff | 比较状态与行为差异，查看冻结的故障报告和候选干预 |
| Repair Lab | 有界搜索、字段缩减、独立确认与回归，审阅配置版本 |
| Merge Review | 审阅有限结构化结果，批准后形成双父历史，再显式创建后续任务 |

三种执行模式有各自的输入和授权要求。

| 模式 | 行为 |
|---|---|
| Recorded | 消费完整录制，重算状态与产物，实际模型和工具调用为零 |
| Live | 以独立输入、预算和授权重新执行 |
| Hybrid | 仅替换显式声明的观测，保留 recorded/synthetic 来源标记 |

适配范围包括固定 MCP HTTP 工具与批准的 Python stdio Server、登记的 LangGraph 图、固定四角色协作和本机 A2A。任意图恢复、公网多租户和完整协议实现超出当前支持范围。模型默认使用免费 Mock，外部模型须单独配置密钥并明确费用上限。[模型与费用说明](doc/MODEL_ADAPTER.md)

## 架构与目录

Electron 主进程通过受限 IPC 和回环 HTTP 连接 FastAPI。独立 Worker 从 PostgreSQL 领取耐久任务，数据库保存历史与调度元数据，CAS 保存不可变内容，固定 Docker Linux 容器承担 Shell/Python 执行。

桌面、API、Worker 分别运行。单独关闭桌面后，独立启动的后端任务可继续执行。一键脚本则负责本轮服务的启动与退出清理。

| 路径 | 用途 |
|---|---|
| `src/agentgit/` | 领域服务、API、CLI、适配器和 Worker |
| `desktop/` | Electron、React、TypeScript、React Flow 与打包 |
| `migrations/` | Alembic 数据库迁移，当前 head 为 `0033_a2a_runs` |
| `scripts/` | Windows 本地启动脚本 |
| `sandbox/` | 固定执行镜像、依赖锁与隔离策略 |
| `examples/` | 离线演示与适配示例 |
| `tests/` | 单元、集成、容器及启动脚本回归测试 |
| `doc/` | 使用指南、设计、实施与验收记录 |
| `.agentgit/` | Git 忽略的本机数据、CAS、日志、证据与分发归档 |

## 环境要求

| 组件 | 版本与用途 |
|---|---|
| Windows | x64，桌面开发和本地启动脚本的目标平台 |
| PowerShell | 7 或以上，通过 `pwsh` 执行启动脚本 |
| Python | 3.12，由根目录 `.python-version` 固定 |
| uv | CI 固定为 0.12.19，Python 依赖使用 `uv.lock` |
| PostgreSQL | 17.6，`compose.yaml` 固定镜像摘要 |
| Docker Desktop | Linux 容器模式，用于本地数据库和容器执行 |
| Node.js / npm | 桌面源码构建使用 Node 26.x / npm 12.1.0，推荐版本见 `desktop/.node-version` |

运行已有安装包或 ZIP 程序时，只需准备后端及对应执行环境。Node/npm 用于桌面源码构建。`-NoDesktop` 模式跳过桌面工具链检查与安装。

首次安装依赖、下载 Electron 或准备镜像需要网络。已准备的包内桌面资源、Mock 演示和固定镜像可用于离线运行。

## 快速开始

以下命令在仓库根目录的 PowerShell 7 执行，适用于**新的本地开发环境**。已有数据的环境先按[备份与升级](#备份与升级)处理。

### 1. 准备 Python 与数据库

确认已安装 uv，并启动 Docker Desktop 的 Linux 引擎。

```powershell
uv python install 3.12
uv sync --locked
docker compose up -d --wait --wait-timeout 90
uv run --locked alembic upgrade head
uv run --locked agentgit doctor --database
```

Compose 默认创建 `agentgit_dev` 数据库，端口为 `127.0.0.1:55432`，数据保存在命名卷中。开发凭据和其他默认值见[配置](#配置)。

### 2. 核验容器执行环境

五工具任务需要本机已有锁定镜像。先按镜像说明导入，再执行核验。

```powershell
uv run --locked python sandbox/prepare.py
uv run --locked agentgit doctor --sandbox
```

`prepare.py` 默认核验已有镜像。导入、显式构建和摘要审阅步骤见[固定镜像说明](sandbox/README.md)。仅浏览桌面或运行下述三工具同步示例时，可以稍后准备执行镜像。

### 3. 启动桌面与后端

```powershell
pwsh -NoProfile -File scripts/start-local.ps1
```

脚本会核对工具链、同步锁定 Python 依赖、首次安装桌面依赖、启动数据库并核对迁移，再生成本轮令牌、启动 API/Worker 和桌面。令牌显示一次，自动通过环境传给桌面。

桌面打开后，在「连接与项目」选择本机已有项目，或创建项目。运行五工具、Fork、Recorded、Live 和 Diff 的完整操作见[D1 桌面指南](doc/D1-desktop-demo.md)。

### 先试离线示例

完成第 1 步的 Python 依赖、数据库与迁移准备后，可以单独运行三工具同步示例。

```powershell
uv run --locked python examples/research_agent/run.py
```

该示例可在无公网、无模型密钥的环境执行，独立于 API/耐久 Worker。其输入、产物与运行方式见[示例说明](examples/research_agent/README.md)。

## 启动与运行

### 一键脚本

```powershell
# 启用 Repair Lab 所需的三个编排器
pwsh -NoProfile -File scripts/start-local.ps1 -Repair

# 仅启动 API 和普通 Worker，按回车结束
pwsh -NoProfile -File scripts/start-local.ps1 -NoDesktop

# 使用其他回环端口
pwsh -NoProfile -File scripts/start-local.ps1 -Port 8001

# 使用已准备的数据库，跳过 Docker Desktop / Compose 启动
pwsh -NoProfile -File scripts/start-local.ps1 -NoDesktop -SkipCompose
```

| 参数 | 行为 |
|---|---|
| `-NoDesktop` | 跳过 Node/npm、桌面依赖安装与桌面启动，按回车停止后端 |
| `-Repair` | 额外启动 `repair-worker`、`repair-study-worker`、`repair-confirm-worker` |
| `-Port <端口>` | 设置 API 回环端口，并自动传给本轮桌面，默认 8000 |
| `-SkipCompose` | 使用当前环境配置的数据库，跳过 Docker/Compose 启动，仍核对迁移与 API 就绪 |

日志位于 `<AGENTGIT_DATA_ROOT>/logs/<本轮标识>/`，默认在 `.agentgit/dev/logs/` 下，每个服务各有 `.out.log` 和 `.err.log`。可以在其他终端查看启动时输出的具体日志路径。

脚本要求数据库已迁移到代码的唯一 head，迁移缺失或落后会停止并提示手动准备。依赖安装、迁移查询、后台进程或桌面启动失败时，会保留日志并清理已启动的服务。

**关闭桌面或结束纯后端模式前，先将活动任务处理至安全状态。** 正常退出和 Ctrl+C 会进入清理流程，按逆序终止本轮 API/Worker/编排器进程树，恢复调用者原有环境变量和工作目录。Docker Desktop 与 PostgreSQL 继续运行，可在确认其他使用者已退出后执行 `docker compose stop`。直接关闭终端窗口后，应核对剩余进程和任务状态。

### 手动分步启动

需要让后台任务独立于桌面生命周期运行时，可以分终端启动。所有后端终端使用相同数据库、数据根和操作者配置。

**终端 1，生成令牌并保持 API 运行。** 复制显示的令牌，供桌面终端使用。

```powershell
$token = (uv run --locked python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$token
$env:AGENTGIT_API_TOKEN = $token
uv run --locked agentgit serve --port 8000
```

**终端 2，保持普通 Worker 运行。**

```powershell
uv run --locked agentgit worker
```

**Repair Lab 按需额外开启三个终端，每个终端运行其中一条命令。**

| 终端 | 命令 |
|---|---|
| 3 | `uv run --locked agentgit repair-worker` |
| 4 | `uv run --locked agentgit repair-study-worker` |
| 5 | `uv run --locked agentgit repair-confirm-worker` |

**桌面终端，首次安装后启动。**

```powershell
cd desktop
npm.cmd ci
npm.cmd run setup
$env:AGENTGIT_API_TOKEN = Read-Host '粘贴 API 终端显示的令牌'
$env:AGENTGIT_API_URL = 'http://127.0.0.1:8000'
npm.cmd start
```

后续启动可直接设置令牌与地址后执行 `npm.cmd start`。`Read-Host` 输入保留在本轮进程环境，桌面主进程读取后清除环境副本，使用受限客户端访问 API。Worker 与编排器直连数据库。

### 运行与构建分发包

已有分发物可运行 Setup，或解压 ZIP 后启动 `AgentGit.exe`。本机历史归档在 `.agentgit/packages/desktop-0.1.8/`，文件摘要见[0.1.8 候选记录](doc/acceptance/S12-package-018.md)。这些本机文件须另行取得。

构建新的 Windows x64 Setup 和 ZIP，在已准备好桌面依赖后执行以下命令。

```powershell
cd desktop
npm.cmd run make
```

输出位于 `desktop/out/`。历史候选按原摘要独立保留，打包细节、开源授权和包内容审计见[桌面说明](desktop/README.md)。

## 配置

程序从**进程环境变量**读取配置。`.env.example` 用于查阅字段，复制为 `.env` 后仍需自行加载到各终端。

| 变量 | 默认值或用途 |
|---|---|
| `AGENTGIT_DATABASE_URL` | `postgresql+psycopg://agentgit:agentgit_local@127.0.0.1:55432/agentgit_dev` |
| `AGENTGIT_DATA_ROOT` | `.agentgit/dev`，包含运行数据与 CAS，启动脚本日志也存放于此 |
| `AGENTGIT_OPERATOR_ID` | `local-operator`，所有参与进程使用相同身份 |
| `AGENTGIT_API_TOKEN` | API 与桌面共享的本轮令牌，至少 32 个可见 ASCII 字符 |
| `AGENTGIT_API_URL` | 桌面连接地址，默认 `http://127.0.0.1:8000` |
| `AGENTGIT_API_ORIGINS` | 显式回环 HTTP Origin 的 JSON 数组，格式见 `.env.example` |
| `AGENTGIT_TEST_DATABASE_URL` | 独立测试库，默认数据库名 `agentgit_test` |
| `AGENTGIT_TEST_DATA_ROOT` | `.agentgit/test`，与开发数据根互不包含 |

`compose.yaml` 中的凭据为本机开发占位值。自备数据库时先设置 `AGENTGIT_DATABASE_URL`，再使用 `-SkipCompose`。各终端的相对路径均从仓库根目录解释。供应商密钥只配置在需要调用模型的独立 Worker 环境。[配置示例](.env.example) · [模型配置](doc/MODEL_ADAPTER.md)

## 常见问题

| 现象 | 检查与处理 |
|---|---|
| 找不到 `pwsh`、`uv`、`docker` 或桌面工具链 | 核对环境要求及 PATH，重新打开终端；桌面 Node/npm 必须符合 `desktop/package.json` |
| PowerShell 执行策略阻止脚本 | 经本机策略允许时，可使用 `pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/start-local.ps1` |
| Docker 无法连接或容器模式错误 | 启动 Docker Desktop 并切换 Linux 容器，执行 `docker info` 和 `docker compose ps` |
| 端口无法绑定 | 核对端口所属进程，或使用 `-Port 8001`，手动启动时同时修改 API 与桌面地址 |
| 提示 schema 落后，或 Worker 报 `database_unavailable` | 先检查数据库健康状态，再用 `uv run --locked alembic current` 与 `alembic heads` 比较，按备份与升级流程迁移 |
| API 可访问但任务持续排队 | 核对普通 Worker、对应 Repair 编排器、Job/Run 状态和本轮服务日志 |
| `sandbox_unavailable` 或执行环境异常 | 运行 `uv run --locked agentgit doctor --sandbox`，检查 Linux 引擎与固定镜像 |
| 桌面连接失败或业务请求被拒绝 | 核对 API 地址、同一令牌与操作者配置，重启 API 后为桌面更新本轮令牌 |
| 桌面依赖不完整或锁文件变更 | 在 `desktop/` 重新执行 `npm.cmd ci` 和 `npm.cmd run setup` |
| 审批或提交结果未知 | 按原请求与幂等键查询，核对精确动作和版本，再决定后续操作 |

可在其他终端检查 API 与数据库连接状态，端口按实际配置调整。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/health` 返回 API 存活状态，`/ready` 额外检查数据库连接。Worker 与容器执行环境需要分别检查。

## 备份与升级

备份需同时保存 **PostgreSQL、完整 `AGENTGIT_DATA_ROOT`（含 CAS），以及匹配的代码、依赖锁和镜像策略**。

1. 停止所有写者，将活动任务处理至安全状态。
2. 使用 PostgreSQL `pg_dump -Fc` 归档到受控目录，复制完整数据根并记录摘要，固定镜像可通过 `docker save` 另存。
3. 更新代码后执行 `uv sync --locked`，再显式执行 `uv run --locked alembic upgrade head`。
4. 核对迁移、数据库、镜像与任务状态，再按相同配置启动服务。

恢复时使用空数据库和独立数据根，通过 `pg_restore` 及原字节恢复，核对引用、hash、HEAD 和 checkpoint 后开放服务。回滚使用整套匹配备份。中断任务保留未知或待核对状态，当前 GC 仅提供 dry-run。数据恢复过程见[备份恢复证据](doc/acceptance/S10-backup-restore.md)。

## 开发验证

### 无外部设施的后端检查

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

默认 pytest 仅选择 `tests/unit`。Windows 启动编排回归使用命令替身，单独运行以下命令即可，无需真实数据库、Docker 或 Electron。

```powershell
pwsh -NoProfile -File tests/scripts/test-start-local.ps1
```

### 数据库与容器测试

集成测试须使用独立的 `_test` 数据库和测试数据根。默认 Compose 初次准备测试库可执行 `docker compose exec postgres createdb -U agentgit agentgit_test`，已存在时直接复用。

```powershell
uv run --locked pytest tests/integration `
  --ignore=tests/integration/test_diagnosis_candidate_effects_v3.py `
  --ignore=tests/integration/test_repair_frozen_evaluation.py

uv run --locked pytest tests/sandbox tests/execution
```

两个忽略项绑定 S8/S10 的历史冻结源码，复现须在对应原字节工作树执行。当前技术回归范围、前置设施与历史研究归属见[回归范围说明](doc/acceptance/S12-regression-scope.md)。运行期间保持相关源码、锁、迁移与驱动固定。

### 桌面检查

在 `desktop/` 目录执行以下命令。

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
```

Electron 边界、真实后端与分发包专项各有独立入口和设施要求，见[桌面开发与测试](desktop/README.md)。已有历史成绩及其源码、包版本和运行范围按各验收记录归属。

## 文档导航

| 主题 | 入口 |
|---|---|
| 桌面完整演示 | [D1 桌面指南](doc/D1-desktop-demo.md) |
| 桌面构建、打包与专项测试 | [desktop/README.md](desktop/README.md) |
| API、CLI 与五工具流程 | [API.md](doc/API.md) |
| 开发环境与历史变更记录 | [DEVELOPMENT.md](doc/DEVELOPMENT.md) |
| 模型与费用授权 | [MODEL_ADAPTER.md](doc/MODEL_ADAPTER.md) |
| 固定容器镜像 | [sandbox/README.md](sandbox/README.md) |
| MCP / LangGraph | [MCP HTTP](doc/S12-A-mcp-demo.md)、[MCP stdio](doc/S12-A-stdio-demo.md)、[固定图](doc/S12-B-langgraph-demo.md) |
| 多 Agent 示例 | [四角色协作](examples/collaboration/README.md)、[本机 A2A](examples/a2a/README.md) |
| 设计与实施 | [设计文档](doc/AgentGit_设计文档_v1.0.0.md)、[实施方案](doc/AgentGit_实施方案_v1.0.0.md)、[界面规范](DESIGN.md) |
| 合并与正式验收 | [main 整合记录](doc/acceptance/MAIN-integration-2026-11-04.md)、[D3 就绪报告](doc/D3-readiness-report.md) |
