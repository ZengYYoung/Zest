import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ProjectCheckEvents } from '../src/projectCheckEvents.ts'
import { MaintenanceNotice } from '../src/Maintenance.tsx'
import { ProjectStatus, VersionSelection } from '../src/ProjectStatus.tsx'

test('version selection distinguishes following HEAD from an explicitly managed version', () => {
  const render = version => renderToStaticMarkup(createElement(VersionSelection, { version }))
  const following = render({ selected_commit: 'aaaaaaaaaaaa1111', managed_commit: null })
  assert.ok(following.includes('当前工作区提交（HEAD） aaaaaaaaaaaa'))
  assert.ok(following.includes('未提交的修改不包含'))
  const pinned = render({ selected_commit: 'bbbbbbbbbbbb2222', managed_commit: 'bbbbbbbbbbbb2222', workspace_head: 'aaaaaaaaaaaa1111' })
  assert.ok(pinned.includes('已选择的管理版本 bbbbbbbbbbbb'))
  assert.ok(pinned.includes('主动选择的版本保持有效'))
  assert.ok(render({ selected_commit: null }).includes('仓库尚无提交'))
  assert.ok(render(null).includes('暂时无法读取'))
})
import { MaintenanceFeedback } from '../src/MaintenanceFeedback.tsx'

test('opening and switching check once; ordinary renders and reselecting do not', async () => {
  const seen = []
  const events = new ProjectCheckEvents(async (id, reason) => { seen.push([id, reason]); return {} })
  await events.select('A'); await events.select('A'); await events.select('B'); await events.select('A')
  assert.deepEqual(seen, [['A', 'open'], ['B', 'switch'], ['A', 'switch']])
})

test('manual checks remain available and overlapping requests are coalesced', async () => {
  let finish
  const seen = []
  const events = new ProjectCheckEvents((id, reason) => { seen.push([id, reason]); return new Promise(resolve => { finish = resolve }) })
  const first = events.select('A'), duplicate = events.check('A', 'manual')
  assert.equal(first, duplicate)
  assert.deepEqual(seen, [['A', 'open']])
  finish({}); await first
  const manual = events.check('A', 'manual'); finish({}); await manual
  assert.deepEqual(seen, [['A', 'open'], ['A', 'manual']])
})

test('failed automatic check does not retry on rerender; a manual event can retry', async () => {
  let calls = 0
  const events = new ProjectCheckEvents(async () => { calls++; if (calls === 1) throw new Error('offline'); return {} })
  await assert.rejects(events.select('A'))
  await events.select('A')
  assert.equal(calls, 1)
  await events.check('A', 'manual')
  assert.equal(calls, 2)
})

const render = value => renderToStaticMarkup(createElement(MaintenanceNotice, {
  connection: { port: 8000 }, value, error: '', visible: true, busy: false,
  onDismiss() {}, onCommand() {},
}))
test('progress reports code and knowledge versions without pretending old knowledge is current', () => {
  const html = render({ status: 'preparing', message: '正在增量更新。', knowledge_commit: 'a'.repeat(40), last_check: { commit: 'b'.repeat(40), dirty: true } })
  assert.ok(html.includes('正在增量更新项目理解'))
  assert.ok(html.includes('aaaaaaaaaaaa'))
  assert.ok(html.includes('bbbbbbbbbbbb'))
  assert.ok(html.includes('有未提交改动'))
})

test('enabling automatic maintenance discloses only event triggers and per-update budget', () => {
  const html = render({ status: 'permission_required', project_id: 'fixture' })
  assert.ok(html.includes('打开、切换项目、任务完成或手动检查'))
  assert.ok(html.includes('32 次模型调用'))
  assert.ok(html.includes('确认并开始更新'))
  assert.ok(html.includes('已提交的安全项目资料会发送至该模型'))
  assert.ok(!html.includes('每30秒'))
})

