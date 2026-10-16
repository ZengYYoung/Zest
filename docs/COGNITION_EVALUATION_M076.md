# M076：新报告的十三条真实独立评分

日期：2026-10-16（Asia/Shanghai）。执行实现：`1aabc5842045d853a3a8da50aa0c01d4ba3eee6f`。

**13次请求全部有效，限定评分流程完成；评分模型仍明确误收，不能宣布语义质量通过。** 模型把含内部空格的字符串 `mixed case` 判为“无空格”，并给原结论整条支持。原始响应及错误判定完整保留，没有人工改标签、修补成绩或追加复测。

## 固定包与执行

用户审阅 [M075准备说明](COGNITION_EVALUATION_M075.md) 和完整外发预览后明确批准。评分探针 `5c0b8d72-fae9-4fe5-8b0b-0b9bf3dfe8dc`，冻结清单SHA256 `421c38a10c17eba7381e2fe21d7885f06233c70616f48b1d18cab25653115877`；批准另存authorization.json，不覆盖冻结输入中的准备期状态。

复用 [内置专职评分Skill](../zest/skills/cognition-evaluation/SKILL.md)、CognitionEvaluationResponse / model.with_structured_output和生产validate_evaluation。没有变更指令、Schema、接收算法、模型参数或候选原句。调用前核对工作区干净、固定HEAD、15份冻结文件、精确SDK请求、来源和历史清单。

评分对象是 [M074](COGNITION_REPORT_GENERATION_M074.md) 新报告中的全部13条observed，各条一次；全部15候选/15未知/6项资料需求本地保留，两条inferred和其他原句/未知只作同报告冲突线索。每条仅附自身实际引用的固定原文，不提供旧核对/评分意见、M074疑点分析或预期答案。

Finance固定commit `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09`；合成片段固定生成实现 `3099576041e1790e45211dd462399286a7a419b4`。局部身份F:F1–F9、S:F1–F6通过生成探针、请求、原报告下标和文件哈希回溯，不等同旧T1:F或认知库发现。

模型deepseek-flash，接口 `https://api.deepseek.com/beta/chat/completions`；完整请求字符数与M075冻结预览一致，均未超过32000。最多13次顺序物理请求、每次60秒/8192输出token、零补试。实际13次全部收到有效响应，0无效、0结果未知、0补试，没有剩余请求或恢复操作，本包调用预算已消费。

## 模型实际判定

下表是**评分模型的意见和拆分数量，不是人工正确率**。全部coverage为空，没有项目参考覆盖评分。

| 局部身份 | 模型check数量 | 模型coverage_complete | 模型整条supported |
| --- | ---: | --- | --- |
| F:F1 | 4 | true | true |
| F:F2 | 4 | true | true |
| F:F3 | 3 | true | true |
| F:F4 | 3 | true | true |
| F:F5 | 8 | true | true |
| F:F6 | 5 | true | true |
| F:F7 | 5 | true | true |
| F:F8 | 4 | true | true |
| F:F9 | 12 | true | true |
| S:F1 | 3 | true | true |
| S:F2 | 1 | true | true |
| S:F3 | 2 | true | true |
| S:F4 | 3 | true | true |

Finance为9条/48个check，合成片段为4条/9个check。合计57个check的verdict全部supported，所有13条coverage_complete=true、conflicts为空，生产门控因而派生13条supported。57只是命题出现次数，部分check仍合并多个属性，未语义去重，不能称57个独立事实或57项正确。

S:F5–F6没有独立judgment，15条unknowns也未单独评分。不存在事先冻结的项目参考项，故没有事实/流程/未知覆盖分母，未重算M058的8/10、3/6；这些候选没有auto_reviewed状态，不计算生产误收率或整体质量通过。

### Finance测试边界

F:F9是新报告第9条，不是旧T1:F9或旧T1:F12。它描述可见测试输入、已见断言，并明确写清洗用例只读到 `transaction = result.transactions[0]`，后续断言未读到。评分模型拆出12项并判支持，理由也保留原文窗口1–60行、more_after=true的边界，没有再把未读清洗断言当成已验证结果。

这一处表述及评分理由与过去的笼统清洗概括不同，是本轮可回读的局部现象；不据此证明所有复杂结论可靠。新报告F:F5仍将文本清洗、日期、金额、type多种规则放在一条候选，评分拆成8个check，但有check继续合并strip/空值、解析/错误分支、合法集合/规范化等属性。原生成粒度及模型拆分完整性问题没有因全支持而自动解决。

### 明确漏判：字符串字面量与描述没有分别核验

S:F4原句同时声称结果断言唯一、期望值为“全小写且无空格的 `mixed case`”、另一个函数不含结果断言。固定合成原文为：

```python
def test_normalization_input():
    result = normalize("  Mixed CASE  ")

def test_normalization_result():
    assert normalize("  Mixed CASE  ") == "mixed case"
```

评分模型在第2个check中原样保留了“全小写且无空格”的强断言，verdict仍为supported，理由直接把字面量 `mixed case` 解释成全小写、无空格。该字面量在mixed与case之间含一个U+0020空格；原文是真实的，文字解释却与它不符。这是可直接检查的反例，不需要执行片段，也不是“缺少运行证据”问题。

本次既没有丢掉原句中的“无空格”，也没有标coverage_complete=false来遮盖缺口；失败出现在语义判断本身，并将“字面量值、大小写、空格属性”合并成一个check。字符位置机制已移除，评分职责和完整性/支持两轴也已明确，仍不足以阻止这次误收。不能把协议有效或模型自报完整视为语义已正确核实。

此处只记录具体反例，不人工重写模型为contradicted、不补算12/13或56/57正确率，也不声称其余项目已获人工金标准认证。未围绕本例临时改Skill、加关键词规则、换模型或补跑；后续改进需要另行确定最小范围与离线标准，新真实调用仍需具体冻结批准。

## 收据、复核与保护

13份reported_model均为deepseek-flash、finish_reason均tool_calls。输入66017、输出8374、总计74391 token，cache_read=41856；耗时约45.613秒，包含保存/本地检查，不等于纯模型延迟，无可靠账单金额。

独立忽略目录 `work/cognition-acceptance/score-m075-20261016/` 原样保存准备与执行材料：frozen/authorization.json、execution_attempt.json、13组attempt/receipt/validated、execution.log、outcome.json、result_verification.json。收据先安全保存，再原样经具名Schema和生产接收器；复核13份解析、身份、空coverage和派生结果均与落盘相同，计数与请求哈希一致。result_verification另记字面量反例，没有覆盖准备期verification或任何模型标签。

15份冻结文件保持原哈希；18次原文引用重新对应固定Git blob/文件哈希/物理行段，七份历史清单合计365份文件字节/SHA256相同，原M064调查及完整调用回读一致，仍failed/44pending。没有把新候选或评分写入知识库、关联旧结论结案或修改历史分数。结果文档提交后另存final_manifest.json绑定报告版本与本次完整私有材料。

本模块只记录真实评分结果、状态链接与开发日志，验证收据/具名Schema/生产接收、计数、精确请求及原文哈希、反例、历史保护、文档链接和diff；不重复完整后端/前端回归，M073相关76项已通过。无LangSmith、包外资料读取、样本或合成程序/测试执行、旧run恢复、知识发布、推送、部署或新会话。
