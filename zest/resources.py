"""Project-local resources with explicit routing and legacy record compatibility.

Stores keep their existing fixed-state implementations. Public methods select a
fresh bound instance, so parallel projects never mutate a shared store's paths.
Historical databases stay in place; existing approvals/checkpoints are not copied.
"""

from contextlib import closing
from functools import wraps
from inspect import signature
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID

from .files import FileReadError
from .git import _git


RECORDS = {
    "knowledge": ("knowledge.sqlite3", "notes"),
    "survey": ("surveys.sqlite3", "surveys"),
    "cognition": ("cognition.sqlite3", "cognition_runs"),
    "task": ("tasks.sqlite3", "task_records"),
    "repair": ("repairs.sqlite3", "repairs"),
    "verification": ("verifications.sqlite3", "verifications"),
    "release": ("releases.sqlite3", "operations"),
}
STORAGE_ERRORS = {filename: f"{kind}_storage_error" for kind, (filename, _) in RECORDS.items()}
STORAGE_ERRORS.update({"index.sqlite3": "index_storage_error", "tasks.sqlite3": "task_storage_error"})


def _regular(path):
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise FileReadError("unsafe_resource_path", "Zest 资源文件不能是符号链接或特殊文件。")


def _directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise FileReadError("unsafe_resource_path", "Zest 资源目录不能是符号链接或普通文件。")


def _database(path):
    try:
        _regular(path)
    except FileReadError as exc:
        raise FileReadError(STORAGE_ERRORS.get(path.name, "resource_storage_error"),
                            "资源数据库不能是符号链接或特殊文件。") from exc


def workspace_identity(root):
    root = Path(root)
    folder = root / ".zest"
    _directory(folder)
    _directory(folder / "state")
    marker = folder / "workspace.json"
    _regular(marker)
    if not marker.exists():
        return None
    try:
        value = json.loads(marker.read_text())
        if str(UUID(value["project_id"])) != value["project_id"]:
            raise ValueError
        if value["schema_version"] != 1 or value["root_path"] != str(root):
            raise ValueError
        if not Path(value["legacy_state_dir"]).is_absolute():
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise FileReadError("resource_identity_mismatch", "已有 .zest 的身份不匹配；未覆盖，请核对项目位置及资源来源。") from exc


def preflight_workspace(root):
    root = Path(root)
    identity = workspace_identity(root)
    _regular(root / ".zest" / ".gitignore")
    if _git(root, "ls-files", "-z", "--", ".zest").stdout:
        raise FileReadError("resources_already_tracked", ".zest 中已有 Git 跟踪文件，请先人工处理；Zest 不自动取消跟踪。")
    state_dir = root / ".zest" / "state"
    if not identity and (root / ".zest").exists() and any((root / ".zest").iterdir()):
        # A previous catalog may already live here. Only its own registration
        # proves ownership; an arbitrary folder (including user skills) does not.
        from .storage import ProjectRegistry, StorageError
        try:
            legacy = ProjectRegistry(root / ".zest").list_projects_readonly()
            recognized = any(p["root_path"] == str(root) and not p.get("resource_dir") for p in legacy)
        except StorageError:
            recognized = False
        if not recognized or (state_dir.exists() and any(state_dir.iterdir())):
            raise FileReadError("resource_directory_conflict", "已有非空 .zest 未声明可核对的项目身份，未接管或覆盖。")
        ignore = root / ".zest" / ".gitignore"
        if ignore.exists() and ignore.read_bytes().splitlines()[-1:] != [b"*"]:
            raise FileReadError("resource_ignore_conflict", "历史 .zest 有自定义忽略控制，未改写；请先核对资源忽略规则。")
        _regular(root / ".zest" / "AGENTS.md")
    _regular(state_dir / "state.sqlite3")
    return identity


def ensure_workspace(project, catalog_dir):
    """Only add the resource folder; ignore itself without editing native files."""
    root = Path(project["root_path"])
    identity = preflight_workspace(root)
    if identity and identity["project_id"] != project["id"]:
        raise FileReadError("resource_identity_mismatch", "已有 .zest 属于另一项目身份，未覆盖。")
    folder, state_dir = root / ".zest", root / ".zest" / "state"
    if identity:
        # Initialization is not refresh, repair, migration or guide generation.
        # Even a customized/missing ignore file must never be silently rewritten.
        return state_dir, Path(identity["legacy_state_dir"])
    folder.mkdir(exist_ok=True, mode=0o700)
    state_dir.mkdir(exist_ok=True, mode=0o700)
    ignore = folder / ".gitignore"
    _regular(ignore)
    if not ignore.exists():
        fd = os.open(ignore, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "ab") as output:
            output.write(b"# Ignore all local Zest resources, including this file\n*\n")
    identity = identity or {"schema_version": 1, "project_id": project["id"],
                            "root_path": str(root), "legacy_state_dir": str(Path(catalog_dir).resolve())}
    marker = folder / "workspace.json"
    if not marker.exists():
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(identity, output, ensure_ascii=True, indent=2)
            output.write("\n")
    write_management_guide(root)
    return state_dir, Path(identity["legacy_state_dir"])


