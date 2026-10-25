import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Workspace } from '../src/App.tsx'
import { Conversation, Turn } from '../src/Conversation.tsx'
import { TextContent } from '../src/TextContent.tsx'

const project = { id: 'project', mode: 'import', root_path: '/tmp/example', git: { branch: 'main', dirty: false, head_commit: 'a'.repeat(40) } }
const render = (component, props) => renderToStaticMarkup(createElement(component, props))
const callbacks = { onSend: async () => true, onCommand() {}, onOlder() {}, onDraft() {} }

test('workspace has a single conversation and only settings and project status as supporting entries', () => {
  const html = render(Workspace, { connection: { port: 8000 }, initialProjects: [project], onDisconnect() {} })
  assert.ok(html.includes('aria-label="设置"'))
  assert.ok(html.includes('项目状态'))
  assert.ok(html.includes('aria-label="和 Zest 对话"'))
  assert.equal((html.match(/<textarea/g) || []).length, 1)
  for (const label of ['app-sidebar', '项目概览', '源码地图', '独立评分', '补查结案', '修复 / 开发建议', '任务类型']) assert.ok(!html.includes(label), label)
})

test('a new user is led to project selection without a dashboard or account form', () => {
  const html = render(Workspace, { connection: { port: 8000 }, initialProjects: [], onDisconnect() {} })
  assert.ok(html.includes('选择项目'))
  assert.ok(!html.includes('<textarea'))
  assert.ok(!html.includes('type="password"'))
})

test('conversation renders both turns and preserves a draft while a pending round is blocked', () => {
  const html = render(Conversation, { connection: { port: 8000 }, busy: false, draft: '尚未发送的补充', ...callbacks,
    value: { id: 'conversation', has_earlier: true, turns: [{ id: 'turn', question: '只支持未付款订单', status: 'unknown' }] } })
  assert.ok(html.includes('只支持未付款订单'))
  assert.ok(html.includes('尚未发送的补充'))
  assert.ok(html.includes('查看更早的消息'))
  assert.match(html, /disabled=""[^>]*aria-label="发送消息"/)
})

test('older unsent messages offer one continuation and keep exact request details optional', () => {
  const html = render(Turn, { connection: { port: 8000 }, busy: false, onCommand() {}, turn: { id: 'message', conversation_id:'conversation', question: '接着说', task: {
    task_id: 'task', status: 'awaiting_approval', payload_sha256: 'b'.repeat(64), target: { model: 'fixture' },
    context: { commit: 'a'.repeat(40), sources: [], conversation_context: { history: [{ user: '历史需求 <script>', assistant: '需要确认业务规则' }] } },
  } } })
  assert.ok(html.includes('继续回答'))
  assert.ok(html.includes('后续提问会在发送后直接回答'))
  assert.ok(!html.includes('发送资料并回答'))
  assert.ok(html.includes('完整请求与对话上下文'))
  assert.ok(html.includes('历史需求 &lt;script&gt;'))
  assert.ok(html.includes('b'.repeat(64)))
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('批准本次操作'))
})

test('automatic answering shows progress without an approval card', () => {
  const html = render(Turn, { connection: { port:8000 },busy:true,answering:true,onCommand() {},
    turn:{id:'message',question:'如何运行？',task:{status:'awaiting_approval',context:{sources:[]}}} })
  assert.ok(html.includes('正在结合项目资料回答'))
  assert.ok(html.includes('role="status"'))
  assert.ok(!html.includes('answer-permission'))
  assert.ok(!html.includes('继续回答'))
  assert.ok(!html.includes('发送资料并回答'))
})

test('assistant messages show actual text, uncertainty and evidence with HTML escaped', () => {
  const html = render(Turn, { connection: { port: 8000 }, busy: false, onCommand() {}, turn: { question: '如何取消？', task: {
    task_id: 'task', status: 'completed', context: { sources: [] }, result: { answer: '**未付款**订单可取消。<script>bad()</script>', unknowns: ['退款规则未确认'] },
  } } })
  assert.ok(html.includes('<strong>未付款</strong>'))
  assert.ok(html.includes('退款规则未确认'))
  assert.ok(html.includes('来源与记录'))
  assert.ok(!html.includes('<script>'))
})

test('documentation formats code as inert text rather than executable HTML', () => {
  const html = render(TextContent, { text: '# API\n\n调用 `cancel()`。\n\n```html\n<script>alert(1)</script>\n```' })
  assert.ok(html.includes('<h3>API</h3>'))
  assert.ok(html.includes('<code>cancel()</code>'))
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'))
})

test('document navigation uses explicit local callbacks without executing link targets', () => {
  const html = render(TextContent, { text: '- [接口](api.md)\n- [不可信链接](javascript:alert)\n', onLink() {} })
  assert.ok(html.includes('<ul>'))
  assert.equal((html.match(/class="document-link"/g) || []).length, 2)
  assert.ok(!html.includes('href='))
  assert.ok(!html.includes('onclick='))
})
