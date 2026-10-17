"""Typed model responses; application validators still enforce evidence and authority.

All fields are required on the model wire.  Explicit research response variants
and tool defaults avoid provider-specific nullable types without changing Zest's
existing tool or result protocols.  No model grants read or modification rights.
"""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .cognition_contracts import POLICY


def _provider_literals(schema):
    """Use supported single-value enums instead of JSON Schema ``const``."""
    if isinstance(schema, dict):
        if "const" in schema:
            schema["enum"] = [schema.pop("const")]
        for value in schema.values():
            _provider_literals(value)
    elif isinstance(schema, list):
        for value in schema:
            _provider_literals(value)


class OutputModel(BaseModel):
    """Closed response object with strict local type validation."""

    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra=_provider_literals)


Section = Literal["purpose", "architecture", "flows", "dependencies", "development", "constraints"]
FindingKind = Literal["observed", "inferred"]
InitialTaskId = Literal["T1", "T2", "T3", "T4"]


class QAResponse(OutputModel):
    """Answer based exclusively on the supplied project evidence."""

    status: Literal["answered", "insufficient_evidence"] = Field(
        description="answered 表示已有证据支持答案；insufficient_evidence 表示仍缺必要资料。")
    answer: str = Field(description="中文回答，区分已观察事实和推测；需要时以 [E编号] 引用提供的来源。")
    citations: list[str] = Field(description="实际用于回答的 sources 来源 ID；answered 至少引用一个已提供的来源。")
    unknowns: list[str] = Field(description="尚未查清或缺少证据的问题；没有缺口时返回空数组。")


class CompleteFileChange(OutputModel):
    """Full replacement content for an approved full-file or new-file source."""

    source_id: str = Field(description="获准可编辑的完整文件或 planned_new_file 来源 ID，不能自行指定路径。")
    content: str = Field(description="修改后的完整文件正文，保留未修改代码、必要功能与换行；允许空正文。")


class AnchorFileChange(OutputModel):
    """Exact replacement within an approved anchor-mode source excerpt."""

    source_id: str = Field(description="获准可编辑且 edit_mode=anchor 的来源 ID，不能自行指定路径。")
    old_text: str = Field(description="批准片段内精确出现一次的非空旧原文；不得扩展到片段外。")
    new_text: str = Field(description="用于替换 old_text 的精确新正文；可以为空以删除锚点内容。")


class RepairProposalResponse(OutputModel):
    """Candidate edits for separate human approval, never direct execution."""

    status: Literal["proposed", "insufficient_evidence"] = Field(
        description="proposed 表示提出具体候选修改；insufficient_evidence 表示证据不足，不能提出修改。")
    summary: str = Field(description="中文说明问题、最小修改和预期行为，不声称已执行或验证。")
    changes: list[CompleteFileChange | AnchorFileChange] = Field(
        description="最多五个获准来源的候选修改，来源不能重复；按各来源 edit_mode 返回完整正文或锚点替换。证据不足时为空。")
    unknowns: list[str] = Field(description="缺失的必要信息；proposed 时必须为空，证据不足时必须说明至少一项缺口。")


class PreliminaryFinding(OutputModel):
    """Planning-stage observation or inference, not verified shared knowledge."""

    statement: str = Field(description="根据摸底资料形成的中文初步认识，限定在当前固定版本，不等于已核对知识。")
    kind: FindingKind = Field(description="observed 表示提供资料直接可见；inferred 表示尚待任务调查确认的推测。")
    source_ids: list[str] = Field(description="至少一个实际提供的 material_catalog 来源 ID，不能重复；按认识所需列出，不设额外数量上限。")


