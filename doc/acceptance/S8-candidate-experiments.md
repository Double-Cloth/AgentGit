# S8 候选到受控干预的工程验收

2026-10-24，在 `codex/s8-diagnosis` 上完成候选与 S7 单变量实验的只读关联。产品源码绑定 `source-sha256:7f76f71d0a4f84551e4b88f14540a0e29de2c844f5b93cccb9b7dda7f2ac3c9f`，uv.lock 为 `ac3497d12e587dc54bb44ae2b6aa40f36a8212ff8855314c96ac9a425303da0e`。

本增量没有更改静态排序、20 例、12/8 划分、人工标签或原评估结果。与 `S8-static-rules-v1-storage-2.json` 绑定的七个实现文件逐字节一致。用户已明确人工清单稍后审阅、先继续工程实现；20 例仍全部 `pending_human`，保留集主要注入点 Top-3 仍为 4/8，S8-T05 未达到 6/8。Windows 独立环境及原生验收继续按用户指示暂缓。

## 实现及边界

- `diagnosis/interventions.py` 沿冻结图内唯一 `call_occurrence` 边，从候选模型结果绑定同一次 `llm.requested`，核对完整 occurrence、精确事件和缺口。不能按邻近 seq、时间戳、排名或用户自报结果选择调用。缺边、多请求、未核验、缺观测、缺开始事件 ID 均拒绝。
- `services/debug_experiments.py` 核对报告归属、当前完整录制的观测摘要/错误、调用身份和前置条件，自动选择该模型决策前检查点。复用 S7 原有安全、权限、已完成节点和冻结输入检查；可准备 `PROMPT_PATCH`、`PLAN_PATCH`，不自动创建干预、Job 或实验。
- 实验关联要求同一失败 Run、同一候选检查点，A 无修改，B 恰好一个指向该候选调用的类型化干预。不同来源、直接分组补丁、同时修改 A 或叠加干预不能冒称该候选的单变量效果。工具观测替换尚未通过此模型候选入口开放。
- 返回独立 `debug_experiment_view/v1`，含原报告引用、候选及不确定项、请求事件、干预内容、冻结条件、全部 trial、统计和混杂限制。它是查询时刻的视图，不是对原不可变报告的更新。原报告仍为 `static_only / not_run / improvement=None`。
- 只有两组所有 trial 都已得到通过/失败判定时，才输出精确分数 `B 成功率 - A 成功率`。排队、取消、预算耗尽及其他未完成结果保留 `incomplete / difference=None`，并列出原状态和实际消耗。分数没有参与已冻结的静态排名，也不宣称严格因果。
- S7 验收服务增加默认开启的 `persist` 参数，原有入口行为不变；新关联查询显式关闭保存，完成后的验收也不写 CAS 或归属索引，不执行模型、工具或 Worker。仍核对冻结代码/依赖版本，不能用新版代码静默重判旧验收器；旧静态报告本身仍可读取。

没有新依赖、新表、迁移、业务库更新、供应商调用或安装操作。新后端尚未进入 Windows 0.1.4 分发包。

## 入口

两个 GET 均要求现有 API 身份和 `X-Repository-ID`；`candidate_event_id` 是数据库正整数 `events.id`，不是 seq 或 UUID。

```text
GET /debug-reports/{report_id}/candidate-context
    ?candidate_event_id=<ID>&validator_ref=<64 位摘要>
GET /debug-reports/{report_id}/candidate-experiment
    ?candidate_event_id=<ID>&experiment_id=<UUID>

agentgit debug-candidate-context <repository_id> <report_id>
    --candidate-event-id <ID> --validator-ref <摘要>
agentgit debug-candidate-experiment <repository_id> <report_id>
    --candidate-event-id <ID> --experiment-id <UUID>
```

准备上下文后，继续用既有 `/interventions` 和 `/experiments` 显式提交内容与预算；查询和重开视图不会提交动作。

## 真实独立 A/B 结果

示例不属于冻结 20 例，也不进入 Top-k 分母。使用标准 Mock、固定语料与三个本地工具：先从正常 Runtime 来源的安全检查点，借助 S7 类型化 `PLAN_PATCH` 省略尚未完成的 `write-sources`，由真实 Job/Worker 生成 `missing_artifacts` 失败 Run。之后在其候选决策前创建新实验，A 保持失败条件，B 只补回计划节点，固定同一验收器、输入、权限、预算和 seed；每组独立运行五次。

| 分组 | 通过/失败/未完成 | 实际模型调用 | 实际工具调用 | Mock UTF-8 字节 | 实际 wall_ms 合计 |
|---|---|---|---|---|---|
| A：失败计划 | 0 / 5 / 0 | 10 | 5 | 129955 | 4678 |
| B：补回来源文件 | 5 / 0 / 0 | 15 | 10 | 235300 | 7738 |

