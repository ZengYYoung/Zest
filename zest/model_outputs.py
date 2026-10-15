"""Typed model responses; application validators still enforce evidence and authority.

All fields are required on the model wire.  Explicit research response variants
and tool defaults avoid provider-specific nullable types without changing Zest's
existing tool or result protocols.  No model grants read or modification rights.
"""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    kind: FindingKind = Field(description="observed 为原文直接支持的事实；inferred 为明确标注、仍待核实的推测。")
    statement: str = Field(description="中文发现，仅限实际非空原文支持的固定版本内容；静态配置不能描述为生产运行事实。metadata_observations 中已保存的空文件/空窗口元数据不在此重复输出，其成因或无法回答的问题可留为未知。")
    citations: list[str] = Field(description="至少一个实际 read 返回且含非空原文的唯一 R 证据编号，按结论所需引用、不设额外数量上限；observed/inferred 都不允许空数组。没有这样的证据就不输出该发现；空元数据没有正文编号，不得编造引用。")


class MaterialRequest(OutputModel):
    """A located material need; package membership is resolved by the application."""

    source_id: str = Field(description="批准包内资料填写 material_catalog 中的来源 ID；真正包外资料填写空字符串。是否包内由程序核对，不能因未读或预算用尽将包内来源称为包外。")
    path: str = Field(description="包内资料填写 material_catalog 中的精确项目相对路径；包外资料填写具体文件路径或资料位置，不含凭据。不能用目录或别名替代已知文件路径。")
    reason: str = Field(description="需要该资料回答的具体问题；已读部分窗口仍需续读时说明缺少哪部分。这里只记录需求，不读取、扩展授权或增加调用。")


class InvestigationReport(OutputModel):
    """Static findings and honest unresolved questions at task completion."""

    status: Literal["completed", "partial"] = Field(description="completed 表示目标已回答且无缺口；有未知、包外需求或未完成目标时为 partial。")
    findings: list[ResearchFinding] = Field(description=f"建议约 {POLICY['recommended_report_findings']} 项独立、非空原文支持的发现；条数不是硬上限，也不为凑数输出元数据或无依据推测。元数据已由程序记录，不是此列表的内容；证据不足可少交或为空并交代未知，遵守完整响应字符/token 与总调用预算。")
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


class ClaimCheck(OutputModel):
    """One independently checked assertion within an unchanged finding."""

    start: int = Field(description="原 statement 的 Unicode 字符起始偏移，含起点；与 end 定位被核对的原句，不改写原结论。")
    end: int = Field(description="原 statement 的 Unicode 字符结束偏移，不含终点。逐项按原顺序覆盖整段，包括连接词和标点，不遗漏限定词。")
    subject: str = Field(description="该独立命题的具体对象，如函数、配置项或业务步骤。")
    attribute: str = Field(description="单独核对的属性，如生命周期、错误分支、返回值；不能把多个独立属性打包。")
    condition: str = Field(description="原断言的触发条件或适用范围；原句无条件写明无条件，不得偷偷补入正确条件来替原断言解围。")
    assertion: str = Field(description="原句对该属性的断言，忠实保留否定、数量和条件；不是修正后的正确答案。")
    evidence_ids: list[str] = Field(description="仅可引用该 finding.evidence_ids 中的 P 原文身份；支持判定至少一项，其他发现和 unknowns 不是原文证据。")
    verdict: Literal["supported", "contradicted", "insufficient_evidence"] = Field(description="supported 为关联原文完整支持；contradicted 为存在相反证据；insufficient_evidence 包括语义依赖、边界或条件没有依据，不能仅因配置值吻合而支持行为解释。")
    reason: str = Field(description="该属性的支持点或具体错误/缺口；区分源码明示与外部框架默认语义，不假称外部知识是包内原文。")


class ReviewConflict(OutputModel):
    """Unresolved conflict with another finding or a located report unknown."""

    reference_id: str = Field(description="冲突的另一 finding_id 或 report_unknowns 的 reference_id；不能引用自己或编造身份。")
    reason: str = Field(description="说明同一对象、属性及条件下为何相互不兼容；其他任务缺少阅读本身不是冲突，也不将未知当事实。")


class FindingJudgment(OutputModel):
    """Attribute checks and conflicts; whole-finding acceptance is computed locally."""

    finding_id: str = Field(description="本批待核对发现的原始 finding_id；每个身份必须恰好输出一次。")
    checks: list[ClaimCheck] = Field(description="逐个独立命题核对全部原文，包括括号内解释、条件、否定和数量；每项用原始字符范围定位，不能只核对大致正确的主干。")
    coverage_complete: bool = Field(description="是否已逐项覆盖全部独立属性；不能确认拆分完整时为 false，程序的字符覆盖检查不能证明语义拆分完整。")
    conflicts: list[ReviewConflict] = Field(description="与本批、other_findings 或 report_unknowns 中明确陈述的未解决矛盾；无矛盾为空。线索不作为支持证据，冲突双方及来源须保留。")
    reason: str = Field(description="中文核对理由，说明原文支持点或具体不足，不根据多个 Agent 一致就认定正确。")


class ProjectReviewResponse(OutputModel):
    """Review only requested findings against their supplied original evidence."""

    judgments: list[FindingJudgment] = Field(description="逐一核对本批 finding_id；other_findings 仅用于检查矛盾，不为其输出 judgments。")


class CoverageJudgment(OutputModel):
    """Independent coverage of one frozen required property."""

    id: str = Field(description="本批 requirements 的精确属性身份，每个身份恰好一次。")
    verdict: Literal["full", "partial", "missing", "reference_unsupported"] = Field(description="full 为候选结论明确完整表达且原文支持；partial 为部分表达或缺边界；missing 为没有表达；reference_unsupported 为参考本身没有原文支持，不私自改参考。")
    finding_ids: list[str] = Field(description="明确表达该属性的 candidates.finding_id；full/partial 至少一项，不能只因引用中存在答案就算覆盖。")
    evidence_ids: list[str] = Field(description="该 requirement.evidence_ids 内支持判断的固定原文 P 身份；full/partial 至少一项，禁止包外引用。")
    reason: str = Field(description="说明参考是否有依据、候选明确表达了什么以及具体缺口；不自报总分或更改分母。")


class CognitionEvaluationResponse(OutputModel):
    """Independent evaluation, separate from investigation and knowledge approval."""

    coverage: list[CoverageJudgment] = Field(description="仅评价本批 requirements；每个属性恰好一次，空批返回空列表。")
    judgments: list[FindingJudgment] = Field(description="仅评价本批 findings 的全部独立属性与冲突；每个发现恰好一次，空批返回空列表。原系统核对结果不作为正确依据。")


OUTPUT_MODELS = {
    "qa": QAResponse,
    "repair_proposal": RepairProposalResponse,
    "project_plan": ProjectPlanResponse,
    "project_research": ProjectResearchResponse,
    "project_report": ProjectReportResponse,
    "project_review": ProjectReviewResponse,
    "cognition_evaluation": CognitionEvaluationResponse,
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
