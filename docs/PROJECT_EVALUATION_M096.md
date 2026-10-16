# M096：独立评分执行，因额外字段停止

日期：2026-10-16。[M095冻结包](PROJECT_EVALUATION_M095.md)获得明确批准后执行，包含第1–5、8、9批精确字符扩展。执行代码为干净的`c30d89a8f55b3bab3e1678c04c4262993a518fcf`，评分记录`ee75dac2-628e-460c-8512-e2c9aa8ee259`。

**本轮评分未完成，不能给出项目实用性通过或不通过的质量结论。** 计划最多9次、0补试，实际2次后按“无效即停止”规则结束；未继续其余7批，也未修改原答案、档案或评分结果。

## 实际取得的结果

评分由内置版本化Skill、`CognitionEvaluationResponse`/`with_structured_output`与生产EvaluationStore任务图执行，不由主协作Agent代打分。

| 范围 | 正式结果 |
| --- | --- |
| D1 档案模块与组装 | 3个主干属性均full |
| D2 档案核心流程 | 已收到响应，但结构无效，未计分 |
| D3、D4及Q1–Q4 | 尚未评分 |

正式接收3个属性，剩余21个未评分；**不能称“质量只有3/24”，也不能当作其余21项缺失或错误。** 程序报告`partial`、`complete=false`、门槛布尔值false，原因是执行不完整。没有完成37条候选全量语义审查，不报告误收率或模型总体准确率。

## 停止原因及离线诊断

第2批返回了合法JSON及具名工具调用，但三个coverage项分别多出`reason_extra: ""`。当前评分结构继承`extra="forbid"`，LangChain/Pydantic因此拒收，账本保存为`invalid`，错误`evaluation_response_invalid`。并非超时、结果未知、输出截断或业务源码错误；两批finish_reason均为`tool_calls`。

只对保存响应的内存副本做诊断，移除这三个空额外字段后，原有Pydantic和`validate_evaluation`身份/引用/覆盖集合校验均通过。**该副本未写回账本、未计分、未触发新调用**；结构有效本身也不证明理由语义正确。原无效收据完整保留。

无效响应中的模型意见将注册登录、发文、关注三个流程均判断为partial，理由集中于档案未明确展开密码校验/令牌创建、作者与标签处理，以及保存与返回主干。这些仅为未接收的模型意见和后续检查线索，不转换为正式分数，也不由本报告追加人工分数。四个真实问答还没有获得本轮独立评价。

空额外字段不影响这些既有评分字段的含义，却阻断了整个评分流程。这与用户希望优先关注主干实用性的目标不符；下一小模块宜限定为评分输出对不参与判定的额外字段的接收兼容，保留原始收据，必需字段、值域、身份、引用和预算校验继续有效。该调整本轮尚未实施，未修改冻结协议或现场重跑。

## 调用与保全

- deepseek-flash，`https://api.deepseek.com/beta/chat/completions`，目标与两份实际HTTP正文均匹配冻结包；每批1次，0补试。
- 2次物理请求：1批completed、1批invalid，0未知/pending请求，7批未发出。剩余额度不自动转成补试、恢复或新包授权。
- 输入25334、输出1453、总26787 token，cache_read9088。两次输出均未到8192上限。
- 25份冻结文件、Skill/Schema、原M094/M091各46份、M092离线3份、M089的40份、163/21/17/533更早材料及45原生文件哈希保持；原8个问答任务与认知记录不变。
- 保存完整批准、尝试标记、原始响应/供应商收据、账本与报告、离线诊断及用量；评分和checkpoint两个SQLite一致备份均`integrity_check=ok`。忽略目录中最终清单记录文件哈希，不提交原始材料。

材料位于`work/project-acceptance/realworld-evaluation-m095-20261016/review/`：`result.json`与`report.json`为正式状态，`call-*-receipt.json`为原始收据，`failure_diagnosis.json`明确标记诊断意见不计分，`metrics.json`、`result_verification.json`与`final_manifest.json`记录验证。

本次只有真实验收记录和文档，无生产代码变化，不重复此前142项或完整回归。完成实际结构/身份诊断、调用/用量核对、冻结及全部历史保护、SQLite检查、文档链接与diff检查；无LangSmith、样本执行、补查、知识发布、Web/API、推送、部署或新会话。
