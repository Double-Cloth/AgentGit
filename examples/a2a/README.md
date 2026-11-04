# A2A 本机操作者演示

开发版桌面已提供“运行 → A2A 远端任务”：明确登记地址／服务／非秘密主体，发现卡片，核对消息、预算及分支HEAD／版本，再显式提交。原键须保留；未确认时先只读确认，显式重试保持同键，明确版本冲突才刷新并重新提交。时间线批准确切发送／取消动作；成功Live可进入“A2A成功轨迹回放”显式Fork及完整Recorded。Worker仍需独立启动并预置凭据，桌面不收集密钥。证据与开发版范围见[桌面记录](../../doc/acceptance/S12-D-desktop.md)，最终包及正式门槛继续。

范围固定为官方v1.0.1、请求版本1.0和已声明的本机HTTP+JSON公开资料整理服务。只支持明确登记的数字回环根地址、固定身份／卡片和公开文本，发送／取消均按本地策略独立精确审批。历史task/context不授予新任务权限；不展示公网多租户能力。

自动演示使用独立标准库协议服务、真实TCP API的两次启动、独立CLI／Worker、独立测试Schema与CAS：提交后关闭API，再启动API审批，由独立Worker完成Live，然后无协议凭据执行完整Recorded。测试自己创建和销毁测试Schema，不迁移业务库。运行时源码、锁、迁移、例子及驱动必须冻结。

```powershell
uv run --locked pytest tests/integration/test_a2a_operator_processes.py -q
```

手动使用前，按[开发说明](../../doc/DEVELOPMENT.md)准备独立测试Schema、当前head和独立 `AGENTGIT_TEST_DATA_ROOT`，并明确启动可信本机Server。`server.json` 是 `A2AServer` 的JSON声明，至少含schema_version=1、server_identity、principal_id和完整endpoint；不含密码或令牌。Worker所需协议凭据仅放其进程环境；本地API认证与协议凭据分别管理。

```powershell
uv run --locked agentgit a2a-bind server.json --testing | Set-Content binding.json -Encoding utf8NoBOM
uv run --locked python examples/a2a/prepare_program.py binding.json | Set-Content program.json -Encoding utf8NoBOM
uv run --locked agentgit init "A2A 演示" --key a2a-demo --testing
uv run --locked agentgit a2a-submit <repository_id> program.json --expected-version 0 --key a2a-live --testing
uv run --locked agentgit a2a-submission <repository_id> --key a2a-live --testing
uv run --locked agentgit worker --once --testing
uv run --locked agentgit approvals <repository_id> <run_id> --testing
uv run --locked agentgit decision <repository_id> <approval_id> approve --expected-version <version> --key a2a-approve --testing
uv run --locked agentgit worker --once --testing
uv run --locked agentgit status <repository_id> <run_id> --testing
```

程序生成器每次为显式新消息生成新messageId；响应未确认时先用 `a2a-submission` 按原键确认，不能重新生成程序或换键当作重试。未知写保持待核对，不盲目重发；普通暂停只停止本地推进，有已确认远端任务的Live取消需要另一项精确审批。

成功来源状态中的 `adapter.recording_candidate` 只是待核验候选。保存该完整JSON为 `recording.json`，使用其from_commit建立显式独立分支，再提交Recorded：

```powershell
uv run --locked agentgit branch <repository_id> replay --source-run <source_run_id> --from-commit <from_commit> --key a2a-fork --testing
uv run --locked agentgit a2a-submit <repository_id> program.json --branch replay --head <from_commit> --expected-version 0 --key a2a-recorded --replay-file recording.json --testing
uv run --locked agentgit worker --once --testing
```

按Job状态继续单次领取到完成；每次最多推进一个历史检查点。Recorded无需协议凭据，实际协议／模型／工具调用0，逻辑hash按完整历史状态加产物重算；仍使用PostgreSQL耐久保存。其取消仅停止本地回放，不能显示为远端任务已停止。同源码／锁、本人／项目、成功范围及缺依赖检查仍严格生效，失败无Live回退。所有数据可经原status／events／artifacts／checkout／diff查询；桌面和最终分发包须另行验证。
