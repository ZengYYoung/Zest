"""Project-scoped standing permission for read-only project understanding."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

from .cognition_contracts import COGNITION_PROMPTS, POLICY
from .cognition_quality import QUALITY_CONTRACT
from .cognition_workflow import WORKFLOW_CONTRACT
from .cognition_store import CognitionStore, digest
from .files import FileReadError
from .initialization import InitializationStore, atomic, now
from .models import ModelSettings, model_target
from .resources import _regular, record_state
from .survey import MAX_BYTES, MAX_FILES


LEGACY_APPROVAL_POLICY = {
    'version': 1, 'purposes': ['initialization', 'repair', 'refresh'],
    'source': 'safe_committed_project_materials', 'max_files': MAX_FILES, 'max_source_bytes': MAX_BYTES,
    'max_model_calls': POLICY['max_model_calls'], 'max_seconds': POLICY['max_seconds'],
    'max_request_chars': POLICY['max_extended_prompt_chars'],
    'contract_sha256': digest({'instructions': COGNITION_PROMPTS, 'policy': POLICY, 'quality': QUALITY_CONTRACT,
                               'workflow': WORKFLOW_CONTRACT}),
    'unknown_request_retry': False, 'code_execution': False,
}
APPROVAL_POLICY = {
    'version': 2, 'purposes': ['initialization', 'repair', 'refresh'],
    'source': 'safe_project_commit_sources', 'source_policy_version': 1,
    'document_destination': '.zest/docs/',
    'max_model_calls': POLICY['max_model_calls'], 'max_seconds': POLICY['max_seconds'],
    'max_request_chars': POLICY['max_extended_prompt_chars'],
    'unknown_request_retry': False, 'code_execution': False,
}


def valid_grant(grant, project, settings):
    legacy = grant.get('policy') == LEGACY_APPROVAL_POLICY
    return (grant.get('policy') in (APPROVAL_POLICY, LEGACY_APPROVAL_POLICY)
            and grant.get('project_id') == project['id'] and grant.get('root_path') == project['root_path']
            and grant.get('target') == model_target(settings)
            and (not legacy or grant.get('configuration_revision') == settings.revision))


class UnderstandingApproval:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.initialization = InitializationStore(state_dir)

    @contextmanager
    def lock(self, directory):
        import fcntl
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / 'understanding-approval.lock'
        _regular(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self, directory):
        path = directory / 'understanding-approval.json'
        _regular(path)
        if not path.exists(): return {'schema_version': 1, 'enabled': False}
        try:
            if path.stat().st_size > 16384: raise ValueError
            value = json.loads(path.read_bytes())
            if value['schema_version'] != 1 or type(value['enabled']) is not bool: raise ValueError
            if value['enabled'] and (not isinstance(value['grant'], dict) or digest(value['grant']) != value['grant_sha256']): raise ValueError
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FileReadError('understanding_approval_corrupt', '项目理解自动批准设置无法读取，已停止自动批准。') from exc

    def show(self, project_id):
        project, directory = self.initialization.location(project_id)
        value = self.read(directory)
        enabled = value['enabled']
        grant = value.get('grant', {})
        valid = False
        if enabled:
            try:
                settings = ModelSettings.from_env()
                valid = valid_grant(grant, project, settings)
            except FileReadError: valid = False
        return {'project_id': project_id, 'enabled': enabled, 'active': bool(valid), 'configured': 'updated_at' in value, 'policy': APPROVAL_POLICY,
                'target': grant.get('target'), 'updated_at': value.get('updated_at'),
                'scope_mode': 'project' if grant.get('policy') == APPROVAL_POLICY else 'legacy_package',
                'message': '已开启，项目范围内自动检索、补读并分批处理请求。' if valid else
                           '模型目标或资料访问权限已变化，请重新开启。' if enabled else '尚未开启，项目理解请求由你手动确认。'}

    def configure(self, project_id, *, enabled):
        if type(enabled) is not bool:
            raise FileReadError('invalid_understanding_approval', '请指定是否自动批准项目理解。')
        project, directory = self.initialization.location(project_id)
        if not project.get('resource_dir'):
            self.initialization.prepare(project_id)
            project, directory = self.initialization.location(project_id)
        settings = ModelSettings.from_env() if enabled else None
        grant = {'id': str(uuid4()), 'project_id': project_id, 'root_path': project['root_path'],
                 'target': model_target(settings),
                 'policy': APPROVAL_POLICY, 'created_at': now()} if enabled else None
        with self.lock(directory):
            value = {'schema_version': 1, 'enabled': enabled, 'updated_at': now()}
            if grant: value.update(grant=grant, grant_sha256=digest(grant))
            atomic(directory / 'understanding-approval.json', json.dumps(value, ensure_ascii=False).encode())
        from .maintenance import MaintenanceStore
        maintenance = MaintenanceStore(self.state_dir)
        if enabled:
            if self.initialization.read(directory) is None:
                self.initialization.prepare(project_id)
            maintenance.enable(project_id)
            self.initialization.auto_start(project_id)
            if not maintenance.auto_start(project_id):
                maintenance.check(project_id, reason='manual')
        else:
            maintenance.disable(project_id)
        return self.show(project_id)

    def _permission(self, project_id, run_id):
        project, directory = self.initialization.location(project_id)
        value = self.read(directory)
        if not value['enabled']: return None
        grant = value['grant']
        initial = self.initialization.read(directory) or {}
        from .maintenance import MaintenanceStore
        maintenance = MaintenanceStore(self.state_dir).read(directory)
        if run_id not in (initial.get('run_id'), maintenance.get('active_run_id')):
            return None
        record = CognitionStore(self.state_dir).show(run_id, include_envelope=True)
        envelope = record['envelope']
        settings = ModelSettings.from_env()
        from .project_sources import SOURCE_SCOPE, project_policy
        project_scope = envelope.get('source_scope')
        policy = project_policy(POLICY) if project_scope else POLICY
        if (not valid_grant(grant, project, settings) or record['project_id'] != project_id
                or grant.get('target') != envelope['target']
                or (project_scope and (grant.get('policy') != APPROVAL_POLICY or project_scope != SOURCE_SCOPE))
                or envelope['policy'] != policy or envelope['instructions'] != COGNITION_PROMPTS
                or envelope.get('quality_contract', QUALITY_CONTRACT) != QUALITY_CONTRACT
                or envelope.get('workflow_contract', WORKFLOW_CONTRACT) != WORKFLOW_CONTRACT
                or (not project_scope and (len(envelope['files']) > MAX_FILES
                                           or sum(len(f['body'].encode()) for f in envelope['files']) > MAX_BYTES))):
            return None
        if record['status'] == 'awaiting_approval':
            from .git import inspect_repository, selected_commit
            if selected_commit(project, inspect_repository(project['root_path'])) != record['commit']:
                return None
        bound = CognitionStore(record_state(self.state_dir, 'cognition', run_id))
        with bound.open() as conn:
            if bound._blocking_calls(conn, run_id, envelope['policy']): return None
        return grant, record, bound

    def approve(self, project_id, run_id, *, contexts=False):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            permission = self._permission(project_id, run_id)
            if permission is None: return False
            grant, record, bound = permission
            store = CognitionStore(self.state_dir)
            if contexts:
                if record['status'] != 'awaiting_context_approval': return False
                pending = [r for r in store.context_requests(run_id) if r['status'] == 'awaiting_approval']
                if not pending or any(r['chars'] > APPROVAL_POLICY['max_request_chars'] for r in pending): return False
                requests = [{'actor': r['actor'], 'sequence': r['sequence'], 'sha256': r['request_sha256']} for r in pending]
                store.decide_contexts(run_id, requests, approved=True)
                detail = {'requests': requests}
            else:
                if record['status'] != 'awaiting_approval': return False
                store.decide(run_id, approved=True, approval_sha256=record['approval_sha256'])
                detail = {'scope_sha256': record['approval_sha256']}
            bound.event(run_id, 'coordinator', 'understanding_context_auto_approved' if contexts else 'understanding_scope_auto_approved',
                        {'grant': grant, 'grant_sha256': digest(grant), **detail})
            return True


def run_understanding(state_dir, project_id, run_id, *, model=None):
    """Resume only exact unsent length approvals; do not restart terminal work."""
    from .cognition import CognitionEngine
    approval = UnderstandingApproval(state_dir)
    result = CognitionStore(state_dir).show(run_id)
    while result['status'] == 'queued':
        result = CognitionEngine(state_dir, model=model).run(run_id)
        if result['status'] != 'awaiting_context_approval' or not approval.approve(project_id, run_id, contexts=True):
            break
        result = CognitionStore(state_dir).show(run_id)
    return result
