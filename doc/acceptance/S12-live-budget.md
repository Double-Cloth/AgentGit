# S12 当前兼容：Live 回放预算校验边界

2026-11-01。最终候选的 v6 续跑在 **229通过、1失败** 后终止（2637.05秒）。`test_model_runtime` 的原反例要求非法有效预算在创建分支前返回领域 `validation`；实际 `ReplayService.prepare` 在组合配置时直接调用 `evolve`，泄漏了 Pydantic `ValidationError`。原源码 b9dee5…9319 的报告、冻结输入和失败保留，不改写为通过。

## 修复与真实边界

`src/agentgit/services/replays.py` 只改 Live 有效预算的组合校验：沿用普通 `JobService.enqueue` 的 `parse_contract`／完整 `AgentConfig` 路径，将不合法组合转换为既定领域错误。预算 `within` 授权仍先检查；真实模型的正整数 USD 上限和至少2 KiB响应上限保持，未扩大预算或修改模型／权限／恢复策略。

原回归增加两个参数：零 USD 及授权内100,000 USD微单位、2047字节响应上限。每例先用真实隔离 PostgreSQL／Worker形成成功来源，然后调用原 `ManagementService.replay`。拒绝后核对全部79业务表行、CAS文件摘要及模型HTTP替身请求／生成计数与拒绝前一致，且没有新分支。HTTP是进程内确定性协议替身，未访问供应商或产生真实费用。

## 正负证据与归属

| 记录 | 实际终态 |
|---|---|
| `S12-live-budget-negative-v1` | 两失败；零USD复现原异常，响应反例因未声明USD上限先被预算授权拒绝。保留该准备错误 |
| `S12-live-budget-negative-v2` | 只将响应反例的USD上限设为原授权内；两个参数均在未改产品源码上以原始Pydantic异常失败，20.09秒 |
| `S12-live-budget-positive-v1` | 测试字节与negative-v2完全相同，产品只差 `replays.py`；**1,245单元＋56不同集成全部通过**，746.23秒，无跳过 |
| 静态检查 | 两文件Ruff／format及231源文件mypy通过；补留同输入实际日志，不增加测试节点 |

56项集成为模型运行时15、Live回放10、Live审批1、Recorded任务15、Hybrid运行时15。新源码为 `source-sha256:dca8f8b5e4400e05ce0a6bc49a26560322638fea315922c1f1e55ff5fce7ba7d`；锁仍 `13df29416247b05eb1ab2a97b9ef2d6035af2903b1e52e8562d3ef1aaa45ed3a`，Python/API0.1.0、head33／79业务表加版本表共80保持，无新依赖、迁移或业务库操作。

运行前908份输入及正负冻结副本已核对，negative-v2和positive-v1仅上述产品文件不同，原回归内容相同。`boundary-audit.json` 绑定真实XML、精确输入及模块节点；它核对测试归属，不声称独立重放全部模型／回放状态机。正例XML SHA256为 `085b9e72a6ccc67e73632a1cd2af2f21f2cdbb547afa3530048d8d3fc0788683`；负例v2为 `5b453e1cf5c3669aa0ed0e78537174e8e8b9b15f344b4b0137f7dd4e50b74afa`。

`S12-live-budget-seal-v1` 索引 **17,819文件**，82链接未跟随；全部索引文件已独立重新读取核验。索引SHA256 `20a150907a296b136582c19ce1d362b6e6cdeff78337cdf595408d7409cff1ed`，汇总SHA256 `570e958cfc95db41b9e861b4440d7d7310280f0880e40454f551dfff77046b95`。原v6失败、两版负例、正例、冻结／原始临时输入及分析器均保留。所有相关Runtime实际终态后才更新本文。

## 后续完整范围

新增参数使当前完整入口实际收集为2,903；两个旧冻结研究模块仍为50项，当前技术范围 **2,853**，不新增默认skip。已有1,301项新源码通过；其余须按精确补集继续执行，并核对同源码／锁／驱动与文档变更归属。旧 b9 源码分版共有2,208不同通过节点，但原套件未完，不重标为本次新源码结果。

0.1.8四件套不嵌Python后端，前端／构建字节保持原候选归属；34项包内验收尚未执行，须连接最新后端。v5的间歇失租仍保留为未定位观察，本修复不宣称解决该问题。完整范围见[当前与历史评估](S12-regression-scope.md)。正式原生／独立环境、人工标签／原定位未达标、S11人工oracle与D3门槛保持，未合并main。
