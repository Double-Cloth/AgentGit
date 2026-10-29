# MCP stdio 可信程序边界

接续 `4a5825a` 的 HTTP 任务增量，补齐 S12-T07 对 stdio 启动配置的实际核对。根规范未变，正式前置及最终 D3 保持；先固定范围再编码，不把拒绝所有 stdio 视为可信启动验证。

固定协议仍为 `2026-07-28`，按本日核对的[官方 stdio 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)使用 UTF-8 单行 JSON-RPC 和逐请求元数据；先 `server/discover` 验证现代协议，无 initialize/旧版回退。STDIN EOF 发起关闭，超时后终止自有进程；终止不等于远端副作用回滚，不自动重启或重发未知写入。

首批只登记操作者审阅的独立、单进程 Python Server，不开放任意 executable/args/env/cwd 或 Shell 命令。登记内容固定绝对解释器及脚本路径、两者 SHA256 和审批操作者；程序引用是完整登记内容的域摘要。后端通过专用 `AGENTGIT_MCP_STDIO_REGISTRY` 指定有限本地白名单，绑定和不可变 Job 只携带程序引用，不携带命令。执行前再次核对登记摘要、实际路径/字节和运行期限，拒绝未登记/变更/伪造/带额外字段的启动配置。可信本地操作者和已审阅 Server 位于信任边界内，该机制不承诺隔离恶意登记者或完整 Python 安装被篡改。

解释器必须是当前 Worker 的固定基础 Python，使用固定 `-I -S -B -X utf8` 运行已批准脚本，无 Shell、无用户追加参数；每次请求使用新建临时工作目录和最小环境，排除后端身份、模型/数据库凭据及 Python 注入变量。此批 stdio 不提供凭据注入或依赖安装；Server 为标准库单文件，资源/提示词、通知交互和通用可执行程序仍不支持。请求/响应、总期限和进程清理均有界；stderr 不作为成功或失败证据，也不反射进历史。Windows 用自有 Job Object 控制生命周期并限制进程数，POSIX 使用自有进程组；不提供模型可调用的宿主执行回退。

沿用现有 Schema 缓存、MCPAdapter、精确本地效果/审批、Run/Worker、CAS/事件与 Recorded。HTTP 编码与历史绑定不改；stdio 的程序引用进入工具签名。工具调用前重新发现固定 Schema；任何可疑写入结果保持未知。Recorded 不读取或解析启动白名单、不创建进程或连接，缺录制仍明确失败。

在创建程序前和每次实际写入协议帧前核对同一租约，包括同一进程的 server/discover 与 tools/call 之间。已产生意图后失租时关闭自有进程，不能发送工具写请求；新 Worker 仍把未决意图保留为未知，不借“尚未观察到效果”自动重发。

独立 fixture 应记录实际 PID/协议方法和 fsync 后的模拟效果，覆盖可信成功、篡改/未批准程序拒绝、现代协议拒绝、总期限/断连/超限、环境过滤、实际进程清理、Schema 变化、外部写审批及完整 Recorded 零新启动；同时保留 HTTP 与核心兼容回归。演示、A 的测试映射和独立原始审计齐备后才进入 B。此计划不改变 S4 终端/Python 必须在容器内执行的边界，stdio 仅为后端固定、受信任 Server 启动。
