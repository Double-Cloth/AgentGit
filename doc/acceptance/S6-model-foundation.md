# S6 模型协议与 HTTPS 基础增量

日期：2026-10-23。实施依据与官方来源见 [模型实施清单](../S6-model-plan.md)。本文只验收可独立调用的 Adapter 基础层，不代表 S6-T06 供应商冒烟或 S6 整体完成。

## 已实现范围

- `core/model_usage.py`：固定 `gpt-5.4-mini-2026-03-17`、标准处理层级、USD 价格版本与有界选项；费用采用整数 nano-USD 合计后向上取整为 micro-USD，不使用浮点货币。缓存输入计折扣，推理 Token 已包含在总输出中，不重复收费。所有数额都是所绑定价格表的估计，不是供应商账单。
- `adapters/model_protocol.py`：闭合 Structured Outputs Schema，所有属性 required、未使用值显式 null；转换后再按既有 Plan/DAG/Tool/Final 契约验证。工具参数通过已有五工具 Schema，不能从模型直接执行宿主命令。指令、Schema、固定模型、选项、端点和处理参数均进入内容签名；计算签名不读取密钥、不访问网络。
- `adapters/model_http.py`：只允许 `api.openai.com:443` 的计数与生成两个路径，系统 TLS 信任与主机名验证；不读代理、不跟随重定向。请求/响应、累计期限和慢速头部/分块读取均有界。系统 DNS 通过最多一个后台解析线程等待，调用方按期限退出；不因系统解析器不可取消而不断创建线程。
- `adapters/openai.py`：单次 `invoke` 至多一次输入计数和一次生成，共享同一期限。计数包含相同的输入、指令和结构化 Schema；输入上限或单次费用上界不足时不生成。只在调用时从 Worker 环境读取 `OPENAI_API_KEY`，不保存供应商完整正文；请求含凭据或供应商转义回显凭据时拒绝，持久结果也不保存含凭据的请求身份头。
- `ModelReply` 同时保存决策、状态和 `ModelMeter`。已知拒绝、输出截断或 Schema 错误仍保存计量；缺失/非法 usage 标记 `conservative_bound`，输入/输出实测值为 null，并使用输入计数及配置输出上界进行保守结算。模型/处理层级变化或出现价格表未覆盖的缓存写入时，`price_verified=false`，不能冒充已验证费用。
- 确定未生成的连接失败/429 只返回重试建议及有界 `Retry-After`，Adapter 自身不重发；认证/参数/重定向等返回停止。生成可能已经发送后的超时、连接断开、408/5xx 进入 `outcome_unknown`，留给调用方保留预留，不回填零用量。

## 自动检查

新增 93 项测试，包含真实回环 TLS；它们使用明确公开的[测试证书与私钥](../../tests/fixtures/model_https.md)，仅修改测试内 DNS 地址和内存信任库，不安装系统证书、不接触供应商或真实凭据。覆盖成功 Plan/Tool、闭合字段、阶段/DAG/工具约束、整数价格、缓存/推理计数、缺 usage、错误价格层级、超时/限流/未知结果、凭据回显、大小/路由/证书/慢速响应和原 Mock 默认契约。

| 检查 | 结果 |
|---|---|
| 全部 Python 单元 | 311 passed，11.42 s；包含新增 93 项与原 218 项，不据此声称完整后端/容器套件已重跑 |
| PostgreSQL 兼容回归 | 60 passed，267.75 s；`test_research.py`、`test_recorded_replayer.py`、`test_live_replay.py`、`test_jobs.py`，使用独立测试 Schema；这是原 Mock 路径回归，不是新 Adapter 已接通数据库的证据 |
| Ruff check / format | 通过；175 个 Python 文件符合格式 |
| mypy strict | 88 个源文件通过 |
| Python sdist / wheel | `uv build` 通过 |
| 固定沙箱 | 原镜像及冻结源文件校验通过，未修改镜像锁或冻结内核 |

原始 JUnit：`.agentgit/evidence/S6-model-foundation-unit.xml`，SHA-256 `2d1ff2e97f56344761dd1bd3931e93ab35b2279162d526b9cbc4c2cc4bdac428`。FastAPI/Starlette 对测试客户端的上游弃用警告仍存在；没有为消除此警告擅自增加或更换依赖。`uv.lock` SHA-256 保持 `16ec1fe0d7dbb54f8cfa2e3547fdc0c19e44444722520e70015fd0f36f929bf5`。

PostgreSQL 原始 JUnit：`.agentgit/evidence/S6-model-foundation-legacy.xml`，SHA-256 `292f01e5185512fa4e2415a7fb7528d1e1e62ca70111280a2372683f7dd40bb1`。原 `AgentConfig`、`BudgetLimits`、`BudgetUsage` 默认 JSON 与实现前保存的基线逐值相等，原 Mock 签名保持不变。

## 接线与验收边界

当前 AgentConfig、Worker、Runtime、预算台账和 Recorded 仍走原 Mock 协议，未注册这个 Adapter；桌面和已构建候选包也没有真实模型选项。必须先完成运行时 provider/计量/预留、恢复/未知结算、纯 Recorded 重算和 Live 独立预算，再开放 API/CLI/桌面配置。特别是预算比较循环、未知调用接管重算和跨模型逻辑用量不能继续假设所有字段都是 Mock 字节整数。

下一增量应在每次调用前持久化 Token/费用上界，未知时保持；根据 `ModelReply.disposition` 区分停止和可重试错误，每次重试受 `max_replans`、`max_model_calls`、墙钟及费用预算约束并有独立调用身份。重放只消费已保存计量，不构造供应商客户端、不等待真实网络退避；未验证价格不能释放成零费用。接口缺失或未支持配置应明确拒绝，不回退 Mock。

真实供应商冒烟尚未运行：未明确开启、未验证账户/快照访问权限，也没有执行计费请求。旧桌面候选包的摘要与证据仍只对应其原构建版本；后续接线后需重新构建并复验。独立 Windows 用户安装/升级/卸载、完整 DPI/键盘/生命周期和原生保存对话框等 S6 剩余条件不变。
