"""Shared, versioned project documents and explicit, non-destructive import.

No model, source-document edits, Git index updates, commits or network calls.
Imported material is a source to check, not automatically confirmed knowledge.
"""

from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .document_layout import LEGACY_IGNORE, SHARED_IGNORE, legacy_document, shared_document
from .files import FileReadError, _validate_path, load_text_blob, validate_directory
from .git import _git, inspect_repository, list_tree, selected_commit, verify_commit


MAX_DOCUMENTS = 128
MAX_IMPORT_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 4096


def workspace_documents(project):
    """List local shared docs for review; never traverse personal .zest resources."""
    from .resources import _directory
    root = Path(project["root_path"])
    _directory(root / ".zest")
    _directory(root / ".zest/docs")
    result = {"documents": [], "source": "workspace", "warnings": []}
    queue, entries = deque([".zest/docs"]), 0
    while queue:
        prefix = queue.popleft()
        folder = root / prefix
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            entries += 1
            if entries > MAX_ENTRIES or len(result["documents"]) >= MAX_DOCUMENTS:
                result["warnings"].append({"code": "document_catalog_budget"})
                return result
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                continue
            try:
                if path.is_dir():
                    validate_directory(relative)
                    queue.append(relative)
                elif path.is_file() and shared_document(relative):
                    _validate_path(relative)
                    item = {"path": relative, "bytes": path.stat().st_size}
                    # Read only the small heading/metadata prefix for navigation.
                    # Full content still passes the ordinary document reader.
                    from .files import CREDENTIAL_PATTERNS
                    with path.open('rb') as stream:
                        head = stream.read(2048)
                    if not any(re.search(pattern, head) for pattern in CREDENTIAL_PATTERNS):
                        try:
                            from codecs import getincrementaldecoder
                            first = getincrementaldecoder('utf-8')().decode(head, final=item['bytes'] <= len(head)).splitlines()
                            title = (json.loads(first[1].removeprefix('title: ')) if first[:1] == ['---'] and first[1].startswith('title: ')
                                     else first[0].removeprefix('# ') if first and first[0].startswith('# ') else None)
                            if isinstance(title, str) and 0 < len(title) <= 100:
                                item['title'] = title
                        except (ValueError, IndexError):
                            pass
                    result["documents"].append(item)
            except FileReadError:
                continue
    return result


def read_workspace_document(project, path, *, start_line=1, end_line=200):
    from .files import MAX_FILE_BYTES, CREDENTIAL_PATTERNS
    if not shared_document(path):
        raise FileReadError("blocked_path", "项目状态只展示 .zest/docs/ 下的项目文档。")
    _validate_path(path)
    if start_line < 1 or end_line < start_line or end_line - start_line >= 200:
        raise FileReadError("invalid_line_range", "每次可读取最多 200 行，请检查行号。")
    root = Path(project["root_path"])
    _parents(root, path)
    try:
        with (root / path).open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise FileReadError("document_not_found", "该项目文档不存在。") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise FileReadError("file_too_large", "文档超过当前单文件读取容量。")
    if b"\0" in raw or any(re.search(pattern, raw) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_content", "文档包含二进制内容或已识别凭据，未返回正文。")
    try:
        body = raw.decode("utf-8")
    except UnicodeError as exc:
        raise FileReadError("unsupported_encoding", "文档须为 UTF-8 文本。") from exc
    lines = body.splitlines(keepends=True)
    return {"path": path, "source": "workspace", "total_lines": len(lines),
            "start_line": start_line, "end_line": min(end_line, len(lines)),
            "content": "".join(lines[start_line - 1:end_line]),
            "more_after": end_line < len(lines), "content_sha256": hashlib.sha256(raw).hexdigest()}


def document_catalog(project, *, commit=None):
    """List only committed shared documents, pinned to one version."""
    state = inspect_repository(project["root_path"])
    target = selected_commit(project, state, commit)
    result = {"project_id": project["id"], "commit": target,
              "working_tree_changes_included": False, "documents": [], "warnings": []}
    if state.dirty:
        result["warnings"].append({"code": "working_tree_excluded"})
    if target is None:
        return result
    root = Path(state.root_path)
    if not verify_commit(root, target):
        raise FileReadError("invalid_commit", "请指定本地存在的完整 commit ID。")
    tree = target
    for name in (".zest", "docs"):
        entry = next((e for e in list_tree(root, tree) if e.path == name), None)
        if entry is None:
            return result
        if entry.object_type != "tree":
            raise FileReadError("unsupported_file_type", "共享文档目录不能经过符号链接或非目录。")
        tree = entry.object_id
    queue = deque([(tree, ".zest/docs/")])
    scanned = 0
    while queue:
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            scanned += 1
            if scanned > MAX_ENTRIES:
                result["warnings"].append({"code": "document_catalog_budget"})
                return result
            path = prefix + entry.path
            try:
                if entry.object_type == "tree":
                    validate_directory(path)
                    queue.append((entry.object_id, path + "/"))
                    continue
                _validate_path(path)
                if not entry.regular_file or not shared_document(path):
                    continue
            except FileReadError:
                continue
            if len(result["documents"]) >= MAX_DOCUMENTS:
                result["warnings"].append({"code": "document_catalog_budget"})
                return result
            result["documents"].append({"path": path, "commit": target, "blob_id": entry.object_id,
                                        "kind": "shared_project_document", "verified_fact": False})
    return result


def document_ranges(project, question, commit, *, limit=3):
    """Small automatic document context, with ordinary source/budget checks."""
    from .index import terms
    from .files import read_project_file
    catalog = document_catalog(project, commit=commit)
    query = terms(question)
    scored, used = [], 0
    for item in catalog["documents"]:
        if used >= 1024 * 1024:
            break
        try:
            record = read_project_file(project, item["path"], commit=commit, end_line=200)
        except FileReadError:
            continue
        used += record["total_bytes"]
        counts = terms(item["path"] + "\n" + record["content"])
        score = sum(min(n, counts.get(term, 0)) for term, n in query.items())
        # The navigation entry accompanies the most relevant documents.
        if item["path"] == ".zest/docs/index.md":
            score += 0.5
        if record["lines"]:
            scored.append((-score, item["path"], min(record["total_lines"], 200)))
    return [(path, 1, end) for _, path, end in sorted(scored)[:limit]]


def _parents(root, path):
    from .resources import _directory, _regular
    current = root
    for part in path.split("/")[:-1]:
        current /= part
        _directory(current)
    _regular(root / path)


def _create(root, path, raw):
    _parents(root, path)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)


