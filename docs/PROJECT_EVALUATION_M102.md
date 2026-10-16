# M102：完整真实评分尝试，首批输出漏项后停止

日期：2026-10-17（Asia/Shanghai）。用户明确批准[M101完整冻结包](PROJECT_EVALUATION_M101.md)及第1–5、8、9批精确字符扩展；执行干净`fe772e87e0db28bc50618f6e970b10cdc9ee0b97`，评分实现为M100/`424af06`。独立评分`9bc328a2-8d5f-45a7-b33c-86b1a2372b2a`。

**本轮仍未完成独立评分。** 第1次真实调用返回有效JSON和新的候选观察，但只交付首批3个目标中的1个，并遗漏必填顶层字段。按冻结的“无效即停止、0补试”规则，实际调用1次，后8批未发，没有正式接收的新成绩。

## 实际返回及停止原因

首批要求D1/assembly、D1/business、D1/shared三个覆盖目标，固定Schema要求coverage与judgments两个顶层字段。实际响应为：

- coverage只含D1/assembly，含候选身份、candidate_observation、原文身份、full和理由。
- D1/business、D1/shared均未返回。
- judgments字段缺失；本批无语义发现，正确交付应包含空数组。
- 该项另有空finding_ids_note。M097兼容仍会隔离这一非判定字段，不是此次拒收原因。

Pydantic拒收点为缺少judgments，账本invalid，错误evaluation_response_invalid。只在离线内存副本加空数组以隔离诊断后，原业务校验仍因coverage只有1项、要求3项而拒绝。副本不写回账本、不计分，没有补生成遗漏目标、修原响应或追加调用。

实际HTTP工具Schema仍具名CognitionEvaluationResponse、strict=true、required含coverage/judgments；实际回复未完整遵守该结构。finish_reason=tool_calls，输出629 token，远低于8192上限，未见截断、超时或未知结果。仅凭本次不能推断供应商内部原因。

## 能与不能得出的结论

新candidate_observation被实际返回，第一项目确实说明了候选表达，但未完成全部目标，不能据此认定M100已改善整体语义质量，也没有到达此前失败的D3或四问评分。

程序报告partial、complete=false、0正式接收属性/24待评属性；这里的0表示本轮没有有效整批结果，**不是项目认知质量0/24**。未复用旧D1/D2分数，M096/M099及其原意见保持，不混写为统一新版成绩。M094真实问答答案仍存在，四问尚未完成独立实用性评价。

当前直接阻塞点是评分Agent单次未完整交付多个目标。缺两个评分目标属于任务未完成，需要后续处理评分交付完整性；本轮不以默认字段或人工分数补齐，不擅自改变分批方式、门槛或预算。后续方案尚未确定。

## 调用与材料保全

- deepseek-flash，`https://api.deepseek.com/beta/chat/completions`；唯一实际HTTP正文与冻结首批37673字符逐字匹配。
- 最多9次批准中实际1次物理请求，0补试，1批invalid、0批completed、0未知/pending，8批未发。不自动消耗剩余许可补发已知失败。
- 输入12636、输出629、总13265 token，cache_read0，reported_model=deepseek-flash。
- 同一固定RealWorld、认知run、原档案、M094四问、24必要属性与门槛保持。24份冻结文件、原M099的44份、M100的6份、M096的44份、M097的7份及全部更早资料、45原生、8原问答任务与认知保持。
- 安全原始供应商响应、批准、尝试标记、实际结果/报告、漏项诊断、用量与验证完整保存；评分和SQLite checkpoint两个一致备份均integrity_check=ok。

忽略目录`work/project-acceptance/realworld-evaluation-m101-20261016/review/`保留`call-1-receipt.json`、`result.json`、`report.json`、`failure_diagnosis.json`、`metrics.json`、`result_verification.json`、两个最终SQLite备份和`final_manifest.json`。目录沿用准备日期，不改名或覆盖原冻结材料。

本次仅已批准真实实测、只读漏项诊断与文档，无生产代码变化，不重复61项或完整回归。完成请求/结构/身份范围/用量、单次停止、原样历史保护、SQLite、文档链接与diff检查；无LangSmith、样本程序/测试执行、包外资料、补查、知识发布/结案、Web/API、推送、部署或新会话。
