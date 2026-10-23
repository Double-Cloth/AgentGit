# S6 真实后端桌面联调

状态：**开发目录中的真实 Electron 联调通过**，不是 S6 最终验收。仍缺安装/便携包、独立 Windows 用户、完整 DPI/键盘/生命周期、真实模型 Adapter 和原生保存对话框交互证据。

## 范围与设施

在 Windows x64 运行 Electron 45.0.0，加载本地构建的自定义协议页面；通过受限 preload/主进程连接真实 FastAPI，使用 PostgreSQL 17.6 的独立随机 `test_desktop_*` Schema、独立 Python Worker 进程及固定 Docker Linux 镜像。不使用浏览器伪造业务响应，不共享开发队列，不以 SQLite 或线程内假 Worker 代替。

命令：在 `desktop/` 运行 `npm run test:electron:real`。测试设施为 `tests/support/real_backend.py`；只在测试库创建/移除自己生成的 Schema，保留忽略目录内的 CAS 供核查，退出正常等待所启动的 Worker。此次结束后查询确认没有遗留 `test_desktop_*` Schema、API/Worker 或执行容器；数据库与 Docker 等外部设施继续运行。

Browser plugin / browser skill 在当前会话不可用；此外普通浏览器无法提供 Electron 自定义协议、sandbox preload、main IPC 和原生写入边界，因此使用已锁定 Playwright 的真实 Electron 支持验证。捕获实际页面、DOM 快照和 trace，未用 Vite 预览代替桌面。

固定镜像 `sha256:37837ca77fde6fa5ab6e183ea9484888a107eb79359d2788069c295478fa8967`，包清单 SHA256 `7e6fc440f1d27570607a7000cd0e46e0d50d88cee8484f2df13f3283064f833d`；联调前 `sandbox/prepare.py` 再次实测 verified=true。测试期间冻结 Python 产品源码，未改变已受理 Job 的 source_revision。

## 已通过的闭环

| 操作 | 实际断言 |
|---|---|
| 窗口创建项目、提交五工具运行 | 真实 Job 完成，15 次 Mock 模型 / 13 次工具调用，五个 provider 均存在；真实 Python 与 Shell 各执行一次 |
| 查看执行与历史产物 | 界面读到 `statistics written`、原始代码与 `claims_per_framework`；所选 Commit 的 7 个文件及统计预览可查 |
| 容器外核验答案 | 3 个框架、9 条 claims、6 个来源，各框架 3 条；校验代码在宿主测试设施，不在容器工作区 |
| Fork | 从执行前安全检查点创建独立分支，未启动额外执行 |
| Recorded | 全范围重算 28 个检查点，verified=true；逻辑终态 completed，实际模型/工具调用均为 0；独立 Worker 上的模型与执行客户端禁止入口触发数均为 0 |
| A/B Live | 从同一待执行 Shell 的检查点建立两个新分支，各 1 次模型与 1 次工具调用；同名 `results/desktop-same.txt` 分别为原字节 A / B，对象摘要不同 |
| Diff | 真实 `/diff` 返回 8 项差异；界面选择产物路径后展示 A/B 各自摘要，没有 AI 因果分数 |
| 受控导出 | 按 A 的产物 ID，经真实主进程下载/校验/保存写出字节 A；**测试替换了原生 SaveDialog 的返回值**，因此只证明写入与权限边界，不证明人工操作原生对话框 |
| 来源与清理 | 来源 Run/Commit/Event/Artifact/Call/Execution 索引指纹及 main 分支前后完全相同；4 个容器名称独立、SQL 清理确认均 true，并逐个真实 inspect 返回 404 |

源 Run：`f610cc2e-3c34-46ff-a802-efa6610c9f17`。Recorded 逻辑用量保留源 15/13 次调用，实际计量为零，不把逻辑用量当作重放费用；全程使用离线 Mock，没有真实供应商调用或凭据需求。

## 证据与问题修复

最终综合测试 **1 passed，43.1s**（总运行 43.4s）。忽略目录 `.agentgit/evidence/` 中：

| 文件 | SHA256 |
|---|---|
| S6-electron-real-dev.xml | `b4fc0cd98e1f192e39beb6cb8f297a142b6226aafa5d75ed366eeb3400a70dd3` |
| S6-real-desktop-dev-audit.json | `0fe92b20fb20e7035b0fd4410fbfe7521cc4861182883c728c0740790dc9cfb8` |

另有 S6-real-source.png、S6-real-diff.png 与 S6-real-electron-trace.zip，均已检查实际页面。历史失败截图保留为排查记录，不算通过证据。

联调发现并修复：

1. 桌面 `base/head` 字段应映射 HTTP 的 `left_commit/right_commit`；初版 fixture 接受了错误查询名，真实 API 正确返回 validation。已修正主进程映射、增加路由断言，并让 fixture 校验真实字段，未给后端增加宽松别名。
2. 切页取消 GET 会误取消并行连接探测，从而把取消解释为断连并丢失页面。新增针对性测试先复现失败，再把主进程连接探测与业务读取取消分开；配置切换/窗口关闭仍取消全部 GET，POST 不受影响。当前 **15 项桌面单元通过**。
3. 产物列表摘要字段使用真实 API 的 `object_id`，不是快照 ArtifactEntry 的 `object_ref`。
4. 后续打包程序联调发现：完成状态到达时，执行列表重新查询会短暂清空并重建详情组件，将用户已选 code 重置为 stdout。已只在同一 Run 内保留已确认的执行列表；切换 Run/项目仍清空旧范围并丢弃迟到回复。增加真实 Electron 的 running→completed 跨两轮轮询选择保持检查，综合测试通过（9.5s）。

测试设施中的 Windows multiprocessing 启动、产物索引作用域、Docker JSON 对象读取等准备问题也已修正，未放宽产品约束。联调现在使用独立 subprocess Worker 并等待明确就绪，异常会失败，不静默跳过。

最终 main/renderer/Electron 测试代码类型检查通过；原 Electron fixture/宽度/边界综合测试再跑 **1 passed**；Ruff check 与 168 文件格式检查通过。未因本次桌面改动重复宣称后端 615 项全量重跑，Python 产品源码保持此前已验证版本。
