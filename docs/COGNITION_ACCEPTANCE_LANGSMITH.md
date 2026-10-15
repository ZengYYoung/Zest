# M050 后单次真实模型与 LangSmith 诊断

日期：2026-10-15；结果记录模块：M051。**这一次原生 JSON、Pydantic 和引用/资料分类检查通过，LangSmith 已上传并回读核对。单次成功不证明历史无效 JSON 问题已经修复或模型理解稳定可靠。**

## 固定输入和执行范围

实现为 `9a6ebdc`，固定公开样本仍为 `3a9e0c00403ec46cc7fa724f8d53f0fb804d2a09`。模型完整请求与 M049 逐字相同：10120 字符，SHA-256 `af4b40688e7c1f2177b0ae70d7078045917445d4a81d157a7c60020f2f0478d7`。目录、元数据、CLI/importer 原文、系统指令、schema、strict 和预算没有变化。

首次执行在自动审批阶段被拒绝，进程未创建，也未产生模型 attempt/response。随后用户明确批准这一次模型请求，以及完整请求（含提示词/schema/原文）、安全响应、步骤/时间向 LangSmith 的上传。仅在此具体批准后执行；旧特定批准未复用。准备冻结 SHA-256 为 `14fd679a4e5f0fd6bdea1b99e789848a5d98c5f7f53b0c9744354ccc6ffe7910`。

仅一条 `project_report` 请求发送至 `https://api.deepseek.com/beta/chat/completions`，模型 `deepseek-flash`，输出最多 8192 token、max_retries=0。没有第二次模型请求、自动补读、样本执行/测试/修改、语义核对模型调用或知识发布。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 原生输出 | 单一 ProjectReportResponse，arguments 3331 字符，完整有效 JSON |
| SDK/Pydantic | structured_status=valid；1 个有效工具调用、0 个 invalid_tool_call |
| 状态 | partial，诚实保留未读资料与未知 |
| 候选 | 9 条：8 条 observed、1 条 inferred；结构/引用检查接收 9 条，无逐条拒绝 |
| 空元数据 | 没有进入正文 findings；只在 unknowns 讨论初始化文件成因/意图，未产生空引用 |
| 资料需求 | 4 项全部包内：1 项已读待续读、3 项包内未读；无包外误报或无效定位 |
| 用量 | 输入 3587、输出 1280、合计 4867 tokens；cache_read=3453；费用未推算 |
| 耗时 | 本地收集的单次链调用约 6.595 秒；含 SDK 调用与解析，不含遥测上传 |

已读原文与程序核对只证明引用存在、原文非空、资料身份及结构符合约定，不能替代逐条语义核对。第 9 条推测缺列校验可能通过 ValueError 表达，模型明确标为 inferred，并承认 validation.py 未读；该条件仍待核实，不能发布为事实。没有重评整个项目认知的历史分数。

## LangSmith 实际接入

使用 M050 的本地收集/事后显式上传方式，目的地 `https://api.smith.langchain.com`，私有项目 `zest-structured-diagnostics`。未创建公共分享；密钥和请求头不在导出中。根步骤集中存放已批准完整请求、安全响应、JSON 诊断和业务检查结果；子步骤为实际 LangChain prompt/model/parser/fallback 包装层的关系及时间。

Trace ID：`837225ca-0bdc-4343-8a59-db9f44f5b8eb`，共 11 个步骤。SDK 上传成功后的第一次即时 read_run 返回 NotFound；随后只读查询同项目/trace 找到全部 11 个步骤，根 inputs、outputs、error 和全部步骤 ID 与本地导出逐项相同。没有为了追踪再次上传模型请求或再调用模型。第一次短暂不可见的底层原因未单独证明。

可在自己的 LangSmith 账户中打开 [本次追踪](https://smith.langchain.com/o/31f1c7ae-3d82-4e5d-848e-da3ee100241e/projects/p/4fa3a2d0-d997-4d48-b175-fbef2360769a/r/837225ca-0bdc-4343-8a59-db9f44f5b8eb?poll=true)。这不是公开分享链接。

本次 trace 中模型与 Pydantic 解析步骤均无错误；RunnableWithFallbacks 包装层存在不表示发生了自动模型重试。失败后的 SDK 解析 fallback 和模型重发是不同概念。

## 对修复方向的判断

- M049 已证明无效 JSON 在 SDK 解析前的原始供应商参数中存在；本次相同请求可以成功，说明该输入不是每次都失败。不能据此断言全部责任属于模型、Schema 完全无关，或估计实际故障率。
- LangSmith 改善了步骤查看与后续对照条件，没有改变原模型请求，也没有修补输出；接入追踪不是这次格式成功的已证实原因。
- 当前不基于一次成功继续修改生产提示词/schema。建议下一步先准备固定样本的结构化兼容性对照和清晰失败分类，再决定是否简化报告封装、调整适配或更换模型目标；任何新真实调用需新的具体范围/预算批准。
- 保留严格接收、已知无效响应留存、未知结果不重发、语义核对和发布边界。本次只完成单个报告轮诊断，不代表完整后台认知流程重新验收通过。

## 证据留存

忽略的私有目录：`work/cognition-acceptance/finance-20261014/review/targeted-m050-langsmith-20261015/`。保存完整预览、冻结、新的具体批准、单次 attempt、原始安全响应、引用/资料接收审计、追踪导出、上传收据、首次读回失败、后续精确核对、人工检查和最终文件清单。文件 0600、目录 0700；历史结果不覆盖。

10 项事后核对通过，包含新准备冻结、旧请求逐字一致、历史结果冻结、原生工具/JSON、非空证据、元数据隔离、资料分类和云端回读一致。接入代码的 40 项定向测试已在 M050 通过；本模块仅记录实际结果，不重复后端/前端回归。
