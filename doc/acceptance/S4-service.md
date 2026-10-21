# S4 业务 HTTP 与 CLI 进展

- 日期：2026-10-21。
- 状态：S4 进行中；服务入口接入，S4-T09–T20 真容器执行仍未完成。
- 前置：ac4f211，见 [核对继续](S4-reconciliation.md)。Schema head 仍为 0008_call_resolutions，无新增依赖/迁移。
- 操作与请求：[API.md](../API.md)。

## 实现

新增 ManagementService 与严格入口契约，HTTP/CLI 共用项目、配置、任务、提交、状态、控制、审批、游标和产物读取。业务写入、响应引用和幂等记录在同一短事务，JobService 原子路径继续复用。API 只提交 Job，独立 Worker 执行，不使用 BackgroundTasks 保存执行。

显式 Bearer、回环 Origin/CORS/Host 和项目头共同保护边界。原始 JSON 在框架解析前有界接收和规范校验；错误含稳定 code/证据字段/关联编号，不返回输入/秘密/堆栈；JSON 日志和最小指标接入。

Run 可继承或收紧 Task/配置预算，固定到 Job payload；恢复查询不会把其他任务快照误算为消耗。状态提供 HEAD/version，产物经实体验证读取 CAS，不接受磁盘路径，attachment 和 manifest ETag 下载。

CLI 提供 init/config/task/run/status/pause/resume/cancel/artifacts/approvals/decision/worker/serve，参数与业务错误为 JSON/非零退出，单次 Worker 失败/耗尽/未知也非零。适配锁定 Typer 0.27.3 的内置 Click 异常，无新增 CLI 依赖。

## 用例证据

| 要求 | 实际验证 |
|---|---|
| S4-T01 | 真实 TCP API 提交后退出、重启，独立 CLI Worker 完成同一 PostgreSQL/CAS 的 Job，HTTP 可查产物 |
| S4-T03 | 重复动作返回原结果，同键异参 409，并发创建一次；响应落库前故障时项目/配置/任务/控制/幂等记录全部回滚 |
| S4-T04/T06 接入 | HTTP 检查动作原文并决定，fake 实际写仍为 1；requested→安全点 applied，普通恢复同 Run，旧版本拒绝 |
| S4-T07 | 无/错令牌、重复认证头、错误 Origin/Host、跨项目 ID、非法 JSON/分页、超大分块请求拒绝，错误与日志无秘密/正文 |
| S4-T08 | 每页 7 条反复重连，seq 无重复遗漏；产物 hash 与 manifest 一致，attachment/ETag/不缓存 |
| CLI/预算 | 完整命令流程、JSON 参数错误、非零预算失败、空队列；收紧生效、扩大拒绝；恢复初期不读其他任务预算 |

最终命令：`uv run --locked pytest tests/unit tests/integration --junitxml=.agentgit/evidence/S4-service.xml`。**267 项通过**（113 单元 + 154 真实 PostgreSQL 集成），耗时 306.88 秒；本批新增 32 项，包含原审批/恢复/核对和 S1–S3。Ruff 检查、83 个文件格式检查、mypy 49 个源码文件通过，仅有既有 Starlette/httpx 弃用警告。本地通过不代表 Linux CI 已运行。

原始报告不入 Git，SHA-256：

- `.agentgit/evidence/S4-service.xml`：`8f0a95beee39279993285997467859d5d8c7e30984c0cd7a0a3e7dc3d3b6c7ae`。
- 未改变的 `uv.lock`：`16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

## 剩余门槛

下一步按 S4 第 7–9 步冻结执行 Schema/镜像/资源策略，建立执行记录与容器管理，验证可信采集、独立截止时间、整容器清理及全部 T09–T20。doctor --sandbox 保持 execution_ready=false，禁止宿主执行回退。全部退出条件通过后才能进 S5；完整 S5–S10 与必选 S12-A/B/C/D 范围不变。
