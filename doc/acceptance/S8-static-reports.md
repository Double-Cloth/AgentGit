# S8 静态候选与不可变 Debug Report 增量

2026-10-24，落实实施方案 S8 的失败依赖候选、无参考静态定位及 Debug Report 后端。本文只描述已实现的后端工程范围；人工标注、完整评估、单变量干预与桌面证据链仍待补，S8、D2 未验收。S6/D1 的独立 Windows 及原生交叉验收继续按用户指示暂缓，main 不合并。

## 实现

- 创建请求只接受 failed_run_id 和可选 reference_run_id，不接受样例、gold、自报硬断言或根因分数。失败断言从所属终态快照和已提交终态事件交叉提取，区分研究报告验收错误与 Runtime 停止码；成功参考必须有通过的硬验收终态及匹配事件。
- 无成功参考仍沿失败依赖提取候选。计划/工具决策通过已验证的模型观测、接受事件和唯一工具请求识别，无法绑定时保留不确定类别及原因。工具输入/观测异常单列，最终仅触发验收的模型输出单列排除；接受或优先某个决策不证明它可修复。
- 排序依次为失败路径、报告角色、失败距离、首个分歧距离及不确定项数。缺产物先检查计划遗漏，其他报告错误先检查产物决策；Runtime 停止没有报告角色偏好。这是未校准的初始启发式，已同步实施方案 S8，不能据此宣称定位优势。相同特征保留竞争并列，不用到达时间或数据库 ID 打破并列；完整候选不截断。
- 没有核验失败断言或没有候选处于核验失败路径时，返回 insufficient_evidence 并保留完整回退列表。缺失或歧义降低证据完整性；无归属、跨项目、摘要损坏及终态/事件矛盾明确拒绝。静态报告总是 static_only、intervention_status=not_run、intervention_improvement=null。
- `POST /debug-reports` 原子发布不可变报告索引、CAS 归属和幂等响应；`GET /debug-reports/{id}` 读取历史结果，不按当前规则重定位。项目报告元数据列表支持有界 UUID 游标，不读取 CAS 或重新验收。CLI 提供 debug-report/debug-report-status，API 与 CLI 同键同参返回同一报告。
- 新增 `0016_static_debug_reports`、所属 Run 复合外键、报告内容归属守卫及不可变触发器。旧迁移及 S4/S5 执行核心不改，业务库不自动迁移；capabilities 仅新增 static_debug_reports 后端标识，0.1.4 桌面包尚无报告桥接或视图。

## 验证

| 证据组 | 通过数 | 耗时 | 范围 |
|---|---:|---:|---|
| S8-debug-reports-final.xml | 543 | 274.01s | 450 单元；报告、行为读取、历史、版本、管理 API/CLI、只读 API、完整迁移往返及既有实验入口 |
| S8-debug-reports-entrypoints.xml | 30 | 128.19s | 17 候选单元、13 报告集成；增加真实 Runtime 工具错误/模型空响应及列表 API、CLI 参考参数 |
| S8-debug-reports-demo.xml | 1 | 13.75s | 实际报告与包含原始 CAS 的离线证据包相互核验 |

三组均无失败或跳过；去重 **546 项**（450 单元、96 集成），不把重复专项相加。本轮只使用合成单元轨迹及开发集 D04/D06/D10 的工程断言，未进行完整 20 例或保留集排名评估。测试全部针对最终同一产品源码，期间冻结 Python 产品文件；补充轮只扩展测试断言和归档，不改产品实现。

验证了无参考、过期输入/报告决策分离、真实 Runtime 停止、并列与到达顺序重标不改变特征/排名、无断言及断开失败路径的完整回退、缺失正文、错误事件 ID、跨项目、损坏报告及缺归属拒绝、终态事件与快照矛盾、非法 gold、报告上限回滚、source 历史/分支不变、无模型/工具/Worker 调用、API/CLI 幂等与冲突、历史读取不重定位、列表不读 CAS、不可变/复合 FK、完整空库和局部迁移往返。

