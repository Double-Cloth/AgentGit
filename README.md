# AgentGit

AgentGit 为 AI Agent 提供执行历史的版本管理与受控调试：回溯状态、隔离分支、重放执行、比较差异，通过实验定位故障、搜索修复，并在审阅后合并有限的结构化结果。

当前定位是 **Windows x64 本机桌面应用**。桌面候选版本 **0.1.8**，Python/API 版本 **0.1.0**。2026-11-04 按用户要求将工程成果合入 `main`；**合并不代表正式 D1/D2/D3 验收通过**。剩余工作见[验收收尾清单](doc/acceptance/MAIN-integration-2026-11-04.md)。

## 基本思想

- 执行状态也需要版本历史：Commit、分支、检查点、事件和内容寻址对象保留来源，HEAD 与状态原子提交，历史不随重试改写。
- 恢复、复现、修复分别验证：Recorded 消费完整录制并重算状态与产物，实际模型/工具调用为零；Live 使用独立输入、预算与授权重执行；Hybrid 只替换声明的观测。
- 用证据判断修复：冻结验收器、A/B 单变量实验、故障候选、有界搜索、字段缩减与独立复测串联；保留失败、不确定及无解结果，不承诺全局最小修复或严格因果证明。
- 审阅与执行分开：Merge Review 验证有限结构化状态，批准生成双父历史；继续执行须显式创建新任务，不继承旧执行授权。
- 效果未知先核对：精确审批、幂等键、租约与 fencing 防重复；外部写入未知不自动重发。Shell/Python 只在固定 Docker 容器执行。

桌面包含 Runs、Timeline、Branch Experiment、Diff、Repair Lab、Merge Review。当前支持 MCP 固定 HTTP/批准的 Python stdio、登记的 LangGraph 图、固定四角色协作和本机 A2A；任意图恢复、公网多租户及完整协议实现不在当前范围内。

## 架构与目录

Electron 主进程通过受限 IPC/回环 HTTP 连接 FastAPI；独立 Worker 从 PostgreSQL 领取耐久任务。数据库保存历史和调度元数据，CAS 保存不可变内容，Docker Linux 容器承担受控执行。桌面不嵌后端、不自动迁移数据库，关闭桌面不取消任务。

| 路径 | 用途 |
|---|---|
| `src/agentgit/` | 领域服务、API、CLI、适配器、Worker |
| `desktop/` | Electron、React、TypeScript、React Flow 与打包 |
| `migrations/` | Alembic；当前 head 为 `0033_a2a_runs` |
| `sandbox/` | 固定执行镜像、锁与隔离策略 |
| `examples/`、`tests/` | 离线演示、固定数据与分层测试 |
| `doc/` | 设计、实施、API 和验收证据 |
| `.agentgit/` | 本机数据、证据、分发包，受 Git 忽略，需另外备份 |

## 首次准备

后端使用 Python **3.12**、uv **0.12.19**、PostgreSQL **17.6**。五工具任务还需 Docker Desktop **Linux 模式**和已核验的固定镜像。桌面构建使用 Node.js **26.x**（`desktop/.node-version` 当前固定 26.13.0）与 npm **12.1.0**；安装包用户无需 Node/npm/Vite，但须独立准备后端。

在仓库根目录 PowerShell 按顺序执行以下命令，完成一次性环境准备：

```powershell
uv python install 3.12
uv sync --locked
docker compose up -d --wait
uv run --locked alembic upgrade head
uv run --locked agentgit doctor --database
```

迁移适用于新环境；已有业务数据先停止写者并备份。Compose 只启动数据库，默认公开开发凭据仅用于本机。程序**不会自动读取 `.env`**；通过进程环境设置，字段见 [.env.example](.env.example)。默认数据根 `.agentgit/dev`，数据库端口 `127.0.0.1:55432`。

容器执行前导入固定镜像并核验：

```powershell
uv run --locked python sandbox/prepare.py
uv run --locked agentgit doctor --sandbox
```

