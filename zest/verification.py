"""Version-bound local Docker verification; project scripts never run on the host."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import threading
import time
from uuid import uuid4

from .files import CREDENTIAL_PATTERNS, FileReadError, _validate_path, load_text_blob
from .git import _git, list_tree, verify_commit
from .repairs import RepairStore, digest


MAX_FILES = 500
MAX_BYTES = 8 * 1024 * 1024
MAX_LOG = 64 * 1024
COMMAND = ["-m", "unittest", "discover", "-s", "tests", "-q"]
POLICY = {"network": "none", "user": "65534:65534", "read_only": True, "cap_drop": "ALL",
          "no_new_privileges": True, "pids": 64, "cpus": 1, "memory_mb": 256, "tmp_mb": 64,
          "mounts": "snapshot_only_readonly", "pull": "never", "entrypoint": "python"}


def baseline_strategy(repair, manifest):
    has_tests = any(entry["path"].startswith("tests/") and PurePosixPath(entry["path"]).name.startswith("test")
                    and entry["path"].endswith(".py") for entry in manifest)
    only_creates = all(change.get("operation") == "create" for change in repair["plan"]["changes"])
    return "not_applicable_initial_creation" if only_creates and not has_tests else "run"


def _candidate(record):
    if record["status"] != "candidate_ready":
        raise FileReadError("candidate_required", "验证需要已批准并完成的候选修改。")
    root = Path(record["plan"]["root_path"])
    candidate, base = record["candidate_commit"], record["plan"]["base_commit"]
    if not isinstance(candidate, str) or not verify_commit(root, candidate):
        raise FileReadError("candidate_changed", "候选提交不存在或不受支持。")
    ref = f"refs/zest/candidates/{record['id']}"
    actual = _git(root, "rev-parse", "--verify", ref, allowed=(0, 1, 128))
    parents = _git(root, "rev-list", "--parents", "-n", "1", candidate).stdout.decode().split()
    changed = _git(root, "diff-tree", "--no-ext-diff", "--no-renames", "--no-commit-id", "--name-only",
                   "-z", "-r", base, candidate).stdout.split(b"\0")
    paths = {os.fsdecode(path) for path in changed if path}
    if (actual.stdout.strip().decode() != candidate or parents != [candidate, base]
            or paths != {change["path"] for change in record["plan"]["changes"]}):
        raise FileReadError("candidate_changed", "候选 ref、父提交或变更范围与批准方案不匹配。")
    return root, base, candidate


def collect_snapshot(root, commit):
    if not verify_commit(root, commit):
        raise FileReadError("invalid_commit", "验证版本必须是本地完整 commit ID。")
    queue, files, total, entries, started = [("", commit)], [], 0, 0, time.monotonic()
    while queue:
        prefix, tree = queue.pop()
        for entry in list_tree(root, tree):
            path = prefix + entry.path
            entries += 1
            if entries > 1000 or time.monotonic() - started > 30:
                raise FileReadError("snapshot_limit", "验证快照目录或采集时长超过预算。")
            if entry.object_type == "tree":
                # File validation at leaves also checks each parent directory.
                queue.append((path + "/", entry.object_id))
                continue
            _validate_path(path)
            raw = load_text_blob(root, entry)
            total += len(raw)
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise FileReadError("snapshot_limit", "验证快照超过 500 个文件或 8 MiB。")
            files.append((path, entry.mode, raw))
    files.sort(key=lambda item: item[0])
    manifest = [{"path": path, "mode": mode, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                for path, mode, raw in files]
    return manifest, files


def _export(files, destination):
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    destination.chmod(0o755)
    for path, mode, raw in files:
        target = destination.joinpath(*_validate_path(path))
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(raw)
        target.chmod(0o755 if mode == "100755" else 0o644)


def _manifest_on_disk(snapshot, expected):
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise FileReadError("snapshot_changed", "验证快照目录不存在或变为链接。")
    actual_paths = set()
    for count, path in enumerate(snapshot.rglob("*"), 1):
        if count > MAX_FILES * 33 + 1:
            raise FileReadError("snapshot_changed", "验证目录的额外条目超过核对预算。")
        if not path.is_dir() or path.is_symlink():
            actual_paths.add(str(path.relative_to(snapshot)))
        if len(actual_paths) > len(expected):
            raise FileReadError("snapshot_changed", "验证目录出现额外文件。")
    if actual_paths != {entry["path"] for entry in expected}:
        raise FileReadError("snapshot_changed", "本地验证快照出现额外或缺失文件。")
    for entry in expected:
        path = snapshot / entry["path"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise FileReadError("snapshot_changed", "本地验证快照在执行期间改变。")
        with path.open("rb") as source:
            raw = source.read(entry["bytes"] + 1)
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise FileReadError("snapshot_changed", "本地验证快照在执行期间改变。")


def _redact(raw):
    for pattern in CREDENTIAL_PATTERNS:
        raw = re.sub(pattern, b"[REDACTED]", raw)
    return raw.decode("utf-8", errors="replace")


class DockerEngine:
    backend = "docker"

    def __init__(self, socket_path=None):
        requested = Path(socket_path) if socket_path else Path.home() / ".docker/run/docker.sock"
        try:
            self.socket = requested.expanduser().resolve(strict=True)
            if not stat.S_ISSOCK(self.socket.stat().st_mode):
                raise OSError
        except (OSError, RuntimeError) as exc:
            raise FileReadError("docker_unavailable", "需要已运行的本地 Docker Unix socket。") from exc

    def _command(self, *args):
        return ["docker", "--host", "unix://" + str(self.socket), *args]

    @staticmethod
    def _env():
        return {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}

    def _control(self, *args, timeout=10):
        try:
            result = subprocess.run(self._command(*args), env=self._env(), capture_output=True,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FileReadError("verification_timeout", "容器验证超过批准时限。") from exc
        except OSError as exc:
            raise FileReadError("docker_unavailable", "无法连接本地 Docker。") from exc
        if result.returncode:
            raise FileReadError("docker_operation_failed", "本地 Docker 操作失败，未回显原始错误。")
        return result.stdout

    def resolve_image(self, image):
        if (not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,199}", image)):
            raise FileReadError("invalid_image", "镜像必须是有效本地镜像名或完整 ID。")
        image_id = self._control("image", "inspect", "--format", "{{.Id}}", image).decode().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise FileReadError("invalid_image", "无法得到完整镜像 ID。")
        return image_id

    def run(self, plan, snapshot, run_id):
        if self.resolve_image(plan["image_id"]) != plan["image_id"]:
            raise FileReadError("image_changed", "批准的镜像已改变。")
        if any(char in str(snapshot) for char in ",\n\r"):
            raise FileReadError("invalid_snapshot_path", "快照目录不能包含逗号或换行。")
        name, container, result = f"zest-verify-{run_id}", None, {}
        try:
            container = self._control(
                "create", "--name", name, "--label", f"zest.verification={run_id}", "--pull", "never",
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "64", "--cpus", "1",
                "--memory", "256m", "--memory-swap", "256m", "--user", "65534:65534",
                "--ulimit", "nofile=128:128", "--no-healthcheck",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
                "--mount", f"type=bind,src={snapshot},dst=/workspace,readonly",
                "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1", "--env", "HOME=/tmp",
                "--workdir", "/workspace", "--entrypoint", "python", plan["image_id"], *COMMAND,
            ).decode().strip()
            if not re.fullmatch(r"[a-f0-9]{64}", container):
                raise FileReadError("docker_operation_failed", "Docker 返回了无效容器 ID。")
            host = json.loads(self._control("inspect", "--format", "{{json .HostConfig}}", container))
            config = json.loads(self._control("inspect", "--format", "{{json .Config}}", container))
            mounts = host.get("Mounts", [])
            policy_verified = (
                host.get("NetworkMode") == "none" and host.get("ReadonlyRootfs") is True
                and host.get("Privileged") is False and host.get("PidsLimit") == 64
                and host.get("Memory") == 256 * 1024 * 1024 and host.get("MemorySwap") == 256 * 1024 * 1024
                and host.get("NanoCpus") == 1_000_000_000 and "ALL" in host.get("CapDrop", [])
                and any(value.startswith("no-new-privileges") for value in host.get("SecurityOpt", []))
                and config.get("User") == "65534:65534" and config.get("Entrypoint") == ["python"]
                and not config.get("Volumes")
                and len(mounts) == 1 and mounts[0].get("Type") == "bind"
                and mounts[0].get("Source") == str(snapshot) and mounts[0].get("Target") == "/workspace"
                and mounts[0].get("ReadOnly") is True)
            if not policy_verified:
                raise FileReadError("container_policy_mismatch", "Docker 实际容器配置未匹配批准限制，未执行测试。")
            self._control("start", container)
            code = int(self._control("wait", container, timeout=plan["timeout_seconds"]).strip())
            inspected = json.loads(self._control("inspect", "--format", "{{json .State}}", container))
            # Bound client-side log memory as well as Docker's log rotation.
            process = subprocess.Popen(self._command("logs", container), env=self._env(),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            expired = threading.Event()
            def stop_logs():
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass
            timer = threading.Timer(10, stop_logs)
            timer.daemon = True
            timer.start()
            try:
                raw = process.stdout.read(MAX_LOG + 1)
            finally:
                timer.cancel()
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            text = _redact(raw[:MAX_LOG])
            counts = re.findall(r"Ran (\d+) tests? in", text)
            tests_run = int(counts[-1]) if counts else 0
            passed = (code == 0 and inspected.get("ExitCode") == code and tests_run > 0 and inspected.get("OOMKilled") is False
                      and len(raw) <= MAX_LOG and not expired.is_set())
            result = {"status": "passed" if passed else "failed", "exit_code": code, "tests_run": tests_run,
                      "oom_killed": inspected.get("OOMKilled"), "output": text, "output_truncated": len(raw) > MAX_LOG,
                      "container_id": container, "cleanup_ok": False, "policy_verified": policy_verified}
        except FileReadError as exc:
            result = {"status": "timed_out" if exc.code == "verification_timeout" else "error",
                      "error": {"code": exc.code, "message": str(exc)}, "cleanup_ok": False}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            result = {"status": "error", "error": {"code": "docker_result_error",
                      "message": "无法验证 Docker 执行结果，未回显原始异常。"}, "cleanup_ok": False}
        finally:
            # Name is generated, never supplied by project code; also clean up create timeouts.
            try:
                self._control("rm", "--force", name)
                result["cleanup_ok"] = True
            except FileReadError:
                result["cleanup_ok"] = False
                result["status"] = "error"
                result["error"] = {"code": "container_cleanup_failed", "message": "容器清理未确认，需要检查后再继续。"}
        return result


class VerificationStore:
    def __init__(self, state_dir, *, engine=None):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "verifications.sqlite3"
        self.engine = engine

    @contextmanager
    def _open(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(sqlite3.connect(self.db_path)) as conn:
                if conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise FileReadError("verification_version_error", "验证数据库版本不兼容。")
                with conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS verifications (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
                    conn.execute("PRAGMA user_version = 1")
                yield conn
        except (OSError, sqlite3.Error) as exc:
            raise FileReadError("verification_storage_error", "无法访问验证状态。") from exc

    @staticmethod
    def _record(conn, verification_id):
        row = conn.execute("SELECT record FROM verifications WHERE id=?", (verification_id,)).fetchone()
        if row is None:
            raise FileReadError("verification_not_found", "未找到该验证。")
        try:
            record = json.loads(row[0])
            if (record["id"] != verification_id or record["status"] not in (
                    "awaiting_approval", "running", "passed", "failed", "error", "rejected")
                    or digest(record["plan"]) != record["plan_sha256"]):
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise FileReadError("verification_record_corrupt", "验证记录损坏。") from exc
        if record["status"] == "running":
            record["status"] = "pending_execution"
        return record

    @staticmethod
    def _save(conn, record):
        with conn:
            conn.execute("INSERT OR REPLACE INTO verifications VALUES (?,?)", (record["id"], json.dumps(record)))

    def create(self, repair_id, image, *, timeout=30, socket_path=None):
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise FileReadError("invalid_timeout", "验证时限须为 1—120 秒。")
        repair = RepairStore(self.state_dir).show(repair_id)
        root, base, candidate = _candidate(repair)
        base_manifest, _ = collect_snapshot(root, base)
        manifest, _ = collect_snapshot(root, candidate)
        expected = {change["path"]: change for change in repair["plan"]["changes"]}
        if not set(expected).issubset({entry["path"] for entry in manifest}):
            raise FileReadError("candidate_changed", "候选缺少批准的替换文件。")
        for entry in manifest:
            if entry["path"] in expected:
                change = expected[entry["path"]]
                if entry["sha256"] != change["new_sha256"] or entry["mode"] != change["mode"]:
                    raise FileReadError("candidate_changed", "候选文件内容与批准方案不匹配。")
        engine = self.engine or DockerEngine(socket_path)
        plan = {"repair_id": repair_id, "repair_plan_sha256": repair["plan_sha256"],
                "root_path": str(root), "base_commit": base, "candidate_commit": candidate,
                "base_manifest": base_manifest, "candidate_manifest": manifest,
                "image_id": engine.resolve_image(image), "socket_path": str(engine.socket), "backend": engine.backend,
                "command": ["python", *COMMAND], "timeout_seconds": timeout, "policy": POLICY}
        plan["baseline_strategy"] = baseline_strategy(repair, base_manifest)
        record = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                  "status": "awaiting_approval", "plan": plan, "plan_sha256": digest(plan),
                  "result": None, "events": [{"step": "prepared"}]}
        with self._open() as conn:
            self._save(conn, record)
        return record

    def show(self, verification_id):
        if not self.db_path.exists():
            raise FileReadError("verification_not_found", "未找到该验证。")
        with self._open() as conn:
            return self._record(conn, verification_id)

    def list_verifications(self, project_id):
        """Bounded read-only summaries; project identity comes from the linked repair.

        Older plans have no project_id, so keep their approved payload unchanged.
        This currently filters the latest 100 global records, without pagination.
        """
        if not self.db_path.exists():
            return []
        repairs = RepairStore(self.state_dir)
        summaries = []
        with self._open() as conn:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM verifications ORDER BY json_extract(record, '$.created_at') DESC, id DESC LIMIT 100")]
            for verification_id in ids:
                record = self._record(conn, verification_id)
                plan = record["plan"]
                repair = repairs.show(plan["repair_id"])
                if repair["project_id"] != project_id:
                    continue
                summaries.append({"id": record["id"], "project_id": repair["project_id"],
                                  "created_at": record["created_at"], "status": record["status"],
                                  "kind": "测试验证", "repair_id": plan["repair_id"],
                                  "backend": plan["backend"], "base_commit": plan["base_commit"],
                                  "candidate_commit": plan["candidate_commit"]})
        return summaries

    def decide(self, verification_id, *, approved, plan_sha256=None):
        with RepairStore(self.state_dir)._lock(verification_id), self._open() as conn:
            record = self._record(conn, verification_id)
            if record["status"] != "awaiting_approval":
                raise FileReadError("verification_not_pending", "验证当前未等待批准，不能重复运行。")
            if approved and record["plan_sha256"] != plan_sha256:
                raise FileReadError("verification_approval_mismatch", "验证批准哈希不匹配。")
            if not approved:
                record["status"] = "rejected"
                record["events"].append({"step": "rejected"})
                self._save(conn, record)
                return record
            plan = record["plan"]
            if plan["command"] != ["python", *COMMAND] or plan["policy"] != POLICY:
                raise FileReadError("verification_policy_changed", "验证策略与当前执行器不兼容，需要重新创建。")
            repair = RepairStore(self.state_dir).show(plan["repair_id"])
            root, base, candidate = _candidate(repair)
            if (repair["plan_sha256"] != plan["repair_plan_sha256"] or base != plan["base_commit"]
                    or candidate != plan["candidate_commit"]):
                raise FileReadError("candidate_changed", "验证批准的候选版本已变化。")
            if plan.get("baseline_strategy", "run") != baseline_strategy(repair, plan["base_manifest"]):
                raise FileReadError("verification_policy_changed", "基线执行策略不符合当前批准方案。")
            engine = self.engine or DockerEngine(plan["socket_path"])
            if str(engine.socket) != plan["socket_path"] or engine.backend != plan["backend"]:
                raise FileReadError("docker_target_changed", "Docker 目标与批准计划不同。")
            record["status"] = "running"
            record["events"].append({"step": "approved", "plan_sha256": plan_sha256})
            self._save(conn, record)
            try:
                results = {}
                for label, commit in (("baseline", base), ("candidate", candidate)):
                    manifest, files = collect_snapshot(root, commit)
                    expected = plan["base_manifest" if label == "baseline" else "candidate_manifest"]
                    if manifest != expected:
                        raise FileReadError("snapshot_changed", "验证快照与批准清单不一致。")
                    if label == "baseline" and plan.get("baseline_strategy") == "not_applicable_initial_creation":
                        results[label] = {"status": "not_applicable", "reason": "initial_creation_without_baseline_tests",
                                          "container_started": False, "tests_run": 0, "cleanup_ok": True}
                        continue
                    path = self.state_dir / "verification-artifacts" / record["id"] / label
                    _export(files, path)
                    results[label] = engine.run(plan, path, str(uuid4()))
                    _manifest_on_disk(path, expected)
                _candidate(repair)
                record["result"] = results
                candidate_result = results["candidate"]
                record["status"] = ("passed" if candidate_result["status"] == "passed"
                                    and candidate_result.get("exit_code") == 0
                                    and candidate_result.get("tests_run", 0) > 0
                                    and candidate_result.get("oom_killed") is False
                                    and candidate_result.get("output_truncated") is False
                                    and candidate_result.get("policy_verified") is True
                                    and candidate_result.get("cleanup_ok") is True
                                    and results["baseline"]["status"] in ("passed", "failed", "not_applicable")
                                    and results["baseline"].get("cleanup_ok") is True else "failed")
            except FileReadError as exc:
                record["status"] = "error"
                record["result"] = {"error": {"code": exc.code, "message": str(exc)}}
            record["events"].append({"step": record["status"]})
            self._save(conn, record)
            return record
