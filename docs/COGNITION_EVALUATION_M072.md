# M072：检查完整性与证据支持分离后的真实复测

日期：2026-10-16（Asia/Shanghai）。执行实现：`01280d4606f68d372c11a309f783f49cb0eadd79`。

**三次请求全部有效，T1:F11 的单项判定改善，四项合成对照均与本地预期吻合；T1:F12 的清洗概括疑点仍未消除。** 本次支持限定规则的局部改善，不能证明复杂结论已能稳定核对或全项目质量通过。

## 冻结与许可

用户审阅 [M071 规则和精确复测包](COGNITION_EVIDENCE_SUPPORT.md) 后明确批准。新探针 `b1b8381b-3ae7-4710-b718-3e3fb9496443`，冻结清单 SHA256 `226270e056cbba9a0d9f1f02b188667e6292fe4ab0b0e35ded6f10022738406a`。

- 前两次原样复用公开 Finance 样本 commit `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09` 的 T1:F11、T1:F12；各自原文和43条其他发现/37条未知线索与 M070 相同。仅指令/具名字段说明改变，旧核对/评分意见未给模型。
- 第三次为新写的合成测试文本和四条不同强度陈述；文本绑定本实现 commit 的真实 Git blob/文件哈希，没有提供本地预期答案，没有执行片段。四项只是受控对照，不是 Finance 额外业务发现。
- 模型 deepseek-flash，接口 `https://api.deepseek.com/beta/chat/completions`，完整请求 28571/29637/8489 字符，无字符扩展；每次超时60秒、输出上限8192 token。
- 许可最多三次顺序物理请求、零补试，无效或未知停止。实际三次全部执行并有效，本包调用额度已消费，没有追加请求或沿用旧许可。

使用专用验收执行器调用现有评分 Skill、CognitionEvaluationResponse / with_structured_output 与生产接收器，不伪造完整调查或 `zest evaluation` 整轮成绩。

## 两条原始样本

| 发现 | 模型检查完整标志 | 单项 verdict | 程序派生整条支持 |
| --- | --- | --- | --- |
| T1:F11 | true | 2 supported、1 insufficient_evidence | false |
| T1:F12 | true | 4 supported | true |

**T1:F11**：模型把不可见的 category/transaction_type/start_date 过滤条件明确判入 insufficient_evidence，理由与标签一致。原句各部分已被检查，所以 coverage_complete=true，程序仍因单项证据不足拒绝整条。M070 则是单项写 supported、理由承认缺口、整条 coverage_complete=false；两份响应分别保存，没有事后改写旧结果。这次正确体现了“检查完整与证据充分是两件事”。不过过滤与排序仍合并在一个 check，不能称为完全独立的原子核对。

**T1:F12**：模型依然把“以该输入验证清洗”解释为测试输入字符串内容即可支持，最终支持整条。新响应没有单列原句开头“测试证据印证清洗与行号追踪行为”的概括，也未交代清洗测试后续结果断言不在 P4 窗口中的边界；部分 check 还引入原句没有列出的 transactions/errors 长度。它宣称检查完整不等于已证明没有遗漏或缩弱原句。以上是输入/响应对照，未由主协作 Agent 补算一个人工样本分数，也没有修改模型 verdict。

## 四项合成对照

| 身份 | 陈述类型 | 模型 verdict | 检查完整 | 本地预期对照 |
| --- | --- | --- | --- | --- |
| L1 | 测试使用指定输入调用函数 | supported | true | 吻合 |
| L2 | 只有输入调用的测试验证了归一化结果 | insufficient_evidence | true | 吻合 |
| L3 | 另一测试包含指定输出的静态断言 | supported | true | 吻合 |
| L4 | 测试已在当前版本实际执行通过 | insufficient_evidence | true | 吻合 |

模型区分了两处测试函数的归属：L2 不能借相邻测试的结果断言来支持当前测试；L3 无需实际运行记录即可支持“源码含此断言”；L4 缺运行证据，不能从断言存在推为已通过。四项事前本地人工期望均未进入 HTTP 请求，评分后由程序按身份关联，检查预期 verdict 出现、整条支持和检查完整标志。

4/4 只表示本次四项受控对照与本地期望一致，不是总体准确率；小片段上的效果不能外推到含多个属性与概括的真实长结论。T1:F12 仍有疑点，不能用对照成功替代实际语义问题。

## 执行与保护

三批/六条陈述全部完成、无额外身份/字段、无无效或未知响应、零补试。六条 coverage_complete 均为 true；合计11个 check，8 supported、3 insufficient_evidence，其中真实样本和合成项分开报告，不将数量当唯一原子命题或准确率。

三份收据均 reported_model=deepseek-flash、finish_reason=tool_calls。输入24655、输出2160、总计26815 token，cache_read=6016；耗时约11.109秒，含本地核查/保存，不等于纯模型延迟，没有可靠账单金额。

材料在忽略目录 `work/cognition-acceptance/score-m071-20261016/`：原冻结包及14份冻结文件不改；批准、独占预占、三批原始安全收据/校验结果、完整 outcome.json 和 result_verification.json 分开保存，未覆盖准备期 verification.json；结果提交后另存 final_manifest.json 绑定材料与报告版本。

M058128份/M064101份/M065-M066探针22份/M067-M068评分26份/M069-M070评分31份历史材料逐项字节数与SHA256一致，旧调查及完整调用记录回读相同。原M058分数、M064 failed/44 pending及此前探针结果保持原样，没有知识发布或结案采纳。

本模块仅提交结果文档、状态链接和开发日志，检查收据、精确请求/原文哈希、计数、历史清单、文档链接和diff；不重复完整后端/前端回归，M071相关79项已通过。无LangSmith、样本或合成程序/测试执行、旧run恢复、推送、部署或新会话。复杂原句的概括与独立属性如何忠实保留，仍待下一小点讨论，本轮不自动继续调参或补跑。