成功率差为 `1/1`，10 个 Job/Run 各自独立，冻结验收器引用一致，未知结果为 0，来源分支、文件和静态报告未改变。该表只包含修复实验本身；正常来源、故障构造和测试基础设施消耗不混入定位查询开销。全部为本地免费 Mock，供应商调用/Token 为 0，CPU/IO 未估价；wall_ms 是本机这次运行记录，不作时延优势结论。

当前图只展开失败 Run 自身事件。已完成读取来自父 Run，因此工具请求带范围缺口，候选按冻结规则仍为 `unaccepted_model_output`，保留“接受事件缺少唯一的已核验工具请求”的不确定项。这里表示接受关系不能完整核验，不表示确定拒绝了该输出。同 occurrence 的模型请求/观测仍可完整核对，足以准备合法 S7 决策前干预；实验修复成功不补造父级依赖边，也不证明该模型输出是原始注入点。

另外实跑两条 `max_model_calls=1` trial，均预算耗尽，结果维持未完成、改善未知。跨项目、非候选事件、多变量设置、错误来源和被替换的录制观测均被拒绝。

## 验证与证据

最终源码在所有真实 Run/Job 执行期间保持不变。专项 21 项通过，回归 547 项通过；其中 16 个单元项重复，去重共 **552 项：466 单元、86 集成**。回归覆盖冻结验收器、类型化干预、A/B Runner/统计、API/CLI、实验发现、静态报告与并发发布。不是重新运行此前所有阶段的完整套件，也未重跑保留集排名。

```powershell
uv run --locked pytest tests/unit/test_debug_candidate_binding.py tests/integration/test_debug_candidate_experiments.py -q -x --junitxml=.agentgit/evidence/S8-candidate-experiments-final.xml
uv run --locked pytest tests/unit tests/integration/test_validator_service.py tests/integration/test_intervention_service.py tests/integration/test_experiment_runner.py tests/integration/test_experiment_statistics.py tests/integration/test_experiment_entrypoints.py tests/integration/test_experiment_listing.py tests/integration/test_debug_reports.py tests/integration/test_debug_report_concurrency.py tests/integration/test_management_api.py tests/integration/test_management_cli.py -q --junitxml=.agentgit/evidence/S8-candidate-regression.xml
```

Ruff 检查、218 个 Python 文件格式检查、117 个源码文件严格 mypy、`git diff --check` 均通过。既有 Starlette TestClient 弃用提示保留。

首轮专项保留 15 通过/1 fixture 断言错误：测试曾预期完整接受分类，但父 Run 范围缺口使冻结规则正确保留不确定性；修正测试预期，没有修改排名或抹去缺口。次轮 3 项通过后，又补齐已完成查询无写入、当前录制摘要和预算边界，最终 21 项通过；旧结果不覆盖。

原始证据位于忽略目录 `.agentgit/evidence/S8-candidate-experiments-20261024/`，由最终测试生成的临时归档原字节复制，先核对当前源码和每个对象摘要。包含 350 个对象、869791 原始字节，以及调用、事件、快照、干预、实验、trial、Job 和报告索引；测试 schema 已按 fixture 清理，不能把归档中的 UUID 当作业务库可查询记录。

| 文件 | SHA256 |
|---|---|
| `effect.json` | `b52b2232346e1958bb860b7b316dda45a30661afe6cad6043fa1ac11e1baf99b` |
| `static-report.json` | `9c3bdfc176d8bf07b3cc6817d2ea050e017748340a1c4fdd10e68270639e1b8c` |
| `evidence.json` | `42340c0fdecbd336968d767367b0f32990f96fbd45044ac6b9eeed18a7dee6bb` |
| `S8-candidate-experiments-summary.json` | `6e883af5d135e454d6cd57daeca5b0550ea196940080e1b4c42463f4c432024d` |
| `S8-candidate-experiments-final.xml` | `3b6a87ad684e27fb93fc5e7b3664c207afbc9399a590f7ace79ce57508b5f0c5` |
| `S8-candidate-regression.xml` | `8e9a1c282c02b218001114b6fecf06c57863c5ec83199d2e5e406ff1605a2b36` |

后续继续桌面 Debug Report、证据跳转和对齐歧义展示，并补充适用故障/预算的效果证据。该示例不替代 20 例的标签审阅、正式 S8 验收或 D2 定位优势证明；S9 如先推进，必须显式采用完整候选降级。完整目标还包含后续 S9–S11 与 S12-A/B/C/D。

后续桌面已进入 0.1.6；当前[二十例效果补充](S8-candidate-effects.md)另建完整持久来源，覆盖每例十个独立 A/B Job，四个正差、十六个零差均保留，完成原始同条件响应与硬验收重算。本文原示例及其旧摘要不覆盖、不累计到新批次。其余适用效果工程证据已补齐，人工标签和原定位未达标仍保留。
