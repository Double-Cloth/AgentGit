# S12-D A2A 操作者 API／CLI 工程记录

2026-11-01，基线 `b0425fac113b50a3bf5984b22b27b7e1e8d33951`，延续[入口细则](../S12-D-operator-plan.md)。当前源码 `source-sha256:99956eeb32e492aa55a2bcd7ee903725e60961151e2b0aa1bf1c51c14e4be1e9`，锁 `13df29416247b05eb1ab2a97b9ef2d6035af2903b1e52e8562d3ef1aaa45ed3a`；head `0033_a2a_runs`、79业务表加版本表共80保持。本批没有新依赖、迁移、业务库操作或分发包，未合并main。

## 实现范围

HTTP新增公开有界发现 `POST /a2a/bind`、项目下 `POST/GET /a2a-runs`、按原键只读确认 `GET /a2a-submissions?key=...` 和本人程序查询 `GET /a2a-runs/{run}/program`。创建固定程序、明确分支HEAD／版本、原键及可选严格Recorded来源，只入队；发现不读取Worker凭据，不接受卡片替代地址。CLI新增 `a2a-bind`、`a2a-submit`、`a2a-submission`、`a2a-list`、`a2a-program`，状态、控制、事件、产物、Fork及精确审批继续复用既有命令和服务。可执行演示与离线程序构造见 [examples/a2a](../../examples/a2a/README.md)。

原键确认只读原收据，并核对同项目／本人／原路由／Job／Run，不因当前环境变更重新提交或重新核验旧来源。审批前核对所属Run，实际决策仍走原精确BoundAction核验；预查询在变更连接外完成，避免单连接池递归占用。发送和远端取消始终分别审批，不允许凭任意远端taskId控制任务。

`services/a2a_views.py` 提供本人内容边界与适配投影，通用Management入口统一覆盖Run／Job、事件、程序、审批／决策、控制、历史／checkout／Diff、产物／下载、执行、行为和Fork。A2A清单只返回本人；通用Run清单在SQL分页前过滤其他操作者A2A任务，其他人的游标不可用。相同CAS提交可由本人实际Run映射授权，单独知道hash不能取得内容。通用研究Replay拒绝A2A；研究语义合并也不接受A2A状态。既有同项目仓库／分支／指标清单仍可能展示不透明ID及状态元数据，这不构成内容权限，也不是新增通用ACL体系。

Run的 `adapter.kind=a2a_program/v1` 分别给出本地状态、远端最后确认状态、等待原因、远端取消确认、逻辑用量及实际计量。未知写／不完整结果的实际量为null，另列 `registered_attempts`；已保存响应字节是网关结果字节，不宣称socket流量。Recorded实际量为0，显示历史来源task/context、cursor、来源范围，以及物理包装状态和完整逻辑状态的不同hash；取消只停止本地回放。成功Live只展示 `requires_verification` 候选，提交时仍严格检查完整来源闭包／环境，候选不是已核验承诺。Recorded仍使用本机PostgreSQL和CAS。

## 执行与复核

两轮各冻结892份完整工作输入及完整Git基线。v1的1,245单元全部通过（26.31秒）；45项新集成中44通过、1失败（227.34秒）。失败为缺失来源的409错误码断言：产品按原语义返回 `replay_mismatch`，测试误写 `recording_missing`。全部相关进程终态后只修订该断言及格式，不改产品；v2其余891份输入与v1相同，源码及锁完全相同。v1报告和原始包保留。

v2的45项新集成全部通过（240.41秒），包括30项操作者／项目负向路由，原键、分页、缺失来源、通用Replay拒绝、未知写、独立远端取消审批、本地Recorded取消、地址预拒绝、重定向和秘密进入物理CAS前拒绝。真实TCP用例实际运行离线示例构造器，先后启动2个API进程和7个CLI进程，覆盖API重启确认、独立审批／Worker和去除全部协议凭据的Recorded。两个API均按测试安排终止并等候退出，CLI均正常退出；不能把这些进程数当作故障注入覆盖数。

同一v2冻结输入的77项相关兼容全部通过（563.48秒）：Management API 23、CLI 3、真实进程1、Inspection 7、版本入口2、MCP入口2、LangGraph入口1、协作入口23、Merge Proposal 1、Merge Run 1、Debug Report 13。本轮共122个不同集成节点，无错误或跳过；单元沿用相同产品源码的v1报告，不重复累计。此前bf3f6c…57b1的215项内核／恢复报告继续归属此前源码，本批没有重标为当前源码全量执行。Ruff检查、561文件格式检查和mypy231源码检查通过。

`S12-D-operator-v2/analysis/reports.json` 独立核对892份当前输入、全部XML、唯一节点及v1失败归属。`audit-live.json`／`audit-recorded.json` 使用标准库读取实际服务、数据库导出和类型化CAS，不导入产品执行器：45份原始包共1,095条类型化对象记录、39条Live实际调用（38完成、1未知）、117个Live检查点、86次协议请求及38条接受消息；数量为不同实例之和。进一步从零重算36个成功Live来源的36调用／108检查点，并核对3个Recorded目标、7个本地检查点及2次来源调用消费（2完成、1本地取消）。全部42个A2A Job的原键归属、实际2 API／7 CLI进程及Recorded前后协议请求数保持一致均通过。两个审计读取同一原始集合，对象数不能相加；HTTP响应字段由实际测试验证，本批没有把HTTP响应另存为独立重放数据集。Live审计早于兼容结束，其中compat_passed=0表示当时未纳入，最终77项以独立reports.json为准。

## 保留门槛

下一步接入既有桌面运行／时间线／分支实验与Diff，再做相关完整兼容、四适配最终包和D3。T04／T05／T06／T07整体尚未退出；独立Windows安装／覆盖升级／卸载、原生保存／DPI、S8人工标注／Top-k及S11人工oracle继续待补。本记录不将工程通过等同正式验收，不合并main。

提交前726份关键运行输入与v2冻结完全一致，722份暂存字节相同；三个既有未修改文件和新增真实进程测试按Git规则转换CRLF／LF，后者逐字节核对仅换行差异，不修改工作输入或产品源码。原stage检查对新增文件换行差异的拒绝日志保留，随后明确单列该差异再通过。最终 `S12-D-operator-seal-v1` 索引12,394份文件及118个未跟随链接，索引SHA256 `b4758e42844482b2b5cf4bbdc0eaa5f2c3b4cee85219b2efa7c64d78e4331b78`，汇总SHA256 `4838f23905e3fa5092d4b9db6dec91ae7c3bdcb4b4117f8581f367ec08b4a6a3`；此前Recorded耐久封存18,877份文件和本次全部索引文件均重新读取复核通过。所有实际Runtime已终态。
