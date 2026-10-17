"""One separately approved real connection check with fixed synthetic evidence."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import UUID, uuid4

from .files import FileReadError
from .models import DeepSeekModel, ModelSettings, model_target, output_contract, prepare_model_request, safe_model_receipt
from .qa import validate_answer


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def implementation():
    root = Path(__file__).parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ('model_connection.py', 'models.py', 'model_outputs.py', 'qa.py')}


def source_commit():
    from .git import git_environment
    try:
        result = subprocess.run(['git', '-C', str(Path(__file__).resolve().parent.parent), 'rev-parse', 'HEAD'],
                                env=git_environment(), capture_output=True, timeout=5, check=True)
        value = result.stdout.decode().strip()
        return value if len(value) == 40 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def payload():
    return {'question': '这是 Zest 模型连接测试。根据 E1，说明 add(7, 5) 的返回值，并引用 E1。',
            'sources': [{'id': 'E1', 'evidence': {'kind': 'synthetic_connection_fixture', 'path': 'connection-example.py',
                         'start_line': 1, 'end_line': 2},
                         'content': 'def add(a, b):\n    return a + b\n'}],
            'limitations': ['仅为固定合成示例，不包含用户项目、文件、记忆或历史对话。']}


class ModelConnectionStore:
    def __init__(self, state_dir):
        self.directory = Path(state_dir).resolve()
        self.path = self.directory / 'model-connections.sqlite3'

    @contextmanager
    def open(self, *, write=False):
        if self.path.is_symlink():
            raise FileReadError('model_check_storage_error', '连接测试数据库不能是符号链接。')
        if write:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        with closing(sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            if write:
                conn.execute('''CREATE TABLE IF NOT EXISTS checks (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, envelope TEXT NOT NULL, approval_sha256 TEXT NOT NULL,
                    result TEXT, error TEXT)''')
                conn.commit()
            yield conn

    def prepare(self):
        settings = ModelSettings.from_env()
        envelope = {'version': 1, 'zest_commit': source_commit(), 'target': model_target(settings), 'payload': payload(),
                    'contract': output_contract('qa'), 'implementation': implementation(),
                    'policy': {'max_calls': 1, 'automatic_retries': 0, 'timeout_seconds': settings.timeout,
                               'max_output_tokens': 2048, 'project_data_sent': False},
                    'request': prepare_model_request(settings, payload(), purpose='qa')}
        identity = str(uuid4())
        with self.open(write=True) as conn, conn:
            conn.execute('INSERT INTO checks VALUES (?,?,?,?,?,?,?,?)',
                         (identity, now(), now(), 'awaiting_approval', json.dumps(envelope), digest(envelope), None, None))
        return self.show(identity)

    def show(self, identity):
        identity = str(UUID(str(identity)))
        if not self.path.exists():
            raise FileReadError('model_check_not_found', '未找到模型连接测试。')
        with self.open() as conn:
            row = conn.execute('SELECT * FROM checks WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise FileReadError('model_check_not_found', '未找到模型连接测试。')
        record = dict(row)
        for name in ('envelope', 'result', 'error'):
            record[name] = json.loads(record[name]) if record[name] else None
        if digest(record['envelope']) != record['approval_sha256']:
            raise FileReadError('model_check_corrupt', '连接测试身份不一致，不能执行。')
        if record['status'] == 'running':
            from .web_operations import WebOperations
            with WebOperations(self.directory).lock('model-' + identity, required=False) as acquired:
                if acquired:
                    record['status'] = 'unknown'
                    record['error'] = {'code': 'model_check_interrupted', 'message': '测试进程已中断，结果待核对，不自动重试。'}
        return record

    def latest(self):
        if not self.path.exists():
            return None
        with self.open() as conn:
            row = conn.execute('SELECT id FROM checks ORDER BY created_at DESC LIMIT 1').fetchone()
        return self.show(row[0]) if row else None

    def decide(self, identity, *, approved, sha256=None):
        from .web_operations import WebOperations
        identity = str(UUID(str(identity)))
        with WebOperations(self.directory).lock('model-' + identity):
            return self._decide(identity, approved=approved, sha256=sha256)

    def _decide(self, identity, *, approved, sha256=None):
        record = self.show(identity)
        envelope = record['envelope']
        if approved:
            if record['approval_sha256'] != sha256:
                raise FileReadError('approval_mismatch', '批准须匹配完整合成示例、模型目标及一次调用预算。')
            settings = ModelSettings.from_env()
            if (model_target(settings) != envelope['target'] or settings.timeout != envelope['policy']['timeout_seconds']
                    or implementation() != envelope['implementation'] or output_contract('qa') != envelope['contract']
                    or prepare_model_request(settings, payload(), purpose='qa') != envelope['request']):
                raise FileReadError('model_check_changed', '配置、代码或完整请求已变化，请重新准备连接测试。')
        with self.open(write=True) as conn, conn:
            changed = conn.execute("UPDATE checks SET status=?,updated_at=? WHERE id=? AND status='awaiting_approval'",
                                   ('running' if approved else 'rejected', now(), identity))
            if changed.rowcount != 1:
                raise FileReadError('model_check_not_pending', '连接测试已决定，不重复调用；结果未知时请核对收据。')
        if not approved:
            return self.show(identity)
        started = time.monotonic()
        receipt = None
        try:
            response = DeepSeekModel(settings).complete(envelope['payload'], purpose='qa', expected_request=envelope['request'])
            receipt = safe_model_receipt(response)
            answer = validate_answer(response.get('text', ''), envelope['payload']['sources'])
            if response.get('structured_status') != 'valid' or answer['status'] != 'answered':
                raise FileReadError('model_check_invalid_answer', '收到响应但结构化问答未通过，连接测试未通过。')
            result = {'answer': answer, 'receipt': receipt, 'elapsed_seconds': round(time.monotonic() - started, 3),
                      'usage': response.get('usage', {}), 'model_called': True, 'attempts_reserved': 1}
            self.finish(identity, 'passed', result=result)
        except FileReadError as exc:
            known = receipt is not None or exc.code in ('model_auth_error', 'model_request_changed')
            result = {'receipt': receipt, 'elapsed_seconds': round(time.monotonic() - started, 3),
                      'attempts_reserved': 1, 'model_called': exc.code != 'model_request_changed'}
            self.finish(identity, 'failed' if known else 'unknown', result=result,
                        error={'code': exc.code, 'message': str(exc)})
        except Exception:
            self.finish(identity, 'unknown', error={'code': 'model_check_unknown', 'message': '结果未知，未自动重试，请核对调用记录。'})
        return self.show(identity)

    def finish(self, identity, status, *, result=None, error=None):
        with self.open(write=True) as conn, conn:
            conn.execute('UPDATE checks SET status=?,updated_at=?,result=?,error=? WHERE id=?',
                         (status, now(), json.dumps(result, ensure_ascii=True), json.dumps(error, ensure_ascii=True), identity))
