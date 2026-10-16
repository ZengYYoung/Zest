# M070：评分目标边界修复后的四条真实复测

日期：2026-10-16（Asia/Shanghai）。执行实现：`48b7afe575f819baef47d2ebbbc6c6c7f764d9c6`。

**四条限定评分全部完成，评分对象越界未在本批复现；语义质量仍有未解决问题。** 四次物理请求、零补试，四份响应通过具名 Schema 与生产接收器，各批只返回本条身份。专职评分 Agent 对三条整条判为支持，对 T1:F11 判断证据覆盖不完整、整条不支持，与 M066 的该条接收判断存在分歧。不能将这三条支持称为人工确认正确，或据此宣布整个项目质量验收通过。

## 冻结与批准

用户审阅 [M069 修复与复测包](COGNITION_EVALUATION_SCOPE.md) 后回复 accept。新评估探针 `12f8ec33-0b80-4c3a-b64b-91ef5ee4c504`，冻结清单 SHA256 `e17e88c2e02c65ad6dc15d2882ec5458c5766aa594fc39488beaa061ca14ff0e`；单独记录批准，未沿用 M068 未执行额度。

公开样本 KaleoOreo/python-personal-finance-tracker，commit `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09`。四条 payload 与 M067 完全相同：原结论、各自限定原文、43 条其他发现和 37 条未知冲突线索不变，只修改 Skill 和 Schema 中评分对象的职责说明。评分模型没有看到 M066 supported/核对意见或主协作 Agent 的疑点答案。

模型 deepseek-flash，接口 `https://api.deepseek.com/beta/chat/completions`；完整请求 27673、30230、27959、29025 字符，无字符扩展。最多四次顺序请求、每次超时 60 秒/输出上限 8192 token，零补试，无效或未知即停止。实际四次全部有效，本包调用许可已消费。

## 专职评分结果

| 身份 | 模型返回 check 数 | 模型语义覆盖标志 | 程序派生整条支持 | 与 M066 接收判断 |
| --- | ---: | --- | --- | --- |
| T1:F1 | 3 | true | true | 一致 |
| T1:F10 | 2 | true | true | 一致 |
| T1:F11 | 4 | false | false | M066 支持，本次不支持 |
| T1:F12 | 5 | true | true | 一致 |

每份响应仅有该批 findings 中的一条 judgment，coverage 均为空，没有额外 note 字段或为冲突线索新增判断。四份 reported_model 均为 deepseek-flash、finish_reason=tool_calls；无无效/未知结果、未补试、未裁剪或修补响应。

供应商收据输入 41070、输出 2475、总计 43545 token，输入 cache_read=7680；执行约 14.544 秒，包含本地核查/保存，不等于纯模型延迟。没有可靠账单金额。

### T1:F11：整条证据覆盖不足被保留

模型识别 P3 的正文窗口始于 database.py 第 106 行，只显示 end_date、WHERE 拼接、排序与模型还原。category、transaction_type、start_date 的过滤分支不在该条获准原文中，因此最终 coverage_complete=false，程序派生 supported=false。该判断没有借用其他条目的 P2 正文来弥补范围，是本次相较 M066 的一处接收分歧。

但模型给“支持四类过滤”的属性仍标 verdict=supported，同时其 reason 又承认三个过滤分支证据不完整，随后整条 reason 也明确无法完整支持。**单项标签与理由仍不一致。** 本次整条门控因 coverage_complete=false 阻止接收，不代表单项评分已经可靠；没有擅自将实际 verdict 改为 insufficient_evidence。

### T1:F12：清洗测试支持边界仍有疑点

评分模型承认 P4 在 `transaction = result.transactions[0]` 处结束，没有后续清洗断言，却仍认为结论只描述测试输入和可见期望，并将“测试印证清洗”的概括判 supported。它没有报告该处证据不足，最终与 M066 的支持判断一致。

这是评分模型的实际意见；测试输入、测试意图与对清洗结果的断言是否被混用，仍需继续研究判断规则。两个角色使用同一模型，对同一句一致不能成为独立正确性证明。没有由主协作 Agent 手工改判、补分或设定必须复现的答案。

## 结果边界

- 本次完成的是四条限定语义评分及与 M066 探针接收判断的比较，不是整轮调查、补查结案或全项目覆盖评分；M058 分数、M064 failed/44 pending、M068 partial 保持原样。
- 共 14 个返回 check 的 verdict 实际全部为 supported；T1:F11 因语义覆盖 false 而整条不支持。因此不能报告为“13 supported、1 insufficient_evidence”，也不能用返回项计数构造准确率。部分 check 仍合并多个字段或属性，未进行语义去重。
- 三条支持、一条不支持只是模型判定分布，不是 75% 准确率。原四条未发布知识，本次只有探针接收分歧，不是生产知识误收率。
- 职责说明修复在本批请求上可处理，不能证明未来所有输入都不会越界，或语义核对已稳定。下一步可围绕单项判定与理由一致、忠实保留原句范围讨论一个小点；本轮不追加修复或真实调用。

## 保存与保护

材料保存在忽略目录 `work/cognition-acceptance/score-m069-20261016/`：执行前完整预览/请求/Skill/Schema/冻结清单保留，批准另记 `frozen/authorization.json`；独占预占与四批原始安全收据、校验结果分别在 `execution_attempt.json`、`call-1..4-attempt/receipt/validated.json`；完整结果为 `outcome.json`，核查为 `result_verification.json`，不覆盖准备期 verification.json。结果提交后另存 final_manifest.json 绑定材料及报告版本。

冻结 12 份文件不变；M058 128 份、M064 101 份、M065/M066 探针 22 份、M067/M068 评分 26 份历史材料逐项字节数/SHA256 相同，原调查与完整调用记录再次回读一致。没有改写历史结果、采纳处理决定或发布知识。

本模块仅文档，完成实际收据、计数、哈希、身份/Schema/生产接收结果及文档链接/diff 检查，不重复完整后端或前端回归；M069 定向 42 项已通过。无 LangSmith 上传、样本程序/测试执行、旧 run 恢复、推送、部署或新会话。
