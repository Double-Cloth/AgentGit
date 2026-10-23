# S6 模型配置与计量

默认仍为 `mock-research/v1`，无外部密钥、无外网可运行离线任务。真实模型使用固定 `openai/gpt-5.4-mini-2026-03-17`、官方 Responses API 与标准处理层级；来源、价格版本及基础边界见[实施清单](S6-model-plan.md)和[基础层记录](acceptance/S6-model-foundation.md)。真实账户/快照访问权限尚未验证，未运行计费冒烟。

## 显式配置

[配置示例](examples/openai-research-config.json)包含固定模型、供应商选项和 USD 预算，默认只使用三个基础工具。`max_cost_micro_usd=1000000` 表示 USD 1 的价格表估计预算；这是配置示例，不授权自动发起真实计费调用。使用前应根据任务、账户和所需费用上限修改。

现有 CLI 可读取同一严格配置文件：

```powershell
uv run --locked agentgit config REPOSITORY_ID --file doc/examples/openai-research-config.json --expected-version CURRENT_CONFIG_VERSION --key UNIQUE_KEY
```

将大写占位符替换为实际值；其余 task/run/worker 流程沿用 [API 与 CLI](API.md)。HTTP 的 `NewAgent.config` 使用同一个 `AgentConfig` 契约。模型、选项和正整数 USD 预算必须同时声明；Run/Live 预算只能收紧，不能省略有限费用上限使其变成无上限。配置不接受密钥或自定义供应商 URL，不会缺配置时回退到 Mock。

只在 Worker 的启动进程环境中设置 `OPENAI_API_KEY`，不写入配置、仓库、Snapshot、日志、桌面或 IPC。创建/读取配置和 Recorded 不需要读取该环境变量。现有 API 身份令牌 `AGENTGIT_API_TOKEN` 与供应商凭据各自使用既有边界，不能互相替代。声明真实模型意味着该 Run 的模型上下文会提交到所选供应商；工具仍由本地 Registry 和固定容器策略执行。

桌面 Runs 已接通能力描述中的模型选择：默认 Mock；选择外部模型时必须另填 USD 上限，最多 6 位小数，不收集密钥。Live 从外部模型来源或切换到外部模型时同样要求本次 USD 上限。Timeline 分别展示已结算估计、未释放/未核价预留、剩余额度及 Replay 实际用量，缺失值显示未知。包含上述能力的 b649bf3 Windows 候选已重建并通过便携复测，见[新候选记录](acceptance/S6-model-packages.md)；独立用户安装和原生保存仍待验收。五工具配置仍需绑定后端已审阅的固定执行策略，不能将 `--with-execution` 与 `--file` 同时使用。

## 预算与结果

每次模型调用在写入不可变调用意图后才允许进入 Adapter；意图中的 `model_budget` 保存配置上界对应的 Token、USD 预留和价格版本。Adapter 先计数输入，再生成一次响应，且遵守剩余墙钟和费用预算。单次预留采用全部输入的普通价格及最大输出，结算时按实测缓存输入折扣计价；推理 Token 不重复加到总输出。

- `BudgetUsage.tokens` 在 Mock 中是原 UTF-8 字节单位，在供应商中是已结算 Token（含明确标注的保守估计）。跨模型 Live 的累计历史标为 `mixed_units`，不能把它当作统一实测 Token；新分支预算只扣 baseline 之后的用量。
- `model_meter` 区分实测调用、估计调用、未生成调用及缺失的缓存/推理明细。缺 usage 时输入/输出实测值仍为 null；完整回复使用输入计数和最大输出进行保守估计后停止，不填入零 usage。
- `cost_micro_units` 为已结算的 USD 价格表估计。查询同时提供 `currency=USD`、`cost_basis=pricebook_estimate`、`remaining.cost_micro_usd`。Live 报告的相应 cost 字段也带同样标记，不冒充供应商账单。
- `reserved_cost_micro_usd` 是未释放预留；其中 `unpriced_cost_micro_usd` 是已有回复但费用不可核验的部分。模型/层级变化、未覆盖缓存写入或计量封装损坏会停止并保留费用预留，不当作零费用。
- 查询的 `pending_model_reservation` 来自未观察的耐久意图，覆盖 Adapter 正在调用、尚无回复的窗口；恢复复用的同一逻辑调用去重，与状态中已有预留取上界，避免重复扣除。

429 或确定未生成的连接失败由 Runtime 的 `max_replans`、`max_model_calls`、Token、费用和墙钟预算共同约束；每次尝试有独立 ledger 身份。`Retry-After` 保存在状态中，只有真正调用 Adapter 才等待；恢复后重新等待完整相对间隔是保守行为，仍受原墙钟期限约束。认证/参数/受控停止不自动重规划；生成可能已发送后的超时、断连或 408/5xx 为 `outcome_unknown`，保留预留，禁止重发。

未知结果只接受已有可信核对接口提供的同一请求、签名和原调用身份回执；结算使用保存的计量，不能重新请求模型冒充原结果。Recorded 纯重算计量与每个检查点，并核对意图中的预留；不构造供应商客户端、读取凭据、调用工具或等待网络退避。Live 使用新配置/预算/授权，来源上的未知负债保留在来源历史中，不转为新分支额度。

上述上限只控制本软件绑定价格表和受控调用路径的估计，不覆盖账户特定收费或供应商事后账单调整。真实冒烟按 S6-T06 单列，必须明确开启并确认凭据、账户和费用预算，确定性 HTTP 替身不证明供应商模型能完成研究任务。
