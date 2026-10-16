# M091：代表性项目四个真实认知问答结果

日期：2026-10-16。用户批准[M090完整冻结包](PROJECT_QA_ACCEPTANCE_M090.md)后，使用干净提交`38ef9bf9db0315dfad9450bc3a3ae54fffe44055`执行。**四个普通问答任务均完成且实际引用了已有认知；回答仍有主要流程与修改定位缺口，尚未取得独立实用性评分。**

## 实际执行

沿用RealWorld固定commit `4b95fb2227dfeb5dd1a45d89b2bf48630b93fd28`和partial认知run `ad1782a3-9328-4bf0-a9ff-c4466f4d3c4c`。后者20条已核对成果包含明确标记的保存响应复用；本次没有重新调查或改写原M086 failed运行。

按正常`TaskRunner.decide`、single/lexical策略依次执行四问，向`https://api.deepseek.com/beta/chat/completions`的`deepseek-flash`实际新增4次请求、0补试。四份SDK正文与冻结包逐字匹配，28560/27689/28493/27543字符；具名`QAResponse`经`with_structured_output`全部有效，生产状态/引用校验通过，finish_reason均tool_calls。没有未知结果、传输错误或剩余未执行任务。

执行器共25.584秒；输入33809、输出3606、总37415 token，cache_read 1152。本包最多4次预算已经消费，不能据本次批准重发或增加请求。

## 实际回答与直接可见的缺口

| 问题 | 模型返回状态 | 实际引用 | 回答直接表现 |
| --- | --- | --- | --- |
| Q1：主要模块与create_app组装 | insufficient_evidence | 2条认知 | 给出三模块模型关联及autoapp入口；明确缺create_app实现、蓝图/扩展组装和路由职责 |
| Q2：注册/登录、UserProfile与JWT | answered | 2条认知、4个原生片段 | 描述注册保存、登录校验/发token、JWT加载与初始化；仍保留save/update具体实现等未知 |
| Q3：make_article发文流程与修改位置 | answered | 1条认知、1个原生片段 | 描述作者/文章/标签模型及测试调用形式；明确缺make_article本体、校验、错误处理和提交数据库流程 |
| Q4：关注关系修改影响和测试 | answered | 2条认知 | 定位profile模型及关联表，讨论连带影响；明确缺视图/序列化和具体测试来源，无法列出测试用例 |

每问均实际引用了本轮认知，没有把纯源码回答称为知识复用。Q2/Q3/Q4的answered是模型状态，不能换算成“3/4实用性通过”；尤其Q3、Q4虽然给出了回答，仍没有满足问题要求的部分主干信息。引用身份有效也不能证明语义正确，独立评分调用仍为0，不给项目准确率或整体通过分数。

完整回答、原始响应和未知项按原样保存，没有补写或修改答案，没有因为小措辞重新调用模型。

## 上下文问题的现场诊断

默认来源上下文上限为24000字符，当前`prepare_context`先放入认知正文、完整provenance和核对review，再追加原生检索片段。

Q1/Q3/Q4都选中了同一条跨模块模型关系T3:F1：其正文412字符，但带完整来源与核对后占20927字符，其中provenance为18003字符、review为2157字符。该大来源占据绝大部分空间。

保存的实际输入与只读检索回查表明：

- Q1确实检索到了`conduit/app.py`，但触发context_budget被排除；回答随后明确缺该文件。
- Q4确实检索到了`conduit/profile/views.py`与`tests/test_models.py`，也因上下文预算被排除；最终没有独立原生片段。
- Q3仅放入`tests/test_articles.py`开头片段。前五个原生检索候选主要是同一测试文件的多个窗口及序列化文件，没有`articles/views.py`；上下文挤占之外，还存在本体定位不足。

这些是保存输入、检索结果和回答自报缺口的直接观察，不是独立评分结果，也不声称已经证明全部答案正确或错误。**下一小模块建议讨论问答上下文分配：压缩模型输入中的认知审计冗余、保留必要证据与身份、给关键源码留出空间；具体规则先与用户确定。** 审计原文和历史核对记录继续完整保留，不在本模块改实现、扩预算或补跑。Q3的本体定位问题也保留，不能承诺仅压缩就解决所有问答缺口。

## 材料与验证

忽略目录`work/project-acceptance/realworld-qa-m090-20261016/`：

- `review/answers.md`、`task_results.json`、`Q*-task_result.json`：四个实际回答与完整任务。
- `review/Q*-model_attempt.json`、`Q*-model_receipt.json`：独占尝试记录、完整安全响应、供应商元数据与用量。
- `review/execution_summary.json`、`metrics.json`、`postrun_verification.json`：调用次数、完成/引用/来源身份、预算及历史核对。
- `review/context_diagnosis.json`：实际输入各部分字符、预算排除记录与只读检索候选。
- `review/authorization.json`、`execution_attempt.json`与原冻结清单：此次具体批准和一次性执行身份。
- `review/prepared_tasks.sqlite3`及`snapshots/tasks-final.sqlite3`：批准前与执行后的任务checkpoint一致备份；最终清单覆盖全部原始材料。

12次问答来源槽位核对均对应固定认知身份/内容或固定commit/blob/哈希/行段，实际引用与任务结果一一对应。19份原冻结文件未改，12份已有资源/目录文件及原认知完整记录不变；M089的40份材料、163份原调查、21份旧问答诊断、17份离线和此前533份材料哈希一致，45个样本原生文件与Git状态保持。新问答只推进新建任务记录，没有将答案发布为项目知识。

本模块仅记录获批真实问答及诊断，未修改生产实现、不重复完整后端回归。完成四份真实结构/业务接收、SDK精确匹配、调用/预算、引用与来源、SQLite integrity_check、历史哈希、文档链接和git diff检查。没有独立评分、LangSmith、样本程序/测试执行、修改、Web/API、推送、部署或新会话。