Ruff、231 文件格式检查、115 产品源文件严格 mypy、git diff --check 通过。保留既有 Starlette TestClient 弃用提示，未新增依赖、容器、付费模型或 Windows 构建/安装操作。测试迁移仅在独立 schema，业务数据库未修改。

## 规则冻结与实际样例

[S8-static-rules-v1.json](S8-static-rules-v1.json) 在保留集排序评估前冻结规则说明、规则域隔离摘要、7 个实现文件摘要、产品源码版本及已冻结 12/8 划分。尚需冻结评估脚本，人工标签仍待审阅；以后不能用同一保留集结果调参后重复宣称优势。规则文件记录的是冻结时状态，不随以后实验或审阅覆盖修改。

实际 D04 示例保存在忽略目录 `.agentgit/evidence/S8-debug-reports-demo/`：report.json 为真实创建/读取的报告，evidence.json 为已校验的原字节及所属索引。报告包含 source_expired:aster-overview、10 个完整静态候选、1 个分列的输入观测异常；产物生成和计划决策在候选中，改善率未知。该开发样例不是 Top-k 全量实测或独立人工 gold。冻结语料自身无效时可能不存在当前操作空间内的可行修复，后续不能通过修改输入或验收器把实验改成通过。

测试 schema 已清理，这些是可离线核对的历史样例，不是仍可用业务 API 查询的项目。源对象在捕获时核验，报告完整性字段不保证未来物理对象永远存在；读取历史报告也不重新执行原任务。

## 尚未完成

S8-T02/T03/T06 仅有本后端代表场景及证据边界的工程验证，桌面事件跳转、报告展示及完整退出仍待完成。S8-T04/T05 尚无 Top-1/Top-3、未定位数、定位步骤/开销与时间基线结果，也无独立单变量效果证据；人工标签不能由代理默认确认。后续继续全量评估、S7 受控干预和 Diff/Timeline 视图，必要时按实施方案保留显式全候选降级，不提前宣称已证明定位优势。

批量生成的小连接池/同键并发边界尚未实测，下一步先核对编译读取与修改事务持有连接的协调，再推进批量评估；当前通过的工程检查不替代该边界。

源码为 `source-sha256:d28818d9080c33b5fabd56f0b1a4ddd11b0a731bea70eedf0a4470af29f63dd2`，规则域隔离摘要为 `fd88d1c4ef9698750a7a294becca6975d288184d3a80c521763c307effd06683`。源码、规范及全部原始 XML/示例的逐文件摘要见忽略目录 `S8-debug-reports-summary.json`：

| 文件 | SHA256 |
|---|---|
| S8-static-rules-v1.json | `a962e390799733d5d772b3fa054436c2dd826b10a68bb2d5d7e1b1802fb3be8f` |
| S8-debug-reports-final.xml | `046aee008044891b43934f0a3e54de953477e317e368920eeeceba9c733c4fa4` |
| S8-debug-reports-entrypoints.xml | `14033aa8df42483ebf36725b91fe222e4cdb8d21142eaa9ecfd11e438dc54c39` |
| S8-debug-reports-demo.xml | `546228e45abefe659c5ddabd10646d7f8bae50ef8d014c5df346c512e216a1bf` |
| S8-debug-reports-demo/report.json | `bc2dd1a271f999e98708c905678b3aaad94b8b5f80bfbf97925bbaecac6a89da` |
| S8-debug-reports-demo/evidence.json | `bb34a791dd868f080db92123a4a04db4bea2106714c91697eecb13c3b28e1ec8` |
| S8-debug-reports-summary.json | `3c45e06fb95c888a571e6b18c418e4b8ff7053c88e95fdc2293f21d4129d902a` |

实施方案仅在 S8 排序条目补充上述静态启发式和冻结/并列要求，当前摘要 `a025d7d5895ade6dfad2799a27470a9330f1434185b11d4f8a50b4f68ada1fc2`。设计文档仍为 `eda6eaa91399be15c85ac55fd14dcf4db8f9ce931f620193976fb99b4fb1cfdc`，DESIGN 为 `af6166c1dd80a7965f1cc3b7cc1d6846ec43287a4891808cd1f89d16e13fb33b`，uv.lock 为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。
