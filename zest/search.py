"""Literal keyword retrieval from bounded, committed and allowed project text."""

from collections import Counter, deque
import hashlib
from pathlib import Path
import time

from .files import BLOCKED_DIRECTORIES, FileReadError, _validate_path, load_text_blob
from .git import inspect_repository, list_tree, verify_commit


MAX_SEARCH_FILES = 200
MAX_SEARCH_BYTES = 4 * 1024 * 1024
MAX_SEARCH_SECONDS = 15
MAX_PREVIEW_CHARS = 2000


def search_project(project: dict, query: str, *, commit: str | None = None,
                   limit: int = 20, case_sensitive: bool = False) -> dict:
    if not query.strip() or len(query) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise FileReadError("invalid_query", "关键词须为 1—200 字符的非空单行文本。")
    if not 1 <= limit <= 100:
        raise FileReadError("invalid_search_limit", "匹配数量须在 1—100 之间。")
    state = inspect_repository(project["root_path"])
    selected = commit.lower() if commit is not None else state.head_commit
    if selected is not None and not verify_commit(Path(state.root_path), selected):
        raise FileReadError("invalid_commit", "请指定本地存在的完整 commit ID。")
    result = {"project_id": project["id"], "query": query,
              "source": {"kind": "git_commit", "commit": selected, "working_tree_changes_included": False},
              "case_sensitive": case_sensitive, "matches": [], "scan_complete": True,
              "files_considered": 0, "files_scanned": 0, "bytes_scanned": 0,
              "skipped_counts": {}, "warnings": []}
    if state.dirty:
        result["warnings"].append({"code": "working_tree_excluded", "message": "搜索不包含未提交内容。"})
    if state.head_commit != project["git"]["head_commit"]:
        result["warnings"].append({"code": "registration_head_changed", "message": "登记 HEAD 已过时，登记记录未更新。"})
    if selected != state.head_commit:
        result["warnings"].append({"code": "historical_commit", "message": "搜索指定历史版本。"})
    if selected is None:
        result["warnings"].append({"code": "unborn_repository", "message": "仓库尚无提交，无已提交文件可搜索。"})
        return result

    root = Path(state.root_path)
    started = time.monotonic()
    queue = deque([(selected, "")])
    skips = Counter()
    needle = query if case_sensitive else query.casefold()

    def stop(code):
        result["scan_complete"] = False
        result["warnings"].append({"code": code, "message": "达到搜索预算，结果不代表全仓库完整匹配。"})

    while queue:
        if time.monotonic() - started > MAX_SEARCH_SECONDS:
            stop("search_time_limit")
            break
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            if time.monotonic() - started > MAX_SEARCH_SECONDS:
                stop("search_time_limit")
                break
            path = prefix + entry.path
            if entry.object_type == "tree":
                if entry.path.casefold() in BLOCKED_DIRECTORIES:
                    skips["blocked_directory"] += 1
                elif len(path.split("/")) >= 32:
                    skips["path_too_deep"] += 1
                else:
                    queue.append((entry.object_id, path + "/"))
                continue
            try:
                _validate_path(path)
            except FileReadError as exc:
                skips[exc.code] += 1
                continue
            if result["files_considered"] >= MAX_SEARCH_FILES:
                stop("search_file_limit")
                break
            result["files_considered"] += 1
            try:
                raw = load_text_blob(root, entry)
            except FileReadError as exc:
                skips[exc.code] += 1
                continue
            if result["bytes_scanned"] + len(raw) > MAX_SEARCH_BYTES:
                stop("search_byte_limit")
                break
            result["files_scanned"] += 1
            result["bytes_scanned"] += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            lines = raw.split(b"\n")
            if lines[-1] == b"":
                lines.pop()
            for number, data in enumerate(lines, 1):
                text = data.decode("utf-8")
                if text.endswith("\r") and (number < len(lines) or raw.endswith(b"\n")):
                    text = text[:-1]
                haystack = text if case_sensitive else text.casefold()
                if needle in haystack:
                    preview = text[:MAX_PREVIEW_CHARS]
                    result["matches"].append({
                        "evidence": {"path": path, "commit": selected, "blob_id": entry.object_id,
                                     "start_line": number, "end_line": number},
                        "text": preview, "text_truncated": len(preview) < len(text),
                        "match_in_preview": needle in (preview if case_sensitive else preview.casefold()),
                        "content_sha256": digest,
                    })
                    if len(result["matches"]) >= limit:
                        stop("search_match_limit")
                        break
            if not result["scan_complete"]:
                break
        if not result["scan_complete"]:
            break
    result["skipped_counts"] = dict(skips)
    return result
