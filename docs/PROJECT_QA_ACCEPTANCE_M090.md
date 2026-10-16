# M090：代表性项目四个真实认知问答的准备

日期：2026-10-16。用户同意继续真实问答验收。[M089](COGNITION_BATCH_ISOLATION_M089.md)已确认部分认知成果可进入上下文，本模块用正常问答入口准备四个固定问题及完整外发请求，尚无新增真实模型调用。具体冻结包批准后再执行；独立评分在实际答案形成后另行准备。

## 对象与现有认知

沿用RealWorld固定commit `4b95fb2227dfeb5dd1a45d89b2bf48630b93fd28`与M088/M089隔离项目，认知run `ad1782a3-9328-4bf0-a9ff-c4466f4d3c4c`保持partial，20条auto_reviewed、8条needs_investigation、9条pending_review。

这些认知包含M086保存结果的显式复用及M089一次新核对。此次不重新调查、不改写旧failed运行，也不声称全新冷启动。已核对认知供问答导航，未经核对的结论不作为事实输入；已自动核对也不等于独立语义评分通过。

## 四问与实际请求

问题沿用[M080提前固定的四问](PROJECT_ACCEPTANCE_M080.md)，没有为当前结果改题：

1. 主要业务模块及其通过create_app组装的职责和关联。
2. 注册、登录如何经过user、UserProfile和JWT，区分保存与响应。
3. make_article怎样协调当前用户、文章、标签和数据库，调整发文规则应看哪些模块。
4. follow_user / UserProfile.follow的修改影响、相关测试及无法确认的运行结果。

通过`TaskRunner.create(..., retrieval="lexical", strategy="single")`新建四个真实任务，均为awaiting_approval。没有指定文件/关键词、手工注入知识或参考答案，没有复用M086的未批准问答任务。

| 问题 | 精确请求字符 | 已核对认知来源 | 独立原生检索片段 |
| --- | ---: | ---: | ---: |
| Q1 | 28560 | 2 | 0 |
| Q2 | 27689 | 2 | 4 |
| Q3 | 28493 | 1 | 1 |
| Q4 | 27543 | 2 | 0 |

认知来源保留原文依据和核对意见。Q1/Q4没有额外原生检索片段，是现有默认上下文构建的真实结果；检验其是否足够支撑回答，不在实测前手工补源。四份请求均在32000字符范围内，没有字符扩展。

模型deepseek-flash，`https://api.deepseek.com/beta/chat/completions`；接口超时60秒，每问输出上限2048 token。具名`QAResponse`绑定`with_structured_output`，原有状态/答案/引用/未知字段以及引用身份校验保持。

## 固定执行范围

最多4次真实调用、每问1次、0补试，顺序执行，全流程上限360秒。任何一问无效、未知、失败，或版本/认知来源/模型/Schema/请求正文变化，停止剩余问题并保存已有结果，不增加调用或自动重发。

执行器调用正常`TaskRunner.decide`，模型适配只负责逐字匹配对应冻结HTTP正文、限制调用次数和保存安全原始响应，不替换问答结果。批准绑定干净执行HEAD、四个任务身份、完整来源/系统指令/Schema、请求哈希、模型及预算；一次性执行标记阻止重复运行。

本阶段记录回答完成情况、引用的认知与源码、未知项和模型用量，不由主协作Agent临时给项目打分。后续按原项目级实用性准则，另行冻结实际档案/答案的独立评分包，关注主要模块、关键流程、修改定位和重大错误，轻微细节只记录。没有新增评分、补查、LangSmith、样本执行/测试、修改、Web/API、推送或部署许可。

## 审阅材料与本地验证

忽略目录`work/project-acceptance/realworld-qa-m090-20261016/`：

- `review/qa_units.json`：四个真实待批准任务、实际上下文、payload、完整SDK正文/URL/哈希。
- `review/questions.json`、`source_cognition.json`、`usability_rubric.local.json`：原问题、来源运行快照与不发送问答模型的本地项目级准则。
- `review/protected_existing_files.json`：12份已有资源/目录文件哈希，问答准备只新建任务数据库，既有数据库和认知记录不覆盖。
- `review/preparation_verification.json`、`runner_rehearsal.json`：准备核对与临时副本、本地HTTP演练记录。
- 提交后生成`review/input_spec.json`、`source_inventory.md`、`execution_request.md`和`freeze_manifest.json`，供具体批准审阅。

本地HTTP演练直接使用实际执行器与临时SQLite副本：四个有效响应共4次；首问无效或超时各仅1次，后3问仍待批准，不补试。实际四任务仍等待批准、0真实调用。M089的40份最终材料、原调查163/问答诊断21/M087离线17及此前533份材料哈希一致，原认知和45个样本原生文件保持。

本模块没有生产代码变化，不重复完整后端回归。完成真实任务准备、SDK编译、预算与来源核对、三种执行器演练、语法、历史哈希、文档链接及git diff检查。尚无真实答案或实用性成绩。
