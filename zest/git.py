"""Read repository identity and sampled Git state without changing the repository."""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID


class RepositoryError(Exception):
    """A repository could not be inspected within the supported scope."""


@dataclass(frozen=True)
class GitState:
    root_path: str
    head_commit: str | None
    branch: str | None
    detached: bool
    unborn: bool
    dirty: bool
    staged_changes: int
    unstaged_changes: int
    untracked_entries: int
    observed_at: str


def git_environment() -> dict[str, str]:
    # Inherited Git overrides must not redirect inspection to another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
               GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
    return env


def _git(root: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    overrides = []
    if args and args[0] == "status":
        # Status may compare working bytes through clean/process filters. Disable
        # configured drivers before sampling; reading config itself executes no filter.
        configured = _git(root, "config", "--null", "--name-only", "--get-regexp",
                          r"^filter\..*\.(clean|smudge|process|required)$", allowed=(0, 1))
        drivers = {os.fsdecode(key).rsplit(".", 1)[0] for key in configured.stdout.split(b"\0") if key}
        for driver in sorted(drivers):
            for field in ("clean", "smudge", "process"):
                overrides.extend(["-c", f"{driver}.{field}="])
            overrides.extend(["-c", f"{driver}.required=false"])
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
             *overrides, "-C", str(root), *args],
            env=git_environment(), capture_output=True, timeout=10, check=False,
        )
    except FileNotFoundError as exc:
        raise RepositoryError("未找到 Git，请先安装 Git。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryError("Git 查询超时，请稍后重试。") from exc
    except OSError as exc:
        raise RepositoryError("无法执行 Git 查询，请检查路径和权限。") from exc
    if result.returncode not in allowed:
        # Git stderr may include configuration details; do not return it verbatim.
        raise RepositoryError("Git 查询失败，请检查仓库是否完整且当前用户具有读取权限。")
    return result


def _head(root: Path) -> tuple[str | None, str | None]:
    commit_result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1))
    commit = commit_result.stdout.decode("ascii").strip() if commit_result.returncode == 0 else None
    branch_result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
    branch = os.fsdecode(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    return commit, branch


def _change_counts(status: bytes) -> tuple[int, int, int]:
    staged = unstaged = untracked = 0
    entries = iter(status.split(b"\0"))
    for entry in entries:
        if not entry:
            continue
        code = entry[:2]
        if code == b"??":
            untracked += 1
            continue
        if len(entry) < 3 or entry[2:3] != b" ":
            raise RepositoryError("无法解析 Git 工作区状态。")
        staged += code[0:1] != b" "
        unstaged += code[1:2] != b" "
        # In porcelain -z, a rename/copy has a second path, not another status row.
        if b"R" in code or b"C" in code:
            if not next(entries, b""):
                raise RepositoryError("Git 重命名状态不完整。")
    return staged, unstaged, untracked


def inspect_repository(path: str | Path) -> GitState:
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepositoryError("项目路径不存在或无法访问。") from exc
    if not root.is_dir():
        raise RepositoryError("项目路径必须是目录。")

    inside = _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip()
    if inside != b"true":
        raise RepositoryError("仅支持非 bare 的本地 Git 工作区。")
    actual_root = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").stdout.rstrip(b"\n"))).resolve()
    if actual_root != root:
        raise RepositoryError("请指定 Git 仓库根目录，而不是子目录。")

    commit, branch = _head(root)
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal").stdout
    if len(status) > 8 * 1024 * 1024:
        raise RepositoryError("工作区状态过大，超出当前模块支持范围。")
    if _head(root) != (commit, branch):
        raise RepositoryError("采集期间分支或 HEAD 发生变化，请重试。")
    staged, unstaged, untracked = _change_counts(status)
    return GitState(
        root_path=str(root), head_commit=commit, branch=branch,
        detached=commit is not None and branch is None, unborn=commit is None,
        dirty=bool(staged or unstaged or untracked), staged_changes=staged,
        unstaged_changes=unstaged, untracked_entries=untracked,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    object_type: str
    object_id: str

    @property
    def regular_file(self) -> bool:
        return self.object_type == "blob" and self.mode in ("100644", "100755")


def list_tree(root: Path, tree: str, paths: list[str] | None = None) -> list[TreeEntry]:
    """List a committed tree, without reading filesystem paths or following links."""
    args = ["ls-tree", "-z", "--full-tree", tree]
    if paths is not None:
        if not paths:
            return []
        args.extend(["--", *paths])
    raw = _git(root, *args).stdout
    if len(raw) > 2 * 1024 * 1024:
        raise RepositoryError("Git 目录信息过大，超出当前概览支持范围。")
    result = []
    for row in raw.split(b"\0"):
        if row:
            header, path = row.split(b"\t", 1)
            mode, object_type, object_id = header.decode("ascii").split(" ")
            result.append(TreeEntry(os.fsdecode(path), mode, object_type, object_id))
    if len(result) > 4096:
        raise RepositoryError("Git 目录条目过多，超出当前概览支持范围。")
    return result


def read_blob(root: Path, entry: TreeEntry, max_bytes: int) -> bytes | None:
    """Read a regular blob by object ID; never run textconv or checkout filters."""
    if not entry.regular_file:
        return None
    size = int(_git(root, "cat-file", "-s", entry.object_id).stdout)
    if size > max_bytes:
        return None
    return _git(root, "cat-file", "blob", entry.object_id).stdout


def verify_commit(root: Path, object_id: str) -> bool:
    """Only accept full local commit IDs, not arbitrary revision expressions."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", object_id):
        return False
    result = _git(root, "cat-file", "-t", object_id, allowed=(0, 1, 128))
    return result.returncode == 0 and result.stdout.strip() == b"commit"


def managed_ref(project_id: str) -> str:
    try:
        if str(UUID(project_id)) != project_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepositoryError("项目 ID 必须是完整 UUID。") from exc
    return f"refs/zest/active/{project_id}"


def read_direct_ref(root: Path, ref: str) -> str | None:
    symbolic = _git(root, "symbolic-ref", "--quiet", ref, allowed=(0, 1, 128))
    if symbolic.returncode == 0:
        raise RepositoryError("Zest 管理引用不能是符号引用。")
    result = _git(root, "show-ref", "--verify", "--hash", ref, allowed=(0, 1, 128))
    if result.returncode != 0:
        return None
    commit = result.stdout.decode().strip()
    if not verify_commit(root, commit):
        raise RepositoryError("Zest 管理引用未指向有效提交。")
    return commit


def selected_commit(project: dict, state: GitState, explicit: str | None = None) -> str | None:
    if explicit is not None:
        return explicit.lower()
    return read_direct_ref(Path(state.root_path), managed_ref(project["id"])) or state.head_commit
