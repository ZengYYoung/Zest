"""Capture verified maintenance evidence as a draft, without inferring business facts."""

import json
from pathlib import Path

from .files import FileReadError
from .git import read_direct_ref
from .knowledge import KnowledgeStore
from .repairs import digest
from .storage import ProjectRegistry
from . import releases


def capture_maintenance(state_dir, operation_id):
    store = releases.ReleaseStore(state_dir)
    operation = store.show(operation_id)
    if operation["status"] != "applied" or operation["plan"]["operation"] != "promote":
        raise FileReadError("maintenance_verified_promotion_required", "案例需要已完成的验证后源码选择操作，不将回退当作新修复。")
    plan = operation["plan"]
    project = ProjectRegistry(state_dir).get_project(plan["project_id"])
    if project is None or project["root_path"] != plan["root_path"]:
        raise FileReadError("maintenance_project_changed", "案例项目位置与操作来源不匹配。")
    root = Path(project["root_path"])
    for side, commit in (("before", plan["previous_commit"]), ("after", plan["target_commit"])):
        if read_direct_ref(root, store._history_ref(operation_id, side)) != commit:
            raise FileReadError("maintenance_history_changed", "操作历史引用已变化，未保存案例。")
    verification, repair = releases._validated_verification(state_dir, plan["verification_id"])
    if digest(verification) != plan["verification_sha256"]:
        raise FileReadError("maintenance_evidence_changed", "验证记录与已批准操作不匹配。")
    result = verification["result"]
    evidence = {"record_type": "maintenance_evidence", "scope": "managed_source_only",
                "operation_id": operation_id, "operation_plan_sha256": operation["plan_sha256"],
                "repair_id": repair["id"], "source_task_id": repair["plan"].get("source_task_id"),
                "verification_id": verification["id"], "verification_plan_sha256": verification["plan_sha256"],
                "base_commit": plan["previous_commit"], "candidate_commit": plan["target_commit"],
                "changes": [{"path": change["path"], "operation": change.get("operation", "replace"),
                             "old_sha256": change["expected_sha256"], "new_sha256": change["new_sha256"]}
                            for change in repair["plan"]["changes"]],
                "proposal_summary_unverified": repair["plan"]["summary"],
                "baseline": {"status": result["baseline"]["status"], "tests_run": result["baseline"].get("tests_run", 0)},
                "candidate": {"status": result["candidate"]["status"], "tests_run": result["candidate"]["tests_run"],
                              "image_id": verification["plan"]["image_id"], "backend": verification["plan"]["backend"]},
                "limits": ["历史案例只作对照，不证明当前版本已修复", "测试通过不证明需求完整正确",
                           "源码选择不代表运行部署", "方案说明不是已确认的故障根因"]}
    # Fixed operation UUID + immutable body makes repeat capture idempotent across processes.
    note = KnowledgeStore(state_dir).add(project, f"历史维护证据 {plan['target_commit'][:12]}",
                                         json.dumps(evidence, ensure_ascii=False, indent=2),
                                         kind="maintenance", scope="project", note_id=operation_id,
                                         observed_commit=plan["target_commit"])
    return {"operation_id": operation_id, "note": note,
            "next_step": "review_knowledge_then_confirm" if note["status"] == "draft" else "already_recorded",
            "model_called": False}
