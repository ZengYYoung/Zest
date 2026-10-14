"""Read-only Python declarations from bounded, fixed Git blobs; never import code."""

import ast
from collections import Counter, deque
import hashlib
from pathlib import Path
import platform
import time

from .files import BLOCKED_DIRECTORIES, FileReadError, _validate_path, load_text_blob
from .git import inspect_repository, list_tree, selected_commit, verify_commit

MAX_FILES = 100
MAX_BYTES = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 4096
MAX_NODES = 20000
MAX_FILE_DECLARATIONS = 200
MAX_DECLARATIONS = 3000
MAX_SECONDS = 30
MAX_PATH_LENGTH = 1024


def _name(value, limit=200):
    if not isinstance(value, str) or len(value) > limit:
        raise FileReadError("structure_name_budget", "声明名称超过结构分析预算。")
    return value


def _declarations(raw, evidence):
    try:
        tree = ast.parse(raw.decode("utf-8"), filename=evidence["path"])
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise FileReadError("structure_parse_failed", "当前 Python 语法解析器无法分析该文件。") from exc
    symbols, imports = [], []
    # Iterative traversal avoids recursion on deeply nested expressions.
    stack = [(tree, ())]
    visited = 0
    while stack:
        node, scope = stack.pop()
        visited += 1
        if visited > MAX_NODES:
            raise FileReadError("structure_node_budget", "语法节点超过单文件分析预算。")
        location = {**evidence, "start_line": getattr(node, "lineno", 1),
                    "end_line": getattr(node, "end_lineno", 1)}
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = _name(node.name)
            lexical = _name(".".join((*scope, name)), 500)
            location["start_line"] = min([node.lineno, *[item.lineno for item in node.decorator_list]])
            symbols.append({"name": name, "lexical_name": lexical,
                            "kind": "class" if isinstance(node, ast.ClassDef) else
                            "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                            "evidence": location})
            scope = (*scope, name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if len(node.names) > 20:
                raise FileReadError("structure_import_budget", "单个 import 的名称数量超过分析预算。")
            imports.append({"kind": "from" if isinstance(node, ast.ImportFrom) else "import",
                            "module": _name(node.module or "", 500) if isinstance(node, ast.ImportFrom) else None,
                            "level": node.level if isinstance(node, ast.ImportFrom) else 0,
                            "scope": _name(".".join(scope), 500),
                            "names": [{"name": _name(item.name, 500), "alias": _name(item.asname) if item.asname else None}
                                      for item in node.names], "evidence": location})
        if len(symbols) + len(imports) > MAX_FILE_DECLARATIONS:
            raise FileReadError("structure_declaration_budget", "声明超过单文件分析预算。")
        stack.extend((child, scope) for child in reversed(list(ast.iter_child_nodes(node))))
    return symbols, imports


def build_structure(project, *, commit=None):
    state = inspect_repository(project["root_path"])
    root = Path(state.root_path)
    selected = selected_commit(project, state, commit)
    if selected is not None and not verify_commit(root, selected):
        raise FileReadError("invalid_commit", "需要本地存在的完整 commit。")
    result = {"project_id": project["id"], "backend": "python_ast_declarations", "commit": selected,
              "parser_python": platform.python_version(),
              "workspace_head": state.head_commit, "working_tree_changes_included": False,
              "modules": [], "files_considered": 0, "bytes_read": 0,
              "skipped_counts": {}, "warnings": [], "limitations": ["python_only", "declarations_not_runtime_dependencies",
                  "dynamic_imports_and_calls_not_resolved", "current_interpreter_grammar", "bounded_analysis"]}
    if selected is None:
        result["warnings"].append({"code": "unborn_repository"})
        return result
    skips = Counter()
    queue = deque([(selected, "")])
    entries_seen, declarations = 0, 0
    started = time.monotonic()
    exhausted = False
    while queue and not exhausted:
        if time.monotonic() - started > MAX_SECONDS:
            result["warnings"].append({"code": "structure_time_budget"})
            break
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            entries_seen += 1
            if entries_seen > MAX_TREE_ENTRIES or time.monotonic() - started > MAX_SECONDS:
                result["warnings"].append({"code": "structure_scan_budget"})
                exhausted = True
                break
            path = prefix + entry.path
            if entry.object_type == "tree":
                if entry.path.casefold() in BLOCKED_DIRECTORIES or len(path.split("/")) >= 32:
                    skips["excluded_directory"] += 1
                else:
                    queue.append((entry.object_id, path + "/"))
                continue
            if Path(path).suffix.casefold() != ".py":
                continue
            if result["files_considered"] >= MAX_FILES:
                result["warnings"].append({"code": "structure_file_budget"})
                exhausted = True
                break
            result["files_considered"] += 1
            try:
                if len(path) > MAX_PATH_LENGTH:
                    raise FileReadError("structure_path_budget", "路径长度超过结构元数据预算。")
                _validate_path(path)
                raw = load_text_blob(root, entry)
                if result["bytes_read"] + len(raw) > MAX_BYTES:
                    result["warnings"].append({"code": "structure_byte_budget"})
                    exhausted = True
                    break
                result["bytes_read"] += len(raw)
                evidence = {"path": path, "commit": selected, "blob_id": entry.object_id,
                            "content_sha256": hashlib.sha256(raw).hexdigest()}
                symbols, imports = _declarations(raw, evidence)
                if declarations + len(symbols) + len(imports) > MAX_DECLARATIONS:
                    result["warnings"].append({"code": "structure_total_declaration_budget"})
                    exhausted = True
                    break
                declarations += len(symbols) + len(imports)
                result["modules"].append({"path": path, "evidence": evidence,
                                          "symbols": symbols, "imports": imports})
            except FileReadError as exc:
                skips[exc.code] += 1
    result["skipped_counts"] = dict(skips)
    result["modules"].sort(key=lambda item: item["path"])
    if skips:
        result["warnings"].append({"code": "structure_skipped_sources"})
    return result


def lookup_symbols(project, query, *, commit=None, limit=10):
    if (not isinstance(query, str) or not query.strip() or len(query) > 200
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20):
        raise FileReadError("invalid_symbol_query", "符号查询须为非空且不超过 200 字符，数量为 1—20。")
    structure = build_structure(project, commit=commit)
    needle = query.strip().casefold()
    matches = []
    for module in structure["modules"]:
        for symbol in module["symbols"]:
            if needle in symbol["lexical_name"].casefold() or needle in module["path"].casefold():
                matches.append(symbol)
    return {"project_id": project["id"], "commit": structure["commit"], "query": query,
            "matches": matches[:limit], "truncated": len(matches) > limit,
            "warnings": structure["warnings"], "skipped_counts": structure["skipped_counts"],
            "limitations": structure["limitations"]}