test('unknown updates only offer reconciliation and keep provider text escaped', () => {
  const html = render({ status: 'pending_execution', message: '<script>bad()</script>' })
  assert.ok(html.includes('核对并继续原更新'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('manual check lives in project status without adding a main navigation item', () => {
  const html = renderToStaticMarkup(createElement(ProjectStatus, {
    connection: { port: 8000 }, project: { id: 'fixture', root_path: '/tmp/demo', git: {} }, refresh: 0,
    busy: false, checking: false, onCheck() {}, onCommand() {},
  }))
  assert.ok(html.includes('检查更新'))
  assert.ok(html.includes('空闲时不定时扫描'))
})

test('project status shows checking progress and the result inside the open panel', () => {
  const props = { connection: { port: 8000 }, project: { id: 'fixture', root_path: '/tmp/demo', git: {} }, refresh: 0,
    busy: false, onCheck() {}, onCommand() {} }
  const checking = renderToStaticMarkup(createElement(ProjectStatus, { ...props, checking: true }))
  assert.ok(checking.includes('更新检查进度'))
  assert.ok(checking.includes('正在比较本地 Git'))
  assert.match(checking, /aria-busy="true" disabled=""/)
  const result = renderToStaticMarkup(createElement(ProjectStatus, { ...props, maintenance: {
    status: 'current', last_check: { commit: 'a'.repeat(40), dirty: true,
      summary: { base_commit: 'a'.repeat(40), target_commit: 'a'.repeat(40), file_count: 0 } },
  } }))
  assert.ok(result.includes('更新检查结果'))
  assert.ok(result.includes('未发现新提交'))
  assert.ok(result.includes('本地未提交改动，尚未纳入'))
})

test('change summaries show real Git subjects and files without claiming knowledge is updated', () => {
  const html = renderToStaticMarkup(createElement(MaintenanceFeedback, { value: {
    status: 'queued', message: '发现变更，后台正在更新。', last_check: { summary: {
      base_commit: 'a'.repeat(40), target_commit: 'b'.repeat(40), file_count: 3,
      counts: { added: 1, modified: 1, deleted: 1 }, commit_subjects: ['新增订单接口<script>bad()</script>'],
      files: [{ path: 'api/orders.py', change: 'added' }],
    } },
  } }))
  for (const text of ['正在排队更新项目理解', '3 个文件', '新增 1、修改 1、删除 1', '近期提交说明', 'api/orders.py', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb']) assert.ok(html.includes(text))
  assert.ok(!html.includes('项目理解已更新'))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('&lt;script&gt;'))
})

test('failed checks show a visible error instead of presenting an old success receipt', () => {
  const html = renderToStaticMarkup(createElement(MaintenanceFeedback, { value: { status: 'completed', message: '旧更新已成功' }, error: '本地服务不可达' }))
  assert.ok(html.includes('role="alert"'))
  assert.ok(html.includes('检查更新未完成'))
  assert.ok(html.includes('本地服务不可达'))
  assert.ok(!html.includes('旧更新已成功'))
})

const projectStatus = (maintenance, extra = {}) => renderToStaticMarkup(createElement(ProjectStatus, {
  connection: { port: 8000 }, project: { id: 'fixture', root_path: '/tmp/demo', git: {} }, refresh: 0,
  busy: false, onCheck() {}, onCommand() {}, maintenance: { project_id: 'fixture', ...maintenance }, ...extra,
}))

test('project status exposes the update action next to a permission-change receipt', () => {
  const value = { status: 'permission_required', message: '模型或自动维护范围已变化，请重新确认。' }
  const html = projectStatus(value)
  assert.ok(html.includes('模型或自动维护范围已变化'))
  assert.match(html, /<button class="solid-button">确认并开始更新<\/button>/)
  assert.ok(html.includes('立即开始增量更新'))
  assert.ok(html.includes('可在设置中关闭自动批准'))
  assert.match(projectStatus(value, { busy: true }), /disabled="">确认并开始更新/)
  assert.match(projectStatus(value, { checking: true }), /disabled="">确认并开始更新/)
  assert.ok(!projectStatus({ status: 'current' }).includes('确认并开始更新'))
  assert.ok(!projectStatus({ status: 'planning' }).includes('确认并开始更新'))
})

test('project status retains explicit approval and recovery actions instead of ending at detection', () => {
  for (const [value, label] of [
    [{ status: 'awaiting_approval', active_run_id: 'run' }, '允许本轮更新'],
    [{ status: 'interrupted' }, '核对并继续原更新'],
    [{ status: 'pending_execution' }, '核对并继续原更新'],
    [{ status: 'blocked', can_reprepare: true }, '重新准备本次更新'],
    [{ status: 'partial', active_run_id: 'run', document_generation: { status: 'partial' } }, '继续未完成文档页面'],
  ]) {
    const html = projectStatus(value)
    assert.equal(html.split(label).length - 1, 1, label)
    assert.ok(!html.includes('确认并开始更新'))
  }
  assert.match(projectStatus({ status: 'awaiting_approval', active_run_id: 'run' }), /disabled="">允许本轮更新/)
  const error = projectStatus({ status: 'permission_required' }, { commandError: '模型设置不可用<script>bad()</script>' })
  assert.ok(error.includes('role="alert"'))
  assert.ok(error.includes('模型设置不可用&lt;script&gt;'))
})

test('exhausted document work offers a disclosed new round rather than a no-op resume loop', () => {
  const html = projectStatus({status:'partial',active_run_id:'source',document_generation:{status:'partial',total:0,pages:[],
    recovery:{action:'new_round',source_run_id:'source',source_sha256:'a'.repeat(64),max_model_calls:32,max_seconds:1800,
      message:'本轮模型调用预算已用完（32 / 32 次）。'}}})
  assert.ok(html.includes('基于已有事实开始新一轮文档生成'))
  assert.ok(html.includes('新一轮最多 32 次模型调用、30 分钟'))
  assert.ok(html.includes('原任务与调用记录保留'))
  assert.ok(!html.includes('继续未完成文档页面'))
  const unknown = projectStatus({status:'partial',active_run_id:'source',document_generation:{status:'unknown',pages:[],
    recovery:{action:'inspect',message:'原请求结果未知，不能新建轮次重发。'}}})
  assert.ok(!unknown.includes('基于已有事实开始新一轮文档生成'))
  assert.ok(!unknown.includes('继续未完成文档页面'))
})
