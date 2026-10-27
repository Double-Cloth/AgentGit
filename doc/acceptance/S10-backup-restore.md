# D1 补验与 D2 隔离备份恢复

2026-10-27，按实施方案 6.4 完成两次独立 PostgreSQL/CAS 备份恢复。两项真实集成全部通过，源产品身份保持 `source-sha256:f66ad3bbdd82a4b16a787bad17002d35827b240faf948aeb8a60748e276e54a9`，Schema head `0023_repair_proposal_reviews`。D1 是当前版本对基础历史/产物能力的补验，不冒充历史 D1 版本当时已做的验收，也不是业务数据备份。独立 Windows、原生及人工标注仍待补。

## 实际过程和边界

`test_backup_restore.py` 的两个独立案例各自生成随机 test schema、临时 CAS 和恢复数据库。D1 包含完成任务的两个产物、完整 Commit/事件及一次真实中断写；D2 另包含失败源、实际修复搜索及尚未领取的缩减 study。通过既有普通 Worker 在 `artifact_write` 实际返回、响应尚未保存时注入进程级中断，保存一项 started 未决调用。测试中的心跳线程已退出，之后才开始一致备份。

使用现有 PostgreSQL 17.6 容器内的 `pg_dump --format=custom` 仅导出自己的 schema，同时复制 CAS。通过带 UUID、以 `_test` 结尾的新数据库和新 CAS 目录恢复；先验证目标无业务表，再运行 `pg_restore --single-transaction --exit-on-error`。没有停止共享 PostgreSQL/API/Worker/Docker，没有导出、迁移或覆盖业务库。

恢复前后逐表比较全部 64 张表的行数与内容摘要，包括迁移、引用、状态与调度记录；逐对象重算带类型域的 hash 和长度，逐 Commit 还原状态及产物，核对 Branch HEAD、Job checkpoint 和记录。D2 的修复 study 保持 queued，没有自动执行。原始 source 在备份后、恢复核验后均保持不变。

恢复后真实普通 Worker 从已过期租约接管中断 Job，最终保持 `outcome_unknown`。禁止任何新模型/工具调用的检测器以及前后调用行数共同核对零重发；再次领取不执行任务，没有把已写但响应未知的动作补成成功。源数据库和 CAS 的备份字节始终保留，恢复数据库和原测试 schema 在验收后仅清理自己的随机名字，独立只读检查确认均已移除。

| 案例 | 索引对象 | 对象引用 | Commit | 备份 dump SHA256 |
|---|---|---|---|---|
| D1 基础历史补验 | 246 | 449 | 38 | `d5e79608c603bcd1186f938ffdfbe2b7d2807bc888ae0297551c51edf3b2c8a6` |
| D2 修复研究记录 | 444 | 830 | 67 | `f8f0ec49aceb5cf7938db21f9d3d9919210f578424d540e69468431e6fe953e0` |

## 证据与复现

忽略目录 `.agentgit/evidence/S10-backup-restore-v1/` 的 D1/D2 UUID 子目录各保留 `database.dump`、完整 `cas/` 与 `summary.json`。两个摘要 SHA256 分别为 `de24754c31df5ad23e8044fd1ad7de1ef57537834a33f3cc340b03025dfe4c66`、`bbf6cbb49a226229257970332c410e1755c1a2ccbe0842f894074e818eee6bf0`。额外独立复核所有物理 CAS 文件的类型域摘要、dump 字节和临时数据库清理；汇总 `S10-training-backup-summary-v1.json` SHA256 为 `86def136b9f257ba051efb86b6f07c722ccf597d77d1944401a0fe3e2a082525`。

最终测试报告为 `S10-backup-restore-v2.xml`，2 项通过、无跳过。首次 v1 的两项在备份前因测试把 provider 名漏写 `/v1` 而失败，保留原报告；修正精确版本标识后两项重跑通过，没有修改产品调用或恢复策略。失败尝试不累计为通过或成功恢复。

复现要求既有 `agentgit-postgres-1` 容器和 `127.0.0.1:55432/agentgit_test`、公开本地测试角色具备创建测试库权限；不安装新依赖或索取密码。脚本保守拒绝其他宿主/库/用户配置，不将此固定本机验收推广为任意生产备份工具。每次生成新的隔离资源及证据子目录，不覆盖旧备份。

```powershell
uv run --locked python -m pytest tests/integration/test_backup_restore.py -q -p no:cacheprovider
```

这两项补齐当前基础历史和修复研究记录的备份恢复证据；S10 的保留十例与完整 D2 报告继续，不能据备份通过宣称阶段或完整项目已验收。