class InvestigationTask(OutputModel):
    """Bounded static investigation assigned to a background task agent."""

    id: InitialTaskId = Field(description="本轮唯一调查身份 T1、T2、T3 或 T4；不能重复。")
    title: str = Field(description="简短中文任务标题，最多 160 字符。")
    goal: str = Field(description="具体调查目标，说明要查清的行为、职责或关系，不要求执行或修改项目。")
    reason: str = Field(description="为何该目标对项目认知重要，以及当前摸底资料的不足。")
    source_ids: list[str] = Field(description="实际提供的 material_catalog 中至少一个唯一的起始来源 ID，按任务需要选择、不设额外数量上限；后续只能在批准包内自主查找。")
    sections: list[Section] = Field(description="该任务覆盖的六部分档案 ID，至少一项；不能重复。")
    deliverable: str = Field(description="预期交付的具体调查结论及证据要求，区分事实、推测和未知。")
    acceptance: list[str] = Field(description="至少一条可审阅的完成标准，以字符串逐条说明。")
    depends_on: list[InitialTaskId] = Field(description="需先完成的本轮任务 ID；无依赖时为空，不能自依赖或形成环。")
    priority: int = Field(description="整数优先级 1、2 或 3，1 为最高优先级。", json_schema_extra={"enum": [1, 2, 3]})

    @field_validator("priority")
    @classmethod
    def supported_priority(cls, value):
        """Reject priorities outside the explicitly supported execution order."""
        if value not in (1, 2, 3):
            raise ValueError("priority must be 1, 2 or 3")
        return value


class PlanGap(OutputModel):
    """Uncovered dossier topic that remains a question."""

    section: Section = Field(description="尚缺调查覆盖的六部分档案 ID。")
    question: str = Field(description="该部分仍需回答的具体中文问题，不能虚构历史或运行结论。")


class ProjectPlanResponse(OutputModel):
    """Initial understanding, investigation plan and explicit coverage gaps."""

    overview: list[PreliminaryFinding] = Field(description="最多六项初步认识；不直接写入已核对共享知识。")
    tasks: list[InvestigationTask] = Field(description="一至四个静态调查任务，可以并行或有依赖；不按六部分机械拆分。")
    gaps: list[PlanGap] = Field(description="最多十二项缺口；tasks 和 gaps 合起来必须交代六部分档案的覆盖。")

    @model_validator(mode="before")
    @classmethod
    def discard_unused_plan_fields(cls, value):
        """Accept inert metadata only here; keep required fields and types strict.

        The provider still receives a closed schema. Work on copies so the
        original safe receipt retains every field, including ignored metadata.
        Shared nested types remain closed when used by other response models.
        """
        if not isinstance(value, dict):
            return value
        plan = {name: item for name, item in value.items() if name in cls.model_fields}
        for field, model in (("overview", PreliminaryFinding), ("tasks", InvestigationTask), ("gaps", PlanGap)):
            if isinstance(plan.get(field), list):
                plan[field] = [
                    {name: item for name, item in row.items() if name in model.model_fields}
                    if isinstance(row, dict) else row
                    for row in plan[field]
                ]
        return plan


class FollowupPlanResponse(OutputModel):
    """Plan source leads for one frozen follow-up goal, not a new project survey."""

    overview: list[PreliminaryFinding] = Field(description="最多六条与 followup_context 选定问题直接相关的初步观察或推测；只引用本次目录身份，旧结论及核对意见不是原文。")
    source_ids: list[str] = Field(description="本次 material_catalog 中至少一个唯一的起始来源 ID；按定向问题选择，后续仍可在完整批准包内自主查找。")
    gaps: list[str] = Field(description="最多十二项与选定问题有关的具体缺口；不为其他五部分档案补写全项目缺口。没有缺口为空。任务目标、完成标准、身份和档案部分由固定包决定，不能扩成旧任务的大范围目标。")


class ListFilesArgs(OutputModel):
    """Query and pagination for listing files inside the approved package."""

    query: str = Field(description="按相对路径进行大小写不敏感的子串筛选；空字符串表示全部文件。")
    offset: int = Field(description="非负分页偏移；0 表示从第零项开始。")


class SearchArgs(OutputModel):
    """Lexical search query; locations still require original-text reading."""

    query: str = Field(description="非空词法检索关键词，最多 200 字符；检索结果仅供定位，不能直接作为原文证据。")