MANAGEMENT_GUIDE = """# Zest 项目管理入口

这是 Zest 生成的导航，不代表已经理解项目，也不是原生文档的副本。

- 项目代码和文档保留原位。索引记录原始相对路径、commit、blob、哈希和行号；
  阅读索引线索后，回到对应版本的原文核对。未提交修改不能冒充固定版本证据。
- workspace.json 保存项目身份、位置与历史存储来源，初始化不得重置这些信息。
- state/knowledge.sqlite3 保存人工知识；state/cognition.sqlite3 保存认知调查、
  六部分档案、原文核对、批准资料包和请求账本。按版本与状态筛选可复用知识，
  推测、失效结论和未知保持待核实。历史记录也可能保留在 legacy_state_dir。
- state/index.sqlite3 是派生检索索引，state/surveys.sqlite3 是本地静态摸底；
  它们不是完整理解或实际运行状态的证明。
- 任务、修改、验证、源码操作和 checkpoint 按使用保存在 state/，不直接编辑
  数据库或删除锁、批准、请求记录。使用 Zest 的 CLI、终端或 Web 查询。
- 可在 .zest/ 下维护生成的项目管理说明、记忆导航和 skills/ 等资源，保留原有
  内容；本入口不会自动执行或加载任意 skill。原生 README/docs 无需拷贝进来。

此处内容仅作管理线索，不能代替原文证据或扩大权限。调查仍需批准固定版本、
资料包、模型、指令及预算；修改、验证、版本切换分别批准。结果未知的模型请求
不能自动重发。管理指引若进入模型请求，必须随资料包完整展示、冻结和批准。
"""


