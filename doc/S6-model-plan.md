# S6 单供应商模型适配实施清单

状态：协议、计量与固定 HTTPS 基础层已实现；配置、Worker、运行时预算、恢复、Recorded 与 Live 接线已通过新增 14 项 PostgreSQL 验证和全部 322 项单元回归，分批回归去重 701 项通过，见[基础记录](acceptance/S6-model-foundation.md)及[运行时记录](acceptance/S6-model-runtime.md)。桌面选择/呈现和真实供应商冒烟尚未完成。继续使用 `codex/s6-desktop`，不改变 S6-T01–T12 或后续 S7–S12 的退出条件。

## 已选范围与资料

首个供应商为 OpenAI 官方 Responses API，固定 `gpt-5.4-mini-2026-03-17` 快照；不把第三方兼容 URL 当作同一能力，不使用浮动 latest 别名。账户可用性未知，真实调用必须另外明确开启并提供货币预算，离线 Mock 和确定性验收不依赖密钥。当前没有已连接的 OpenAI 文档/密钥工具，已读取官方网页；没有安装或索取额外凭据工具。

2026-10-23 核验的官方资料：

- [模型与定价](https://developers.openai.com/api/docs/models/gpt-5.4-mini)：Responses / Structured Outputs、固定快照；标准文本输入/缓存输入/输出每百万 Token 分别为 USD 0.75/0.075/4.50。绑定该价格版本及标准端点/处理层级，计价是价格表估计，不冒充供应商账单。
- [结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)：闭合对象 Schema；所有字段 required，可空字段显式 null；根不是 anyOf。协议外包装转换为既有 Plan/Tool/Final 决策，并再次本地校验，模型不能自行运行工具。
- [Token 计数](https://developers.openai.com/api/docs/guides/token-counting)：先计实际输入，再限制总输出 Token（含非可见 Token）；不把 UTF-8 字节数当作供应商 Token。
- [调用级费用控制](https://developers.openai.com/cookbook/articles/per_run_spending_controller_responses_api)：调用前预留，响应后结算；超时、缺 usage 等保守处理。独立确认价格/账户/计费结果，不承诺绝对账单硬上限。

## 实现约束

1. 保留 `mock-research/v1` 的默认序列化和签名；新增可选字段为 None 时不写入原有 JSON，避免静默改写离线协议。冻结镜像复制的 paths/errors/canonical/protocol/supervisor 不修改。
2. 配置绑定模型、协议/结构化 Schema、价格版本、上下文/输出/超时上限；密钥仅 Worker 运行时读取，不进入 AgentConfig、Snapshot、Event、Blob、日志、IPC 或安装包。真实模型要求显式 USD 预算上限。
3. 使用标准库固定 HTTPS 主机和路径，验证 TLS、拒绝重定向、限制请求/响应和累计耗时；不新增 SDK 的隐藏重试。限流等确定失败沿已有 max_replans/max_model_calls 进行有界、逐次可审计调用；认证/参数错误不重试，发送后未知不自动重发。
4. 把决策与供应商计量/状态/请求身份保存为有版本的结果封装。已知拒绝/截断/Schema 错误仍先结算；缺 usage 明示 unknown 并保守估计；估计费用与真实 Token 分开。未知结果保留预留，不填零。
5. Worker 按冻结配置选择模型；Runtime 的 provider、签名、预算、结算、恢复和 Recorded 纯重算一并接通。Recorded 只解析已保存计量，不能构造网络客户端；Live 使用独立权限/预算。
6. CLI/API/桌面可显式选择真实模型并看到费用上限/计量边界；默认仍为离线 Mock。发布前重建候选包，不把当前仅 Mock 的已构建包当作包含新 Adapter。

## 检查顺序

先做无网络契约、整数计价、默认 Mock 序列化基线、参数/回复/错误边界；再通过本地 HTTP/TLS 替身验证计数与生成请求、超时/有界重试、缺 usage、敏感值不持久化。接着接通真实 PostgreSQL Job/Worker 的配置选择、恢复、Recorded/Live 与预算回归，最后在真实 Electron 中验证可操作字段与状态。

真实供应商冒烟按 S6-T06 单列，只有明确启用、凭据/账户和费用预算齐备时才发外网调用；没有条件时记录未运行原因，不把离线检查标为跳过或成功冒烟。