class ReadArgs(OutputModel):
    """Original-text range for one source in the approved material package."""

    source_id: str = Field(description="批准包内实际存在的文件来源 ID，不接受包外文件或自行指定的路径。")
    start_line: int = Field(description="从 1 开始的原文起始行号；从文件头读取时填 1。")
    end_line: int = Field(description=f"包含在内的结束行号，不得早于起始行；0 表示采用工具默认窗口，单次最多 {POLICY['read_max_lines']} 行及 {POLICY['read_chars']} 字符，不截半行。")


class SymbolsArgs(OutputModel):
    """Query over Python declaration names; declarations are not behavior proof."""

    query: str = Field(description="非空 Python 声明名称关键词，最多 200 字符；声明地图不是运行或业务行为证据。")


class ListFilesAction(OutputModel):
    """Read-only file listing action within the approved package."""

    tool: Literal["list_files"] = Field(description="固定为 list_files，仅列出批准包内文件。")
    args: ListFilesArgs = Field(description="文件名筛选和分页参数；默认查询填空字符串，默认分页填 0。")


class SearchAction(OutputModel):
    """Read-only lexical retrieval action within the approved package."""

    tool: Literal["search"] = Field(description="固定为 search，使用词法 TF-IDF 定位批准包内内容。")
    args: SearchArgs = Field(description="明确的非空检索关键词参数。")


class ReadAction(OutputModel):
    """Read-only original-source action, the basis for evidence citations."""

    tool: Literal["read"] = Field(description="固定为 read，读取批准包内原文并生成可引用的 R 证据编号。")
    args: ReadArgs = Field(description="来源身份及原文行范围；默认起始行填 1，默认结束窗口填 0。")


class SymbolsAction(OutputModel):
    """Read-only Python declaration lookup action within the approved package."""

    tool: Literal["symbols"] = Field(description="固定为 symbols，只查找批准包内 Python 声明。")
    args: SymbolsArgs = Field(description="Python 声明名称查询参数。")


class ResearchFinding(OutputModel):
    """A text-grounded finding; saved file metadata is a separate input channel."""

    section: Section = Field(description="该发现所属的六部分档案 ID。")
    kind: FindingKind = Field(description="本项只有一种性质：observed 为原文直接且充分支持的事实或概括；有依据但需要推断的概括单独写 inferred，说明推断依据；无法判断或没有依据的猜测留 unknowns，不混入 observed 或用 inferred 凑数。")
    statement: str = Field(description="固定版本有依据的独立发现，服务于读者理解模块职责、协作与核心流程，而不止罗列名称。相关流程步骤可连贯概括或由数条共同说明，不为拆分丢失因果；不同依据或强度的断言分条并分别给依据。只说明项目实际存在且影响理解的关键行为，不套统一功能清单、不以测试输入/调用冒充结果已验证或已运行通过。元数据不重复为正文发现，证据不足留未知。")
    citations: list[str] = Field(description="至少一个实际 read 返回且含非空原文的唯一 R 证据编号，按结论所需引用、不设额外数量上限；observed/inferred 都不允许空数组。没有这样的证据就不输出该发现；空元数据没有正文编号，不得编造引用。")


class MaterialRequest(OutputModel):
    """A located material need; package membership is resolved by the application."""

    source_id: str = Field(description="批准包内资料填写 material_catalog 中的来源 ID；真正包外资料填写空字符串。是否包内由程序核对，不能因未读或预算用尽将包内来源称为包外。")
    path: str = Field(description="包内资料填写 material_catalog 中的精确项目相对路径；包外资料填写具体文件路径或资料位置，不含凭据。不能用目录或别名替代已知文件路径。")
    reason: str = Field(description="需要该资料回答的具体问题；已读部分窗口仍需续读时说明缺少哪部分。这里只记录需求，不读取、扩展授权或增加调用。")


