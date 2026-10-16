# M094：上下文调整后的四问真实复测结果

日期：2026-10-16。用户批准[M093具体冻结包](PROJECT_QA_ACCEPTANCE_M093.md)后，以干净提交`2ac4e41b5f62e9ce807a9aec6634c4e571d6c3a5`执行；生产修复为[M092/f8b6d22](QA_CONTEXT.md)。**四个普通问答均完成并返回answered，应用组装、发文保存和关注测试三处原缺口在新回答中得到回应；整体实用性仍待专职评分。**

## 固定范围与实际执行

RealWorld仍固定`4b95fb2227dfeb5dd1a45d89b2bf48630b93fd28`，认知run仍为`ad1782a3-9328-4bf0-a9ff-c4466f4d3c4c`（partial，20已核对/8待调查/9待核对），四个问题与原本地准则不变。使用四个新任务，原[M091任务和回答](PROJECT_QA_ACCEPTANCE_M091.md)保留，不重发旧调用，不重跑调查。

实际新增4次物理请求、0补试，目标`deepseek-flash`与`https://api.deepseek.com/beta/chat/completions`；四份HTTP正文32091/38558/69700/30063字符与冻结内容逐字一致。具名`QAResponse`/`with_structured_output`、生产状态与引用校验全部通过；finish_reason均tool_calls，无截断、传输错误、未知结果或剩余待执行任务。新4次预算已经消费，不授权再次发送。

执行器共29.759秒，用量如下。每问输出上限仍2048 token，没有通过增加输出预算取得回答。

| 问题 | 输入token | 输出token | 实际引用认知/原生来源 |
| --- | ---: | ---: | --- |
| Q1 | 10025 | 930 | 3/4 |
| Q2 | 11908 | 1186 | 3/4（提供5个原生片段，实际引用4个） |
| Q3 | 20801 | 1215 | 3/3 |
| Q4 | 9404 | 1129 | 3/3 |

累计输入52138、输出4460、总56598 token，cache_read 3072。输入较原四问增加，属于此次显式批准的来源扩充；本次单样本耗时不作为性能提升结论。

## 新回答与旧缺口

以下只记录回答明确表达的内容，不代替独立评分或人工金标准：

- **Q1模块组装**：原为insufficient_evidence，现为answered。说明create_app依次创建Flask、加载配置、注册扩展/蓝图/错误处理/shell/命令并返回app；给出user/profile/articles职责与主要数据关联。仍保留各模块完整API职责等未知。
- **Q2注册登录**：继续answered。说明User与UserProfile保存、密码哈希、登录校验和token生成、JWT身份回流及返回响应；新输入提供用户/资料模型。仍明确缺CRUDMixin.save完整实现、schema和部分配置，未将缺口补成确定运行事实。
- **Q3发文流程**：继续answered，但原回答缺视图本体，现说明`current_user.profile`绑定Article作者、标签查询/新建保存、`article.add_tag`、`article.save`、返回对象与序列化；指向views/serializers/models及`tests/test_articles.py`。所说测试步骤来自静态测试源码，本次没有执行样本测试。
- **Q4关注修改**：继续answered，现能定位profile视图/模型的调用和保存、返回路径，并列出`test_follow_user`、`test_unfollow_user`、`test_follow_self`、`test_unfollow_self`及相关文章模型测试。仍保留迁移、运行配置、实际测试结果和部分API测试等未知。

四问都实际引用同版本认知和原生片段，没有把纯源码回答冒充认知复用。模型自报answered不等于“4/4质量通过”，引用身份有效不等于每句话语义正确；本轮独立评分调用仍为0，未给总体准确率或项目级通过分数。没有因轻微措辞补试、改答案或修改原认知。

这次实测表明，上下文扩充、去重和实现定位调整后，新答案能利用先前缺失的主干材料继续回答。下一阶段可按原项目级准则，由专职评分Agent检查实际档案与四份回答，关注重大遗漏、主干错误、修改定位及运行边界；当前未实施新的评分或Web/API工作。

## 证据与验证

忽略目录`work/project-acceptance/realworld-qa-m093-20261016/`：

- `review/answers.md`、`task_results.json`、`Q*-task_result.json`：完整原样回答、未知项和任务状态。
- `review/Q*-model_attempt.json`、`Q*-model_receipt.json`：每问唯一尝试、完整安全原始响应和供应商元数据。
- `review/metrics.json`、`execution_summary.json`、`postrun_verification.json`：用量、调用数、精确请求及来源核对。
- `review/before_after.json`：同问题、同项目/认知下的原回答与新回答对照，未自动评分。
- 原审批/冻结清单、批准记录、准备checkpoint及`snapshots/tasks-final.sqlite3`，最终清单覆盖完整材料。

27个输入来源槽位均核对同项目/版本、原finding+review哈希和去重原文，或原生commit/blob/内容哈希/行段；实际引用26个槽位，与任务证据列表对应。19份原冻结文件、12份已有资源/目录文件及认知完整记录不变。原四问任务与46份材料、M092离线3份、M089的40份、163/21/17/533历史材料及45个样本原生文件全部保持。执行前后任务数据库备份均通过SQLite integrity_check。

本模块只有真实实测/证据/文档，没有生产实现修改，不重复M092的142项或完整回归。完成真实结构与业务接收、SDK正文精确匹配、调用/用量、27来源核对、历史/旧任务/原认知核对、checkpoint备份、文档链接及git diff检查。无独立评分、LangSmith、样本程序/测试执行、知识手动发布、Web/API、推送、部署或新会话。