@contextmanager
def _lock(root):
    from .resources import _regular
    path = root / ".zest" / "documents.lock"
    _regular(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def initialize_documents(project):
    """Explicitly upgrade ignore rules and copy committed legacy documents.

    Existing shared documents always win. Changed sources produce a conflict for
    review instead of overwriting the team's document or its recorded origin.
    """
    from .resources import _directory, _regular, preflight_workspace
    root = Path(project["root_path"])
    identity = preflight_workspace(root)
    if identity is None or identity["project_id"] != project["id"]:
        raise FileReadError("resource_identity_mismatch", "请先接入项目，再建立共享文档。")
    _directory(root / ".zest" / "docs")
    _regular(root / ".gitignore")
    with _lock(root):
        return _initialize(project, root)


def _initialize(project, root):
    ignore = root / ".zest" / ".gitignore"
    previous = ignore.read_bytes() if ignore.exists() else None
    if previous not in (None, LEGACY_IGNORE, SHARED_IGNORE.encode()):
        raise FileReadError("resource_ignore_conflict", "已有自定义 .zest 忽略规则；未覆盖，请先核对共享范围。")
    state = inspect_repository(root)
    target = selected_commit(project, state)
    result = {"project_id": project["id"], "source_commit": target, "created": [],
              "updated": [], "conflicts": [], "skipped": [], "requires_review": True,
              "committed": False, "source_documents_deleted": False, "written_hashes": {}}
    imports_path = ".zest/docs/imports.json"
    _parents(root, imports_path)
    manifest_path = root / imports_path
    manifest = {"schema_version": 1, "status": "imported_unverified", "documents": []}
    if manifest_path.exists():
        from .files import MAX_FILE_BYTES, CREDENTIAL_PATTERNS
        import re
        with manifest_path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or any(re.search(p, raw) for p in CREDENTIAL_PATTERNS):
            raise FileReadError("document_manifest_invalid", "文档来源清单过大或包含凭据，未修改。")
        try:
            manifest = json.loads(raw)
            if (manifest["schema_version"] != 1 or manifest["status"] != "imported_unverified"
                    or not isinstance(manifest["documents"], list)
                    or not all(isinstance(d, dict) and shared_document(d["destination"])
                               and isinstance(d["source_sha256"], str) for d in manifest["documents"])):
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise FileReadError("document_manifest_invalid", "已有文档来源清单无效，未覆盖。") from exc
    known = {d["destination"]: d for d in manifest["documents"]}
    pending, scanned, total = [], 0, 0
    queue = deque([(target, "")]) if target else deque()
    while queue:
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            scanned += 1
            if scanned > MAX_ENTRIES:
                raise FileReadError("document_import_budget", "文档目录超过当前扫描容量，未写入迁移内容。")
            path = prefix + entry.path
            if entry.object_type == "tree":
                # Only legacy documentation roots; never traverse code/local state.
                if not prefix and path.casefold() not in ("docs", "doc"):
                    continue
                try:
                    validate_directory(path)
                except FileReadError:
                    continue
                queue.append((entry.object_id, path + "/"))
                continue
            if not legacy_document(path):
                continue
            try:
                _validate_path(path)
                raw = load_text_blob(root, entry)
            except FileReadError as exc:
                result["skipped"].append({"path": path, "code": exc.code})
                continue
            destination = ".zest/docs/imported/" + path
            if not shared_document(destination):
                result["skipped"].append({"path": path, "code": "unsupported_document"})
                continue
            _validate_path(destination)
            _parents(root, destination)
            digest = hashlib.sha256(raw).hexdigest()
            existing = root / destination
            origin = {"destination": destination, "source_path": path,
                      "source_commit": target, "source_blob_id": entry.object_id, "source_sha256": digest}
            if destination in known and known[destination]["source_sha256"] != digest:
                result["conflicts"].append({"path": path, "destination": destination, "code": "source_changed"})
                continue
            if existing.exists():
                if existing.read_bytes() != raw:
                    result["conflicts"].append({"path": path, "destination": destination, "code": "document_exists"})
                elif destination not in known:
                    # Recover provenance after interruption between copy and manifest.
                    manifest["documents"].append(origin)
                continue
            total += len(raw)
            if len(pending) >= MAX_DOCUMENTS or total > MAX_IMPORT_BYTES:
                raise FileReadError("document_import_budget", "文档超过当前迁移容量，未写入迁移内容。")
            pending.append((destination, raw))
            # Do not silently reassign an old document's provenance.
            if destination not in known:
                manifest["documents"].append(origin)
    from .repairs import validate_change_paths
    validate_change_paths([path for path, _ in pending])
    _parents(root, ".zest/docs/index.md")
    if selected_commit(project, inspect_repository(root)) != target:
        raise FileReadError("document_source_changed", "准备期间项目版本发生变化，未写入迁移内容。")
    if previous != SHARED_IGNORE.encode():
        ignore.write_bytes(SHARED_IGNORE.encode())
        result["updated"].append(".zest/.gitignore")
        result["written_hashes"][".zest/.gitignore"] = hashlib.sha256(SHARED_IGNORE.encode()).hexdigest()
    # Negate only the parent exclusion; the allowlist inside .zest controls children.
    if _git(root, "check-ignore", "--no-index", "--quiet", "--", ".zest/docs/index.md", allowed=(0, 1)).returncode == 0:
        root_ignore = root / ".gitignore"
        before = root_ignore.read_bytes() if root_ignore.exists() else b""
        root_ignore.write_bytes(before + (b"\n" if before and not before.endswith(b"\n") else b"")
                               + b"\n# Allow shared Zest documents; local files use .zest/.gitignore\n!/.zest/\n")
        result["updated"].append(".gitignore")
        result["written_hashes"][".gitignore"] = hashlib.sha256(root_ignore.read_bytes()).hexdigest()
    for destination, raw in pending:
        _create(root, destination, raw)
        result["created"].append(destination)
        result["written_hashes"][destination] = hashlib.sha256(raw).hexdigest()
    root.joinpath(".zest/docs").mkdir(exist_ok=True)
    if manifest["documents"]:
        raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        if not manifest_path.exists():
            _create(root, imports_path, raw)
            result["created"].append(imports_path)
            result["written_hashes"][imports_path] = hashlib.sha256(raw).hexdigest()
        elif manifest_path.read_bytes() != raw:
            staging = root / ".zest/documents-imports.tmp"
            from .resources import _regular
            _regular(staging)
            staging.write_bytes(raw)
            os.replace(staging, manifest_path)
            result["updated"].append(imports_path)
            result["written_hashes"][imports_path] = hashlib.sha256(raw).hexdigest()
    index_path = root / ".zest/docs/index.md"
    if not index_path.exists():
        links = "\n".join(f"- [{d['source_path']}]({quote(d['destination'][len('.zest/docs/'):])})" for d in manifest["documents"])
        body = ("# 项目文档\n\n项目文档统一维护在 .zest/docs/，随当前分支的 Git 版本共享。\n"
                "本地对话、数据库、缓存与执行记录不属于共享文档。\n\n"
                "## 接入资料\n\n迁入内容保留原文，仅作待核实资料，不表示 Zest 已理解或验证项目。\n"
                "来源版本与内容标识见 imports.json；后续修改维护此处，原 docs/ 不再由 Zest 维护。\n\n"
                + (links or "尚无已迁入资料，业务规则、接口和设计背景等待补充。") + "\n")
        _create(root, ".zest/docs/index.md", body.encode())
        result["created"].append(".zest/docs/index.md")
        result["written_hashes"][".zest/docs/index.md"] = hashlib.sha256(body.encode()).hexdigest()
    result["warnings"] = [{"code": "source_working_tree_excluded"}] if state.dirty else []
    return result