class InvestigationReport(OutputModel):
    """Static findings and honest unresolved questions at task completion."""

    status: Literal["completed", "partial"] = Field(description="completed 表示目标已回答且无缺口；有未知、包外需求或未完成目标时为 partial。")
    findings: list[ResearchFinding] = Field(description=f"交付可理解的模块协作与代表性流程，不能用入口/名称清单代替主要处理与结果；按项目实际情况选择主干，保留各项依据与性质。建议约 {POLICY['recommended_report_findings']} 项并非硬上限，不为凑数混合不同强度或输出无依据推测。证据不足可少交或为空并交代具体未知，遵守完整响应字符/token 与总调用预算。")
    unknowns: list[str] = Field(description=f"最多 {POLICY['report_unknowns']} 项尚未回答的问题或预算造成的缺口；不把推测当事实。空文件/窗口本身已作为元数据记录，其成因/意图或因此无法回答的问题可保持未知，不必把已知元数据假装为未知。")
    material_requests: list[MaterialRequest] = Field(description=f"最多 {POLICY['report_material_requests']} 项尚需读取的具体资料，包内待补读与真正包外需求统一列出位置/来源/原因；程序对照批准包分类，不将未读等同包外。没有需求时为空。")


class ContinueInvestigation(OutputModel):
    """Continue investigating with approved read-only tool actions."""

    kind: Literal["actions"] = Field(description="固定为 actions，表示调查尚未结束，需要继续读取批准资料。")
    actions: list[ListFilesAction | SearchAction | ReadAction | SymbolsAction] = Field(
        description="一至四个批准工具动作；禁止执行、修改及包外访问。此变体不包含最终报告。")


class FinishInvestigation(OutputModel):
    """Finish investigating and deliver evidence-aware findings and gaps."""

    kind: Literal["report"] = Field(description="固定为 report，表示调查已收束并提交最终报告，不再调用工具。")
    report: InvestigationReport = Field(description="依据实际读取原文交付完整调查报告，诚实保留未知和包外需求。")


class ProjectResearchResponse(OutputModel):
    """Explicit wire envelope for continuing or completing an investigation."""

    response: ContinueInvestigation | FinishInvestigation = Field(
        description="选择一个互斥的响应变体：kind=actions 时提供工具动作；kind=report 时提供最终报告。不得混合两种变体。")


class ProjectReportResponse(OutputModel):
    """Reserved closing turn: report findings and gaps without more tool calls."""

    response: FinishInvestigation = Field(description="本次只提交 kind=report 的最终报告；依据已读原文保留发现与未知，不再请求工具。")


class _ClaimAssertion(OutputModel):
    """Shared assertion fields; evidence observation belongs to scoring only."""

    subject: str = Field(description="该独立命题的具体对象，如函数、配置项或业务步骤。")
    attribute: str = Field(description="单独核对的属性，如生命周期、错误分支、返回值；不能把多个独立属性打包。")
    condition: str = Field(description="原断言的触发条件或适用范围；原句无条件写明无条件，不得偷偷补入正确条件来替原断言解围。")
    assertion: str = Field(description="原句对该属性的断言，忠实保留否定、数量、条件和结论强度；不能改成只陈述证据可见的较弱说法后判支持，也不补入原句未声称的内容。")
    evidence_ids: list[str] = Field(description="仅可引用该 finding.evidence_ids 中的 P 原文身份；支持判定至少一项，其他发现和 unknowns 不是原文证据。")


class ClaimCheck(_ClaimAssertion):
    """One independently checked assertion within an unchanged finding."""

    verdict: Literal["supported", "contradicted", "insufficient_evidence"] = Field(description="supported 须关联原文完整支持原属性及其条件；只支持一部分、所需窗口/条件/语义依据缺失则 insufficient_evidence，不以 supported 加理由说明不足。contradicted 须有相反证据，未见证据本身不是反驳。检查完但证据不足仍用 insufficient_evidence，与 coverage_complete 分开。")
    reason: str = Field(description="与本项 verdict 一致地说明完整支持点、相反证据或具体缺口；区分输入/声明、结果断言和实际运行记录，只依据该项获准证据，不假称外部知识或测试意图证明更强结论。")


