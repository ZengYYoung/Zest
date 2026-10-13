"""Read bounded text evidence from a fixed, committed project version."""

import hashlib
from pathlib import Path, PurePosixPath
import re

from .git import inspect_repository, list_tree, read_blob, verify_commit
from .overview import EXCLUDED_DIRECTORIES


MAX_FILE_BYTES = 256 * 1024
MAX_READ_LINES = 200
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".scala", ".sh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
    ".md", ".rst", ".txt", ".json", ".toml", ".yaml", ".yml", ".xml",
    ".ini", ".cfg", ".properties", ".gradle",
}
TEXT_NAMES = {"readme", "license", "dockerfile", "makefile", ".gitignore", ".gitattributes", ".editorconfig",
              "uv.lock", "poetry.lock", "cargo.lock", "yarn.lock", "pnpm-lock.yaml"}
BLOCKED_DIRECTORIES = EXCLUDED_DIRECTORIES | {".aws", ".ssh", ".kube", "secrets", "credentials"}
CREDENTIAL_PATTERNS = (
    rb"gh[pousr]_[A-Za-z0-9]{20,}", rb"github_pat_[A-Za-z0-9_]{20,}",
    rb"sk-[A-Za-z0-9_-]{20,}", rb"AKIA[0-9A-Z]{16}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
)


class FileReadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_path(path: str) -> list[str]:
    parts = path.split("/")
    if (not path or PurePosixPath(path).is_absolute() or "\\" in path
            or re.match(r"^[A-Za-z]:", path) or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in ("", ".", "..") for part in parts)):
        raise FileReadError("invalid_path", "请使用规范的仓库相对路径，不含绝对路径、点目录或控制字符。")
    if len(parts) > 32:
        raise FileReadError("path_too_deep", "路径超过当前支持的 32 层。")
    lowered = [part.casefold() for part in parts]
    if any(part in BLOCKED_DIRECTORIES for part in lowered[:-1]):
        raise FileReadError("blocked_path", "该目录不在当前文件读取范围内。")
    name = lowered[-1]
    if name.startswith((".env", "credentials", "secrets", "id_rsa", "id_ed25519")):
        raise FileReadError("blocked_path", "该文件可能包含凭据，不在当前文件读取范围内。")
    if PurePosixPath(name).suffix not in TEXT_SUFFIXES and name not in TEXT_NAMES:
        raise FileReadError("unsupported_extension", "当前只支持允许的源码、文档和文本配置文件。")
    return parts


def _find_file(root: Path, commit: str, parts: list[str]):
    # Walk exact tree entry names: user paths are not passed as Git patterns.
    tree = commit
    for index, part in enumerate(parts):
        entry = next((item for item in list_tree(root, tree) if item.path == part), None)
        if entry is None:
            raise FileReadError("file_not_found", "指定 commit 中不存在该文件。")
        if index < len(parts) - 1:
            if entry.object_type != "tree":
                raise FileReadError("unsupported_file_type", "路径经过符号链接、子模块或非目录，未跟随。")
            tree = entry.object_id
        elif not entry.regular_file:
            raise FileReadError("unsupported_file_type", "指定路径不是普通文件，未读取或跟随链接。")
        else:
            return entry
    raise FileReadError("file_not_found", "指定 commit 中不存在该文件。")


def load_text_blob(root: Path, entry) -> bytes:
    """Shared size, encoding and credential checks for read/search consumers."""
    if not entry.regular_file:
        raise FileReadError("unsupported_file_type", "指定路径不是普通文件。")
    raw = read_blob(root, entry, MAX_FILE_BYTES)
    if raw is None:
        raise FileReadError("file_too_large", "文件超过 256 KiB，超出当前读取范围。")
    if b"\0" in raw:
        raise FileReadError("binary_file", "文件包含二进制数据，未返回内容。")
    try:
        raw.decode("utf-8")
    except UnicodeError as exc:
        raise FileReadError("unsupported_encoding", "当前只读取 UTF-8 文本文件。") from exc
    if any(re.search(pattern, raw) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_content", "文件包含已识别的凭据模式，未返回内容。")
    return raw


def read_project_file(project: dict, path: str, *, commit: str | None = None,
                      start_line: int = 1, end_line: int | None = None) -> dict:
    parts = _validate_path(path)
    if start_line < 1 or (end_line is not None and end_line < start_line):
        raise FileReadError("invalid_line_range", "行号从 1 开始，结束行不能小于开始行。")
    if end_line is not None and end_line - start_line + 1 > MAX_READ_LINES:
        raise FileReadError("line_limit", "每次最多读取 200 行，请缩小行号范围。")

    state = inspect_repository(project["root_path"])
    root = Path(state.root_path)
    selected_commit = commit.lower() if commit is not None else state.head_commit
    if selected_commit is None:
        raise FileReadError("unborn_repository", "仓库尚无提交，暂无可读取的已提交文件。")
    if not verify_commit(root, selected_commit):
        raise FileReadError("invalid_commit", "请指定该仓库中存在的完整 commit ID；不接受分支名、缩写或其他 Git 表达式。")

    entry = _find_file(root, selected_commit, parts)
    raw = load_text_blob(root, entry)

    # Physical line numbers are defined by LF, not Unicode paragraph separators.
    physical_lines = raw.split(b"\n")
    if physical_lines[-1] == b"":
        physical_lines.pop()
    total = len(physical_lines)
    if total and start_line > total:
        raise FileReadError("invalid_line_range", "开始行超出文件总行数。")
    if not total and start_line != 1:
        raise FileReadError("invalid_line_range", "空文件只支持默认开始行 1。")
    actual_end = min(end_line if end_line is not None else start_line + MAX_READ_LINES - 1, total)
    selected = physical_lines[start_line - 1:actual_end]
    excerpt = b"\n".join(selected)
    if total and (actual_end < total or raw.endswith(b"\n")):
        excerpt += b"\n"
    lines = []
    for number, line in enumerate(selected, start=start_line):
        # Remove CR only when it is part of a physical CRLF line ending.
        if line.endswith(b"\r") and (number < total or raw.endswith(b"\n")):
            line = line[:-1]
        lines.append({"number": number, "text": line.decode("utf-8")})

    warnings = []
    if state.dirty:
        warnings.append({"code": "working_tree_excluded", "message": "读取的是已提交版本，不包含未提交修改。"})
    if project["git"]["head_commit"] != state.head_commit:
        warnings.append({"code": "registration_head_changed", "message": "当前 HEAD 与上次登记不同，登记记录未自动更新。"})
    if selected_commit != state.head_commit:
        warnings.append({"code": "historical_commit", "message": "读取的是指定历史版本，不是当前 HEAD。"})
    return {
        "project_id": project["id"], "root_path": state.root_path,
        "source": {"kind": "git_commit", "commit": selected_commit,
                   "working_tree_changes_included": False},
        "evidence": {"path": path, "commit": selected_commit, "blob_id": entry.object_id,
                     "start_line": start_line if total else None, "end_line": actual_end if total else None},
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "excerpt_sha256": hashlib.sha256(excerpt).hexdigest(),
        "total_lines": total, "lines": lines, "content": excerpt.decode("utf-8"),
        "more_before": total > 0 and start_line > 1, "more_after": actual_end < total,
        "warnings": warnings,
    }
