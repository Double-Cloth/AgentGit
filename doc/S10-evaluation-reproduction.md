# S10 冻结评估复现与原始复核

本说明针对运行 release `0f981aba918904366022c05373484be572cf36b96a5042d05304e6200a3524de`。完整三十例结果尚未齐备；以下是执行和复核步骤，不是预填成绩。所有结果仅适用于固定离线 Mock、合成语料、有限模板和预算，不代表真实供应商模型泛化。S8 定位未达标，主方法使用完整候选降级。

## 环境和不可变输入

环境准备遵循 [DEVELOPMENT.md](DEVELOPMENT.md)，使用 Python 3.12、`uv run --locked` 和已准备的独立 PostgreSQL 测试库。本次实际 Python 补丁版本、依赖摘要、产品源码身份和逐文件摘要见 `tests/fixtures/repair/evaluation-release-v1.json`。不迁移业务库、不运行供应商调用、不需要外部模型密钥。测试会迁移自己新建的 `test_<uuid>` schema，退出后只清理该 schema；各例 CAS 位于独立 pytest 临时目录。

复现实验应使用独立工作目录及隔离测试库，保持完整冻结版本的字节，包括设计/实施规范、所有产品 `.py/.json`、迁移、依赖和辅助代码。运行入口自动比较完整 release；不同字节会拒绝。原始试验首次启动前的 189 文件快照位于忽略目录 `.agentgit/evidence/S10-evaluation-v1/frozen/`，清单为 `snapshot.json`。冻结以后新增的分析代码不进入模型、搜索或候选选择路径。

输出固定为 `.agentgit/evidence/S10-evaluation-v1/cases/Rxx/`，逐例清单及原始容器使用只创建一次模式。不要在已留有这些结果的原目录重复执行，也不要删除旧结果后冒充首次执行。新复现使用另一个独立工作目录，保留旧数据和条件；不通过修改固定输出常量或覆盖 release 来复用当前目录。

## 执行顺序

在根目录启动开发集入口。测试断言归档和执行完整性，不要求每个算法结果成功，`rejected`、`needs_review`、无解和预算停止均按真实状态保存。

```powershell
uv run --locked python -u -m pytest tests/integration/test_repair_frozen_evaluation.py -q --capture=tee-sys -p no:cacheprovider -k 'R01 or R02 or R03 or R04 or R05 or R06 or R07 or R08 or R09 or R10 or R11 or R12 or R13 or R14 or R15 or R16 or R17 or R18 or R19 or R20' --junitxml=.agentgit/evidence/S10-evaluation-v1/reports/train.xml
```

本次采用最多四个独立外层批次，按训练 ID 模四分组；每个方法内部并发仍为一，预算及方法顺序不变。串行复现仍使用同一预算，但墙钟不可直接当作相同机器负载下的性能比较。所有开发测试进程终止并经原始审计后，执行完整性门禁，再启动十例保留集。保留入口自己再次检查所有二十例、五方法及无活动任务，不以开发指标是否达标作为删例条件。

```powershell
uv run --locked python tests/integration/repair_benchmark_analysis.py --split train --require-complete --audit-all --destination .agentgit/evidence/S10-reanalysis-train
uv run --locked python -u -m pytest tests/integration/test_repair_frozen_evaluation.py -q --capture=tee-sys -p no:cacheprovider -k 'R21 or R22 or R23 or R24 or R25 or R26 or R27 or R28 or R29 or R30' --junitxml=.agentgit/evidence/S10-evaluation-v1/reports/heldout.xml
uv run --locked python tests/integration/repair_benchmark_analysis.py --split all --require-complete --audit-all --destination .agentgit/evidence/S10-reanalysis-all
```

如果任何条件需要修复，停止该版本的后续评估并保留失败；另建版本和未见保留资产，不能看到保留成绩后调整现有模型、预算、策略或标签再覆盖旧成绩。正在运行的试验不得修改被冻结的执行文件。

## 不启动模型的原始审计

上述分析入口读取逐例 `case.json`、`objects.bin` 和可信冻结模型源码，不领取 Job、不调用供应商、不执行 Agent 工具。`--audit-all` 逐个复算原始对象域 hash、响应与固定模型的一致性、来源/确认/回归硬验收、实际计量及无正式采纳；已有审计收据须与重算值相等。未传该选项时，仅在 release 和案例清单摘要匹配时复用审计；容器整体摘要仍会重算。

`verified/Rxx.json` 保存独立核对收据，`analysis/*-complete.json` 或 `*-partial-NNN.json` 保存任务级结果及原始引用摘要。新汇总始终只创建一次；重复复核应换新的 `--destination`，而不是覆盖旧分析。缺案例时保留明确缺例清单，`--require-complete` 会拒绝完整报告。缺失或损坏的原始容器必须报错，不能靠历史汇总补成通过。

统计以唯一任务/方法为单位，确认与回归 trial 另计。任务分母保留失败、未知和未确认；观测替换即使使 fixture 通过也不计可采纳修复。开发十二例目标分母固定，保留成功率分母固定十例；Wilson 区间是任务级描述，不将六次确定性确认当作六个独立研究任务。来源前缀逻辑用量与本次实际模型/工具/Mock UTF-8 字节分开，Hybrid 虚拟观测不计成实际工具调用。

原始大文件、数据库、CAS 与运行输出不进入 Git。可提交内容只含执行/分析代码、固定公开资产和验收说明；正式报告应列出本机原始证据位置及摘要，不能仅提供没有原始引用的成功率。