class EvaluationClaimCheck(_ClaimAssertion):
    """Observe permitted evidence before judging one original assertion property."""

    attribute: str = Field(description="一个可独立判真的属性；具体值与对值的描述分别核对，不能因字面量相同就合并其大小、单位、空白、范围或行为解释。只检查原句实际声称的属性。")
    evidence_observation: str = Field(description="先独立描述本项获准 P 原文实际显示的相关值、行为或条件；缺少所需内容时明确写可见范围和缺口。保留影响本属性的符号、单位、边界和空白区别，不照抄待评断言作观察，不借用其他发现/未知作事实，不提前写支持结论。不要求字符位置；非空且最多800字符。它仍是模型对原文的描述，不是程序认证。")
    verdict: Literal["supported", "contradicted", "insufficient_evidence"] = deepcopy(ClaimCheck.model_fields['verdict'])
    reason: str = Field(description="将 evidence_observation 中的实际证据与 assertion 的原属性/条件直接比较，解释一致、相反或缺口，再与 verdict 保持一致；不因值能对上就默认描述和概括也成立。")

    @field_validator('evidence_observation')
    @classmethod
    def nonempty_observation(cls, value):
        if not value.strip() or len(value) > 800:
            raise ValueError('evidence observation must be nonempty and at most 800 characters')
        return value


class ReviewConflict(OutputModel):
    """Unresolved conflict with another finding or a located report unknown."""

    reference_id: str = Field(description="冲突的另一 finding_id 或 report_unknowns 的 reference_id；不能引用自己或编造身份。")
    reason: str = Field(description="说明同一对象、属性及条件下为何相互不兼容；其他任务缺少阅读本身不是冲突，也不将未知当事实。")


class FindingJudgment(OutputModel):
    """Attribute checks and conflicts; whole-finding acceptance is computed locally."""

    finding_id: str = Field(description="本批待核对发现的原始 finding_id；每个身份必须恰好输出一次。")
    checks: list[ClaimCheck] = Field(description="围绕 finding_id 对应的完整原结论，逐个独立属性核对，包括括号内解释、条件、否定和数量；不返回字符位置，不修正或替换原断言。")
    coverage_complete: bool = Field(description="仅表示模型是否已逐项检查原结论的全部独立属性，包括否定/数量/条件/概括；全部已检查时即使有反驳或证据不足也可 true，分别在各项 verdict 标明。遗漏或不能确认拆分完整则 false；不是证据充分或整条正确的标志，也不是程序证明。")
    conflicts: list[ReviewConflict] = Field(description="与本批、other_findings 或 report_unknowns 中明确陈述的未解决矛盾；无矛盾为空。线索不作为支持证据，冲突双方及来源须保留。")
    reason: str = Field(description="中文核对理由，说明原文支持点或具体不足，不根据多个 Agent 一致就认定正确。")


class ProjectReviewResponse(OutputModel):
    """Review only requested findings against their supplied original evidence."""

    judgments: list[FindingJudgment] = Field(description="逐一核对本批 finding_id；other_findings 仅用于检查矛盾，不为其输出 judgments。")


class CoverageJudgment(OutputModel):
    """Independent coverage of one frozen required property."""

    id: str = Field(description="本批 requirements 的精确属性身份，每个身份恰好一次。")
    finding_ids: list[str] = Field(description="自主从本批全部 candidates 中选择实际表达本属性的 finding_id，无需预先指派或同时出现在 findings。full/partial 至少一项；candidates 为空或确无表达才可空，不以源码有答案代替候选表达。")
    candidate_observation: str = Field(description="简述候选实际表达的主干及缺口，不把源码内容补成候选已表达。不要求逐字摘抄或字符位置；可留空，说明缺省不改变评分身份或判定。")
    evidence_ids: list[str] = Field(description="该 requirement.evidence_ids 内支持判断的固定原文 P 身份；full/partial 至少一项，禁止包外引用。")
    verdict: Literal["full", "partial", "missing", "reference_unsupported"] = Field(description="根据候选表达观察与固定原文对比后判定：full 须全部必要主干已表达且有支持；partial 为候选只表达部分或缺必要边界；missing 为没有表达；reference_unsupported 为参考本身没有原文依据。源码完整不自动使候选full；不得与观察或理由中的主干缺口矛盾。")
    reason: str = Field(description="说明原文支持什么，再与 candidate_observation 中候选实际表达比较，解释本项判定及必要主干缺口；不以源码复述代替候选覆盖，不在理由承认必要主干缺失后标full。轻微措辞和非关键细节遵循冻结准则，不自报总分或更改分母。")


