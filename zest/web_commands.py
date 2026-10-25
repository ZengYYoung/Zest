"""Validated Web commands, delegating all business decisions to CLI services."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .files import FileReadError
from .storage import ProjectRegistry


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(Input):
    project_id: UUID


class DocumentRoundInput(ProjectInput):
    source_run_id: UUID
    source_sha256: str = Field(min_length=64, max_length=64, pattern='^[0-9a-f]{64}$')


class ImportInput(Input):
    path: str = Field(min_length=1, max_length=4096)


class AutomaticImportInput(ImportInput):
    automatic_understanding: bool = Field(default=False, strict=True)


class InitInput(ImportInput):
    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=4000)
    author_name: str | None = Field(default=None, max_length=200)
    author_email: str | None = Field(default=None, max_length=200)


class SourceRange(Input):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class TaskInput(ProjectInput):
    question: str = Field(min_length=1, max_length=4000)
    files: list[str] = Field(default_factory=list, max_length=40)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    retrieval: Literal["keyword", "lexical"] = "keyword"
    strategy: Literal["single", "multi"] = "single"
    purpose: Literal["question", "repair_proposal"] = "question"
    new_files: list[str] = Field(default_factory=list, max_length=5)
    ranges: list[SourceRange] = Field(default_factory=list, max_length=10)


class ConversationMessage(Input):
    id: UUID
    message_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    automatic_answer: bool = Field(default=True, strict=True)


class ConversationAnswer(Input):
    id: UUID
    message_id: UUID


class InitializationPrepare(ProjectInput):
    replace_pending: bool = False


class InitializationStart(ProjectInput):
    resume_setup: bool = Field(default=False, strict=True)


class AutomaticRepairInput(InitializationPrepare):
    automatic_understanding: bool = Field(default=False, strict=True)


class UnderstandingConfiguration(ProjectInput):
    enabled: bool = Field(strict=True)


class InitializationDecision(ProjectInput):
    approved: bool
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    automatic_maintenance: bool = False


class InitializationContext(InitializationDecision):
    actor: str = Field(pattern=r"^(coordinator|T[1-6]|review_(?:fix_)?[0-9a-f]{12})$")
    sequence: int = Field(ge=0)


class ContextIdentity(Input):
    actor: str = Field(pattern=r"^(coordinator|T[1-6]|review_(?:fix_)?[0-9a-f]{12})$")
    sequence: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ContextDecisions(ProjectInput):
    approved: bool = Field(strict=True)
    requests: list[ContextIdentity] = Field(min_length=1, max_length=32)


class RecordInput(Input):
    id: UUID


class DecisionInput(RecordInput):
    approved: bool = Field(strict=True)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ContextDecision(DecisionInput):
    actor: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=0)


class FollowupInput(RecordInput):
    finding_id: str = Field(min_length=1, max_length=160)


class InvalidateFinding(FollowupInput):
    reason: str = Field(min_length=1, max_length=1000)


class VerificationInput(RecordInput):
    image: str = Field(min_length=1, max_length=256)
    timeout: int = Field(default=30, ge=1, le=120)


class KnowledgeInput(ProjectInput):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    kind: Literal["decision", "constraint", "maintenance"] = "decision"
    scope: Literal["snapshot", "project"] = "snapshot"


class InvalidateInput(RecordInput):
    reason: str = Field(min_length=1, max_length=1000)


class EvaluationInput(RecordInput):
    criteria: dict
    max_calls: int = Field(default=32, ge=1, le=128)


class ExtendedDecision(DecisionInput):
    allow_extended_requests: bool = Field(default=False, strict=True)


class AdoptInput(RecordInput):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExportInput(ProjectInput):
    destination: str = Field(min_length=1, max_length=4096)


COMMANDS = {
    "model.prepare": Input, "model.decide": DecisionInput,
    "project.import": AutomaticImportInput, "project.init": InitInput,
    "project.remove": ProjectInput,
    "project.choose-directory": Input,
    "project.documents.init": ProjectInput,
    "conversation.create": ProjectInput, "conversation.send": ConversationMessage, "conversation.answer": ConversationAnswer,
    "initialization.prepare": InitializationPrepare, "initialization.decide": InitializationDecision,
    "initialization.start": ProjectInput, "initialization.recover": ProjectInput,
    "initialization.initialize": InitializationStart,
    "initialization.repair": AutomaticRepairInput, "initialization.republish": ProjectInput,
    "understanding.configure": UnderstandingConfiguration,
    "initialization.context-decide": InitializationContext,
    "initialization.context-decide-many": ContextDecisions,
    "maintenance.decide": InitializationDecision, "maintenance.context-decide": InitializationContext,
    "maintenance.resume": ProjectInput, "maintenance.reprepare": ProjectInput,
    "maintenance.documents-new-round": DocumentRoundInput,
    "maintenance.context-decide-many": ContextDecisions,
    "task.create": TaskInput, "task.decide": DecisionInput,
    "cognition.create": ProjectInput, "cognition.decide": DecisionInput,
    "cognition.start": RecordInput, "cognition.recover": RecordInput,
    "cognition.context-decide": ContextDecision,
    "cognition.refresh": ProjectInput, "cognition.followup": FollowupInput,
    "cognition.invalidate": InvalidateFinding,
    "repair.decide": DecisionInput,
    "verification.create": VerificationInput, "verification.decide": DecisionInput,
    "release.promote": RecordInput, "release.rollback": RecordInput,
    "release.decide": DecisionInput, "release.recover": RecordInput,
    "knowledge.add": KnowledgeInput, "knowledge.confirm": RecordInput,
    "knowledge.invalidate": InvalidateInput, "release.capture": RecordInput,
    "index.build": ProjectInput, "survey.build": ProjectInput,
    "evaluation.create": EvaluationInput, "evaluation.decide": ExtendedDecision,
    "evaluation.run": RecordInput, "evaluation.recover": RecordInput,
    "resolution.create": RecordInput, "resolution.decide": ExtendedDecision,
    "resolution.run": RecordInput, "resolution.recover": RecordInput, "resolution.adopt": AdoptInput,
    "release.export": ExportInput, "release.verify-export": ImportInput,
}

Action = Literal[tuple(COMMANDS)]


class Command(Input):
    request_id: UUID
    action: Action
    parameters: dict


def validate_command(action, parameters):
    from pydantic import ValidationError
    try:
        return COMMANDS[action].model_validate(parameters).model_dump(mode="json")
    except (KeyError, ValidationError) as exc:
        raise FileReadError("invalid_web_command", "操作参数不符合要求，请检查必填字段、记录身份和哈希。") from exc


def execute(state_dir, action, p):
    """Called only once by a claimed durable operation; never from a GET."""
    from .model_settings import model_settings_scope
    with model_settings_scope(state_dir):
        return _execute(state_dir, action, p)


def _execute(state_dir, action, p):
    if action == "project.choose-directory":
        from .directory_picker import choose_project_directory
        return choose_project_directory(state_dir)
    if action.startswith("model."):
        from .model_connection import ModelConnectionStore
        store = ModelConnectionStore(state_dir)
        if action == "model.prepare":
            return store.prepare()
        return store.decide(p["id"], approved=p["approved"], sha256=p["sha256"])
    registry = ProjectRegistry(state_dir)

    if action == "project.remove":
        return registry.remove_project(p["project_id"])

    if action.startswith("maintenance."):
        from .maintenance import MaintenanceStore
        store = MaintenanceStore(state_dir)
        if action == 'maintenance.documents-new-round':
            return store.continue_documents(p['project_id'], source_run_id=p['source_run_id'], source_sha256=p['source_sha256'])
        if action == 'maintenance.reprepare':
            return store.reprepare(p['project_id'])
        if action == "maintenance.resume":
            return store.resume(p["project_id"])
        if action == "maintenance.context-decide-many":
            return store.decide_contexts(p["project_id"], p['requests'], approved=p['approved'])
        return store.decide(p["project_id"], approved=p["approved"], sha256=p["sha256"],
                            actor=p.get("actor"), sequence=p.get("sequence"))

    def project():
        record = registry.get_project(p["project_id"])
        if record is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        return record

    if action == "project.import":
        from .git import inspect_repository
        from .initialization import InitializationStore
        from .web_operations import report_progress
        report_progress('repository', '正在检查项目目录和 Git 版本')
        registered = registry.register(inspect_repository(p["path"]), workspace=True)
        initializer = InitializationStore(state_dir)
        registered["initialization"] = (initializer.begin if p.get('automatic_understanding') else initializer.prepare)(registered["id"])
        return registered
    if action == 'understanding.configure':
        from .understanding_approval import UnderstandingApproval
        return UnderstandingApproval(state_dir).configure(p['project_id'], enabled=p['enabled'])
    if action.startswith("initialization."):
        from .initialization import InitializationStore
        store = InitializationStore(state_dir)
        if action == 'initialization.initialize':
            return store.begin(p['project_id'], resume_setup=p.get('resume_setup', False))
        if action == "initialization.prepare":
            return store.prepare(p["project_id"], replace_pending=p["replace_pending"])
        if action == "initialization.repair":
            result = store.repair(p["project_id"], replace_pending=p["replace_pending"])
            return store.begin(p['project_id']) if p.get('automatic_understanding') and result.get('status') in ('awaiting_approval', 'awaiting_context_approval') else result
        if action == "initialization.republish":
            return store.republish(p["project_id"])
        if action == "initialization.decide":
            return store.decide(p["project_id"], approved=p["approved"], sha256=p["sha256"], automatic_maintenance=p.get("automatic_maintenance", False))
        if action == "initialization.recover":
            return store.recover(p["project_id"])
        if action == "initialization.context-decide":
            return store.decide_context(p["project_id"], p["actor"], p["sequence"],
                                        approved=p["approved"], sha256=p["sha256"])
        if action == "initialization.context-decide-many":
            return store.decide_contexts(p['project_id'], p['requests'], approved=p['approved'])
        store.launch(p["project_id"])
        return store.show(p["project_id"])
    if action == "project.init":
        from .development import create_project
        return create_project(state_dir=state_dir, **p)
    if action == "project.documents.init":
        from .documents import initialize_documents
        return initialize_documents(project())
    if action.startswith("conversation."):
        from .conversations import ConversationStore
        store = ConversationStore(state_dir)
        if action == "conversation.create":
            return store.create(project())
        if action == 'conversation.answer':
            return store.answer(p['id'], message_id=p['message_id'])
        return store.send(p["id"], p["question"], message_id=p["message_id"], automatic_answer=p.get('automatic_answer', True))
    if action.startswith("task."):
        from .tasks import TaskRunner
        runner = TaskRunner(state_dir)
        if action == "task.create":
            return runner.create(project(), **{k: v for k, v in p.items() if k != "project_id"})
        return runner.decide(p["id"], approved=p["approved"], payload_sha256=p["sha256"])
    if action.startswith("cognition."):
        from .cognition_store import CognitionStore
        from .cognition import start_worker
        from .resources import record_state
        store = CognitionStore(state_dir)
        if action == "cognition.create":
            return store.create(p["project_id"])
        if action == "cognition.refresh":
            from .cognition_refresh import prepare_refresh
            return prepare_refresh(state_dir, p["project_id"])
        if action == "cognition.followup":
            from .cognition_followup import prepare_followup
            return prepare_followup(state_dir, p["id"], p["finding_id"])
        if action == "cognition.invalidate":
            return store.invalidate(p["id"], p["finding_id"], p["reason"])
        if action == "cognition.context-decide":
            result = store.decide_context(p["id"], p["actor"], p["sequence"],
                                          approved=p["approved"], request_sha256=p["sha256"])
        elif action == "cognition.decide":
            result = store.decide(p["id"], approved=p["approved"], approval_sha256=p["sha256"])
        elif action == "cognition.recover":
            result = store.recover(p["id"])
        else:
            result = store.show(p["id"])
            if result["status"] != "queued":
                raise FileReadError("cognition_not_queued", "只有已批准排队的调查可以启动。")
        if result["status"] == "queued":
            result["worker"] = start_worker(record_state(state_dir, "cognition", p["id"]))
        return result
    if action == "repair.decide":
        from .repairs import RepairStore
        return RepairStore(state_dir).decide(p["id"], approved=p["approved"], plan_sha256=p["sha256"])
    if action.startswith("verification."):
        from .verification import VerificationStore
        store = VerificationStore(state_dir)
        if action == "verification.create":
            return store.create(p["id"], p["image"], timeout=p["timeout"])
        return store.decide(p["id"], approved=p["approved"], plan_sha256=p["sha256"])
    if action == "release.export":
        from .exports import SourceExporter
        return SourceExporter(state_dir).export(p["project_id"], p["destination"])
    if action == "release.verify-export":
        from .exports import verify_export
        return verify_export(p["path"])
    if action.startswith("release."):
        from .releases import ReleaseStore
        store = ReleaseStore(state_dir)
        if action == "release.promote":
            return store.promote(p["id"])
        if action == "release.rollback":
            return store.rollback(p["id"])
        if action == "release.recover":
            return store.recover(p["id"])
        if action == "release.capture":
            from .history import capture_maintenance
            return capture_maintenance(state_dir, p["id"])
        return store.decide(p["id"], approved=p["approved"], plan_sha256=p["sha256"])
    if action.startswith("knowledge."):
        from .knowledge import KnowledgeStore
        store = KnowledgeStore(state_dir)
        if action == "knowledge.add":
            return store.add(project(), **{k: v for k, v in p.items() if k != "project_id"})
        if action == "knowledge.confirm":
            return store.confirm(p["id"])
        return store.invalidate(p["id"], p["reason"])
    if action == "survey.build":
        from .survey import SurveyStore
        return SurveyStore(state_dir).build(project())
    if action.startswith(("evaluation.", "resolution.")):
        from .evaluation import EvaluationStore
        from .cognition_resolution import ResolutionStore
        store = EvaluationStore(state_dir) if action.startswith("evaluation.") else ResolutionStore(state_dir)
        verb = action.split(".")[1]
        if verb == "create":
            if action.startswith("evaluation."):
                return store.create(p["id"], p["criteria"], max_calls=p["max_calls"])
            return store.create(p["id"])
        if verb == "adopt":
            return store.adopt(p["id"], proposal_sha256=p["sha256"])
        if verb == "decide":
            result = store.decide(p["id"], approved=p["approved"], approval_sha256=p["sha256"],
                                  allow_extended_requests=p["allow_extended_requests"])
            if not p["approved"]:
                return result
            return store.run(p["id"])
        return getattr(store, verb)(p["id"])
    if action == "index.build":
        from .index import RetrievalIndex
        return RetrievalIndex(state_dir).build(project())
    raise FileReadError("invalid_web_command", "不支持的操作。")
