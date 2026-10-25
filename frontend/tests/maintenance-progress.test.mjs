import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MaintenanceFeedback } from '../src/MaintenanceFeedback.tsx'
import { waitingDuration } from '../src/MaintenanceProgress.tsx'

const render = (value, extra = {}) => renderToStaticMarkup(createElement(MaintenanceFeedback, { value, ...extra }))

test('each active update stage shows motion, stage and real counters without invented percentages', () => {
  for (const status of ['queued', 'preparing', 'planning', 'investigating', 'reviewing', 'publishing']) {
    const html = render({ status, research: { call_count: 4, jobs: [{ status: 'completed' }, { status: 'running' }] } })
    assert.ok(html.includes('aria-busy="true"'), status)
    assert.ok(html.includes('maintenance-spinner'), status)
    assert.ok(html.includes('maintenance-motion'), status)
    assert.ok(html.includes('aria-current="step"'), status)
    assert.ok(html.includes('已等待'), status)
    assert.ok(html.includes('完成、暂停或失败后会自动显示结果'), status)
    assert.ok(html.includes('模型调用 4 次'))
    assert.ok(html.includes('调查已完成 1 / 2 项'))
    assert.ok(!html.includes('%'))
  }
})

test('completion and every paused or failed state stop motion and do not imply success', () => {
  for (const status of ['completed', 'partial', 'failed', 'blocked', 'publication_failed', 'pending_execution', 'interrupted', 'permission_required', 'awaiting_approval', 'awaiting_context_approval']) {
    const html = render({ status })
    assert.ok(html.includes('aria-busy="false"'), status)
    assert.ok(!html.includes('maintenance-spinner'), status)
    assert.ok(!html.includes('maintenance-motion'), status)
    assert.ok(!html.includes('已等待'), status)
    assert.ok(html.includes(status === 'completed' ? 'maintenance-completed' : 'maintenance-paused'), status)
    if (status !== 'completed') assert.ok(!html.includes('项目理解已更新'), status)
  }
})

test('waiting time uses the original research start and tolerates old records without timestamps', () => {
  const originalNow = Date.now
  Date.now = () => Date.parse('2026-10-23T12:00:00Z')
  try {
    const html = render({ status: 'investigating', progress_received_at: '2026-10-23T11:59:58Z', research: { started_at: '2026-10-23T11:58:55Z' } })
    assert.ok(html.includes('已等待 1 分 05 秒'))
    assert.ok(html.includes('最近读取 2 秒前'))
    assert.ok(render({ status: 'planning', research: { started_at: 'bad' } }).includes('已等待 0 秒'))
    assert.ok(!render({ status: 'planning' }).includes('NaN'))
    assert.equal(waitingDuration(-3), '0 秒')
    assert.equal(waitingDuration(3600), '60 分 00 秒')
  } finally { Date.now = originalNow }
})

test('lost progress stops animation and explains status recovery without retrying the update', () => {
  const html = render({ status: 'investigating' }, { error: '本地连接暂时中断' })
  assert.ok(html.includes('更新进度暂时无法读取'))
  assert.ok(html.includes('后台可能仍在执行'))
  assert.ok(html.includes('不会重新发起更新'))
  assert.ok(!html.includes('maintenance-spinner'))
  assert.ok(!html.includes('maintenance-motion'))
  assert.ok(!html.includes('项目理解已更新'))
})

test('current page generation shows the document stage while research has already ended', () => {
  const value = { status: 'preparing', active_run_id: 'current', research: { status: 'completed' },
    document_generation: { run_id: 'current', status: 'writing', completed: 1, total: 3 } }
  const html = render(value)
  assert.ok(html.includes('正在更新项目文档'))
  assert.match(html, /aria-current="step"[^>]*><span[^>]*>5<\/span>更新文档/)
  assert.ok(html.includes('文档已发布 1 / 3 页'))
  assert.ok(render({ ...value, document_generation: { ...value.document_generation, run_id: 'older' } }).includes('正在整理变更资料'))
})