class EvaluationFindingJudgment(FindingJudgment):
    """Scoring-only attribute checks; the project review wire remains unchanged."""

    checks: list[EvaluationClaimCheck] = Field(description="逐项检查完整原结论的独立属性；每项先描述证据观察，再比较并判定；将具体值和描述它的属性分别核对，不遗漏否定、条件、量词和概括。")


class CognitionEvaluationResponse(OutputModel):
    """Independent evaluation, separate from investigation and knowledge approval."""

    coverage: list[CoverageJudgment] = Field(description="身份集合必须与本批 requirements.id 完全相同；每个属性恰好一次，requirements 为空则返回空列表。candidates 仅供判断覆盖，不新增评分目标。")
    judgments: list[EvaluationFindingJudgment] = Field(description="身份集合必须与本批 findings.finding_id 完全相同；逐项先描述证据再判断原属性与冲突，findings 为空则返回空列表。不为 other_findings、report_unknowns 或 candidates 输出 judgment；对方矛盾只放入目标的 conflicts。解释使用 reason，不新增 note 等字段。原系统核对结果不作为正确依据。")

    @model_validator(mode="before")
    @classmethod
    def normalize_evaluation_fields(cls, value):
        """Normalize inert presentation while preserving raw model receipts.

        Never invent a target or verdict. Required identities and references
        remain checked per target; omitted targets stay ungraded.
        """
        def project(row, model):
            if not isinstance(row, dict):
                return row
            return {name: item for name, item in row.items() if name in model.model_fields}

        result = project(value, cls)
        if not isinstance(result, dict):
            return result
        result = {'coverage': [], 'judgments': [], **result}
        for field, model in (("coverage", CoverageJudgment), ("judgments", EvaluationFindingJudgment)):
            if result[field] is None: result[field] = []
            if isinstance(result[field], dict): result[field] = [result[field]]
            if isinstance(result.get(field), list):
                result[field] = [project(row, model) for row in result[field]]
        if isinstance(result['coverage'], list):
            result['coverage'] = [
                {'finding_ids': [], 'evidence_ids': [], 'candidate_observation': '', 'reason': '', **row}
                if isinstance(row, dict) else row for row in result['coverage']]
            for row in result['coverage']:
                if isinstance(row, dict):
                    for field in ('candidate_observation', 'reason'):
                        if not isinstance(row[field], str): row[field] = ''
                    for field in ('finding_ids', 'evidence_ids'):
                        if isinstance(row[field], str): row[field] = [row[field]]
                    if isinstance(row.get('verdict'), str): row['verdict'] = row['verdict'].strip().lower()
        if isinstance(result.get("judgments"), list):
            for row in result["judgments"]:
                if isinstance(row, dict):
                    for field, model in (("checks", EvaluationClaimCheck), ("conflicts", ReviewConflict)):
                        if isinstance(row.get(field), list):
                            row[field] = [project(item, model) for item in row[field]]
        return result


