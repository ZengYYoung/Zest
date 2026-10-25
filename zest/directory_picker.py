"""Native folder selection on the local Web service's Mac; never uploads files."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

from .files import FileReadError


SCRIPT = '''try
    activate
    set chosenFolder to choose folder with prompt "选择 Zest 项目根目录" without multiple selections allowed
    return POSIX path of chosenFolder
on error number -128
    return ""
end try'''


@contextmanager
def picker_lock(state_dir):
    import fcntl
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / 'directory-picker.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FileReadError('directory_picker_busy', '已有文件夹选择窗口，请先完成或取消选择。') from exc
        yield
    finally:
        os.close(fd)


def choose_project_directory(state_dir):
    if sys.platform != 'darwin':
        raise FileReadError('directory_picker_unsupported', '当前文件夹选择支持 macOS，请手动填写项目根目录。')
    with picker_lock(state_dir):
        try:
            result = subprocess.run(['/usr/bin/osascript', '-e', SCRIPT], capture_output=True,
                                    text=True, timeout=300, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FileReadError('directory_picker_timed_out', '文件夹选择已超时，请重新选择或手动填写路径。') from exc
        except OSError as exc:
            raise FileReadError('directory_picker_unavailable', '无法打开文件夹选择窗口，请手动填写项目根目录。') from exc
    if result.returncode:
        raise FileReadError('directory_picker_unavailable', '无法完成文件夹选择，请手动填写项目根目录。')
    # Remove only osascript's output terminator, preserving spaces in folder names.
    path = result.stdout.removesuffix('\n')
    if not path:
        return {'cancelled': True, 'path': None}
    selected = Path(path)
    if not selected.is_absolute() or not selected.is_dir():
        raise FileReadError('directory_picker_invalid', '选择的文件夹已不可用，请重新选择。')
    return {'cancelled': False, 'path': str(selected)}
