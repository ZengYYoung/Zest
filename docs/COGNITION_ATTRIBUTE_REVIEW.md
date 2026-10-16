# 逐项属性核对与冲突保留

M059 引入逐项核对，M065 移除由模型计算字符位置的要求。核对仍复用原有单轮 Agent、批准包和 SQLite 账本，不增加调用阶段。

每条发现的 statement 保留原样，通过 finding_id 对照完整原结论。核对 Agent 用具名 ClaimCheck 逐项返回对象、属性、原断言条件、断言、关联原文及 supported / contradicted / insufficient_evidence 判定，不返回 start/end 或计算字符数量。模型不能修正条件后替原断言判支持。

程序要求每个 finding 身份恰好出现一次、至少一项属性核对、支持项有其关联 P 原文、不借用其他发现的证据；仅 observed、全部命题支持、coverage_complete=true 且没有未解决冲突时整条 auto_reviewed。部分正确仍保存明细，整条保持 needs_investigation，不单独发布正确子项。coverage_complete 是模型对全部属性、括号解释、条件、否定和数量是否已核对的判断；程序检查身份和结构，不能证明模型没有漏掉语义，离线测试不证明模型已掌握拆分或语义。

M071 明确检查完整与证据充分分开：所有属性都已检查但某项证据不足时，coverage_complete 可为 true，该项 verdict 必须保留 insufficient_evidence；有相反证据用 contradicted。静态测试输入、结果断言和实际运行记录支持不同强度的原句，不能缩弱原断言来判支持。共用字段说明与核对提示同步，整条门控不变，见 [证据支持规则](COGNITION_EVIDENCE_SUPPORT.md)。

任务 unknowns 以 Tn:Um 身份进入核对输入，与 other_findings 都只作为冲突线索，不成为正文证据。对同一对象、属性、条件的不兼容陈述保存 reference_id 和原因。跨批发现冲突时，同 run 的另一方即使已自动接收也降为待调查；先前 invalidated 不复活。关联冲突不会因为另一批简单返回支持而消失。本模块保守保留冲突，自动消解与跨 run 追溯分别设计；历史 M058 不重评或改写。

例如 fixture 的创建方式、关闭行为和生命周期应分别判断；空输入与非数字的错误分支不可混用；配置值存在与其行为解释应分开。它们是核对维度的示例，不是针对 pytest 的硬编码规则。框架默认语义缺少包内依据时保持证据不足，不能把内部常识冒充原文或擅自联网。

ProjectReviewResponse 继续通过 with_structured_output 生成，supported 为程序派生的内部字段。新指令和 Schema 进入新批准哈希；旧批准不能执行新契约，已结束历史仍可读，原始字符索引也不删除。原有一次格式补试、精确请求冻结、预算和未知请求不重发规则保持生效；语义业务校验失败不触发格式补试。

离线验收包含三类历史反例的人工标注判定及正确对照、部分支持、coverage_complete=false、坏引用、冲突身份、跨批双方阻断和恢复边界。M065 将 M064 保存响应的副本仅移除 start/end 后作为协议迁移夹具，验证核对/评分接收路径；它不是新模型响应或内容评分，不写回旧调查。历史成绩和原始失败记录不变。见 [M065](COGNITION_SEMANTIC_REVIEW.md)。