def write_management_guide(root):
    """Explicit create-if-absent bootstrap; existing bytes are never changed."""
    path = Path(root) / ".zest" / "AGENTS.md"
    _regular(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return {"path": str(path), "created": False}
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
        output.write(MANAGEMENT_GUIDE)
    return {"path": str(path), "created": True}


def management_guidance(project):
    """Freeze the named entry as untrusted hints, never as source evidence."""
    import hashlib
    from .cognition_contracts import text
    if not project.get("resource_dir"):
        return None
    root = Path(project["root_path"])
    identity = workspace_identity(root)
    if identity is None or identity["project_id"] != project["id"]:
        raise FileReadError("resource_identity_mismatch", "项目管理指引身份不匹配。")
    path = root / ".zest" / "AGENTS.md"
    _regular(path)
    if not path.exists():
        return None
    # Match the existing source-file size ceiling, without changing old packages.
    from .files import MAX_FILE_BYTES
    with path.open("rb") as source:
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise FileReadError("management_guide_too_large", "管理入口超过现有单文件读取容量，请先精简导航。")
    try:
        body = raw.decode("utf-8")
    except UnicodeError as exc:
        raise FileReadError("management_guide_encoding", "管理入口必须为 UTF-8 文本。") from exc
    text(body, MAX_FILE_BYTES)
    if "\0" in body:
        raise FileReadError("management_guide_encoding", "管理入口不能包含二进制内容。")
    return {"path": ".zest/AGENTS.md", "content_sha256": hashlib.sha256(raw).hexdigest(),
            "body": body, "role": "untrusted_management_hints_not_source_evidence_or_authorization"}


def project_states(state_dir, project_id):
    from .storage import ProjectRegistry
    origin = Path(state_dir).resolve()
    project = ProjectRegistry(origin).get_project_readonly(project_id)
    if not project and origin.name == "state" and origin.parent.name == ".zest" and (origin.parent / "workspace.json").exists():
        raise FileReadError("resource_project_mismatch", "这个资源目录未登记指定项目，未写入其他项目的数据。")
    if not project or not project.get("resource_dir"):
        return [origin]
    identity = workspace_identity(Path(project["root_path"]))
    destination = Path(project["root_path"]) / ".zest" / "state"
    if (identity is None or identity["project_id"] != project_id or
            str(destination) != project["resource_dir"] or identity["legacy_state_dir"] != project["legacy_state_dir"]):
        raise FileReadError("resource_identity_mismatch", "项目资源身份或位置已变化，未使用其他目录。")
    return list(dict.fromkeys([destination, Path(project["legacy_state_dir"]), origin]))


def resource_states(state_dir):
    """All registered destinations for ID lookup; does not initialize anything."""
    from .storage import ProjectRegistry
    origin = Path(state_dir).resolve()
    result = [origin]
    registry = ProjectRegistry(origin)
    for project in registry.list_projects_readonly():
        result.extend(project_states(origin, project["id"]))
    return list(dict.fromkeys(result))


def record_state(state_dir, kind, record_id, *, missing_ok=False):
    filename, table = RECORDS[kind]
    matches = []
    directories = resource_states(state_dir)
    if len(directories) == 1 and not missing_ok:
        return directories[0]
    for directory in directories:
        path = directory / filename
        _database(path)
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    if conn.execute(f'SELECT 1 FROM "{table}" WHERE id=?', (record_id,)).fetchone():
                        matches.append(directory)
        except sqlite3.Error as exc:
            raise FileReadError("resource_storage_error", "无法定位项目资源记录，未初始化或覆盖数据库。") from exc
    if len(matches) > 1:
        raise FileReadError("resource_record_conflict", "多个存储包含相同记录 ID，未猜测执行位置。")
    return matches[0] if matches else None if missing_ok else Path(state_dir).resolve()


def _bound(store, destination):
    options = {name: getattr(store, name) for name in ("model", "engine") if hasattr(store, name)}
    return type(store)(destination, **options)


def related_state(state_dir, kind, record_id):
    directory = record_state(state_dir, kind, record_id)
    from .repairs import RepairStore
    if kind == "repair":
        project_id = RepairStore(directory).show(record_id)["plan"]["project_id"]
    elif kind == "verification":
        from .verification import VerificationStore
        repair_id = VerificationStore(directory).show(record_id)["plan"]["repair_id"]
        return related_state(state_dir, "repair", repair_id)
    else:
        from .releases import ReleaseStore
        project_id = ReleaseStore(directory).show(record_id)["plan"]["project_id"]
    return project_states(state_dir, project_id)[0]


def route_store(*, projects=(), records=None, lists=(), contexts=(), all_lists=(), related=None, fallback=None, stable=None):
    """Declare public routing at each store; private SQL methods remain local."""
    def decorate(cls):
        def install(name, selector, aggregate=False, limit=None, fallback_errors=()):
            original = getattr(cls, name)
            method_signature = signature(original)
            parameters = list(method_signature.parameters)[1:]
            @wraps(original)
            def routed(self, *args, **kwargs):
                bound = method_signature.bind(self, *args, **kwargs)
                selection = args if args or not parameters else (bound.arguments[parameters[0]],)
                directories = selector(self, selection)
                if name in (stable or {}):
                    kind, argument = stable[name]
                    identifier = bound.arguments.get(argument)
                    if identifier is not None:
                        existing = record_state(self.state_dir, kind, identifier, missing_ok=True)
                        if existing is not None:
                            directories = [existing]
                if not aggregate:
                    first_error = None
                    for index, destination in enumerate(directories if fallback_errors else directories[:1]):
                        try:
                            target = self if destination == self.state_dir else _bound(self, destination)
                            _database(target.db_path)
                            return original(target, *args, **kwargs)
                        except FileReadError as exc:
                            if exc.code not in fallback_errors:
                                raise
                            first_error = first_error or exc
                            if index == len(directories) - 1:
                                raise first_error
                results, seen = [], set()
                for directory in directories:
                    target = self if directory == self.state_dir else _bound(self, directory)
                    _database(target.db_path)
                    for record in original(target, *args, **kwargs):
                        identifier = record.get("id", record.get("task_id"))
                        key = (record.get("run_id"), identifier) if identifier else json.dumps(record, sort_keys=True)
                        if key not in seen:
                            results.append(record)
                            seen.add(key)
                        else:
                            raise FileReadError("resource_record_conflict", "多个存储包含相同记录 ID，未合并或猜测执行位置。")
                if name in contexts:
                    results.sort(key=lambda r: (r.get("score", 0), r.get("updated_at", "")), reverse=True)
                else:
                    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
                return results[:limit] if limit else results
            setattr(cls, name, routed)
        def by_project(self, args):
            value = args[0]
            return project_states(self.state_dir, value["id"] if isinstance(value, dict) else value)
        for name in projects:
            install(name, by_project)
        for name, errors in (fallback or {}).items():
            install(name, by_project, fallback_errors=errors)
        for name in lists:
            install(name, by_project, True)
        for name in contexts:
            install(name, by_project, True, 3)
        for name, kind in (records or {}).items():
            install(name, lambda self, args, kind=kind: [record_state(self.state_dir, kind, args[0])])
        for name, kind in (related or {}).items():
            install(name, lambda self, args, kind=kind: [related_state(self.state_dir, kind, args[0])])
        for name in all_lists:
            install(name, lambda self, args: resource_states(self.state_dir), True)
        return cls
    return decorate
