# M078：证据观察协议的七次真实复测准备

日期：2026-10-16（Asia/Shanghai）。本页保留准备阶段状态：用户要求真实模型检测，准备结束时**0次新模型调用，具体包外发尚待批准**。

后续获批执行见 [M079真实复测](COGNITION_EVALUATION_M079.md)：7次全部有效，六项粗粒度对照吻合，原S:F4仍误收。本包预算已消费，本页准备期状态不是未消费许可。

[M077](COGNITION_EVALUATION_OBSERVATION.md) 已实现评分专用evidence_observation，108项相关离线回归通过。本轮不改Skill、Schema、接收算法或模型参数，只检查该实现下原失败及不同类型对照的实际模型表现。

## 固定范围

限定7次顺序请求，每次一个目标，使用内置专职评分Agent、具名CognitionEvaluationResponse / EvaluationClaimCheck与model.with_structured_output。

| 请求 | 输入范围 | 完整SDK请求字符数 |
| --- | --- | ---: |
| S:F4 | M076原失败陈述、原文及原5条其他发现/5条未知线索，payload完全相同 | 10202 |
| C:V1 | 字面量值、字母大小写、内部空格陈述 | 8598 |
| C:V2 | 同类属性的另一陈述 | 8597 |
| C:B1 | 数值上限及等值边界陈述 | 8580 |
| C:B2 | 同一边界的另一陈述 | 8582 |
| C:E1 | 静态结果断言存在的陈述 | 8603 |
| C:E2 | 实际执行并通过的陈述 | 8576 |

S:F4的具体值、描述属性及冲突上下文全部保留，仅使用M077后的指令/输出契约重新编译请求；不提供M076的supported、错误诊断、评分理由或本地预期。它来自合成静态片段，并非Finance真实程序执行证据，固定commit `3099576041e1790e45211dd462399286a7a419b4`。

六项对照复用M077的静态文本`tests/fixtures/evaluation_observation.py.txt`，固定commit `53b436d814dfbb84eae7352ccd1da9ba9002784e`，没有执行片段。每个情景只发送一条陈述和完整原文，other_findings/report_unknowns为空；正反候选不互作冲突线索。程序只白名单选取标注文件中的id/statement，不把checks、预期verdict、示例evidence_observation或人工理由送给模型。

两份原文共7次引用逐项对应固定Git blob、文件SHA256和完整物理行段，保留原末尾换行。没有新增Finance文件、Zest实现源码、运行记录、外部框架资料、工具读取或完整数据库外发。

## 本地对照与验收边界

人工预期及旧支持状态只在control_expectations.json本地冻结，用于结果后的限定对照。预期答案对模型隐藏，不表示这些案例是独立保留集或总体代表性样本。不能据少量对照吻合就宣布整个项目理解、评分模型或持续维护已稳定。

协议验收：精确目标身份、空coverage、非空证据观察、具名Schema与生产接收器一致；原始安全收据先保存，响应不补写、不裁剪。7份都有效才称限定流程completed；无效/未知停止为partial，未评项不算通过。

语义回读：分别检查原反例和六个对照的证据观察、独立属性拆分、verdict/reason及冲突；具体值与解释属性应分别核对，不仅看整条门控。原始响应与旧结果保留，不自动修正模型标签。

执行器的本地自动比较只检查整条supported、coverage_complete与预期verdict是否出现，并将未评项标为未评，不计错/对。该粗粒度比较不能证明对应属性判对或拆分完整，不能当原子准确率。模型结果及本地比较都不输出项目覆盖率、生产误收率、总体准确率或质量通过；本轮requirements/candidates/coverage为空，不沿用旧8/10或3/6分母。

## 外发目标、预算与冻结

模型deepseek-flash，接口 `https://api.deepseek.com/beta/chat/completions`；每次60秒、最多8192输出token，最多7次物理请求、零补试。所有请求低于32000字符默认上限，无字符扩展。无效、异常或结果未知保存后立即停止，不修补/自动重发、不另建包绕过停止条件。

新隔离忽略目录：`work/cognition-acceptance/score-m078-20261016/`。提交本准备模块后，要求工作区干净，固定执行HEAD、完整请求/HTTP正文/URL/哈希、Skill、具名Schema、原文与关联、本地预期、预算及执行器，再申请用户对具体包的批准。旧M076许可已消费，M077实施确认不包含本次未冻结外发许可。

冻结后的审阅入口：frozen/execution_request.md；candidate_preview.md列全部7条原句/原文及原冲突线索，batches.json为实际外发正文，skill.md/contract.json为精确模型指令与具名输出契约，report_sources.json/local_lineage.json/source_reverification.json保存来源关联。control_expectations.json只用于本地，不在HTTP正文。

execute_once.py必须匹配用户授权、清单/执行器/请求哈希、目标/预算、干净固定HEAD、Skill/Schema和全部历史材料后，才能独占预占并执行；批准和执行记录另存，原冻结文件不覆盖。没有授权时阻断，不因“要求真实检测”跳过具体包预览。

## 已完成离线检查

原S:F4 payload逐项与M076相同；六项白名单输入不含标注结果，各自情景隔离。人工标注接收夹具验证正反/证据不足门控与观察保存，不产生模型成绩。四个准备/执行脚本语法检查通过，未冻结/未授权执行器在外发前阻断。

九份历史清单合计434份文件字节/SHA256一致，旧M064调查及完整调用只读回读一致，failed/44pending保持；没有改写旧评分、补写观察、知识入库或结案。文档链接和git diff --check通过。本模块没有生产代码变化，不重复M077的108项回归或完整后端/前端测试。

没有真实调用、LangSmith上传、样本/合成程序或测试执行、旧run恢复、知识发布、推送、部署或新会话。具体外发批准后才执行并另记真实结果。
