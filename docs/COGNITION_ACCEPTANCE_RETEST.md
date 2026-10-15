# 修正后的真实认知复测

日期：2026-10-15；模块：M039。**验收未通过，工作流停在规划数量校验，尚未形成可评价的项目认知成果。**

用户明确批准本次完整固定包、实际 /beta 目标、指令/schema 和预算后执行一次，没有追加请求、恢复或重新试跑。此前 M036 结果保持原样，见 [首次验收](COGNITION_ACCEPTANCE_RESULTS.md)。

| 项目 | 实测/记录 |
| --- | --- |
| 样本 | KaleoOreo/python-personal-finance-tracker，commit 3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09 |
| Zest 执行版本 | 230061cc493d2dbd1064a3b3a6dd662f71fef8a5 |
| 资料 | 原 19 文件/33028 正文字节，同一冻结参考和门槛；独立冷启动、无共享认知 |
| 模型 | deepseek-flash，实际 https://api.deepseek.com/beta；返回名 deepseek-flash |
| 请求 | 1 次 project_plan，完整请求体 9294 字符；0 调查/报告/核对 |
| 状态 | failed / invalid_cognition_output；0 job、0 finding |
| 应用阶段耗时 | 10.787231 秒，不是计费时长或性能基准 |
| usage | 输入 3180、输出 1658、合计 4838 tokens；cache_read=128；费用未知 |
| 原评分 | 核心 0/3、事实 0/10、流程 0/6；可信率/误收率 N/A；三项未知未涉及 |

## 已定位原因

原生 strict 返回单个 ProjectPlanResponse，Pydantic 类型、必填字段和额外字段校验通过；finish_reason=tool_calls，输出未耗尽 4096 上限。完整原始响应及 usage 已安全保存。

后置 [validate_plan](../zest/cognition_contracts.py) 拒绝了数组数量：

- overview 第 2 条 source_ids 有 **7 项，上限 6**。这个上限藏在 strings() 默认值中，[Pydantic 字段描述](../zest/model_outputs.py)只说明至少一个来源。
- T4 的 source_ids 有 **10 项，上限 8**。这项上限在描述里，但原生 schema 没有数组数量约束；由本地诊断补充发现。

全部 ID 都属于本次批准且模型实际可见的目录；其他 validate_plan 校验在仅收集数量错误的离线诊断中通过。原响应没有改写或截断，也没有被当成有效规划执行。

DeepSeek strict 当前明确不支持数组 minItems/maxItems，描述文字不能替代本地可执行约束。[官方契约](https://api-docs.deepseek.com/guides/tool_calls/) 当前实现需要重新对齐“真实业务边界、字段描述、类型校验、错误定位”，不能将一次原生 schema 解析成功等同于业务计划有效。

本轮账本 completed 仅表示解析 JSON 已保存；plan 未接受，后续 Agent 未启动。初步概览不补入已核对档案，不用零输出推导误收率为零。本轮没有验证真实调查/报告/核对、M038 上下文收束或超限恢复，更不能评价最终业务理解能力。

## 证据与下一讨论点

授权、固定预览、原始响应/完整请求、进度、档案、逐项 score.json 和 contract_diagnostic.json 保存在忽略的私有目录 `work/cognition-acceptance/finance-20261014/review/retry-m038-20261015/`。原参考、12 文件准备冻结、26 文件上轮结果清单与本次 5 文件准备冻结均保持原样。没有包外调查、项目执行/修改、未知请求重发、字符扩展、推送或部署。

建议下一小模块先讨论 **source_ids 的约束边界**：这些值只是批准目录中的起始线索，子 Agent 后续本就能在包内查找。可考虑取消额外 6/8 上限，改为批准的可见目录内、有效且唯一的 ID，受目录规模和总请求预算约束；保留任务数、工具/模型预算和包外拒绝。同时统一描述与校验，并给出字段路径、实际数量与允许范围。

以上是待共同决定的提案，不是已实施修复。本次未改变产品代码、输出契约或冻结评价规则，没有自动补正规划；可先用保存响应做离线验证，任何新真实运行仍需新的具体批准。
