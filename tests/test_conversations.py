"""Durable conversation history without inventing project facts or replaying calls."""

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from tests import test_projects as fixtures
from tests.test_tasks import FixtureModel
from zest.conversations import ConversationStore
from zest.documents import initialize_documents, read_workspace_document, workspace_documents
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.qa import model_payload
from zest.storage import ProjectRegistry
from zest.tasks import TaskRunner, approval_digest


class ConversationsTest(TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self):
        (self.repo / 'README.md').write_text('# Demo\nPython CLI\n')
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)

    def conversation(self):
        project = self.project()
        self.model = FixtureModel()
        self.store = ConversationStore(self.state_dir, model=self.model)
        value = self.store.create(project)
        return project, value

    def answer(self, turn):
        task = turn['task']
        return TaskRunner(self.state_dir, model=self.model).decide(task['task_id'], approved=True, payload_sha256=task['payload_sha256'])

    def test_two_turns_persist_and_freeze_history_into_the_next_exact_approval(self):
        project, conversation = self.conversation()
        first = self.store.send(conversation['id'], '项目使用什么语言？', message_id=str(uuid4()))
        self.assertEqual(self.model.calls, [])
        first_result = self.answer(first)
        second = self.store.send(conversation['id'], '它如何使用？', message_id=str(uuid4()))
        context = second['task']['context']
        history = context['conversation_context']['history']
        self.assertEqual(history[0]['user'], '项目使用什么语言？')
        self.assertEqual(history[0]['assistant'], first_result['result']['answer'])
        self.assertEqual(history[0]['observed_commit'], context['commit'])
        self.assertEqual(model_payload(context)['conversation_context']['history'], history)
        changed = json.loads(json.dumps(context))
        changed['conversation_context']['history'][0]['user'] = 'changed'
        self.assertNotEqual(approval_digest(context, second['task']['target']), approval_digest(changed, second['task']['target']))
        reopened = ConversationStore(self.state_dir).show(conversation['id'])
        self.assertEqual([t['question'] for t in reopened['turns']], ['项目使用什么语言？', '它如何使用？'])
        self.assertEqual(reopened['project_id'], project['id'])
        self.assertTrue((Path(project['resource_dir']) / 'conversations.sqlite3').exists())
        self.assertFalse((self.state_dir / 'conversations.sqlite3').exists())

    def test_duplicate_message_is_read_only_and_conflicting_identity_is_rejected(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        first = self.store.send(conversation['id'], '语言？', message_id=message)
        again = self.store.send(conversation['id'], '语言？', message_id=message)
        self.assertEqual(first['task_id'], again['task_id'])
        self.assertEqual(len(self.store.show(conversation['id'])['turns']), 1)
        with self.assertRaises(FileReadError) as caught:
            self.store.send(conversation['id'], '其他问题', message_id=message)
        self.assertEqual(caught.exception.code, 'conversation_message_conflict')
        self.assertEqual(self.model.calls, [])

    def test_pending_turn_blocks_new_work_and_cancellation_allows_followup(self):
        _, conversation = self.conversation()
        first = self.store.send(conversation['id'], '语言？', message_id=str(uuid4()))
        with self.assertRaises(FileReadError) as caught:
            self.store.send(conversation['id'], '继续？', message_id=str(uuid4()))
        self.assertEqual(caught.exception.code, 'conversation_turn_pending')
        TaskRunner(self.state_dir, model=self.model).decide(first['task_id'], approved=False)
        next_turn = self.store.send(conversation['id'], '换个问题', message_id=str(uuid4()))
        self.assertEqual(next_turn['sequence'], 2)
        self.assertEqual(self.model.calls, [])

    def test_old_answers_keep_observed_version_and_are_not_new_source_evidence(self):
        _, conversation = self.conversation()
        first = self.store.send(conversation['id'], '语言？', message_id=str(uuid4()))
        self.answer(first)
        (self.repo / 'README.md').write_text('# Demo\nTypeScript CLI\n')
        new_commit = self.commit('change implementation')
        second = self.store.send(conversation['id'], '现在呢？', message_id=str(uuid4()))
        context = second['task']['context']
        self.assertEqual(context['commit'], new_commit)
        self.assertNotEqual(context['conversation_context']['history'][0]['observed_commit'], new_commit)
        self.assertIn('not_project_evidence', context['conversation_context']['role'])
        self.assertTrue(all(s['evidence'].get('commit') == new_commit for s in context['sources']))

    def test_interruption_preserves_task_identity_and_does_not_reprepare(self):
        project, conversation = self.conversation()
        message = str(uuid4())
        original = TaskRunner.create
        def prepared_then_interrupted(runner, *args, **kwargs):
            original(runner, *args, **kwargs)
            raise RuntimeError('simulated process interruption')
        with patch.object(TaskRunner, 'create', prepared_then_interrupted), self.assertRaises(RuntimeError):
            self.store.send(conversation['id'], '语言？', message_id=message)
        with patch.object(TaskRunner, 'create', side_effect=AssertionError('must not repeat')):
            saved = self.store.send(conversation['id'], '语言？', message_id=message)
        self.assertEqual(saved['status'], 'awaiting_approval')
        self.assertEqual(self.model.calls, [])
        self.assertEqual(ConversationStore(self.state_dir).show(conversation['id'])['project_id'], project['id'])

    def test_failed_preparation_is_saved_and_can_be_followed_up(self):
        _, conversation = self.conversation()
        with patch.object(TaskRunner, '_target', side_effect=FileReadError('model_missing', '请配置模型')):
            failed = self.store.send(conversation['id'], '语言？', message_id=str(uuid4()))
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error']['code'], 'model_missing')
        next_turn = self.store.send(conversation['id'], '现在已配置', message_id=str(uuid4()))
        self.assertEqual(next_turn['sequence'], 2)

    def test_personal_conversation_and_sensitive_messages_are_not_shared(self):
        _, conversation = self.conversation()
        with self.assertRaises(FileReadError):
            self.store.send(conversation['id'], 'sk-' + 'a'*40, message_id=str(uuid4()))
        self.assertEqual(self.store.show(conversation['id'])['turns'], [])
        self.store.send(conversation['id'], '只存在本地的讨论', message_id=str(uuid4()))
        self.git('check-ignore', '--', '.zest/state/conversations.sqlite3')
        self.git('check-ignore', '--', '.zest/state/conversation-locks/test.lock')

    def test_followup_rereads_previously_named_source_at_current_version(self):
        _, conversation = self.conversation()
        (self.repo/'app.py').write_text('def cancel():\n    return False\n')
        self.commit('add source')
        first = self.store.send(conversation['id'], '解释 app.py 中的取消规则', message_id=str(uuid4()))
        self.answer(first)
        (self.repo/'app.py').write_text('def cancel():\n    return True\n')
        current = self.commit('change source')
        followup = self.store.send(conversation['id'], '它现在有何变化？', message_id=str(uuid4()))
        sources = followup['task']['context']['sources']
        source = next(s for s in sources if s['evidence'].get('path') == 'app.py')
        self.assertEqual(source['evidence']['commit'], current)
        self.assertIn('True', json.dumps(source))

    def test_web_reopen_lists_and_reads_conversation_without_calling_model(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        from zest.web_commands import execute, validate_command
        project = self.project()
        client = TestClient(create_app(self.state_dir))
        self.assertEqual(client.get('/api/projects/'+project['id']+'/conversations').json()['data'], [])
        value = execute(self.state_dir, 'conversation.create', validate_command('conversation.create', {'project_id': project['id']}))
        model = FixtureModel()
        with patch.object(TaskRunner, '_target', return_value=TaskRunner(self.state_dir, model=model)._target()):
            result = execute(self.state_dir, 'conversation.send', validate_command('conversation.send', {'id': value['id'], 'question': '语言？', 'message_id': str(uuid4()), 'automatic_answer': False}))
        self.assertEqual(result['status'], 'awaiting_approval')
        self.assertEqual(client.get('/api/conversations/'+value['id']).json()['data']['turns'][0]['question'], '语言？')
        self.assertEqual(len(client.get('/api/projects/'+project['id']+'/conversations').json()['data']), 1)
        self.assertEqual(model.calls, [])

    def test_workspace_reader_includes_uncommitted_docs_but_blocks_private_paths_and_links(self):
        project = self.project()
        initialize_documents(project)
        path = '.zest/docs/api.md'
        (self.repo/path).write_text('# API\n尚未提交的新接口\n')
        self.assertIn(path, [d['path'] for d in workspace_documents(project)['documents']])
        self.assertIn('尚未提交', read_workspace_document(project, path)['content'])
        for bad in ('.zest/workspace.json', '.zest/docs/../workspace.json', '.zest/state/tasks.sqlite3'):
            with self.subTest(path=bad), self.assertRaises(FileReadError):
                read_workspace_document(project, bad)
        (self.repo/'.zest/docs/link.md').symlink_to(self.repo/'README.md')
        self.assertNotIn('.zest/docs/link.md', [d['path'] for d in workspace_documents(project)['documents']])
        with self.assertRaises(FileReadError):
            read_workspace_document(project, '.zest/docs/link.md')

    def test_workspace_reader_paginates_and_rejects_secrets(self):
        project = self.project()
        initialize_documents(project)
        path = '.zest/docs/api.md'
        (self.repo/path).write_text(''.join(f'line{i}\n' for i in range(250)))
        first = read_workspace_document(project, path)
        second = read_workspace_document(project, path, start_line=201, end_line=400)
        self.assertTrue(first['more_after'])
        self.assertFalse(second['more_after'])
        self.assertIn('line200', second['content'])
        self.assertEqual(first['content_sha256'], second['content_sha256'])
        (self.repo/path).write_text('sk-'+'x'*40)
        with self.assertRaises(FileReadError):
            read_workspace_document(project, path)

    def test_pagination_keeps_all_messages_while_model_history_is_explicitly_bounded(self):
        project, conversation = self.conversation()
        local = ConversationStore(project['resource_dir'])
        with local._open(write=True) as conn, conn:
            for number in range(1, 53):
                conn.execute('INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)',
                    (str(uuid4()), conversation['id'], number, f'历史需求{number}', str(uuid4()), 'failed', None, conversation['created_at']))
        latest = self.store.show(conversation['id'])
        self.assertTrue(latest['has_earlier'])
        self.assertEqual([t['sequence'] for t in latest['turns']], list(range(3, 53)))
        earlier = self.store.show(conversation['id'], before=3)
        self.assertEqual([t['sequence'] for t in earlier['turns']], [1, 2])
        self.assertFalse(earlier['has_earlier'])
        history = local._history(conversation['id'])
        self.assertTrue(history['truncated'])
        self.assertEqual(len(history['history']), 12)
        self.assertEqual(len(self.store.show(conversation['id'])['turns']), 50)

    def test_automatic_questions_answer_once_and_continue_with_previous_answer(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        first = self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(len(self.model.calls), 1)
        again = self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
        self.assertEqual(again['task_id'], first['task_id'])
        self.store.answer(conversation['id'], message_id=message)
        self.store.show(conversation['id'])
        self.assertEqual(len(self.model.calls), 1)
        followup = self.store.send(conversation['id'], '如何运行？', message_id=str(uuid4()), automatic_answer=True)
        self.assertEqual(followup['status'], 'completed')
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(followup['task']['context']['conversation_context']['history'][0]['assistant'], first['task']['result']['answer'])
        self.assertEqual(approval_digest(first['task']['context'], first['task']['target']), first['task']['payload_sha256'])

    def test_older_prepared_message_can_be_continued_once_without_repreparing(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        prepared = self.store.send(conversation['id'], '语言？', message_id=message)
        with patch.object(TaskRunner, 'create', side_effect=AssertionError('must reuse saved task')):
            result = self.store.answer(conversation['id'], message_id=message)
            self.store.answer(conversation['id'], message_id=message)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_id'], prepared['task_id'])
        self.assertEqual(len(self.model.calls), 1)

    def test_prepared_then_interrupted_automatic_message_is_read_back_without_replay(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        with patch.object(ConversationStore, '_answer_prepared', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
        restored = self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
        self.assertEqual(restored['status'], 'awaiting_approval')
        self.assertFalse(self.model.calls)
        self.assertEqual(self.store.answer(conversation['id'], message_id=message)['status'], 'completed')
        self.assertEqual(len(self.model.calls), 1)

    def test_automatic_answer_failure_or_lost_response_is_never_retried(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        calls = []
        def lost(payload):
            calls.append(payload)
            raise FileReadError('model_request_failed', '离线模拟结果不明。')
        with patch.object(self.model, 'complete', side_effect=lost):
            first = self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
            self.store.send(conversation['id'], '语言？', message_id=message, automatic_answer=True)
            self.store.answer(conversation['id'], message_id=message)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(len(calls), 1)

    def test_cancelled_message_and_foreign_conversation_cannot_be_automatically_executed(self):
        project, conversation = self.conversation()
        message = str(uuid4())
        first = self.store.send(conversation['id'], '语言？', message_id=message)
        other = self.store.create(project)
        with self.assertRaises(FileReadError) as caught:
            self.store.answer(other['id'], message_id=message)
        self.assertEqual(caught.exception.code, 'conversation_message_not_found')
        TaskRunner(self.state_dir, model=self.model).decide(first['task_id'], approved=False)
        self.assertEqual(self.store.answer(conversation['id'], message_id=message)['status'], 'rejected')
        self.assertFalse(self.model.calls)

    def test_automatic_continuation_refuses_non_question_tasks(self):
        _, conversation = self.conversation()
        message = str(uuid4())
        prepared = self.store.send(conversation['id'], '语言？', message_id=message)
        other_purpose = {**prepared, 'task': {**prepared['task'], 'purpose': 'repair_proposal'}}
        with patch.object(ConversationStore, '_turn', return_value=other_purpose), self.assertRaises(FileReadError) as caught:
            self.store.answer(conversation['id'], message_id=message)
        self.assertEqual(caught.exception.code, 'conversation_task_mismatch')
        self.assertFalse(self.model.calls)

    def test_web_defaults_to_automatic_answer_and_mode_is_strict_boolean(self):
        from zest.web_commands import execute, validate_command
        _, conversation = self.conversation()
        parameters = {'id': conversation['id'], 'question': '语言？', 'message_id': str(uuid4())}
        value = validate_command('conversation.send', parameters)
        self.assertTrue(value['automatic_answer'])
        with patch('zest.tasks.ModelSettings.from_env', return_value=self.model.settings), patch('zest.tasks.DeepSeekModel', return_value=self.model):
            result = execute(self.state_dir, 'conversation.send', value)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.model.calls), 1)
        with self.assertRaises(FileReadError):
            validate_command('conversation.send', {**parameters, 'automatic_answer': 'false'})
