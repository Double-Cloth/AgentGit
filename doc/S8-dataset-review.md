# S8 故障标签人工审阅清单

本清单来自 20 个故障规格的代理提案，当前全部为 **待人工审阅**。冻结样例与 12/8 划分不代表人工确认标签。当前未实现定位排序，也没有 Top-k 实测或定位优势结论。

每例请核对三项：实际注入发生在哪里、哪个事件首次可以看出异常、哪些决策值得尝试修复。可提出多个合理决策，也可注明无可用决策或证据不足；不要求把最早的异常输入标成可修复决策。`model:plan` 为规划响应，`model:<node>` 为选择该节点的模型响应，`tool:<node>` 为工具观测，`corpus:<id>` 为冻结输入。真实引用由集成测试解析为 Run/seq/整数事件 ID。

D01–D12 是开发集，D13–D20 是保留集。请按故障语义审阅，不依据任何排序器输出反向改标签；保留集在规则冻结后才评估。修正意见另存审阅记录并绑定规格摘要，不覆盖冻结规格；只有真实用户反馈才可变更人工审阅状态。

| ID / 划分 | 故障 | 注入位置 | 首个可见异常（提案） | 可修复决策（提案） |
|---|---|---|---|---|
| D01 / train | 搜索返回空字节 | `tool:search` | `tool:search` | `model:search` |
| D02 / train | 搜索结果包含不存在的来源 | `tool:search` | `tool:search` | `model:search` |
| D03 / train | 搜索来源 ID 重复 | `tool:search` | `tool:search` | `model:search` |
| D04 / train | 过期资料未经验证写入报告 | `corpus:aster-overview` | `tool:read-0` | `model:write-report` |
| D05 / train | 来源响应封装升级为未知版本 | `tool:read-0` | `tool:read-0` | `model:read-0` |
| D06 / train | 来源读取的确定性超时替身 | `tool:read-0` | `tool:read-0` | `model:read-0` |
| D07 / train | 计划遗漏结构化来源产物 | `model:plan` | `model:plan` | `model:plan` |
| D08 / train | 工具观测中的事实与冻结语料冲突 | `tool:read-0` | `tool:read-0` | `model:write-report` |
| D09 / train | 模型计划返回截断 JSON | `model:plan` | `model:plan` | `model:plan` |
| D10 / train | 模型规划返回空字节 | `model:plan` | `model:plan` | `model:plan` |
| D11 / train | 报告文本与可核验结构化结论不一致 | `model:write-report` | `model:write-report` | `model:write-report` |
| D12 / train | 结构化结论的值没有来源支持 | `model:write-sources` | `model:write-sources` | `model:write-sources` |
| D13 / heldout | 检索仅覆盖两个框架 | `tool:search` | `tool:search` | `model:search` |
| D14 / heldout | 来源响应的身份与请求不一致 | `tool:read-1` | `tool:read-1` | `model:read-1` |
| D15 / heldout | 未来采集的来源被写入当前报告 | `corpus:cedar-overview` | `tool:read-4` | `model:write-report` |
| D16 / heldout | 后段来源读取的确定性超时替身 | `tool:read-5` | `tool:read-5` | `model:read-5` |
| D17 / heldout | 计划依赖包含循环 | `model:plan` | `model:plan` | `model:plan` |
| D18 / heldout | 尚未执行计划即宣告结束 | `model:search` | `model:search` | `model:search` |
| D19 / heldout | 结构化来源产物使用未知版本 | `model:write-sources` | `model:write-sources` | `model:write-sources` |
| D20 / heldout | 报告引用并不存在的原文 | `model:write-sources` | `model:write-sources` | `model:write-sources` |

审阅反馈可以直接写为：`D04：首个可见异常维持 tool:read-0；可修复决策补充 model:plan；理由……`。未反馈的样例保持待审阅；不因经过一段时间或默认选项而视为同意。

本表只收集定位标签。是否能修复、改善大小和复现比例，仍须通过预算内 S7 独立单变量实验验证；修复效果不由人工标签替代。
