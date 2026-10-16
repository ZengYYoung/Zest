# M079：证据观察协议真实复测，原反例仍误收

日期：2026-10-16（Asia/Shanghai）。执行实现：`cf428ab4136bddfb1287a2c9db1cd2811b190d5d`（M077代码加M078准备文档）。

**7次请求全部有效，但原反例仍被错误支持；新增evidence_observation没有解决本次核心漏判。** 六个独立对照符合冻结的粗粒度预期，不能据此宣布评分可靠或将本轮算成总体准确率。

## 批准与固定输入

用户审阅 [M078完整包](COGNITION_EVALUATION_M078.md) 后明确批准。探针`02e2c9ea-154f-4429-a99b-aa9125e28f04`，冻结清单SHA256 `c4e3de7039f952d86131160fa17b7c59d66256cfdbc916ffb368e70bab00c9a7`。批准另存authorization.json，执行前核对干净HEAD、16份冻结文件、Skill/具名Schema、完整SDK请求、目标/预算和历史材料。

原S:F4的payload与M076完全相同：原句、P1完整原文、5条其他发现与5条未知冲突线索，只使用M077更新后的指令与输出契约。其合成原文固定commit `3099576041e1790e45211dd462399286a7a419b4`，不是Finance运行结果。六项对照固定commit `53b436d814dfbb84eae7352ccd1da9ba9002784e`，每次一条陈述与完整静态片段，正反情景彼此隔离。

模型未接收旧评分、错误诊断、预期verdict/checks、示例观察或人工理由；对照标注和旧支持状态仅本地保存。使用既有内置专职评分Skill、CognitionEvaluationResponse / EvaluationClaimCheck、model.with_structured_output及生产接收器；没有修改提示、参数或接收规则。

模型deepseek-flash，接口`https://api.deepseek.com/beta/chat/completions`，完整请求10202/8598/8597/8580/8582/8603/8576字符；每次60秒、最多8192输出token，最多7次顺序物理请求、零补试、无字符扩展。实际7次完整有效，0无效、0结果未知、0补试；本包预算已消费，没有追加调用。

## 原失败没有修复

S:F4仍把期望字面量`mixed case`描述为“全小写且无空格”。固定原文含：

```python
assert normalize("  Mixed CASE  ") == "mixed case"
```

本次模型只拆出2个check。第1项合并断言唯一性、所属函数、期望值、大小写与空格描述，evidence_observation明确写右侧字面量“全小写、无内部空格、无首尾空格”，verdict=supported；reason再次称“全小写无空格一致”。第2项是另一个函数没有结果断言，亦判supported。最终coverage_complete=true、conflicts为空，程序按原门控派生整条supported。

`mixed case`在两个单词之间实际含一个U+0020空格。错误不只留在verdict里，还进入新增的“证据观察”；观察字段通过类型/非空检查，不能证明它忠实于原文。模型继续合并多个可独立判真的属性，也不能因自报检查完整就认为拆分可靠。

M076该条3个check全部支持，本次2个check全部支持；拆分数量变化不是改善分数。两次原始响应、标签和引用全部保留，没有人工改成contradicted、填补新字段或补算正确率。

## 六个独立对照

| 目标 | 模型实际check判定 | 整条supported | 本地粗粒度预期匹配 |
| --- | --- | --- | --- |
| C:V1 字面量/小写/含内部空格 | 3 supported | true | 是 |
| C:V2 字面量/小写/不含空格 | 2 supported、1 contradicted | false | 是 |
| C:B1 上限3、包含等于3 | 2 supported | true | 是 |
| C:B2 上限3、不包含等于3 | 1 supported、1 contradicted | false | 是 |
| C:E1 存在静态结果断言 | 2 supported | true | 是 |
| C:E2 声称当前版本实际运行通过 | 1 insufficient_evidence | false | 是 |

六项coverage_complete均true、conflicts均空。模型能在C:V2指出`quiet lake`含内部空格、在C:B2识别`<=`包含等值、在C:E2保留运行证据缺口；这些是限定对照中的实际判断。原反例和C:V2都涉及空格，却得出不同结果；输入措辞、对象、上下文均不同，不能据此确定哪一个差异造成失败，也没有控制变量证据支持“删线索就会好”等推断。

本地自动比较只检查整条支持、检查完整性、预期verdict是否出现，六项均吻合；这不是逐属性正确率或独立金标准认证。C:E1又把断言右侧标识符单列，存在重复计数；C:V1观察里还将quiet写成quied。原文观察并非无损引用，粗粒度匹配不能代替对其文字准确性和独立性的检查。

全部7条共15个check：12 supported、2 contradicted、1 insufficient_evidence；4条整条支持、3条不支持。它们是模型判定分布和出现次数，包含原失败、复合与重复项，不当作唯一原子分母、6/7准确率、项目覆盖或生产误收率。requirements/candidates/coverage为空，没有重算旧8/10、3/6或宣称整体质量通过。

## 收据、验证与保护

7份reported_model均deepseek-flash、finish_reason均tool_calls；输入26535、输出3352、总计29887 token、cache_read20864。执行约31.243秒，包含保存/本地复核，不等于纯模型延迟，无可靠账单金额。

原始材料在忽略目录`work/cognition-acceptance/score-m078-20261016/`。frozen/authorization.json、execution_attempt.json和7组attempt/receipt/validated分别保存批准、预占、原始安全收据和生产接收；execution.log、outcome.json与result_verification.json记录计数、原样结果和核查。先保存收据，再具名Schema/接收器验证；重新离线解析7份与落盘结果相同，全部观察非空且不超800字符，精确身份与请求哈希一致。

16份冻结文件不变，两份原文共7次引用仍对应真实固定Git blob/文件哈希/物理行段；九份历史清单434份文件字节/SHA256一致，原M064调查及完整调用只读回读一致，仍failed/44pending。旧评分和新错误标签未改写，没有知识入库、结案或旧run恢复。结果提交后另存final_manifest.json绑定本结果文档与完整私有材料。

本模块只提交结果文档、状态链接和开发日志，核查真实收据/Schema/生产接收、引用/请求/历史哈希、比较与用量、具体反例、文档链接和diff；不重复完整后端/前端回归，M077相关108项已通过。没有LangSmith、包外来源、样本或合成程序/测试执行、推送、部署或新会话。

本轮保留M077字段与失败证据，没有立即再加提示、字段、组件或预算。下一步改动须围绕现有失败另行讨论，并先确定可区分原因的离线材料与验收条件；任何新真实调用都需新完整冻结批准。
