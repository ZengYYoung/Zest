"""Private, local DeepSeek configuration shared by requests and their workers."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import stat
import tempfile
from uuid import UUID, uuid4

from .files import FileReadError


DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
_directory = ContextVar("zest_model_settings_directory", default=None)


@contextmanager
def model_settings_scope(state_dir):
    token = _directory.set(Path(state_dir).absolute())
    try:
        yield
    finally:
        _directory.reset(token)


def settings_directory():
    return _directory.get() or Path.cwd() / ".zest"


def worker_environment():
    # A project worker can have a different state root from the web catalog.
    # Pass only the settings directory, never the credential itself.
    return {**os.environ, "ZEST_MODEL_SETTINGS_DIRECTORY": str(settings_directory())}


def valid_key(value):
    if not isinstance(value, str):
        return False
    return 8 <= len(value) <= 4096 and all(33 <= ord(char) <= 126 for char in value)


class ModelSettingsStore:
    def __init__(self, state_dir):
        self.directory = Path(state_dir).absolute()
        self.path = self.directory / "model-settings.json"

    def read(self):
        try:
            if self.directory.is_symlink() or self.path.is_symlink():
                raise ValueError
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
            except FileNotFoundError:
                return None
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                    raise ValueError
                value = json.loads(stream.read(16385))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not valid_key(value.get("api_key"))
                    or str(UUID(value["revision"])) != value["revision"]):
                raise ValueError
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FileReadError("model_settings_unavailable", "无法读取本机模型配置，请检查状态目录权限或重新保存密钥。") from exc

    def save(self, api_key):
        if not valid_key(api_key):
            raise FileReadError("invalid_model_key", "请填写有效的 DeepSeek API Key，不能包含空格或换行。")
        temporary = None
        try:
            if self.directory.is_symlink() or self.path.is_symlink():
                raise ValueError
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            value = {"version": 1, "api_key": api_key, "revision": str(uuid4())}
            fd, temporary = tempfile.mkstemp(prefix=".model-settings-", dir=self.directory)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as exc:
            raise FileReadError("model_settings_unavailable", "无法保存本机模型配置，请检查状态目录权限。") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return {"configured": True, "provider": "deepseek", "name": DEFAULT_MODEL,
                "base_url": DEFAULT_BASE_URL + "/beta", "configured_base_url": DEFAULT_BASE_URL,
                "revision": value["revision"], "connectivity_tested": False}
