"""Known review-format failures stay pending; other evidence can still be delivered."""
import copy
import json
import unittest
from unittest.mock import patch

try:
    import httpx
    import langgraph
except ImportError as exc:
    raise unittest.SkipTest('AI extras required') from exc

from tests import test_cognition as fixtures
from tests.test_model_transport import reply
from zest.cognition import CognitionEngine
from zest.cognition_context import review_payload
from zest.cognition_contracts import POLICY, canonical
from zest.cognition_store import digest
from zest.cognition_views import format_cognition, format_dossier
from zest.files import FileReadError
from zest.models import DeepSeekModel, prepare_model_request
from zest.qa import prepare_context
from zest.tasks import TaskRunner


class BatchIsolationTest(unittest.TestCase):
    setUp = fixtures.CognitionTest.setUp
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def install_reviews(self, handler):
        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(handler)))
        sdk = DeepSeekModel(self.model.settings, http_client=client)
        original = self.model.complete

        def complete(payload, *, purpose='qa'):
            if purpose != 'project_review':
                return original(payload, purpose=purpose)
            self.model.calls.append((purpose, copy.deepcopy(payload)))
            return sdk.complete(payload, purpose=purpose)
        self.enterContext(patch.object(self.model, 'complete', side_effect=complete))

    @staticmethod
    def payload(request):
        return json.loads(json.loads(request.content)['messages'][1]['content'])

    @staticmethod
    def good(request, payload):
        return reply(json.loads(request.content), {'judgments': [fixtures.review_judgment(f) for f in payload['findings']]})

    def bad_first_batch(self):
        self.sent = []

        def handle(request):
            payload = self.payload(request)
            self.sent.append(payload)
            if len(payload['findings']) == 4:
                return reply(json.loads(request.content), {}, argument_text='{} }')
            return self.good(request, payload)
        self.install_reviews(handle)

    def seed_exhausted_review(self):
        self.bad_first_batch()
        run = self.approved()['id']
        envelope = self.store.show(run, include_envelope=True)['envelope']
        self.store.set_status(run, 'planning')
        from zest.cognition_tools import PackageTools
        tools = PackageTools(envelope)
        self.engine.plan(run, envelope, tools)
        self.engine.dispatch(run, envelope, tools, self.store.show(run)['plan'])
        self.store.set_status(run, 'reviewing')
        batch = self.store.show(run)['findings'][:4]
        payload = review_payload(envelope['commit'], batch, [])
        actor = 'review_' + digest([f['id'] for f in batch])[:12]
        with self.assertRaises(FileReadError) as caught:
            self.engine.call(run, actor, 0, 'project_review', payload, envelope)
        self.assertEqual(caught.exception.code, 'cognition_response_invalid')
        return run, envelope

    def test_failed_batch_is_pending_other_batch_is_delivered_and_qa_reuses_it(self):
        self.bad_first_batch()
        run = self.approved()['id']
        result = self.engine.run(run)
        self.assertEqual(result['status'], 'partial', result.get('error'))
        self.assertEqual(len(result['review_failures']), 1)
        failed_ids = set(result['review_failures'][0]['finding_ids'])
        self.assertEqual(len(failed_ids), 4)
        self.assertEqual([f['status'] for f in result['findings']], ['pending_review'] * 4 + ['auto_reviewed'] * 2)
        calls = self.store.show(run, include_calls=True)['calls']
        failed = next(c for c in calls if c['status'] == 'invalid')
        self.assertEqual(len(failed['attempts']), 2)
        self.assertTrue(all(a['receipt']['raw_response']['provider_tool_calls'][0]['function']['arguments'] == '{} }' for a in failed['attempts']))
        self.assertEqual(len(self.sent), 3)
        self.assertIn('批次格式失败', format_cognition(result))
        self.assertIn('批次格式失败', format_dossier(self.store.dossier(run)))
        notes = self.store.for_context(self.project['id'], result['commit'])
        self.assertEqual(len(notes), 2)
        self.assertFalse(failed_ids & {n['id'] for n in notes})
        from zest.management_context import build_management_context
        management = build_management_context(self.state_dir, self.project['id'])
        actions = management['cognition']['followup_actions']
        self.assertEqual(len(actions), 4)
        self.assertTrue(all('批次格式失败' in a['reason'] for a in actions))
        context = prepare_context(self.project, 'add返回什么？', state_dir=self.state_dir)
        self.assertEqual(sum(s['evidence'].get('kind') == 'auto_reviewed_project_knowledge' for s in context['sources']), 2)
        runner = TaskRunner(self.state_dir, model=self.model)
        task = runner.create(self.project, 'add返回什么？')
        answer = runner.decide(task['task_id'], approved=True, payload_sha256=task['payload_sha256'])
        self.assertEqual(answer['status'], 'completed')
        self.assertEqual(answer['result']['evidence'][0]['evidence']['kind'], 'auto_reviewed_project_knowledge')
        count = self.store.show(run)['call_count']
        self.engine.review(run, self.store.show(run, include_envelope=True)['envelope'])
        self.assertEqual(self.store.show(run)['call_count'], count)
        self.assertEqual(len(self.sent), 3)

    def test_recovery_after_saved_failure_before_event_skips_failed_members_without_resend(self):
        run, _ = self.seed_exhausted_review()
        self.assertFalse(any(e['action'] == 'review_batch_isolated' for e in self.store.show(run)['events']))
        self.assertEqual(len(self.store.show(run)['review_failures']), 1)
        self.store.recover(run)
        result = CognitionEngine(self.state_dir, model=self.model).run(run)
        self.assertEqual(result['status'], 'partial', result.get('error'))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(sum(f['status'] == 'auto_reviewed' for f in result['findings']), 2)

    def test_later_conflict_with_failed_batch_is_preserved_and_not_published(self):
        sent = []
        failed_id = []
        def handle(request):
            payload = self.payload(request);sent.append(payload)
            if len(payload['findings']) == 4:
                failed_id[:] = [payload['findings'][0]['finding_id']]
                return reply(json.loads(request.content), {}, argument_text='{} }')
            judgments = [fixtures.review_judgment(f) for f in payload['findings']]
            judgments[0]['conflicts'] = [{'reference_id': failed_id[0], 'reason': '离线夹具：保留未解决冲突。'}]
            return reply(json.loads(request.content), {'judgments': judgments})
        self.install_reviews(handle)
        with patch.object(self.engine, 'followups', return_value=False):
            result = self.engine.run(self.approved()['id'])
        self.assertEqual(result['status'], 'partial')
        item = next(f for f in result['findings'] if f['id'] == failed_id[0])
        self.assertEqual(item['status'], 'needs_investigation')
        self.assertTrue(item['review']['conflict_links'])
        self.assertEqual(len(result['review_failures']), 1)
        self.assertEqual(len(self.store.for_context(self.project['id'], result['commit'])), 1)
        self.assertEqual(len(sent), 3)

    def test_exact_extension_after_isolation_can_be_approved_but_mixed_unknown_cannot(self):
        run, envelope = self.seed_exhausted_review()
        payload = {'padding': 'x' * 33000}
        request = prepare_model_request(self.model.settings, payload, purpose='project_review')
        with self.assertRaises(FileReadError):
            self.store.require_context(run, 'future_review', 0, envelope, request)
        pending = self.store.context_requests(run)[0]
        self.store.set_status(run, 'awaiting_context_approval')
        result = self.store.decide_context(run, 'future_review', 0, approved=True, request_sha256=pending['request_sha256'])
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(len(self.sent), 2)
        self.store.set_status(run, 'reviewing')
        with patch.object(self.model, 'complete', side_effect=TimeoutError):
            with self.assertRaises(FileReadError):
                self.engine.call(run, 'unrelated', 0, 'project_plan', {}, envelope)
        with self.assertRaises(FileReadError):
            self.store.recover(run)
        with self.assertRaises(FileReadError):
            self.store.require_context(run, 'another_review', 0, envelope, request)
        pending = next(r for r in self.store.context_requests(run) if r['status'] == 'awaiting_approval')
        with self.assertRaises(FileReadError) as caught:
            self.store.decide_context(run, 'another_review', 0, approved=True, request_sha256=pending['request_sha256'])
        self.assertEqual(caught.exception.code, 'cognition_execution_unknown')
        self.assertEqual(len(self.sent), 2)

    def test_timeout_still_stops_and_never_continues_the_next_batch(self):
        sent = []
        def handle(request):
            sent.append(request)
            raise httpx.ReadTimeout('offline', request=request)
        self.install_reviews(handle)
        run = self.approved()['id']
        result = self.engine.run(run)
        self.assertEqual(result['status'], 'pending_execution')
        self.assertEqual(result['review_failures'], [])
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(f['status'] == 'pending_review' for f in result['findings']))
        with self.assertRaises(FileReadError):
            self.store.recover(run)

    def test_valid_structure_with_forged_evidence_still_stops(self):
        sent = []
        def handle(request):
            payload = self.payload(request);sent.append(payload)
            judgments = [fixtures.review_judgment(f) for f in payload['findings']]
            judgments[0]['checks'][0]['evidence_ids'] = ['outside']
            return reply(json.loads(request.content), {'judgments': judgments})
        self.install_reviews(handle)
        result = self.engine.run(self.approved()['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['review_failures'], [])
        self.assertEqual(result['error']['code'], 'invalid_cognition_reference')
        self.assertEqual(len(sent), 1)

    def test_target_change_after_known_batch_failure_still_stops_before_another_request(self):
        from dataclasses import replace
        sent = []
        def handle(request):
            sent.append(request)
            if len(sent) == 2:
                self.model.settings = replace(self.model.settings, name='changed-target')
            return reply(json.loads(request.content), {}, argument_text='{} }')
        self.install_reviews(handle)
        result = self.engine.run(self.approved()['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'model_target_changed')
        self.assertEqual(len(result['review_failures']), 1)
        self.assertEqual(len(sent), 2)

    def test_isolation_does_not_increase_call_budget(self):
        self.bad_first_batch()
        with patch.dict(POLICY, {'max_model_calls': 7}):
            result = self.engine.run(self.approved()['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['error']['code'], 'cognition_call_budget')
        self.assertEqual(result['call_count'], 7)
        self.assertEqual(len(result['review_failures']), 1)
        self.assertEqual(len(self.sent), 2)

    def test_only_durable_exhausted_review_receipts_qualify_and_old_policy_stays_closed(self):
        run, envelope = self.seed_exhausted_review()
        with self.store.open() as conn:
            call = dict(conn.execute("SELECT * FROM cognition_calls WHERE status='invalid' AND run_id=?", (run,)).fetchone())
            attempt = dict(conn.execute('SELECT * FROM cognition_call_attempts WHERE run_id=? AND actor=? AND attempt=2', (run, call['actor'])).fetchone())
        for field, value in (('status', 'uncertain'), ('status', 'pending'), ('receipt', None),
                             ('receipt', canonical({'structured_status':'invalid','structured_error':'sensitive_model_output','raw_response':None}))):
            with self.subTest(field=field, value=value):
                with self.store.open(write=True) as conn, conn:
                    conn.execute(f'UPDATE cognition_call_attempts SET {field}=? WHERE run_id=? AND actor=? AND attempt=2', (value,run,call['actor']))
                self.assertEqual(self.store.show(run)['review_failures'], [])
                with self.assertRaises(FileReadError): self.store.recover(run)
                with self.store.open(write=True) as conn, conn:
                    conn.execute(f'UPDATE cognition_call_attempts SET {field}=? WHERE run_id=? AND actor=? AND attempt=2', (attempt[field],run,call['actor']))
        for purpose in ('project_plan', 'project_report'):
            with self.subTest(purpose=purpose):
                with self.store.open(write=True) as conn, conn:conn.execute('UPDATE cognition_calls SET purpose=? WHERE run_id=? AND actor=?',(purpose,run,call['actor']))
                self.assertEqual(self.store.show(run)['review_failures'], [])
                with self.assertRaises(FileReadError):self.store.recover(run)
        with self.store.open(write=True) as conn, conn:conn.execute("UPDATE cognition_calls SET purpose='project_review' WHERE run_id=? AND actor=?",(run,call['actor']))
        old = copy.deepcopy(envelope);del old['policy']['review_batch_failure']
        with self.store.open(write=True) as conn, conn:
            conn.execute('UPDATE cognition_runs SET envelope=?,envelope_sha256=? WHERE id=?',(canonical(old),digest(old),run))
        self.assertNotIn('review_failures', self.store.show(run))
        with self.assertRaises(FileReadError):self.store.recover(run)
        with self.assertRaises(FileReadError) as caught:
            self.engine.call(run, call['actor'], 0, 'project_review', json.loads(call['payload']), old)
        self.assertEqual(caught.exception.code, 'cognition_contract_changed')
        self.assertEqual(len(self.sent), 2)


if __name__ == '__main__':
    unittest.main()
