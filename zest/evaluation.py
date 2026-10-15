"""Separately approved, checkpointed cognition scoring with a fixed built-in Skill."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
from typing import TypedDict
from uuid import uuid4

from .cognition_contracts import canonical, parse_response, text
from .cognition_store import CognitionStore, digest, now, valid_id
from .evaluation_contracts import aggregate, evaluation_units, merge_payloads, validate_evaluation
from .evaluation_skill import skill_text
from .files import FileReadError
from .models import DeepSeekModel, ModelSettings, PROMPTS, model_target, output_contract, prepare_model_request, safe_model_receipt
from .resources import project_states, record_state, route_store


class EvaluationState(TypedDict, total=False):
    next_batch: int


def load_criteria(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512000:
        raise FileReadError('invalid_evaluation_criteria', '评分参考须为最多 512000 字节的普通 JSON 文件。')
    try:
        raw = path.read_text(encoding='utf-8'); text(raw, 512000)
        return json.loads(raw)
    except (ValueError, OSError, UnicodeError) as exc:
        raise FileReadError('invalid_evaluation_criteria', '无法读取评分参考 JSON，未回显文件正文。') from exc


@route_store(records={name: 'evaluation' for name in ('show', 'decide', 'run', 'recover', 'report')})
class EvaluationStore:
    def __init__(self, state_dir, *, model=None):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / 'evaluations.sqlite3'
        self.model = model

    @contextmanager
    def open(self, *, write=False):
        if self.db_path.is_symlink():
            raise FileReadError('evaluation_storage_error', '评分数据库不能是符号链接。')
        try:
            if write:
                self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600); os.close(fd)
            with closing(sqlite3.connect(self.db_path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                if write:
                    conn.executescript('''
                    CREATE TABLE IF NOT EXISTS evaluations (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_run_id TEXT NOT NULL,
                        created_at TEXT NOT NULL, envelope TEXT NOT NULL, envelope_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL, result TEXT, error TEXT);
                    CREATE TABLE IF NOT EXISTS evaluation_calls (
                        evaluation_id TEXT NOT NULL, batch INTEGER NOT NULL, request_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL, response TEXT, receipt TEXT, error TEXT,
                        PRIMARY KEY(evaluation_id,batch));
                    CREATE TABLE IF NOT EXISTS evaluation_decisions (
                        evaluation_id TEXT PRIMARY KEY, approved INTEGER NOT NULL,
                        approval_sha256 TEXT NOT NULL, allow_extended_requests INTEGER NOT NULL,
                        decided_at TEXT NOT NULL);
                    ''')
                yield conn
        except (OSError, sqlite3.Error) as exc:
            raise FileReadError('evaluation_storage_error', '无法读写评分记录，未回显原始异常。') from exc

    def create(self, run_id, criteria, *, max_calls=32):
        source = record_state(self.state_dir, 'cognition', valid_id(run_id))
        record = CognitionStore(source).show(run_id, include_envelope=True)
        if record['status'] not in ('completed', 'partial', 'failed'):
            raise FileReadError('evaluation_source_active', '只评价已结束调查的固定快照，不干扰活跃调查。')
        destination = project_states(self.state_dir, record['project_id'])[0]
        target = self if destination == self.state_dir else EvaluationStore(destination, model=self.model)
        return target._create(record, criteria, max_calls)

    def _create(self, record, criteria, max_calls):
        if type(max_calls) is not int or not 1 <= max_calls <= 128:
            raise FileReadError('evaluation_budget', '独立评分预算须为 1—128 次；准备不调用模型。')
        settings = self.model.settings if self.model else ModelSettings.from_env()
        if skill_text() != PROMPTS['cognition_evaluation']:
            raise FileReadError('evaluation_contract_changed', '评分 Skill 已变化，请重启后重新准备。')
        units = evaluation_units(record, criteria)
        batches, group = [], []
        def compile_group(values):
            # SQLite persists canonical JSON. Normalize before SDK compilation
            # so reloading cannot change the exact human-message byte ordering.
            payload = json.loads(canonical(merge_payloads(values)))
            return {'payload': payload, 'request': prepare_model_request(settings, payload, purpose='cognition_evaluation')}
        def append(values):
            batch = compile_group(values)
            if batch['request']['chars'] > 48000:
                raise FileReadError('evaluation_prompt_budget', '单个评分项完整请求超过 48000 字符；须先讨论并拆分范围，不截断原文。')
            batch['requires_extension'] = batch['request']['chars'] > 32000
            batches.append(batch)
        for unit in units:
            limit = 4 if unit['kind'] == 'coverage' else 2
            if group and (group[0]['kind'] != unit['kind'] or len(group) >= limit):
                append(group); group = []
            candidate = [*group, unit]
            if group and compile_group(candidate)['request']['chars'] > 32000:
                append(group); group = []
            group.append(unit)
        if group: append(group)
        if len(batches) > max_calls:
            raise FileReadError('evaluation_budget', f'精确评分需 {len(batches)} 次请求，超过所选独立预算；请先审议范围或预算，不沿用旧调查许可。')
        snapshot = {'run_id': record['id'], 'commit': record['commit'], 'status': record['status'],
                    'source_approval_sha256': record['approval_sha256'],
                    'findings': [{'id': f['id'], 'status': f['status'], 'finding': f['finding']} for f in record['findings'] if f['finding']['kind'] == 'observed'],
                    'jobs': record['jobs']}
        envelope = {'version': 1, 'project_id': record['project_id'], 'source_run_id': record['id'],
                    'snapshot': snapshot, 'snapshot_sha256': digest(snapshot), 'criteria': criteria,
                    'criteria_sha256': digest(criteria), 'skill': skill_text(), 'skill_sha256': digest(skill_text()),
                    'contract': output_contract('cognition_evaluation'), 'target': model_target(settings),
                    'policy': {'max_calls': len(batches), 'selected_cap': max_calls, 'max_format_retries': 0,
                               'request_timeout_seconds': settings.timeout,
                               'max_request_chars': 32000, 'max_extended_request_chars': 48000,
                               'max_response_tokens': 8192}, 'batches': batches,
                    'authority': 'score_this_frozen_snapshot_only_no_knowledge_mutation'}
        if len(canonical(envelope).encode()) > 16 * 1024 * 1024:
            raise FileReadError('evaluation_package_budget', '完整评分包超过 16 MiB，须先讨论范围。')
        text(canonical(envelope), 16 * 1024 * 1024)
        identity = str(uuid4())
        with self.open(write=True) as conn, conn:
            conn.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?)',
                         (identity, record['project_id'], record['id'], now(), canonical(envelope), digest(envelope), 'awaiting_approval', None, None))
        return self.show(identity)

    def show(self, evaluation_id, *, preview=False):
        valid_id(evaluation_id)
        if not self.db_path.exists():
            raise FileReadError('evaluation_not_found', '未找到独立评分记录。')
        with self.open() as conn:
            row = conn.execute('SELECT * FROM evaluations WHERE id=?', (evaluation_id,)).fetchone()
            if row is None: raise FileReadError('evaluation_not_found', '未找到独立评分记录。')
            try:
                envelope = json.loads(row['envelope'])
                if digest(envelope) != row['envelope_sha256'] or digest(envelope['snapshot']) != envelope['snapshot_sha256']:
                    raise ValueError
                result = {k: row[k] for k in ('id', 'project_id', 'source_run_id', 'created_at', 'status')}
                result.update(approval_sha256=row['envelope_sha256'], target=envelope['target'], policy=envelope['policy'],
                              criteria_sha256=envelope['criteria_sha256'], skill_sha256=envelope['skill_sha256'],
                              result=json.loads(row['result']) if row['result'] else None,
                              error=json.loads(row['error']) if row['error'] else None)
                result['calls'] = [{**dict(call), 'response': json.loads(call['response']) if call['response'] else None,
                                    'receipt': json.loads(call['receipt']) if call['receipt'] else None}
                                   for call in conn.execute('SELECT * FROM evaluation_calls WHERE evaluation_id=? ORDER BY batch', (evaluation_id,))]
                result['extended_requests'] = [{'batch': i, 'chars': b['request']['chars'], 'request_sha256': b['request']['body_sha256']}
                                               for i, b in enumerate(envelope['batches']) if b['requires_extension']]
                decision = conn.execute('SELECT * FROM evaluation_decisions WHERE evaluation_id=?', (evaluation_id,)).fetchone()
                result['decision'] = dict(decision) if decision else None
                if preview: result['envelope'] = envelope
                return result
            except (ValueError, TypeError, KeyError) as exc:
                raise FileReadError('evaluation_record_corrupt', '评分记录身份不一致或损坏。') from exc

    def decide(self, evaluation_id, *, approved, approval_sha256=None, allow_extended_requests=False):
        record = self.show(evaluation_id, preview=True)
        if approved and approval_sha256 != record['approval_sha256']:
            raise FileReadError('approval_mismatch', '批准须匹配完整独立评分包。')
        if approved and record['extended_requests'] and allow_extended_requests is not True:
            raise FileReadError('evaluation_context_approval_required', '评分包中有精确超限请求；须审阅各条完整请求并显式批准这份扩展清单，不增加其他权限。')
        with self.open(write=True) as conn, conn:
            changed = conn.execute("UPDATE evaluations SET status=? WHERE id=? AND status='awaiting_approval'",
                                   ('queued' if approved else 'rejected', evaluation_id))
            if changed.rowcount != 1: raise FileReadError('evaluation_not_pending', '评分未等待批准，不能重复批准。')
            conn.execute('INSERT INTO evaluation_decisions VALUES (?,?,?,?,?)',
                         (evaluation_id, int(approved), record['approval_sha256'], int(allow_extended_requests is True), now()))
        return self.show(evaluation_id)

    def _status(self, identity, status, error=None):
        with self.open(write=True) as conn, conn:
            conn.execute('UPDATE evaluations SET status=?,error=? WHERE id=?', (status, canonical(error) if error else None, identity))

    def _results(self, identity):
        with self.open() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT response FROM evaluation_calls WHERE evaluation_id=? AND status='completed' ORDER BY batch", (identity,))]

    def _call(self, identity, index, envelope):
        provider = self.model or DeepSeekModel(ModelSettings.from_env())
        settings = provider.settings
        if (model_target(settings) != envelope['target'] or settings.timeout != envelope['policy']['request_timeout_seconds']
                or envelope['skill'] != skill_text()
                or skill_text() != PROMPTS['cognition_evaluation'] or envelope['contract'] != output_contract('cognition_evaluation')):
            raise FileReadError('evaluation_contract_changed', '评分目标、Skill 或结构契约已变化；需新预览和批准。')
        batch = envelope['batches'][index]; request = batch['request']
        if prepare_model_request(settings, batch['payload'], purpose='cognition_evaluation') != request:
            raise FileReadError('evaluation_request_changed', '完整评分请求与冻结身份不同，未外发。')
        with self.open(write=True) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if batch['requires_extension']:
                decision = conn.execute('SELECT * FROM evaluation_decisions WHERE evaluation_id=?', (identity,)).fetchone()
                if not decision or not decision['approved'] or not decision['allow_extended_requests'] or decision['approval_sha256'] != digest(envelope):
                    raise FileReadError('evaluation_context_approval_required', '本次精确扩展请求没有匹配的批准，未外发。')
            old = conn.execute('SELECT * FROM evaluation_calls WHERE evaluation_id=? AND batch=?', (identity, index)).fetchone()
            if old:
                if old['request_sha256'] != request['body_sha256']:
                    raise FileReadError('evaluation_request_changed', '保存的评分请求身份不匹配。')
                if old['status'] == 'completed': return json.loads(old['response'])
                raise FileReadError('evaluation_execution_unknown' if old['status'] == 'pending' else 'evaluation_response_invalid', '既有评分请求不可重发，请核对已保存收据。')
            count = conn.execute('SELECT count(*) FROM evaluation_calls WHERE evaluation_id=?', (identity,)).fetchone()[0]
            if count >= envelope['policy']['max_calls']:
                raise FileReadError('evaluation_budget', '独立评分请求预算已用完。')
            conn.execute('INSERT INTO evaluation_calls VALUES (?,?,?,?,?,?,?)', (identity, index, request['body_sha256'], 'pending', None, None, None))
        try:
            response = provider.complete(batch['payload'], purpose='cognition_evaluation', expected_request=request) if isinstance(provider, DeepSeekModel) else provider.complete(batch['payload'], purpose='cognition_evaluation')
        except Exception as exc:
            raise FileReadError('evaluation_execution_unknown', '评分请求未取得可确认结果，不自动重发。') from exc
        receipt = safe_model_receipt(response)
        error, value = None, None
        try:
            from .model_outputs import CognitionEvaluationResponse
            raw = parse_response(receipt)
            CognitionEvaluationResponse.model_validate(raw)
            value = validate_evaluation(raw, batch['payload'])
        except Exception:
            error = {'code': 'evaluation_response_invalid', 'message': '已保存评分响应，但结构、原文身份或逐项覆盖校验未通过；不修补或重发。'}
        with self.open(write=True) as conn, conn:
            conn.execute('UPDATE evaluation_calls SET status=?,response=?,receipt=?,error=? WHERE evaluation_id=? AND batch=?',
                         ('invalid' if error else 'completed', canonical(value) if value else None, canonical(receipt), canonical(error) if error else None, identity, index))
        if error: raise FileReadError(error['code'], error['message'])
        return value

    def run(self, evaluation_id):
        from .cognition import execution_lock
        with execution_lock(self.state_dir, f'evaluation-{valid_id(evaluation_id)}'):
            record = self.show(evaluation_id, preview=True)
            if record['status'] != 'queued': raise FileReadError('evaluation_not_queued', '独立评分尚未批准或已结束。')
            envelope = record['envelope']
            self._status(evaluation_id, 'running')
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                from langgraph.graph import StateGraph, START, END
                from langsmith import tracing_context
                directory = self.state_dir / 'evaluation-checkpoints'
                if directory.is_symlink(): raise FileReadError('evaluation_storage_error', '评分检查点不能是符号链接。')
                directory.mkdir(exist_ok=True, mode=0o700)
                path = directory / f'{evaluation_id}.sqlite3'
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600); os.close(fd)
                def evaluate(state):
                    index = state.get('next_batch', 0)
                    self._call(evaluation_id, index, envelope)
                    return {'next_batch': index + 1}
                builder = StateGraph(EvaluationState)
                builder.add_node('evaluate', evaluate)
                builder.add_edge(START, 'evaluate')
                builder.add_conditional_edges('evaluate', lambda state: END if state['next_batch'] >= len(envelope['batches']) else 'evaluate')
                with closing(sqlite3.connect(path, check_same_thread=False)) as conn, tracing_context(enabled=False):
                    graph = builder.compile(checkpointer=SqliteSaver(conn))
                    config = {'configurable': {'thread_id': evaluation_id}, 'recursion_limit': 260}
                    snapshot = graph.get_state(config)
                    if not snapshot.values or snapshot.next:
                        graph.invoke(None if snapshot.values else {'next_batch': 0}, config, durability='sync')
                self._status(evaluation_id, 'completed')
            except FileReadError as exc:
                self._status(evaluation_id, 'pending_execution' if exc.code == 'evaluation_execution_unknown' else 'partial', {'code': exc.code, 'message': str(exc)})
            except Exception:
                self._status(evaluation_id, 'partial', {'code': 'evaluation_failed', 'message': '评分执行停止，请核对保存的状态和收据。'})
            return self.report(evaluation_id)

    def recover(self, evaluation_id):
        from .cognition import execution_lock
        with execution_lock(self.state_dir, f'evaluation-{valid_id(evaluation_id)}'):
            record = self.show(evaluation_id)
            if record['status'] not in ('running', 'partial', 'pending_execution'):
                raise FileReadError('evaluation_not_recoverable', '评分状态不需要恢复。')
            if any(c['status'] != 'completed' for c in record['calls']):
                raise FileReadError('evaluation_execution_unknown', '存在未知或无效评分请求，不重发；已完成响应可单独查看。')
            self._status(evaluation_id, 'queued')
        return self.show(evaluation_id)

    def report(self, evaluation_id):
        record = self.show(evaluation_id, preview=True)
        result = aggregate(record['envelope'], self._results(evaluation_id))
        # A full set of receipts alone cannot turn an unapproved/rejected record
        # or a failed finalization into an overall pass.
        if record['status'] != 'completed': result.update(status='partial', overall_passed=False)
        return {'evaluation_id': evaluation_id, 'source_run_id': record['source_run_id'],
                'scope': 'frozen_cognition_quality_only',
                'commit': record['envelope']['snapshot']['commit'],
                'source_status': record['envelope']['snapshot']['status'],
                'snapshot_sha256': record['envelope']['snapshot_sha256'],
                'reference_phase': record['envelope']['criteria']['reference_phase'],
                'workflow_status': record['status'], 'approval_sha256': record['approval_sha256'],
                'criteria_sha256': record['criteria_sha256'], 'skill_sha256': record['skill_sha256'],
                'target': record['target'], 'result': result, 'error': record['error']}


def format_evaluation(report):
    from .terminal import display_text
    result = report['result']
    lines = ['Zest · 独立认知评分', f"评分：{report['evaluation_id']}", f"调查：{report['source_run_id']}",
             f"状态：{report['workflow_status']}；质量门槛：{'通过' if result['overall_passed'] else '未通过或未完成'}"]
    for category in ('facts', 'flows', 'unknowns'):
        count = result['coverage'][category]
        lines.append(f"{category}：{count['full']}/{count['total']} 完整覆盖")
    lines.append(f"已审查 observed：{result['reviewed_observed_findings']}/{result['total_observed_findings']}")
    lines.append('自动接收中有问题的发现：' + ', '.join(result['flagged_auto_reviewed_findings']))
    lines.append('模型评分不是人工金标准；原子项按出现次数统计，未做语义去重。')
    if report['error']: lines.append(display_text(report['error']))
    return '\n'.join(lines)
