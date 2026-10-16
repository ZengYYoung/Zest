# M068：四条独立评分的真实执行与范围越界

日期：2026-10-16（Asia/Shanghai）。执行实现：`13de8fff986473c7d04ec6d9df58343ab29dd551`。

**本次限定评分为 partial：两次请求，一条有效评分，第二条响应无效后停止。** 专职评分 Agent 认为 T1:F1 被支持，与 M066 的该条判断一致；T1:F10 批次把冲突线索也当成待评对象，并返回额外字段，未形成有效成绩。T1:F11、T1:F12 未调用，原先的两个语义疑点尚未取得独立评分结论。

## 许可与固定对象

用户审阅 [M067 四条评分包](COGNITION_EVALUATION_M067.md) 后明确批准最多四次顺序物理请求、零补试。评估探针 `14133e65-4939-43d6-98bf-b8b35c5a86c1`，冻结清单 SHA256 `423172c5ca33f796ac33d11e890899a73894787ecdc2517b6bc6406cea546560`。

公开样本仍为 KaleoOreo/python-personal-finance-tracker，commit `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09`。四条原句、各自获准原文、43 条其他发现及 37 条未知冲突线索固定；没有提供 M066 核对意见、supported 或主协作 Agent 的疑点答案。使用原内置版本化评分 Skill、CognitionEvaluationResponse / with_structured_output 与生产接收器，没有为本次结果调整准则。

模型 `deepseek-flash`，接口 `https://api.deepseek.com/beta/chat/completions`，每次超时 60 秒、输出上限 8192 token。完整请求分别 26862、29419、27148、28214 字符，均无字符扩展。实际只执行前两条；第二条无效触发已批准的停止条件，未补试、未跳过失败批次继续后两条。剩余两条没有消费，也不是其他请求的新许可。

## 实际结果

| 发现 | 本次状态 | 可报告结果 |
| --- | --- | --- |
| T1:F1 | 有效完成 | 3 项 check 均 supported、coverage_complete=true、无冲突；与 M066 接收判断一致 |
| T1:F10 | 完整响应无效 | 原始收据已保存，整个批次不计有效评分 |
| T1:F11 | 未调用 | 无独立评分结论 |
| T1:F12 | 未调用 | 无独立评分结论 |

一条有效结果只表示该次评分模型的意见，不是主协作 Agent 人工核验的真值，也不证明三项 check 已是完全拆开的原子命题。1/4 是完成范围，不能称为准确率或项目质量分数；未完成四条比较，不生成整体通过结论。

两次收据均 reported_model=deepseek-flash、finish_reason=tool_calls。输入共 19897、输出 3018、总计 22915 token，输入 cache_read=2176；执行约 14.002 秒，包含本地检查/保存，不等于纯模型延迟。第二条输出 2444 token，低于 8192 上限，完整原生参数可解析为 JSON，没有依据归因为输出截断。没有结果未知的已发送请求。

## 第二条为什么无效

第二条只要求为 T1:F10 返回判断，模型实际在同一个 CognitionEvaluationResponse 中返回七条：

`T1:F10、T2:F6、T2:F7、T2:F8、T2:F9、T3:F6、T4:F1`。

后六条均来自 other_findings 的冲突线索，而非本批 findings。模型还在 T2:F6 的判断中加入 `note`。原生输出虽然是完整 JSON，仍有两层确定性问题：

- 具名 Pydantic 拒绝 `judgments[1].note`，错误类型 extra_forbidden，适配器记 structured_status=invalid。
- 不改动原生参数、直接离线调用生产评分业务接收器，也因“实际 7 项，允许 1—1 项”拒绝。即使只解决额外字段，目标身份越界也不能成为有效评分。

实际执行链在解析无效收据时给出通用 invalid_cognition_output /“认知文本为空或超过预算”。这一表面错误不能解释成 HTTP 请求字符超限：本条请求 29419 字符，真正的结构及身份问题可由保存的原生参数确定性复现。没有删除 note、抽取 T1:F10 或改写实际响应来挽救成绩，未将无效批次中的其他判断纳入统计。

目前输入角色表达存在值得优先澄清之处：Skill 说“逐条审查包内全部 observed 发现”，Schema 则要求“仅评价本批 findings”；other_findings 本身也带 observed 类型，但只应提供冲突线索。真实输出证明本次模型越过待评范围，不能仅凭一条响应断言这就是唯一成因。

下一小点建议先明确“本批待评身份”和“仅供冲突检查的线索”的职责，并用本次原始响应做离线拒绝回归；保留严格身份校验，不自动裁剪多余判断，不扩大预算。本模块只记录结果，未修改 Skill、Schema、输入或接收规则，新的方案及任何模型复测须另行确定和冻结批准。

## 材料与保护

材料继续保存在忽略目录 `work/cognition-acceptance/score-m067-20261016/`：

- `frozen/authorization.json`、`execution_attempt.json`、`call-1/2-attempt.json`：具体批准和独占预占；预览及原冻结清单不改写。
- `frozen/call-1/2-receipt.json`、`call-1-validated.json`、`call-2-failure.json`、`outcome.json`：原始安全收据、有效评分、失败及 partial 汇总。
- `frozen/failure_diagnostics.json`：完整原生 JSON 的只读结构/身份诊断，不是修补后的响应。
- `frozen/result_verification.json`、`execution.log`：计数、用量和保护核查；与准备期 verification.json 分开保存。
- 结果提交后另存 `final_manifest.json`，覆盖本轮材料和报告版本，不覆盖执行前冻结清单。

执行前冻结的 12 份文件全部不变；M058 128 份、M064 101 份、M065/M066 探针 22 份历史材料的字节数和 SHA256 全部相同，旧调查及完整调用记录再次回读一致。原 M064 仍 failed/20 次调用/44 pending，M066 四条结果不改写。

没有整项目覆盖评分、人工替代成绩、知识发布、LangSmith 上传、样本程序/测试执行、旧 run 恢复、补查结案、推送、部署或新会话。只提交结果文档与状态链接、开发日志，完成收据/离线诊断/哈希/文档链接/diff 检查，不重复完整后端或前端回归。
