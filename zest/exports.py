"""Export an approved managed source snapshot; never launch it or alter the workspace."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

from .files import FileReadError, MAX_FILE_BYTES
from .git import read_direct_ref
from .repairs import RepairStore, digest, validate_change_paths
from . import releases
from .verification import collect_snapshot, _export, _manifest_on_disk, MAX_FILES, MAX_BYTES


MAX_MANIFEST_BYTES = 512 * 1024


def _new_destination(path, project_root):
    requested = Path(path).expanduser().absolute()
    try:
        destination = requested.parent.resolve(strict=True) / requested.name
    except (OSError, RuntimeError) as exc:
        raise FileReadError("invalid_export_parent", "导出父目录必须已经存在。") from exc
    if destination.exists() or destination.is_symlink():
        raise FileReadError("export_path_exists", "导出路径已经存在，不能覆盖。")
    if destination.is_relative_to(project_root):
        raise FileReadError("export_into_project", "导出目录需要位于原项目之外，避免改变原工作区。")
    return destination


class SourceExporter:
    def __init__(self, state_dir):
        self.store = releases.ReleaseStore(state_dir)

    def export(self, project_id, destination):
        with RepairStore(self.store.state_dir)._lock(project_id):
            active = self.store.active(project_id)
            commit = active["managed_commit"]
            if commit is None:
                raise FileReadError("export_managed_version_required", "请先批准管理版本选择，再导出该版本。")
            root = Path(active["root_path"])
            target = _new_destination(destination, root)
            operations = self.store.list_operations(project_id)
            operation = next((self.store.show(row["id"]) for row in operations
                              if row["status"] == "applied" and row["target_commit"] == commit), None)
            if operation is None or operation["plan"]["root_path"] != str(root):
                raise FileReadError("export_history_required", "当前引用没有匹配的已完成版本操作，不能标记为受批准导出。")
            for side, expected in (("before", operation["plan"]["previous_commit"]), ("after", commit)):
                if read_direct_ref(root, self.store._history_ref(operation["id"], side)) != expected:
                    raise FileReadError("export_history_changed", "已批准版本的历史引用已变化。")
            if operation["plan"]["operation"] == "promote":
                verification, _ = releases._validated_verification(self.store.state_dir, operation["plan"]["verification_id"])
                if digest(verification) != operation["plan"]["verification_sha256"]:
                    raise FileReadError("export_evidence_changed", "已批准的验证记录已变化。")
                validation = {"kind": "verified_candidate", "backend": verification["plan"]["backend"],
                              "verification_id": verification["id"], "tests_run": verification["result"]["candidate"]["tests_run"]}
            else:
                validation = {"kind": "previous_recorded_source", "runtime_health_verified": False}
            manifest, files = collect_snapshot(root, commit)
            payload = {"schema_version": 1, "export_id": str(uuid4()), "project_id": project_id,
                       "commit": commit, "operation_id": operation["id"],
                       "operation_plan_sha256": operation["plan_sha256"], "validation": validation,
                       "created_at": datetime.now(timezone.utc).isoformat(), "files": manifest,
                       "scope": "managed_source_snapshot", "runtime_deployed": False}
            envelope = {"payload": payload, "sha256": digest(payload)}
            text = json.dumps(envelope, ensure_ascii=True, indent=2)
            if len(text.encode()) > MAX_MANIFEST_BYTES:
                raise FileReadError("export_manifest_limit", "导出清单超过 512 KiB。")
            created = False
            try:
                target.mkdir(mode=0o700)
                created = True
                _export(files, target / "source")
                _manifest_on_disk(target / "source", manifest)
                if read_direct_ref(root, active["ref"]) != commit:
                    raise FileReadError("export_version_changed", "导出期间管理版本改变，未完成导出。")
                with (target / "manifest.json").open("x", encoding="utf-8") as output:
                    output.write(text)
                (target / "manifest.json").chmod(0o600)
                return {"status": "complete", "commit": commit, "export_id": payload["export_id"],
                        "source_dir": str(target / "source"), "manifest_path": str(target / "manifest.json"),
                        "manifest_sha256": envelope["sha256"], "file_count": len(manifest),
                        "validation": validation, "runtime_deployed": False}
            except (OSError, FileReadError) as exc:
                if created:
                    raise FileReadError("export_incomplete", "导出未完成，已创建目录保留供检查；不会删除或覆盖用户文件。") from exc
                raise FileReadError("export_creation_failed", "无法创建导出目录，未覆盖既有路径。") from exc


def verify_export(directory):
    root = Path(directory).expanduser()
    try:
        if root.is_symlink() or (root / "manifest.json").is_symlink() or (root / "source").is_symlink():
            raise FileReadError("invalid_export", "导出目录、清单和 source 不能是符号链接。")
        with (root / "manifest.json").open("rb") as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise FileReadError("export_manifest_limit", "导出清单超过 512 KiB。")
        envelope = json.loads(raw)
        payload = envelope["payload"]
        if (payload["schema_version"] != 1 or payload["scope"] != "managed_source_snapshot"
                or payload["runtime_deployed"] is not False or digest(payload) != envelope["sha256"]
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", payload["commit"])):
            raise ValueError
        for key in ("export_id", "project_id", "operation_id"):
            if str(UUID(payload[key])) != payload[key]:
                raise ValueError
        files = payload["files"]
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise ValueError
        total = 0
        for entry in files:
            if (set(entry) != {"path", "mode", "sha256", "bytes"} or entry["mode"] not in ("100644", "100755")
                    or type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES
                    or not re.fullmatch("[a-f0-9]{64}", entry["sha256"])):
                raise ValueError
            total += entry["bytes"]
        if total > MAX_BYTES:
            raise ValueError
        validate_change_paths([entry["path"] for entry in files])
        _manifest_on_disk(root / "source", files)
        for entry in files:
            path = root / "source" / entry["path"]
            if path.stat().st_size != entry["bytes"] or bool(path.stat().st_mode & 0o111) != (entry["mode"] == "100755"):
                raise FileReadError("export_changed", "导出文件大小或执行权限不匹配。")
        return {"status": "intact", "commit": payload["commit"], "export_id": payload["export_id"],
                "manifest_sha256": envelope["sha256"], "file_count": len(files),
                "scope": "local_manifest_consistency", "runtime_deployed": False}
    except FileReadError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise FileReadError("invalid_export", "无法核对导出清单与目录，请检查完整性；未执行源码。") from exc