缺镜像按[镜像说明](sandbox/README.md)导入或显式构建并审阅新摘要；不绕过锁或回退到宿主执行。

## 启动与运行

启动顺序为 **API → 普通Worker →（按需）Repair 编排器 → 桌面**。**推荐使用一键脚本**（在仓库根目录的一个终端里完成全部启动，无需手动开多个终端）：

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\start-local.ps1
```

脚本自动完成：核对/启动 Docker Desktop 与 PostgreSQL → 核对数据库迁移（落后时提示先备份再升级）→ 生成随机令牌（显示一次，并通过环境变量自动传给各服务，无需复制粘贴）→ 后台启动 API 与普通 Worker（日志在 `.agentgit/dev/logs/`，窗口隐藏）→ 首次运行自动 `npm ci` + `npm run setup` → 前台启动桌面。关闭桌面或 Ctrl+C 后自动停止本轮启动的后台服务。

可选开关：`-Repair` 同时启动 Repair Lab 三个编排器（仅 Repair Lab 需要）；`-NoDesktop` 只启动后端服务，按回车停止。Worker/编排器直连数据库、不需要令牌；操作者默认 `local-operator`，数据根 `.agentgit/dev`，数据库 `127.0.0.1:55432`。

桌面打开后，在「连接与项目」的**本机已有项目**列表中直接点击项目即可进入工作区（显示名称、短 ID 与创建时间），无需手抄 UUID；也可粘贴 UUID 或新建项目。

### 手动分步启动（备选）

不想用脚本时可按以下步骤手动启动；令牌在本轮启动中只共享给桌面：**终端 1 生成并显示一次，复制后在第 4 步粘贴**。除下述变量外全部终端保持默认配置，且后端命令都在仓库根目录运行。

**第 1 步 · 启动 API**（终端 1，保持运行。第 2 行会**显示生成的令牌，复制它**，第 4 步要用）：

```powershell
$token = (uv run --locked python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
$token    # 显示生成的令牌，复制这个字符串
$env:AGENTGIT_API_TOKEN = $token
uv run --locked agentgit serve --port 8000
```

**第 2 步 · 启动普通 Worker**（终端 2，仓库根目录）：

```powershell
uv run --locked agentgit worker
```

**第 3 步 · 启动 Repair 编排器**（终端 3，仅 Repair Lab 需要；按启用流程分别独立运行，不能代替普通 Worker）：

```powershell
uv run --locked agentgit repair-worker
uv run --locked agentgit repair-study-worker
uv run --locked agentgit repair-confirm-worker
```

**第 4 步 · 启动桌面**（终端 4。需 Node 26.x／npm 12.1.0，可先 `node --version` 核对；首次构建较慢）：

```powershell
cd desktop
npm ci
npm run setup
$env:AGENTGIT_API_TOKEN = Read-Host '粘贴终端 1 显示的令牌'
$env:AGENTGIT_API_URL = 'http://127.0.0.1:8000'
npm start
```

`Read-Host` 交互输入不进入 PowerShell 命令历史。桌面主进程读取 `AGENTGIT_API_TOKEN` 后即删除环境副本，令牌不进入渲染进程、IPC 或偏好文件。`npm start` 编译后启动真实 Electron，加载包内页面；首次连接时选择或创建项目。全部启动后可用 `Invoke-RestMethod http://127.0.0.1:8000/health` 核对 API 存活（不代表 Worker/Docker 就绪）。分发用户运行 Setup 或解压 ZIP，四件套在 `.agentgit/packages/desktop-0.1.8/`，摘要见[候选记录](doc/acceptance/S12-package-018.md)。这些文件不随 Git 克隆自动提供。构建新包在 `desktop/` 执行 `npm run make`，输出 `desktop/out/`，不要覆盖历史归档。

无公网/无模型密钥的三工具同步演示：

```powershell
uv run --locked python examples/research_agent/run.py
```

该演示自行执行，不代替耐久 Worker/五工具验收。桌面五工具、Fork、Recorded、Live、Diff 按[D1 指南](doc/D1-desktop-demo.md)操作，CLI 见[五工具流程](doc/API.md#五工具容器流程)。适配演示见 [MCP](doc/S12-A-mcp-demo.md)、[LangGraph](doc/S12-B-langgraph-demo.md)、[四角色](examples/collaboration/README.md)、[A2A](examples/a2a/README.md)。默认 Mock 不产生模型费用；在线供应商调用另行配置并明确预算。

## 日常运维

| 场景 | 操作 |
|---|---|
| 健康检查 | `Invoke-RestMethod http://127.0.0.1:8000/health` 检查存活，`/ready` 检查数据库；不代表 Worker/Docker 就绪 |
| worker 报 `database_unavailable` | 先确认 Docker Desktop 已运行、`docker compose ps` 为 healthy；再 `uv run --locked alembic current` 核对迁移（须为 head `0033_a2a_runs`）；`doctor --database` 只验证连通性，schema 落后也会导致此报错 |
| 排队无进展 | 检查普通 Worker、对应编排器和 Run/Job 状态，不重复创建任务 |
| 执行环境异常 | `uv run --locked agentgit doctor --sandbox` 检查 Docker Linux 与固定镜像，不删除共享镜像排障 |
| 审批/未知结果 | 查询精确动作和版本，核对实际效果后再继续，保留原幂等键 |
| 停机 | 先暂停/处理活动任务并确认安全状态，再停编排器、Worker、API，最后 `docker compose stop`；Ctrl+C 不表示外部动作已撤销 |
| 升级 | 停止写者并备份数据库/数据根，保存代码/锁/镜像版本；`uv sync --locked` 后迁移只前进，再按相同配置启动 |

备份必须同时保存 **PostgreSQL、完整 `AGENTGIT_DATA_ROOT`（含 CAS）和匹配的代码/依赖锁/镜像策略**。停止所有写者后，用 PostgreSQL `pg_dump -Fc` 归档到受控目录，复制数据根并记录摘要；固定镜像用 `docker save` 另存。不要只备份安装目录，不执行 `docker compose down -v` 删除业务卷。

恢复时在空数据库和独立数据根用 `pg_restore` 及原字节恢复，使用匹配版本核对引用、hash、HEAD、checkpoint 后开放服务。中断任务保留未知/待核对状态，不自动重发写入；不绕过旧 Job 的源码/锁守卫。回滚使用整套备份，不在业务库 downgrade。参考[恢复证据](doc/acceptance/S10-backup-restore.md)。当前 GC 仅 dry-run，不把扫描结果用于删除。

## 开发验证与验收

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
```

默认 pytest 只运行单元。集成测试先准备独立 `_test` 数据库（Compose 首次可运行 `docker compose exec postgres createdb -U agentgit agentgit_test`）及独立测试数据根，再执行 `uv run --locked pytest tests/integration`；容器测试显式选择 `tests/sandbox tests/execution`。不得连接业务库。S8/S10 历史研究使用对应冻结源码，见[回归范围](doc/acceptance/S12-regression-scope.md)，不改写旧成绩。

桌面在固定工具链下执行 `npm run typecheck`、`npm test`、`npm run build`；Electron/真实后端专项见[桌面说明](desktop/README.md)。运行期间冻结源码、锁、迁移和驱动，终态后才修改。

既有证据为 2,855 技术节点分批覆盖、34 实际包内节点通过，另 50 项历史冻结研究，不是单进程全量一次全绿。本轮验证与正式缺口见[收尾清单](doc/acceptance/MAIN-integration-2026-11-04.md)。详细说明见 [DEVELOPMENT.md](doc/DEVELOPMENT.md)、[API.md](doc/API.md)、[设计文档](doc/AgentGit_设计文档_v1.0.0.md)、[实施方案](doc/AgentGit_实施方案_v1.0.0.md)。
