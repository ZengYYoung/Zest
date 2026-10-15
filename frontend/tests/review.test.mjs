import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { RecordDetail } from '../src/RecordDetail.tsx'
import { ProjectStructure } from '../src/ProjectStructure.tsx'
import { CognitionDetail } from '../src/CognitionDetail.tsx'

const render = (kind, value) => renderToStaticMarkup(createElement(RecordDetail, { kind, value, onClose() {} }))
const sourceRecord = { id: 'E1', evidence: { path: 'app.py', commit: 'a'.repeat(40), start_line: 4, end_line: 5 }, content: 'def add(a,b):\n    return a-b\n', content_sha256: 'c'.repeat(64) }

test('context extension is visibly unsent and keeps Web read only', () => {
  const html = renderToStaticMarkup(createElement(CognitionDetail, { onClose() {}, value: {
    status: 'awaiting_context_approval', context_requests: [{ actor: 'T1', sequence: 2, status: 'awaiting_approval',
      chars: 35000, default_chars: 32000, over_percent: 9.38 }],
  } }))
  for (const value of ['完整请求待扩展批准', '35000', '32000', '9.38', '尚未外发', '终端或 CLI']) assert.ok(html.includes(value), value)
  assert.ok(!html.includes('<button>批准'))
})

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

test('source map preserves symbol locations and incomplete static coverage without rendering HTML', () => {
  const html = renderToStaticMarkup(createElement(ProjectStructure, { value: { commit: 'a'.repeat(40),
    modules: [{ path: '<script>app.py', symbols: [{ lexical_name: 'Service.method', kind: 'function', evidence: { start_line: 3, end_line: 4 } }], imports: [{ kind: 'from', module: 'utils', level: 2, scope: 'Service', evidence: { start_line: 1 }, names: [{ name: 'helper', alias: 'h' }] }] }],
    skipped_counts: { sensitive_content: 1 }, warnings: [{ code: 'structure_skipped_sources' }], limitations: ['python_only'] } }))
  for (const value of ['Service.method', '行 3–4', 'from ..utils import helper as h', '部分来源未覆盖', '不执行项目源码', '&lt;script&gt;']) assert.ok(html.includes(value), value)
  assert.ok(!html.includes('<script>'))
})

test('cognition shows background tasks, source proofs and uncertainty without write controls', () => {
  const html = renderToStaticMarkup(createElement(CognitionDetail, { onClose() {}, value: {
    status: 'partial', commit: 'a'.repeat(40), call_count: 7, file_count: 2, policy: { max_model_calls: 32 }, worker_active: false,
    plan: { overview: [{ statement: '初步认识', kind: 'inferred' }], gaps: [{ section: 'constraints', question: '缺少历史决策' }] },
    jobs: [{ id: 'T1', status: 'completed', spec: { title: '调查入口', goal: '确认调用链', deliverable: '流程说明', depends_on: [], acceptance: ['有原文'] } }],
    findings: [{ id: 'T1:F1', job_id: 'T1', status: 'auto_reviewed', finding: { section: 'flows', kind: 'observed', statement: '<script>unsafe</script>', evidence: [{ text: 'return a+b', evidence: { path: 'app.py', commit: 'a'.repeat(40), start_line: 1, end_line: 2 } }] }, review: { reason: '原文支持' } }],
  } }))
  for (const value of ['部分完成', '7 / 32', '调查入口', '自动核对通过', 'app.py', '原文支持', '缺少历史决策', '仍可能出错', '&lt;script&gt;']) assert.ok(html.includes(value), value)
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('批准并启动'))
})

test('auto reviewed knowledge is distinct from human confirmation and retains original proofs', () => {
  const html = render('tasks', { context: { sources: [{ id: 'E1', title: '认知', body: '声明使用数据库', evidence: { kind: 'auto_reviewed_project_knowledge', run_id: 'run', finding_id: 'T1:F1', observed_commit: 'a'.repeat(40) }, provenance: [{ text: 'database=mysql', evidence: { path: 'config.toml', commit: 'a'.repeat(40), start_line: 1, end_line: 1 } }] }] } })
  for (const value of ['自动核对认知', '不等于人工确认', 'config.toml', 'database=mysql']) assert.ok(html.includes(value), value)
})