class ResolutionCheck(OutputModel):
    """One old assertion attribute answered by freshly reviewed findings."""

    subject: str = Field(description="本项核对的对象。")
    attribute: str = Field(description="对象的独立属性，不能用主题近似代替。")
    condition: str = Field(description="断言成立的完整条件或边界。")
    assertion: str = Field(description="旧断言中本项需要确认或纠正的内容。")
    outcome: Literal["confirmed", "corrected", "unresolved"] = Field(description="完整确认、完整纠正/补全，或仍未解决；未知问题回答用 corrected。")
    finding_ids: list[str] = Field(description="明确回答本属性的本次 candidates 身份；已处理项至少一个。")
    evidence_ids: list[str] = Field(description="上述候选实际支持该判定的 P 原文身份；已处理项至少一个。")
    reason: str = Field(description="逐项解释新旧关系及原文支持；未解决时说明缺口。")


class ResolutionJudgment(OutputModel):
    """Complete response to one old finding or located unknown."""

    reference_id: str = Field(description="提供的旧发现或未知项身份，必须恰好判断一次。")
    checks: list[ResolutionCheck] = Field(description="通过 reference_id 关联完整旧结论，按独立对象、属性、条件逐项判断；至少一项，不计算字符位置。")
    coverage_complete: bool = Field(description="模型判断所有语义属性、边界和旧核对意见是否已处理；不确定或遗漏时为 false，不是程序证明的完整性。")
    reason: str = Field(description="总体说明本问题是否已完整回答，不替代逐项证据。")


class ResolutionConflict(OutputModel):
    """Explicit disposition of one frozen conflict edge."""

    left: str = Field(description="提供的冲突 left 身份，不能自行增加、删除或交换。")
    right: str = Field(description="提供的冲突 right 身份。")
    verdict: Literal["resolved", "unresolved"] = Field(description="两端属性与关系均已处理才 resolved；否则保留 unresolved。")
    finding_ids: list[str] = Field(description="解释冲突的本次候选身份；resolved 至少一个。")
    evidence_ids: list[str] = Field(description="关联候选中实际用于消解冲突的 P 原文身份。")
    reason: str = Field(description="说明哪方错误或各自成立的条件；未解决时说明缺口。")


class CognitionResolutionResponse(OutputModel):
    """Proposal only; application gates and explicit human adoption control effects."""

    judgments: list[ResolutionJudgment] = Field(description="完整判断所有旧 references，不遗漏或重复。")
    conflicts: list[ResolutionConflict] = Field(description="完整判断提供的冲突关系，无冲突时为空。")


OUTPUT_MODELS = {
    "qa": QAResponse,
    "repair_proposal": RepairProposalResponse,
    "project_plan": ProjectPlanResponse,
    "project_followup_plan": FollowupPlanResponse,
    "project_research": ProjectResearchResponse,
    "project_report": ProjectReportResponse,
    "project_review": ProjectReviewResponse,
    "cognition_evaluation": CognitionEvaluationResponse,
    "cognition_resolution": CognitionResolutionResponse,
}


def model_for(purpose):
    """Return the explicit response model for a supported model purpose."""
    return OUTPUT_MODELS[purpose]


def get_output_schema(purpose):
    """Return a closed, inlined schema for approval and provider-contract review.

    These models have no recursive references.  Expanding them avoids depending
    on provider-specific support for either ``$defs`` or ``$def`` naming.
    """
    schema = model_for(purpose).model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ValueError("unexpected output schema reference")
            target = deepcopy(definitions[reference.removeprefix("#/$defs/")])
            target.update({key: item for key, item in value.items() if key != "$ref"})
            return expand(target)
        return {key: expand(item) for key, item in value.items()}

    return expand(schema)


def output_to_protocol(response):
    """Serialize a typed result, restoring existing task and tool protocols."""
    if not isinstance(response, tuple(OUTPUT_MODELS.values())):
        raise TypeError("expected a supported typed model response")
    output = response.model_dump(mode="json")
    if isinstance(response, (ProjectResearchResponse, ProjectReportResponse)):
        variant = output["response"]
        if isinstance(response.response, FinishInvestigation):
            return {"actions": [], "report": variant["report"]}
        output = {"actions": variant["actions"], "report": None}
        for action in output["actions"]:
            if action["tool"] == "read" and action["args"]["end_line"] == 0:
                del action["args"]["end_line"]
    return output
