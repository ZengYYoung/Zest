# M089：批次失败隔离后的单次真实模型验证

日期：2026-10-16。用户批准[M088具体冻结包](COGNITION_BATCH_ISOLATION_M088.md)后，使用干净提交`50ef251df7e0f8406c1836826ab3a0eb5bfea4ba`执行；生产修复为[M087/8160f36](COGNITION_REVIEW_BATCH_ISOLATION.md)。**本次限定的机制验证通过：失败批次保留待办，下一批真实核对成功，部分成果进入问答上下文。**

## 实际执行与结果

仍使用RealWorld公开样本固定commit `4b95fb2227dfeb5dd1a45d89b2bf48630b93fd28`。新诊断run为`ad1782a3-9328-4bf0-a9ff-c4466f4d3c4c`，在独立资源中显式复用M086保存的计划、四份报告、37条候选和原核对结果及账本；原M086 run保持failed/27次，不恢复或改写。

向`https://api.deepseek.com/beta/chat/completions`的`deepseek-flash`实际发送1次请求，正文与冻结包逐字一致，共26510字符。具名`ProjectReviewResponse`及`with_structured_output`接收成功，finish_reason为tool_calls，无补试、未知结果或待执行请求。新请求用量为输入9671、输出2014、共11685 token，其中cache_read 1664；整个执行器约18.577秒。

| 检查项 | 实际结果 |
| --- | --- |
| 原失败批次 | T3:F7、T3:F8、T4:F1、T4:F10仍为pending_review；原失败收据和候选保留，未重发 |
| 后续真实批次 | T4:F11、T4:F2、T4:F3、T4:F4均通过结构和业务接收，进入auto_reviewed |
| 部分交付 | partial，20条auto_reviewed、8条needs_investigation、9条pending_review |
| 单次上限 | 新增1次真实调用、0补试；账本为27次历史复用加1次新调用，到28次后在下一次预占前按cognition_call_budget停止 |
| 四问上下文 | 正常prepare_context/lexical入口分别包含2、2、1、2条已核对认知来源；未手动注入知识 |

新核对内容涉及flake8配置、开发依赖、根依赖转发文件和Travis配置。模型按属性提供支持判断，原生产校验完整接收；新增四次候选原文引用均重新核对固定commit/blob/内容哈希/行段，与真实原文一致。

已有16条自动核对成果为保存响应复用，本次新增4条。这些接收标签不代表独立评分或总体准确率。例如dev依赖中“首行引入”的表述，模型解释实际位于注释后第2行，核心引入关系正确；这类位置措辞不作为本轮机制验证阻塞项，也未改写原始结论或响应。

## 对整体使用的意义与剩余边界

这次确认修复解决了原先“一批格式失败，整轮失败，已经核对的成果也无法用于问答”的阻塞：新流程能够保留问题并交付可读取的部分成果。partial在本次单次调用预算下符合预期，仍有待调查、待核对内容。

本次只准备四问的真实上下文，**没有生成四个模型答案，也没有调用独立评分Agent**；没有重跑完整冷启动调查，不能据此宣称“导入→调查→档案→认知问答”整体实用性已经通过。下一步应围绕实际问答体验验证，再决定是否需要补查；Web和接口补齐继续暂缓。

## 证据保存与验证

原始材料在忽略目录`work/project-acceptance/review-isolation-real-m088-20261016/`，不提交：

- `review/execution_request.md`、`input_spec.json`、`freeze_manifest.json`：完整固定来源、目标、Schema、指令、请求与预算；`authorization.json`记录本次明确批准。
- `review/new_model_receipt.json`、`result.json`：完整新响应/用量与新运行账本，原保存响应另在`reuse_record.json`。
- `review/outcome.json`、`postrun_verification.json`：机制判据、引用/预算/旧记录/历史哈希核对。
- `review/dossier.json`、`dossier.md`、`management_context.json`、`qa_contexts_only.json`：实际档案、管理总览与四问来源。
- `snapshots/`：6份SQLite一致备份，包括认知数据库、独立任务图checkpoint、索引和目录状态；最终清单覆盖冻结资料、响应和备份。

20份原冻结文件未改；原M086的23条逻辑调用及完整运行记录不变，163份调查材料、21份问答诊断、17份M087离线材料和此前533份历史材料哈希一致；45个样本原生文件、Git索引和固定HEAD不变。没有LangSmith、样本程序/测试执行、模型问答、独立评分、Web/API修改、手动知识发布、推送、部署或新会话。

本模块只记录真实调用结果及其证据，未修改生产实现；验证包括真实结构/业务接收、精确请求、预算、四次原文引用、真实上下文读取、历史哈希、6份SQLite integrity_check、文档链接和git diff --check，不重复M087已完成的166项或完整后端回归。
