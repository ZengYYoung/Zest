"""Local reconnaissance material from one Git snapshot; no model or project execution."""

from collections import Counter, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import time
from uuid import UUID, uuid4

from .files import BLOCKED_DIRECTORIES, CREDENTIAL_PATTERNS, FileReadError, _validate_path, load_text_blob
from .git import inspect_repository, list_tree, selected_commit, verify_commit
from .overview import ENTRYPOINTS, MANIFESTS, _node_manifest, _python_manifest
from .structure import build_structure


MAX_FILES = 200
MAX_BYTES = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 4096
MAX_SECONDS = 30
MAX_EXCERPT_LINES = 80
MAX_EXCERPT_CHARS = 6000
MAX_TOTAL_EXCERPT_CHARS = 24000
MAX_RECORD_BYTES = 8 * 1024 * 1024
MANIFEST_ECOSYSTEMS = {name.casefold(): ecosystem for name, ecosystem in MANIFESTS.items()}
CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
                 ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".scala", ".vue", ".svelte", ".sql"}
SECTIONS = (
    ("purpose", "项目目标与业务边界", "项目解决什么问题，有哪些用户、功能和业务规则？"),
    ("architecture", "模块与架构关系", "模块职责、入口及协作关系是什么？"),
    ("flows", "核心业务流程", "关键操作经过哪些接口、函数和数据处理步骤？"),
    ("dependencies", "数据与外部依赖", "关键数据、数据库和外部服务如何关联？"),
    ("development", "开发与验证方式", "如何配置、启动和测试，哪些步骤已有运行证据？"),
    ("constraints", "约束与认知缺口", "有哪些约束、文档矛盾和待核实背景？"),
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _id(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise FileReadError("invalid_survey_id", "摸底记录 ID 必须是完整 UUID。") from exc
    return value


def _path(path, *, directory=False):
    if len(path) > 1024:
        raise FileReadError("survey_path_budget", "路径超过摸底元数据预算。")
    try:
        encoded = path.encode("utf-8")
    except UnicodeError as exc:
        raise FileReadError("invalid_path", "路径不是 UTF-8 文本。") from exc
    if any(re.search(pattern, encoded) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_path", "路径包含已识别凭据，未保存。")
    directories = path.split("/") if directory else path.split("/")[:-1]
    if any(part.casefold() in BLOCKED_DIRECTORIES or part.casefold().startswith((".env", "credentials", "secrets", "id_rsa", "id_ed25519"))
           for part in directories):
        raise FileReadError("excluded_directory", "该目录不在摸底范围内。")
    if directory:
        _validate_path(path + "/probe.py")
    else:
        _validate_path(path)


def _roles(path):
    value = PurePosixPath(path)
    name = value.name.casefold()
    parts = [part.casefold() for part in value.parts]
    roles = []
    if value.suffix.casefold() in CODE_SUFFIXES:
        roles.append("code_candidate")
    if name in MANIFEST_ECOSYSTEMS:
        roles.append("manifest")
    if value.suffix.casefold() in (".md", ".rst") or name.startswith("readme") or (value.suffix.casefold() == ".txt" and any(p in ("doc", "docs") for p in parts[:-1])):
        roles.append("document")
    if any(p in ("test", "tests", "__tests__") for p in parts[:-1]) or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts", "test.java")):
        roles.append("test_candidate")
    if path in ENTRYPOINTS or name == "__main__.py":
        roles.append("entrypoint_candidate")
    return roles


def collect_survey(project, *, commit=None, progress=None):
    started = time.monotonic()
    def emit(phase, **details):
        if progress is not None:
            progress({"phase": phase, "elapsed_seconds": round(time.monotonic() - started, 3), **details})

    emit("snapshot")
    state = inspect_repository(project["root_path"])
    root = Path(state.root_path)
    pinned = selected_commit(project, state, commit)
    if pinned is not None and not verify_commit(root, pinned):
        raise FileReadError("invalid_commit", "摸底需要本地存在的完整 commit，不接受分支名或缩写。")
    record = {"schema_version": 1, "id": str(uuid4()), "project_id": project["id"],
              "created_at": datetime.now(timezone.utc).isoformat(), "kind": "local_reconnaissance",
              "source": {"kind": "git_commit", "commit": pinned, "working_tree_changes_included": False},
              "workspace": {"head_commit": state.head_commit, "dirty": state.dirty, "observed_at": state.observed_at},
              "status": "collected", "directories": [], "sources": [], "manifests": [],
              "declared_commands": [], "structure": None, "warnings": [],
              "limitations": ["not_a_project_understanding_dossier", "no_model_calls", "no_project_execution",
                              "entrypoints_and_tests_are_candidates", "no_runtime_or_business_claims",
                              "bounded_scan", "credential_detection_is_pattern_based"]}
    warnings = record["warnings"]
    if state.dirty:
        warnings.append({"code": "working_tree_excluded"})
    if project["git"]["head_commit"] != state.head_commit:
        warnings.append({"code": "registration_head_changed"})
    if pinned is not None and pinned != state.head_commit:
        warnings.append({"code": "historical_snapshot" if commit is not None else "managed_version_selected"})
    coverage = {"files_considered": 0, "files_read": 0, "bytes_read": 0, "tree_entries_seen": 0,
                "inventory_scan_finished": True, "skipped_counts": {},
                "limits": {"files": MAX_FILES, "bytes": MAX_BYTES, "tree_entries": MAX_TREE_ENTRIES,
                           "inventory_seconds": MAX_SECONDS, "excerpt_lines": MAX_EXCERPT_LINES,
                           "excerpt_chars": MAX_EXCERPT_CHARS, "total_excerpt_chars": MAX_TOTAL_EXCERPT_CHARS}}
    record["coverage"] = coverage
    skips = Counter()
    queue = deque([(pinned, "")]) if pinned else deque()
    inventory_started = time.monotonic()
    excerpt_chars = 0
    exhausted = False
    emit("inventory", files_read=0)
    while queue and not exhausted:
        if time.monotonic() - inventory_started > MAX_SECONDS:
            warnings.append({"code": "survey_time_budget"})
            exhausted = True
            break
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            if coverage["tree_entries_seen"] >= MAX_TREE_ENTRIES or time.monotonic() - inventory_started > MAX_SECONDS:
                warnings.append({"code": "survey_scan_budget"})
                exhausted = True
                break
            coverage["tree_entries_seen"] += 1
            path = prefix + entry.path
            try:
                _path(path, directory=entry.object_type == "tree")
                if entry.object_type == "tree":
                    record["directories"].append({"path": path, "commit": pinned, "tree_id": entry.object_id})
                    queue.append((entry.object_id, path + "/"))
                    continue
                if coverage["files_considered"] >= MAX_FILES:
                    warnings.append({"code": "survey_file_budget"})
                    exhausted = True
                    break
                coverage["files_considered"] += 1
                raw = load_text_blob(root, entry)
                if coverage["bytes_read"] + len(raw) > MAX_BYTES:
                    warnings.append({"code": "survey_byte_budget"})
                    exhausted = True
                    break
                coverage["bytes_read"] += len(raw)
                coverage["files_read"] += 1
                lines = raw.decode("utf-8").split("\n")
                if lines[-1] == "":
                    lines.pop()
                source_id = f"S{coverage['files_read']}"
                roles = _roles(path)
                source = {"id": source_id, "roles": roles,
                          "evidence": {"path": path, "commit": pinned, "blob_id": entry.object_id,
                                       "content_sha256": hashlib.sha256(raw).hexdigest()},
                          "total_bytes": len(raw), "total_lines": len(lines)}
                if "manifest" in roles or "document" in roles:
                    excerpt = "\n".join(lines[:MAX_EXCERPT_LINES])
                    if len(excerpt) <= MAX_EXCERPT_CHARS and excerpt_chars + len(excerpt) <= MAX_TOTAL_EXCERPT_CHARS:
                        source["excerpt"] = {"start_line": 1 if lines else None,
                                             "end_line": min(len(lines), MAX_EXCERPT_LINES) if lines else None,
                                             "text": excerpt,
                                             "text_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                                             "more_after": len(lines) > MAX_EXCERPT_LINES}
                        excerpt_chars += len(excerpt)
                    else:
                        source["excerpt_omitted"] = "survey_excerpt_budget"
                        skips["survey_excerpt_budget"] += 1
                record["sources"].append(source)
                if "manifest" in roles:
                    name = PurePosixPath(path).name.casefold()
                    manifest = {"source_id": source_id, "ecosystem": MANIFEST_ECOSYSTEMS[name], "status": "inventory_only"}
                    record["manifests"].append(manifest)
                    if name in ("pyproject.toml", "package.json"):
                        try:
                            metadata, commands = (_python_manifest if name == "pyproject.toml" else _node_manifest)(raw)
                            manifest.update(status="parsed", metadata=metadata)
                            record["declared_commands"].extend({"source_id": source_id, "name": name, "target": target,
                                                                "kind": "declaration_not_executed"}
                                                               for name, target in sorted(commands.items()))
                        except (ValueError, UnicodeError, RecursionError):
                            manifest["status"] = "invalid"
                            warnings.append({"code": "invalid_manifest", "source_id": source_id})
                if coverage["files_read"] % 10 == 0:
                    emit("inventory", files_read=coverage["files_read"])
            except FileReadError as exc:
                skips[exc.code] += 1
    coverage["inventory_scan_finished"] = not exhausted
    coverage["skipped_counts"] = dict(skips)
    record["directories"].sort(key=lambda item: item["path"])
    if skips:
        warnings.append({"code": "survey_skipped_sources"})
    emit("structure", files_read=coverage["files_read"])
    if pinned is not None:
        record["structure"] = build_structure(project, commit=pinned)
        safe_modules = []
        for module in record["structure"]["modules"]:
            try:
                _path(module["path"])
                safe_modules.append(module)
            except FileReadError as exc:
                counts = record["structure"]["skipped_counts"]
                counts[exc.code] = counts.get(exc.code, 0) + 1
        if len(safe_modules) != len(record["structure"]["modules"]):
            record["structure"]["warnings"].append({"code": "survey_structure_paths_filtered"})
        record["structure"]["modules"] = safe_modules
        if exhausted or record["structure"]["warnings"] or any(m["status"] == "invalid" for m in record["manifests"]):
            record["status"] = "partial"
    else:
        record["status"] = "no_committed_source"
        warnings.append({"code": "unborn_repository"})
    sections = []
    for name, title, question in SECTIONS:
        wanted = {"document"}
        if name in ("architecture", "flows"):
            wanted.update(("entrypoint_candidate", "code_candidate"))
        if name in ("architecture", "dependencies", "development"):
            wanted.add("manifest")
        if name == "development":
            wanted.add("test_candidate")
        sections.append({"id": name, "title": title, "status": "pending_investigation",
                         "source_ids": [source["id"] for source in record["sources"] if wanted.intersection(source["roles"])],
                         "question": question})
    record["sections"] = sections
    record["gaps"] = [{"code": "project_understanding_pending", "section": section["id"]} for section in sections]
    if not any("document" in source["roles"] for source in record["sources"]):
        record["gaps"].append({"code": "no_readable_documents_found"})
    if not record["manifests"]:
        record["gaps"].append({"code": "no_readable_manifests_found"})
    if not any("test_candidate" in source["roles"] for source in record["sources"]):
        record["gaps"].append({"code": "no_test_candidates_found"})
    record["gaps"].extend({"code": "manifest_parser_unavailable", "source_id": m["source_id"]}
                          for m in record["manifests"] if m["status"] == "inventory_only")
    record["gaps"].append({"code": "runtime_not_verified"})
    other_suffixes = sorted({PurePosixPath(source["evidence"]["path"]).suffix.casefold() for source in record["sources"]
                             if "code_candidate" in source["roles"] and PurePosixPath(source["evidence"]["path"]).suffix.casefold() != ".py"})
    if other_suffixes:
        record["gaps"].append({"code": "non_python_structure_not_parsed", "suffixes": other_suffixes})
    record["collection_elapsed_seconds"] = round(time.monotonic() - started, 3)
    emit("collected", status=record["status"], files_read=coverage["files_read"])
    return record


class SurveyStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "surveys.sqlite3"

    @contextmanager
    def _open(self, *, write=False):
        try:
            if self.db_path.is_symlink():
                raise FileReadError("survey_storage_error", "摸底数据库不能是符号链接。")
            if write:
                self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            with closing(sqlite3.connect(self.db_path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1) or (not write and version != 1):
                    raise FileReadError("survey_version_error", "摸底记录版本不兼容。")
                if write:
                    with conn:
                        conn.execute("""CREATE TABLE IF NOT EXISTS surveys (
                            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                            record TEXT NOT NULL, record_sha256 TEXT NOT NULL)""")
                        conn.execute("CREATE INDEX IF NOT EXISTS surveys_project_time ON surveys(project_id, created_at DESC, id DESC)")
                        conn.execute("PRAGMA user_version=1")
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("survey_storage_error", "无法读取或保存摸底记录，请检查本地数据库与权限。") from exc

    @staticmethod
    def _record(row):
        if row is None:
            raise FileReadError("survey_not_found", "未找到该摸底记录。")
        try:
            raw = row["record"]
            if len(raw.encode()) > MAX_RECORD_BYTES or hashlib.sha256(raw.encode()).hexdigest() != row["record_sha256"]:
                raise ValueError
            value = json.loads(raw)
            if (value["schema_version"] != 1 or value["id"] != row["id"] or value["project_id"] != row["project_id"]
                    or value["created_at"] != row["created_at"]):
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise FileReadError("survey_record_corrupt", "摸底记录内容或一致性校验失败。") from exc
        return {**value, "record_sha256": row["record_sha256"]}

    def build(self, project, *, commit=None, progress=None):
        value = collect_survey(project, commit=commit, progress=progress)
        raw = _canonical(value)
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise FileReadError("survey_record_budget", "摸底记录超过保存预算，未保存新记录。")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if progress:
            progress({"phase": "saving", "elapsed_seconds": value["collection_elapsed_seconds"]})
        with self._open(write=True) as conn, conn:
            conn.execute("INSERT INTO surveys VALUES (?,?,?,?,?)",
                         (value["id"], value["project_id"], value["created_at"], raw, digest))
        return {**value, "record_sha256": digest}

    def show(self, survey_id):
        _id(survey_id)
        if not self.db_path.exists():
            raise FileReadError("survey_not_found", "未找到该摸底记录。")
        with self._open() as conn:
            return self._record(conn.execute("SELECT * FROM surveys WHERE id=?", (survey_id,)).fetchone())

    def list_surveys(self, project_id):
        if not self.db_path.exists():
            return []
        summaries = []
        with self._open() as conn:
            for row in conn.execute("SELECT * FROM surveys WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (project_id,)):
                record = self._record(row)
                summaries.append({key: record[key] for key in ("id", "project_id", "created_at", "status", "source", "coverage", "record_sha256")})
        return summaries


def _console(value):
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else f"\\x{ord(char):02x}" for char in str(value))


def format_survey(record):
    coverage = record["coverage"]
    lines = ["Zest · 本地项目摸底", f"记录：{record['id']}", f"版本：{record['source']['commit'] or '尚无提交'}",
             f"采集状态：{record['status']}（资料准备，项目认知仍待调查）",
             f"允许文本：读取 {coverage['files_read']} / 尝试 {coverage['files_considered']} 个，{coverage['bytes_read']} 字节",
             f"目录遍历：{'完成本轮范围' if coverage['inventory_scan_finished'] else '预算内部分结果'}",
             f"采集耗时：{record['collection_elapsed_seconds']:.3f} 秒（不含保存）", ""]
    for role, title in (("document", "文档"), ("manifest", "依赖声明"), ("entrypoint_candidate", "入口候选"), ("test_candidate", "测试候选")):
        sources = [source for source in record["sources"] if role in source["roles"]]
        lines.append(f"{title}：{len(sources)} 项")
        lines.extend(f"  {source['id']}  {_console(source['evidence']['path'])}" for source in sources[:20])
        if len(sources) > 20:
            lines.append("  更多条目见 JSON 完整记录。")
    structure = record["structure"]
    lines.extend(["", f"Python 声明地图：{len(structure['modules']) if structure else 0} 个已解析模块；其他语言仅列材料",
                  "待调查：", *[f"  {section['title']}：{section['question']}" for section in record["sections"]],
                  f"缺口 {len(record['gaps'])} 项；扫描警告 {len(record['warnings'])} 项。"])
    if coverage["skipped_counts"]:
        lines.append("跳过计数：" + json.dumps(coverage["skipped_counts"], ensure_ascii=False))
    if structure and structure["warnings"]:
        lines.append("源码地图警告：" + json.dumps(structure["warnings"], ensure_ascii=False))
    lines.extend(["未调用模型、运行项目或测试；没有自动写入共享知识。", f"记录校验：{record['record_sha256']}"])
    return "\n".join(lines)
