# S10 搜索原子准备与桌面桥接

2026-10-27，`codex/s10-repair`。为 Repair Lab 的一次搜索提交补齐预注册竞争边界，并提供 14 项固定 IPC。页面、精确 Diff 与桌面完整操作尚未实现，本记录不代表 Repair Lab、S10 或 D2 退出；后续范围见[桌面计划](../S10-desktop-plan.md)。业务库、外部 Worker 和既有分发候选未修改，没有调用付费模型。

## 实际行为

原两步入口要求登记前搜索从未被领取。若已有修复 Worker 持续轮询，界面先提交搜索、再登记 suite 存在竞争。新增 `POST /repair-searches/prepared` 和 `repair-prepare`：先验证所有已提交来源，再在同一事务发布搜索、Job、提案、suite 和十张采样票据。事务提交前其他连接不能领取；任一步失败全部回滚。无需新迁移或暂停 Worker。旧接口、默认空间/预算和历史编码保持。

成功返回同一搜索及 `validation_suite`。嵌套请求和四项回归来源共同进入幂等摘要；改参数冲突，原键在搜索完成后仍返回原身份，不再生成模板或票据。确认/回归仍须在最终候选锁后显式开启，不在提交时预执行。

实际 HTTP 用例在登记发布前暂停事务，另一真实数据库连接的 Worker 领取返回空，且看不到新搜索。释放后只出现一个搜索、一个 suite 和十张票据，提交动作没有新研究 Job/Run；随后 Worker 可领取，实际筛选、缩减和锁后六加四全部通过，仅标 `verified_under_fixture`。不同项目请求和原键异参被拒绝。

另外在十张票据均已写入、外层提交前注入失败，验证搜索/Job/提案/suite/票据全部回滚且无法领取。同键重试只发布一次；提交结果确认时禁止模板重编译、CAS 写入及模型/工具调用仍成功。六个并发请求在 pool_size=1/max_overflow=0 下取得同一搜索与 suite；CLI 同键复用该身份，没有额外执行。

首次完整验证因测试替身要求源 Goal 的适配器摘要，却被用于未带该摘要的固定回归，得到 `needs_review`。已让替身仅作用于声明的源故障，其他任务保持标准 Mock；适配器文件摘要冻结在源 Goal 后重跑通过。失败报告保留，没有降低确认门槛或把执行错误计作失败/通过。

## 桌面边界

新增搜索准备/查询/列表/取消、study 创建/查询/取消、suite 查询、验证开启/查询、最终建议生成/查询、审阅票据与精确决定共 14 项 IPC。主进程固定路径、方法、项目头、请求/响应上限及幂等键，渲染进程不提供 URL。决定逐字段验证 proposal/review UUID、摘要、安全整数版本及固定枚举；禁止附加执行字段。

32 项桌面单元通过，涵盖上述固定路由与非法参数在 HTTP 前拒绝。另以项目已有 Playwright Electron 流程启动真实 Windows Electron，验证 14 项 preload 暴露、实际主进程 HTTP 请求、原键/项目头、非法版本拒绝、保存实验映射，以及渲染进程没有 require/process、IPC 不泄漏身份令牌。该用例使用明确标记的 HTTP fixture，不冒充真实后端执行或 Repair Lab 的用户操作验收。当前没有 Browser 插件入口，复用现有原生 Electron 测试工具。

桌面 typecheck 和 build 通过，依赖与 lockfile 未变；仍使用 0.1.5 源码构建验证，未生成新安装包。新桥接没有进入既有 0.1.5 分发候选。窗口重开和可见操作仍随后续页面验收。

## 复现

冻结源码为 `source-sha256:ebaa512bd4a5d821a5c597c188574bac34cf5d628b93e850bd025c464df4bd82`，Schema head 仍为 `0023_repair_proposal_reviews`。同源码 700 单元与 21 真实集成去重通过；另有 32 桌面单元及 1 原生 Electron 桥接通过，均无失败、错误或跳过。Ruff/格式、mypy 160 个源码文件、桌面 typecheck/build 及完整收集通过；收集不计作执行。

原始归档 `.agentgit/evidence/S10-prepared-v2.json` 的 SHA-256 为 `2f41c1b37206d03148b9ae43aeb1a6c31d6204dd64839bab95ef46dfbdea6b2a`，含 1357 个对象、3,223,091 原始字节。六加四共 56 次新模型调用、592,252 Mock UTF-8 字节；Mock 费用为零，不是供应商 Token 或实付费用。独立复核器 `verify_s10_prepared_v1.py` 重新执行冻结硬验收、核对 CAS 域/长度、调用/Job/Run/语料、确认统计、原子请求与 suite 幂等摘要及票据时序；汇总 `S10-prepared-summary-v1.json` 的 SHA-256 为 `5255a5b7444648623d7d8c4af34b31efc51b4ecada3a9e96a26385c67ee485a1`。

最终报告为 `S10-prepared-unit-v1.xml`（700）、`S10-prepared-v2.xml`（3）、`S10-prepared-compat-v1.xml`（18）、`S10-prepared-node-v1.xml`（32）及 `S10-prepared-v1-repair.xml`（1）。早期失败不累计通过。Python 包和桌面代码已核对暂存字节；两个未修改字体清单的既有 CRLF/LF 规范化差异单独列入汇总，没有改写原资产。原生边界详情归档为 `S10-prepared-native-v1.json`。这些运行数据均位于忽略目录，不进入提交或安装包。

```powershell
uv run --locked pytest tests/unit
uv run --locked pytest tests/integration/test_repair_prepared_search.py
uv run --locked pytest tests/integration/test_repair_pools.py tests/integration/test_repair_pool_registration.py tests/integration/test_repair_api.py
cd desktop
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
npx.cmd playwright test --config playwright.repair.config.ts
```
