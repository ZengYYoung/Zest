import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { App } from '../src/App.tsx'
import { WorkspaceActions } from '../src/WorkspaceActions.tsx'

const render = (tab, detail = null, projectId = 'fixture') => renderToStaticMarkup(createElement(WorkspaceActions, {
  connection: { port: 8000 }, projectId, tab, detail, onResult() {}, onRefresh() {},
}))
test('connection screen requires no token or account', () => {
  const html = renderToStaticMarkup(createElement(App))
  assert.ok(html.includes('无需登录'))
  assert.ok(!html.includes('type="password"'))
  assert.ok(!html.includes('访问令牌'))
})
test('empty console offers project import and creation', () => {
  const html = render('overview', null, '')
  assert.ok(html.includes('导入已有 Git 项目'))
  assert.ok(html.includes('创建新项目'))
})
test('question creation and candidate testing have concrete forms', () => {
  const html = render('tasks')
  for (const label of ['项目问答', '修复 / 开发建议', '添加上下文与文件范围', '准备请求并审阅']) assert.ok(html.includes(label))
  const candidate = render('repairs', { id: 'fixture', status: 'candidate_ready' })
  assert.ok(candidate.includes('已安装的 Docker 镜像'))
  assert.ok(candidate.includes('准备独立验证计划'))
})
test('approval cannot be clicked before loading the full preview and consent', () => {
  const html = render('tasks', { task_id: 'fixture', status: 'awaiting_approval', payload_sha256: 'a'.repeat(64) })
  assert.ok(html.includes('审阅完整请求 / 执行计划'))
  assert.ok(!html.includes('批准本次操作'))
  assert.ok(html.includes('拒绝本次操作'))
})
test('unknown cognition offers reconciliation and keeps source text escaped', () => {
  const html = render('cognition', { id: 'fixture', status: 'pending_execution', findings: [
    { id: 'T1:F1', status: 'needs_investigation', finding: { statement: '<script>bad()</script>' } },
  ] })
  assert.ok(html.includes('核对并恢复已知响应'))
  assert.ok(html.includes('准备定向补查'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('model connection has an explicit consent gate and fixed synthetic scope', async () => {
  const { ModelConnection } = await import('../src/ModelConnection.tsx')
  const html = renderToStaticMarkup(createElement(ModelConnection, {
    connection: { port: 8000 }, disabled: false, refresh: 0, onCommand() {},
    check: { id: 'fixture', status: 'awaiting_approval', approval_sha256: 'a'.repeat(64), envelope: { payload: { sources: [{ id: 'E1', content: 'synthetic example' }] } } },
  }))
  assert.ok(html.includes('不包含项目源码'))
  assert.ok(html.includes('最多调用 1 次'))
  assert.ok(html.includes('type="checkbox"'))
  assert.match(html, /disabled=""[^>]*>执行真实连接测试/)
})

test('real model answers are escaped and usage is labelled as received', async () => {
  const { ModelConnection } = await import('../src/ModelConnection.tsx')
  const html = renderToStaticMarkup(createElement(ModelConnection, {
    connection: { port: 8000 }, disabled: false, refresh: 0, onCommand() {},
    check: { id: 'fixture', status: 'passed', envelope: {}, result: { answer: { answer: '<script>bad()</script>' }, elapsed_seconds: 1.2, usage: { total_tokens: 42 }, receipt: {} } },
  }))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('42'))
  assert.ok(html.includes('实际模型响应与用量收据'))
})
