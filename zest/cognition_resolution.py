"""Separately approved resolution proposals and explicit, append-only adoption."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .cognition_contracts import canonical, parse_response, text
from .cognition_resolution_contracts import RESOLUTION_PROMPT, build_payload, proposal_hash, snapshot, validate_resolution
from .cognition_store import CognitionStore, digest, now, valid_id
from .files import FileReadError
from .git import inspect_repository, selected_commit
from .models import DeepSeekModel, ModelSettings, PROMPTS, model_target, output_contract, prepare_model_request, safe_model_receipt
from .resources import project_states, route_store
from .storage import ProjectRegistry


@route_store(records={name: 'resolution' for name in ('show', 'decide', 'run', 'recover', 'adopt')}, lists=('adopted_records',))
class ResolutionStore:
    def __init__(self, state_dir, *, model=None):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / 'resolutions.sqlite3'
        self.model = model

    @contextmanager
    def open(self, *, write=False):
        if self.db_path.is_symlink():
            raise FileReadError('resolution_storage_error', '处理决定数据库不能是符号链接。')
        try:
            if write:
                self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600); os.close(fd)
            with closing(sqlite3.connect(self.db_path.as_uri()+('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                if write:
                    conn.executescript('''
                    CREATE TABLE IF NOT EXISTS resolutions (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                        envelope TEXT NOT NULL, envelope_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                        result TEXT, error TEXT);
                    CREATE TABLE IF NOT EXISTS resolution_approvals (
                        id TEXT PRIMARY KEY, approved INTEGER NOT NULL, approval_sha256 TEXT NOT NULL,
                        allow_extended INTEGER NOT NULL, decided_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS resolution_calls (
                        id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                        response TEXT, receipt TEXT, error TEXT);
                    CREATE TABLE IF NOT EXISTS resolution_adoptions (
                        id TEXT PRIMARY KEY, proposal_sha256 TEXT NOT NULL, adopted_at TEXT NOT NULL);
                    ''')
                yield conn
        except (OSError, sqlite3.Error) as exc:
            raise FileReadError('resolution_storage_error', '无法读写处理决定，未回显原始异常。') from exc

    def _current_target(self, value):
        project = ProjectRegistry(self.state_dir).get_project_readonly(value['project_id'])
        if project is None or selected_commit(project, inspect_repository(project['root_path'])) != value['commit']:
            raise FileReadError('resolution_version_changed', '结案只作用于当前选定的同一版本，请先核对版本。')

    def create(self, followup_run_id):
        value = snapshot(CognitionStore(self.state_dir), valid_id(followup_run_id))
        destination = project_states(self.state_dir, value['project_id'])[0]
        target = self if destination == self.state_dir else ResolutionStore(destination, model=self.model)
        return target._create(value)

    def _create(self, value):
        self._current_target(value)
        old = value['source']
        effect = effective_resolutions(self.state_dir, value['project_id'], value['commit']).get((old['run_id'], old['selected']['finding_id']))
        if effect and effect['active']:
            raise FileReadError('resolution_already_closed', '原问题已通过处理决定关闭，不重复准备。')
        settings = self.model.settings if self.model else ModelSettings.from_env()
        payload = json.loads(canonical(build_payload(value)))
        if not payload['candidates']:
            raise FileReadError('resolution_no_candidates', '补查尚无通过逐项核对的 observed 候选，继续保留原问题。')
        request = prepare_model_request(settings, payload, purpose='cognition_resolution')
        if request['chars'] > 48000:
            raise FileReadError('resolution_prompt_budget', '完整新旧对照请求超过 48000 字符，请先讨论拆分范围；不截断冲突或原文。')
        envelope = {'version': 1, 'project_id': value['project_id'], 'commit': value['commit'],
                    'snapshot': value, 'snapshot_sha256': digest(value), 'payload': payload,
                    'instructions': RESOLUTION_PROMPT, 'contract': output_contract('cognition_resolution'),
                    'target': model_target(settings), 'request': request,
                    'policy': {'max_calls': 1, 'max_format_retries': 0, 'timeout': settings.timeout,
                               'max_request_chars': 32000, 'max_extended_request_chars': 48000, 'max_response_tokens': 8192},
                    'requires_extension': request['chars'] > 32000,
                    'authority': 'propose_relations_only_explicit_adoption_required'}
        if len(canonical(envelope).encode()) > 16*1024*1024:
            raise FileReadError('resolution_package_budget', '完整新旧对照包超过 16 MiB，请先讨论拆分范围。')
        text(canonical(envelope), 16*1024*1024)
        self._check_source(envelope)
        self._current_target(value)
        identity = str(uuid4())
        with self.open(write=True) as conn, conn:
            conn.execute('INSERT INTO resolutions VALUES (?,?,?,?,?,?,?,?)',
                         (identity, value['project_id'], now(), canonical(envelope), digest(envelope), 'awaiting_approval', None, None))
        return self.show(identity)

    def show(self, resolution_id, *, preview=False):
        valid_id(resolution_id)
        if not self.db_path.exists(): raise FileReadError('resolution_not_found', '未找到处理建议。')
        with self.open() as conn:
            row = conn.execute('SELECT * FROM resolutions WHERE id=?', (resolution_id,)).fetchone()
            if row is None: raise FileReadError('resolution_not_found', '未找到处理建议。')
            try:
                envelope = json.loads(row['envelope'])
                if digest(envelope) != row['envelope_sha256'] or digest(envelope['snapshot']) != envelope['snapshot_sha256'] or envelope['project_id'] != row['project_id']:
                    raise ValueError
                result = json.loads(row['result']) if row['result'] else None
                if result is not None:
                    raw = {k: result[k] for k in ('judgments', 'conflicts')}
                    for j in raw['judgments']: j.pop('outcome', None)
                    result = validate_resolution(raw, envelope['payload'])
                    if result != json.loads(row['result']): raise ValueError
                output = {k: row[k] for k in ('id', 'project_id', 'created_at', 'status')}
                output.update(commit=envelope['commit'], followup_run_id=envelope['snapshot']['run_id'],
                              source_run_id=envelope['snapshot']['source']['run_id'],
                              finding_id=envelope['snapshot']['source']['selected']['finding_id'],
                              approval_sha256=row['envelope_sha256'], target=envelope['target'], policy=envelope['policy'],
                              request_chars=envelope['request']['chars'], requires_extension=envelope['requires_extension'],
                              comparison={k: envelope['payload'][k] for k in ('references','candidates','evidence')},
                              result=result, error=json.loads(row['error']) if row['error'] else None,
                              proposal_sha256=proposal_hash(envelope, result) if result is not None else None)
                for key, table in (('approval','resolution_approvals'), ('call','resolution_calls'), ('adoption','resolution_adoptions')):
                    saved = conn.execute(f'SELECT * FROM {table} WHERE id=?', (resolution_id,)).fetchone()
                    output[key] = dict(saved) if saved else None
                if output['call']:
                    for k in ('response', 'receipt', 'error'):
                        output['call'][k] = json.loads(output['call'][k]) if output['call'][k] else None
                if output['adoption'] and output['adoption']['proposal_sha256'] != output['proposal_sha256']: raise ValueError
                if preview: output['envelope'] = envelope
                return output
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise FileReadError('resolution_record_corrupt', '处理建议、采纳记录或身份损坏。') from exc

    def _check_source(self, envelope):
        try:
            current = snapshot(CognitionStore(self.state_dir), envelope['snapshot']['run_id'])
            if digest(current) != envelope['snapshot_sha256']: raise ValueError
        except (FileReadError, ValueError, KeyError, TypeError) as exc:
            raise FileReadError('resolution_source_changed', '原问题、冲突或补查结果已变化；须重新准备处理建议。') from exc

    def decide(self, resolution_id, *, approved, approval_sha256=None, allow_extended_requests=False):
        record = self.show(resolution_id, preview=True)
        if approved:
            if record['approval_sha256'] != approval_sha256:
                raise FileReadError('approval_mismatch', '批准须匹配完整新旧对照请求。')
            self._check_source(record['envelope'])
            if record['requires_extension'] and allow_extended_requests is not True:
                raise FileReadError('resolution_context_approval_required', '须审阅并另行明确同意这条精确超限请求。')
        with self.open(write=True) as conn, conn:
            cursor = conn.execute("UPDATE resolutions SET status=? WHERE id=? AND status='awaiting_approval'", ('queued' if approved else 'rejected', resolution_id))
            if cursor.rowcount != 1: raise FileReadError('resolution_not_pending', '处理建议没有等待请求批准。')
            conn.execute('INSERT INTO resolution_approvals VALUES (?,?,?,?,?)', (resolution_id, int(approved), record['approval_sha256'], int(allow_extended_requests is True), now()))
        return self.show(resolution_id)

    def _status(self, identity, status, error=None):
        with self.open(write=True) as conn, conn:
            conn.execute('UPDATE resolutions SET status=?,error=? WHERE id=?', (status, canonical(error) if error else None, identity))

    def _call(self, identity, envelope):
        self._check_source(envelope)
        provider = self.model or DeepSeekModel(ModelSettings.from_env())
        settings = provider.settings
        if (model_target(settings) != envelope['target'] or settings.timeout != envelope['policy']['timeout']
                or envelope['instructions'] != RESOLUTION_PROMPT or PROMPTS['cognition_resolution'] != RESOLUTION_PROMPT
                or envelope['contract'] != output_contract('cognition_resolution')):
            raise FileReadError('resolution_contract_changed', '模型、指令或结构契约变化，请重新准备批准。')
        request = envelope['request']
        if prepare_model_request(settings, envelope['payload'], purpose='cognition_resolution') != request:
            raise FileReadError('resolution_request_changed', '对照请求与冻结正文不匹配，未外发。')
        with self.open(write=True) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            approval = conn.execute('SELECT * FROM resolution_approvals WHERE id=?', (identity,)).fetchone()
            if not approval or not approval['approved'] or approval['approval_sha256'] != digest(envelope):
                raise FileReadError('approval_mismatch', '缺少本次精确对照请求的批准。')
            if envelope['requires_extension'] and not approval['allow_extended']:
                raise FileReadError('resolution_context_approval_required', '本次精确扩展未获批准。')
            old = conn.execute('SELECT * FROM resolution_calls WHERE id=?', (identity,)).fetchone()
            if old:
                if old['request_sha256'] != request['body_sha256']: raise FileReadError('resolution_request_changed', '保存的请求身份变化。')
                if old['status'] == 'completed': return json.loads(old['response'])
                raise FileReadError('resolution_execution_unknown' if old['status'] == 'pending' else 'resolution_response_invalid', '已存在未知或无效结果，不重发。')
            conn.execute('INSERT INTO resolution_calls VALUES (?,?,?,?,?,?)', (identity, request['body_sha256'], 'pending', None, None, None))
        try:
            response = provider.complete(envelope['payload'], purpose='cognition_resolution', expected_request=request) if isinstance(provider, DeepSeekModel) else provider.complete(envelope['payload'], purpose='cognition_resolution')
        except Exception as exc:
            raise FileReadError('resolution_execution_unknown', '处理建议请求结果未知，不自动重发。') from exc
        receipt = safe_model_receipt(response)
        value, error = None, None
        try:
            from .model_outputs import CognitionResolutionResponse
            raw = parse_response(receipt); CognitionResolutionResponse.model_validate(raw)
            value = validate_resolution(raw, envelope['payload'])
        except Exception:
            error = {'code': 'resolution_response_invalid', 'message': '已保存响应，但结构、身份、逐项覆盖或冲突校验未通过；不修补或重发。'}
        with self.open(write=True) as conn, conn:
            conn.execute('UPDATE resolution_calls SET status=?,response=?,receipt=?,error=? WHERE id=?',
                         ('invalid' if error else 'completed', canonical(value) if value else None, canonical(receipt), canonical(error) if error else None, identity))
        if error: raise FileReadError(error['code'], error['message'])
        return value

    def run(self, resolution_id):
        from .cognition import execution_lock
        with execution_lock(self.state_dir, 'resolution-'+valid_id(resolution_id)):
            record = self.show(resolution_id, preview=True)
            if record['status'] != 'queued': raise FileReadError('resolution_not_queued', '处理建议请求尚未批准或已经结束。')
            self._status(resolution_id, 'running')
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                from langgraph.graph import StateGraph, START, END
                from langsmith import tracing_context
                directory = self.state_dir/'resolution-checkpoints'
                if directory.is_symlink(): raise FileReadError('resolution_storage_error', '处理建议检查点不能是符号链接。')
                directory.mkdir(exist_ok=True, mode=0o700)
                path = directory/(resolution_id+'.sqlite3')
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600); os.close(fd)
                builder = StateGraph(dict)
                def compare(state):
                    self._call(resolution_id, record['envelope']); return {'done': True}
                builder.add_node('compare', compare); builder.add_edge(START,'compare'); builder.add_edge('compare', END)
                with closing(sqlite3.connect(path, check_same_thread=False)) as conn, tracing_context(enabled=False):
                    graph = builder.compile(checkpointer=SqliteSaver(conn))
                    config = {'configurable': {'thread_id': resolution_id}}
                    saved = graph.get_state(config)
                    if not saved.values or saved.next: graph.invoke(None if saved.values else {'done': False}, config, durability='sync')
                self._check_source(record['envelope'])
                with self.open(write=True) as conn, conn:
                    call = conn.execute("SELECT response FROM resolution_calls WHERE id=? AND status='completed'", (resolution_id,)).fetchone()
                    if not call: raise FileReadError('resolution_response_invalid', '缺少已保存的有效处理建议。')
                    conn.execute("UPDATE resolutions SET status='proposed',result=?,error=NULL WHERE id=?", (call[0],resolution_id))
            except FileReadError as exc:
                self._status(resolution_id, 'pending_execution' if exc.code == 'resolution_execution_unknown' else 'partial', {'code': exc.code, 'message': str(exc)})
            except Exception:
                self._status(resolution_id, 'partial', {'code': 'resolution_failed', 'message': '处理建议停止，请检查已保存状态和收据。'})
        return self.show(resolution_id)

    def recover(self, resolution_id):
        from .cognition import execution_lock
        with execution_lock(self.state_dir, 'resolution-'+valid_id(resolution_id)):
            record = self.show(resolution_id, preview=True)
            if record['status'] not in ('running','partial','pending_execution'):
                raise FileReadError('resolution_not_recoverable', '处理建议无需恢复。')
            self._check_source(record['envelope'])
            if record['call'] and record['call']['status'] != 'completed':
                raise FileReadError('resolution_execution_unknown', '未知或无效请求不可重发。')
            self._status(resolution_id, 'queued')
        return self.show(resolution_id)

    def adopt(self, resolution_id, *, proposal_sha256):
        record = self.show(resolution_id, preview=True)
        if record['proposal_sha256'] != proposal_sha256:
            raise FileReadError('resolution_proposal_mismatch', '采纳须匹配完整逐项建议及效果清单。')
        from .cognition import execution_lock
        destination = project_states(self.state_dir, record['project_id'])[0]
        with execution_lock(destination, 'resolution-project-'+record['project_id']):
            record = self.show(resolution_id, preview=True)
            if record['proposal_sha256'] != proposal_sha256:
                raise FileReadError('resolution_proposal_mismatch', '采纳前处理建议身份发生变化。')
            if record['adoption']: return self.show(resolution_id)
            if record['status'] != 'proposed': raise FileReadError('resolution_not_proposed', '尚无可审阅采纳的有效建议。')
            self._check_source(record['envelope']); self._current_target(record['envelope']['snapshot'])
            existing = effective_resolutions(self.state_dir, record['project_id'], record['commit'])
            source = record['envelope']['snapshot']['source']
            for effect in record['result']['effects']:
                old = existing.get((source['run_id'], effect['reference_id']))
                if effect['closable'] and old and old['active']:
                    raise FileReadError('resolution_already_closed', '已有采纳决定处理了相同旧断言，请重新审阅。')
            with self.open(write=True) as conn, conn:
                conn.execute('INSERT INTO resolution_adoptions VALUES (?,?,?)', (resolution_id, proposal_sha256, now()))
        return self.show(resolution_id)

    def adopted_records(self, project_id):
        if not self.db_path.exists(): return []
        with self.open() as conn:
            identities = [r[0] for r in conn.execute('SELECT r.id FROM resolutions r JOIN resolution_adoptions a ON a.id=r.id WHERE r.project_id=? ORDER BY a.adopted_at', (project_id,))]
        return [self.show(identity, preview=True) for identity in identities]


def effective_resolutions(state_dir, project_id, commit):
    """Readonly overlay. Stale replacement evidence reopens work, never revives old errors."""
    output = {}
    records = ResolutionStore(state_dir).adopted_records(project_id)
    for record in sorted(records, key=lambda r: r['adoption']['adopted_at']):
        if record['commit'] != commit: continue
        envelope = record['envelope']; source = envelope['snapshot']['source']
        try:
            current = snapshot(CognitionStore(state_dir), envelope['snapshot']['run_id'])
            active = digest(current) == envelope['snapshot_sha256']
        except FileReadError:
            active = False
        leads = {f['finding_id']: f for f in [source['selected'], *source['related_findings']]}
        for effect in record['result']['effects']:
            if not effect['closable']: continue
            output[(source['run_id'],effect['reference_id'])] = {
                **effect, 'resolution_id': record['id'], 'active': active,
                'source_identity_sha256': leads.get(effect['reference_id'], {}).get('identity_sha256'),
                'followup_run_id': record['followup_run_id'], 'adopted_at': record['adoption']['adopted_at'],
                'proposal_sha256': record['proposal_sha256']}
    return output


def item_resolution(effects, run_id, item):
    effect = effects.get((run_id, item['id']))
    if effect and effect['source_identity_sha256'] == digest({
            'finding': item['finding'], 'review': item['review'], 'status': item['status'], 'job_id': item['job_id']}):
        return effect
    return None


def format_resolution(record):
    from .terminal import display_text
    lines = ['Zest · 补查结果处理建议', f"记录：{record['id']}", f"状态：{record['status']}",
             f"原问题：{record['source_run_id']} / {record['finding_id']}", f"补查：{record['followup_run_id']}"]
    result = record['result']
    if result:
        labels = {'confirmed':'确认','corrected':'纠正/补全','unresolved':'未解决'}
        comparison = record['comparison']
        references = {r['reference_id']: r for r in comparison['references']}
        candidates = {c['finding_id']: c for c in comparison['candidates']}
        for judgment in result['judgments']:
            effect = next(e for e in result['effects'] if e['reference_id'] == judgment['reference_id'])
            lines.append(display_text(f"- {judgment['reference_id']}：{labels[judgment['outcome']]}；{'可结案' if effect['closable'] else '保持未解决'}"))
            lines.append('  旧内容：'+display_text(references[judgment['reference_id']]['statement']))
            lines.append('  '+display_text(judgment['reason']))
            chosen = sorted({i for check in judgment['checks'] for i in check['finding_ids']})
            for identity in chosen:
                candidate = candidates[identity]
                lines.append('  新结论：'+display_text(f"{identity} · {candidate['statement']}"))
            for identity in sorted({i for check in judgment['checks'] for i in check['evidence_ids']}):
                proof = comparison['evidence'][identity]['evidence']
                lines.append('  来源：'+display_text(f"{identity} · {proof['path']}:{proof['start_line']}–{proof['end_line']} · {proof['commit']}"))
            for check in judgment['checks']:
                lines.append('  '+display_text(f"{check['attribute']} / {check['condition']}：{labels[check['outcome']]} · {check['reason']} · 候选 {check['finding_ids']} · 原文 {check['evidence_ids']}"))
        for conflict in result['conflicts']:
            lines.append(display_text(f"冲突 {conflict['left']} / {conflict['right']}：{conflict['verdict']} · {conflict['reason']}"))
        lines.append('完整建议哈希：'+record['proposal_sha256'])
    if record['adoption']: lines.append('已明确采纳；历史原始记录保留，当前有效知识按处理决定筛选。')
    else: lines.append('尚未采纳；请求获批或模型提出建议都不会自动关闭原问题。')
    if record['error']: lines.append(display_text(record['error']))
    lines.append('JSON preview 包含完整新旧断言、候选和原文；模型判断仍需审阅。')
    return '\n'.join(lines)
