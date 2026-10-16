# 独立认知评分 Agent

M060。评分 Agent 与调查 Agent、逐条知识核对 Agent 分工独立，使用内置版本化 [Skill](../zest/skills/cognition-evaluation/SKILL.md)。由模型分析参考属性、候选表达、固定原文和冲突，程序校验结果并汇总门槛。评分不更改原调查、知识状态、批准、原始请求/响应或历史成绩。

## 使用与权限

```bash
zest evaluation create <已结束认知run-id> --criteria /absolute/path/criteria.json
zest evaluation preview <evaluation-id>
zest evaluation approve <evaluation-id> --approval-sha256 <完整评分包哈希>
zest evaluation report <evaluation-id> --format text
```

create 只读原认知包和结果，准备独立评分快照，不调用模型。preview 包含参考属性、阈值、原候选及状态快照、Skill 全文/哈希、具名 Pydantic Schema、模型目标、预算、每批完整 payload 和真实 SDK 编译出的 HTTP body/URL/哈希。模型密钥不入包。旧认知许可不适用于评分；approve 是新的外发批准，默认同步执行独立 LangGraph 任务，不创建新聊天或后台调查。

`approve --no-run` 只批准，之后用 `evaluation run <id>` 执行。`evaluation reject <id>` 拒绝待批准包。`evaluation show <id>` 查看状态与逐批原始安全收据。`evaluation recover <id>` 仅对已有响应可复用的中断任务重新排队，之后显式 run；pending/无效请求不重发。原源码或知识后来改变，不改写本次已经冻结的历史评分对象；新结果需新评分包。

新记录路由到项目 `.zest/state/evaluations.sqlite3`，独立检查点存于 `evaluation-checkpoints/`。历史调查仍在原位读取，既有项目初始化和管理指引不覆盖。评分只提供 CLI/本地服务层，Web 和 Textual 不增加写入入口。

## 固定准则与样本参考

Skill 固定的是评价方法，不写死某个样本的答案或 10/6 数量：

- 事实、流程、未知边界分别评价；每个参考项按必要属性检查，全部 full 才计完整覆盖。
- 候选结论必须明确表达且有原文支持；“引用里有答案”不等于产出覆盖。组合多条结论可以覆盖，重复不加分。
- 流程按样本实际需要的触发、调用、转换、错误/持久化与返回结果拆必要属性，不要求所有项目使用同一架构。
- 全部 observed 发现都进入独立语义审查，含未自动接收和参考之外的新增事实；模型看不到旧核对意见和自动接收状态。覆盖度候选只取 observed/auto_reviewed 的明确表述，unknowns 类别另含任务的未知陈述。
- 逐条独立命题检查条件、默认值、量词、否定、运行/历史越界与冲突。原系统状态由程序在评分后关联，不能成为模型判正确的理由。
- 参考本身无依据时标 reference_unsupported，保持原分母并阻止整体通过，等待有留痕的新版本参考，不能迁就结果改题。

参考 JSON 示例（替换 commit、来源身份和内容，不是 Finance 样本参考答案）：

```json
{
  "version": 1,
  "commit": "固定的完整commit",
  "reference_phase": "retrospective",
  "items": [
    {
      "id": "G01", "category": "facts", "core": true,
      "attributes": [
        {
          "id": "return", "statement": "明确说明 add 返回 a + b。",
          "sources": [{"source_id": "S2", "start_line": 1, "end_line": 2}]
        }
      ]
    }
  ],
  "thresholds": {
    "facts": 1, "flows": 0, "unknowns": 0,
    "all_core": true, "max_flagged_auto_reviewed_findings": 0
  }
}
```

category 为 facts / flows / unknowns；每项含一个或多个不可遗漏的必要属性，来源限于被评调查冻结的原生文件。程序重新验证包内字节哈希和候选所存行段文本。外部框架语义没有冻结依据时，评分模型应明确证据不足，不得联网或将常识伪装为原文。参考通常应在调查前独立准备冻结，不能给调查 Agent；本模块允许评价历史产出，必须声明 retrospective。pre_run_declared 只是提供方声明，系统记录本次实际冻结时间，不据文件时间证明其在调查之前。当前不提供自动生成金标准或可信时间戳认证。

## 模型输出、分批与收束

CognitionEvaluationResponse 通过 model.with_structured_output 返回具名 CoverageJudgment / FindingJudgment / ClaimCheck / ReviewConflict。每批要求身份完整且唯一；支持判定必须关联本项原文，覆盖度须指出明确表达的候选。整条语义支持沿用 M059 的严格属性门控。

M065 同步移除 ClaimCheck 的 start/end，评分 Skill 按 finding_id 对照完整原句作逐属性判断，coverage_complete 是模型判断，不是程序证明。固定参考、分母、证据与误收门槛保持原样；旧评分包仍可查看，未执行旧包不能在新 Skill/Schema 下外发，须重新准备。见 [职责与验证边界](COGNITION_SEMANTIC_REVIEW.md)。

准备时按最多四个参考属性或两个语义发现尝试分批，合并重复原文并重定位 P 身份；完整真实请求超过 32000 字符就缩批，单项仍超过 32000 但不超过 48000 时冻结为精确扩展清单，未获额外同意不能执行；超过 48000 停止准备，不能截原文或静默丢候选。输出上限每批 8192 tokens。默认所选评分调用上限 32，实际批准预算固定为本包精确批次数，调查预算不变；需要更多时 prepare 显式传 `--max-calls`（最多 128），仍须审阅新包再批准。执行前复核完整请求、Skill、Schema、模型接口和超时；默认字符预算不变；含扩展清单的包须审阅每条完整请求，approve 还必须显式传 `--allow-extended-requests`，决定、清单及包哈希一起留存。没有新增工具权限。

评分请求单独预占并留账，已知响应及校验结果原子保存，再推进 SQLite checkpoint。该初版评分流程 max_format_retries=0；已知无效保留收据后停止，未知请求不重发。新包有新批准，不能通过另建任务暗中替用户补试。模型输出结构、引用或完整性无效都不补写字段。

程序汇总每类完整参考项/固定总项、核心项门槛、自动接收中有问题的发现身份、逐项判定与冲突；全部批次完整且各门槛通过、参考无缺陷时才 overall_passed。未完成报告展示已审查范围和 partial，不把缺失批次算正确。参考门槛控制候选覆盖与误收，不评价整个项目的运行/维护成熟度；原调查的预算、执行与来源工程边界继续依据独立账本核验。

原子项目前按出现次数统计，不自动做跨表述的语义去重，不输出“唯一原子准确率”。同一问题重复出现在两条发现会保留两处身份。因此这些新统计不能冒充 M058 人工去重后的 229/218 分母或替换其历史分数。模型评分不是人工金标准，也不能证明模型整体准确率；当前离线验证只覆盖契约、门控和恢复，真实评分效果需要单独冻结、批准和实测。
