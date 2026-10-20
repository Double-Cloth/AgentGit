# 本地开发与测试

实现进度与阶段证据见 [PROGRESS.md](PROGRESS.md)。README 保留项目介绍；方案和设计的 S0 描述是规划时的历史基线。

## 环境

- Python 3.12；`.python-version` 与 `requires-python` 限定 3.12 系列。
- uv 0.12.19；后端版本由 `uv.lock` 冻结，使用 `--locked` 阻止隐式改锁。
- PostgreSQL 17.6；Docker Compose 只启动本地开发数据库，端口限定 `127.0.0.1:55432`。
- Docker Desktop Linux 模式。S1 只探测引擎；执行镜像、实际隔离和资源限制在 S4 单独冻结与验收。

Windows 在项目根目录用 PowerShell 执行：

```powershell
uv python install 3.12
uv sync --locked
docker compose up -d --wait
docker compose exec postgres createdb -U agentgit agentgit_test
uv run --locked alembic upgrade head
uv run --locked agentgit doctor --database
uv run --locked agentgit doctor --database --testing
```

`createdb` 只在首次准备测试库时执行，已存在时报错，不删除旧库。Compose 中的用户名/密码为公开的本地占位值。使用其他环境时通过 `AGENTGIT_*` 进程环境变量设置配置；程序**不会自动加载 `.env`**，可参考 `.env.example`。不要在命令记录或提交中填写真实凭据。配置错误与数据库连接错误只返回固定信息，不输出 URL 或驱动异常。

## 入口与诊断

```powershell
uv run --locked agentgit version
uv run --locked agentgit doctor --sandbox
uv run --locked uvicorn agentgit.api.main:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` 是不依赖数据库的存活探测；`GET /ready` 实际连接 PostgreSQL，连接不可用返回 503 和 `database_unavailable`。S1 无业务 API，身份认证和项目访问控制在 S4 实现。

`doctor --sandbox` 不创建容器、下载镜像或执行命令；在 S4 完成前 `execution_ready=false`，退出码为 1。引擎可达不代表执行工具已实现。资源能力必须由 S4 的实际隔离测试证明，禁止使用宿主进程作为执行回退。

## 检查命令

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked pytest tests/integration
```

默认 pytest 只运行单元测试，不要求数据库。显式选择 integration 后，缺少真实 PostgreSQL 即失败，不以 skip 或 SQLite 代替通过。GitHub Actions 配置 Windows/Linux 单元检查及 Linux PostgreSQL 集成检查；本地执行不代表远程 CI 已运行。

`AGENTGIT_DATA_ROOT` 与 `AGENTGIT_TEST_DATA_ROOT` 默认是 `.agentgit/dev`、`.agentgit/test`，不能相同或互为祖先。路径用 `resolve()` 核验符号链接/目录联接；Windows 路径比较遵循平台大小写语义。测试库必须与开发库名称不同，且以 `_test` 结尾；仅更换用户名或主机别名不能绕过保护。

迁移默认连接开发库，只前进。集成用例显式设置 Alembic `testing` 属性才允许测试库回退；不要在正式或共享数据库运行回退测试。S1 revision 只建立 Alembic 基线，不提前创建后续业务表。

## 内容协议

领域内容采用 AGJSON-v1；规范、限制和固定向量见 [canonical fixtures](../tests/fixtures/canonical/README.md)。金额使用整数微单位，时长使用整数毫秒，不接受浮点数。Blob 直接保存二进制原字节。

基础契约使用 Pydantic strict/extra=forbid/frozen。载入内容时调用 `parse_contract`，同时校验 AGJSON 与显式 Schema 版本；未知版本返回 `unsupported_schema`，未知字段返回 `validation`，不默默丢弃字段。

S1 实现参考官方资料：[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[SQLAlchemy psycopg 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)、[Pydantic 配置](https://docs.pydantic.dev/latest/api/config/)。这里只冻结工程契约，不宣称 Agent 已可执行任务。
