import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { RecordDetail } from '../src/RecordDetail.tsx'

const render = (kind, value) => renderToStaticMarkup(createElement(RecordDetail, { kind, value, onClose() {} }))
const sourceRecord = { id: 'E1', evidence: { path: 'app.py', commit: 'a'.repeat(40), start_line: 4, end_line: 5 }, content: 'def add(a,b):\n    return a-b\n', content_sha256: 'c'.repeat(64) }

test('review links cited answer to fixed original source and preserves unknowns', () => {
  const { content, ...lineSource } = sourceRecord
  const html = render('tasks', { status: 'completed', context: { question: '加法错误？', sources: [{ ...lineSource, lines: [{ number: 4, text: 'return a-b' }] }] }, result: { answer: '需要检查加法。[E1]', citations: ['E1'], unknowns: ['缺少调用端'] } })
  for (const value of ['调查结论', '[E1]', 'app.py', '行 4–5', '结果引用', '缺少调用端', 'a'.repeat(40)]) assert.ok(html.includes(value), value)
  assert.ok(html.includes('引用可定位来源，仍需检查来源是否支持结论'))
  assert.ok(html.includes('4  return a-b'))
})

test('untrusted model, source and diff HTML are escaped rather than executed', () => {
  const payload = '<script>alert("bad")</script><img src=x onerror=alert(1)>'
  const task = render('tasks', { context: { sources: [{ ...sourceRecord, content: payload }] }, result: { answer: payload } })
  const repair = render('repairs', { plan: { summary: payload, changes: [{ path: 'app.py', diff: '+' + payload, content: payload }] } })
  for (const html of [task, repair]) {
    assert.ok(!html.includes('<script>'))
    assert.ok(!html.includes('<img src='))
    assert.ok(html.includes('&lt;script&gt;'))
  }
})

test('candidate diff and approval identity remain separate from verification and release', () => {
  const html = render('repairs', { status: 'awaiting_approval', plan_sha256: 'd'.repeat(64), candidate_commit: null, plan: { base_commit: 'b'.repeat(40), summary: '修复加法', changes: [{ path: 'app.py', expected_sha256: 'c'.repeat(64), new_sha256: 'e'.repeat(64), diff: '-return a-b\n+return a+b\n', content: 'return a+b\n' }] } })
  for (const value of ['等待批准', 'd'.repeat(64), '-return a-b', '+return a+b', '独立验证']) assert.ok(html.includes(value), value)
  assert.ok(!html.includes('<button>批准'))
})

test('planned new paths and draft knowledge are not presented as confirmed facts', () => {
  const html = render('tasks', { status: 'awaiting_approval', context: { sources: [{ id: 'E1', evidence: { kind: 'planned_new_file', path: 'new.py', commit: 'a'.repeat(40) }, content: '' }] } })
  assert.ok(html.includes('不代表已有文件或代码证据'))
  assert.ok(html.includes('等待这份数据请求的批准'))
  const note = render('knowledge', { title: '约定', body: '待审阅', scope: 'project', status: 'draft' })
  assert.ok(note.includes('待确认'))
  assert.ok(note.includes('草稿不进入 Agent 上下文'))
})

test('verification distinguishes offline, unrun baseline and actual candidate measurements', () => {
  const html = render('verifications', { status: 'passed', plan: { backend: 'offline_fixture', baseline_strategy: 'not_applicable_initial_creation' }, result: {
    baseline: { status: 'not_applicable', tests_run: 0, container_started: false, cleanup_ok: true },
    candidate: { status: 'passed', tests_run: 3, exit_code: 0, cleanup_ok: true, policy_verified: true, output: '<img src=x onerror=alert(1)> fixture log' } } })
  for (const value of ['离线或未知后端', '基线未运行，不是测试通过', '候选结果', '测试数量', '&lt;img']) assert.ok(html.includes(value), value)
  assert.ok(!html.includes('<img src='))
  const pending = render('verifications', { status: 'awaiting_approval', plan: { backend: 'docker' }, result: null })
  assert.ok(pending.includes('尚无执行结果'))
  assert.ok(!pending.includes('验证通过'))
})
