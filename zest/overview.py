"""Bounded, deterministic overview of a registered project's committed files."""

from dataclasses import asdict
import json
from pathlib import Path
import tomllib

from .git import TreeEntry, inspect_repository, list_tree, read_blob


MAX_MANIFEST_BYTES = 256 * 1024
MAX_REFERENCES = 50
MANIFESTS = {
    "pyproject.toml": "python", "requirements.txt": "python",
    "package.json": "node", "pom.xml": "java",
    "build.gradle": "jvm", "build.gradle.kts": "jvm",
    "go.mod": "go", "Cargo.toml": "rust",
}
READMES = {"README", "README.md", "README.rst", "README.txt"}
EXCLUDED_DIRECTORIES = {
    ".git", ".zest", "node_modules", ".venv", "venv", "__pycache__",
    "build", "dist", "target", ".idea", ".gradle", ".pytest_cache",
}
ENTRYPOINTS = {
    "__main__.py", "main.py", "app.py", "src/main.py", "src/app.py",
    "index.js", "index.ts", "src/index.js", "src/index.ts",
    "server.js", "server.ts", "src/main.js", "src/main.ts",
}


def _evidence(entry: TreeEntry, commit: str, path: str | None = None) -> dict:
    return {"path": path or entry.path, "commit": commit, "blob_id": entry.object_id}


def _string_fields(data: dict, names: tuple[str, ...]) -> dict:
    result = {}
    for name in names:
        if name in data:
            if not isinstance(data[name], str):
                raise ValueError("Expected a string field")
            result[name] = data[name]
    return result


def _string_mapping(value: object) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("Expected string mapping")
    return value


def _string_list(value: object) -> list:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected string list")
    return value


def _python_manifest(raw: bytes) -> tuple[dict, dict]:
    data = tomllib.loads(raw.decode("utf-8"))
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise ValueError("Invalid project table")
    metadata = _string_fields(project, ("name", "version", "requires-python"))
    metadata["dependencies"] = _string_list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("Invalid optional dependency table")
    metadata["optional-dependencies"] = {name: _string_list(values) for name, values in optional.items()}
    if "dynamic" in project:
        metadata["dynamic"] = _string_list(project["dynamic"])
    return metadata, _string_mapping(project.get("scripts", {}))


def _node_manifest(raw: bytes) -> tuple[dict, dict]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid package object")
    metadata = _string_fields(data, ("name", "version", "main", "type"))
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        metadata[section] = _string_mapping(data.get(section, {}))
    return metadata, _string_mapping(data.get("scripts", {}))


def build_overview(project: dict) -> dict:
    state = inspect_repository(project["root_path"])
    root = Path(state.root_path)
    commit = state.head_commit
    registered_commit = project["git"]["head_commit"]
    overview = {
        "project_id": project["id"], "root_path": state.root_path,
        "source": {"kind": "git_commit", "commit": commit,
                   "working_tree_changes_included": False},
        "registered_head_commit": registered_commit,
        "registration_head_changed": registered_commit != commit,
        "git": asdict(state), "root_directories": [], "manifests": [],
        "documents": [], "declared_commands": [], "entrypoint_candidates": [],
        "warnings": [],
    }
    warnings = overview["warnings"]
    if registered_commit != commit:
        warnings.append({"code": "registration_head_changed",
                         "message": "HEAD 与上次登记不同；概览使用本次采集的 commit，登记记录未自动更新。"})
    if state.dirty:
        warnings.append({"code": "working_tree_excluded",
                         "message": "工作区存在未提交修改；概览不包含暂存、未暂存和未跟踪内容。"})
    if commit is None:
        warnings.append({"code": "unborn_repository", "message": "仓库尚无提交，暂无可概览的已提交文件。"})
        return overview

    entries = {entry.path: entry for entry in list_tree(root, commit)}
    directories = sorted(name for name, entry in entries.items()
                         if entry.object_type == "tree" and name not in EXCLUDED_DIRECTORIES)
    overview["root_directories"] = directories[:MAX_REFERENCES]
    if len(directories) > MAX_REFERENCES:
        warnings.append({"code": "directory_limit", "message": "根目录列表仅展示前 50 项。"})

    for name, ecosystem in MANIFESTS.items():
        entry = entries.get(name)
        if entry is None:
            continue
        if not entry.regular_file:
            warnings.append({"code": "unsupported_file_type", "path": name,
                             "message": "该声明路径不是普通文件，未读取或跟随链接。"})
            continue
        evidence = _evidence(entry, commit)
        manifest = {"path": name, "ecosystem": ecosystem,
                    "status": "inventory_only", "evidence": evidence}
        overview["manifests"].append(manifest)
        if name not in ("pyproject.toml", "package.json"):
            continue
        raw = read_blob(root, entry, MAX_MANIFEST_BYTES)
        if raw is None:
            manifest["status"] = "too_large"
            warnings.append({"code": "manifest_too_large", "path": name,
                             "message": "声明文件超过 256 KiB，仅记录位置。"})
            continue
        try:
            parser = _python_manifest if name == "pyproject.toml" else _node_manifest
            metadata, commands = parser(raw)
        except (ValueError, UnicodeError, RecursionError):
            manifest["status"] = "invalid"
            warnings.append({"code": "invalid_manifest", "path": name,
                             "message": "声明文件格式或字段类型不受支持，未推断其内容。"})
            continue
        manifest.update(status="parsed", metadata=metadata)
        # Declarations are reported as data, never treated as execution permission.
        overview["declared_commands"].extend(
            {"name": command, "target": target, "ecosystem": ecosystem, "evidence": evidence}
            for command, target in sorted(commands.items())
        )

    for name in sorted(READMES):
        entry = entries.get(name)
        if entry and entry.regular_file:
            overview["documents"].append(_evidence(entry, commit))
        elif entry:
            warnings.append({"code": "unsupported_file_type", "path": name,
                             "message": "文档路径不是普通文件，未读取或跟随链接。"})

    for directory in ("docs", "doc"):
        parent = entries.get(directory)
        if parent and parent.object_type == "tree":
            documents = [item for item in list_tree(root, parent.object_id)
                         if item.regular_file and Path(item.path).suffix.lower() in (".md", ".rst", ".txt")]
            for item in sorted(documents, key=lambda item: item.path)[:MAX_REFERENCES]:
                overview["documents"].append(_evidence(item, commit, f"{directory}/{item.path}"))
            if len(documents) > MAX_REFERENCES:
                warnings.append({"code": "document_limit", "path": directory,
                                 "message": "该文档目录仅展示前 50 项，不递归扫描。"})

    candidates = sorted(ENTRYPOINTS | {f"{directory}/__main__.py" for directory in directories[:MAX_REFERENCES]})
    for entry in list_tree(root, commit, candidates):
        if entry.path in candidates and entry.regular_file:
            overview["entrypoint_candidates"].append({"kind": "convention_candidate",
                                                      "evidence": _evidence(entry, commit)})
    return overview
