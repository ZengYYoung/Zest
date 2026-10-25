import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { InitializationCard } from '../src/Initialization.tsx'

const render = (value, preview = null, contexts = []) => renderToStaticMarkup(createElement(InitializationCard, {
  value: { project_id: 'fixture', ...value }, preview, contexts, busy: false,
  onCommand() {}, onSettings() {}, onDocuments() {}, onDismiss() {},
}))
const prepared = { status: 'awaiting_approval', commit: 'a'.repeat(40), research: {
  file_count: 8, approval_sha256: 'b'.repeat(64), target: { model: 'fixture-model' }, policy: { max_model_calls: 32, max_seconds: 1800 },
} }

test('initialization is one explicit action and scope details are optional', () => {
  const waiting = render(prepared)
  assert.ok(waiting.includes('8 个安全文本文件'))
  assert.ok(waiting.includes('32 次模型调用'))
  assert.ok(waiting.includes('未提交改动不包含'))
  assert.ok(!/disabled=""[^>]*>开始初始化/.test(waiting))
  assert.ok(waiting.includes('点击后自动完成项目理解'))
  const loaded = render(prepared, { envelope: { files: [{ body: '<script>source()</script>' }] } })
  assert.ok(!/disabled=""[^>]*>开始初始化/.test(loaded))
  assert.ok(loaded.includes('&lt;script&gt;'))
  assert.ok(!loaded.includes('<script>'))
})

test('initialization reports missing setup and a version change without claiming understanding', () => {
  const noKey = render({ status: 'needs_model' })
  assert.ok(noKey.includes('配置 DeepSeek'))
  assert.ok(!noKey.includes('已建立初步项目理解'))
  assert.ok(render({ status: 'needs_commit' }).includes('项目还没有 Git 提交'))
  const changed = render({ ...prepared, stale: true }, { envelope: {} })
  assert.match(changed, /disabled=""[^>]*>开始初始化/)
  assert.ok(changed.includes('项目版本已变化'))
})

test('running initialization stays compact and reveals actual stage and call count', () => {
  const html = render({ status: 'investigating', worker_active: true, research: { call_count: 3 } })
  assert.ok(html.includes('正在阅读源码与文档'))
  assert.ok(html.includes('已调用 3 次'))
  assert.ok(!html.includes('项目概览'))
  assert.ok(!html.includes('开始初始化'))
  assert.ok(html.includes('初始化步骤'))
  assert.match(html, /aria-current="step"[^>]*>调查核心流程/)
  assert.ok(html.includes('最多进行一轮定向补读'))
})

test('usable initialization and remaining knowledge gaps are separate states', () => {
  const html = render({stage:'published',status:'partial',summary:{documents:7,reviewed_findings:8,
    pending_items:2,readiness:'ready',knowledge_status:'has_gaps'}})
  assert.ok(html.includes('初始化完成，可以开始使用'))
  assert.ok(html.includes('2 项知识缺口'))
  assert.ok(html.includes('按需补充项目知识'))
  assert.ok(!html.includes('initialization-spinner'))
  const empty = render({stage:'published',status:'partial',summary:{documents:7,readiness:'unavailable'}})
  assert.ok(!empty.includes('初始化完成，可以开始使用'))
  assert.ok(empty.includes('尚无可复用的核对结论'))
  assert.ok(!empty.includes('现在可以直接'))
})

test('backlog counters use one total and do not keep an endless supplement action', () => {
  const html = render({stage:'published',status:'partial',summary:{documents:7,reviewed_findings:20,
    readiness:'ready',pending_items:5,pending_total:5,unresolved_gaps:2,repairable_items:0,
    backlog:{total:5,attempted:2,external:1,deferred:2},pending_categories:[]}})
  assert.ok(html.includes('查看待补充知识记录 · 5 项'))
  assert.ok(!html.includes('7 项知识缺口'))
  assert.ok(!html.includes('按需补充项目知识'))
  assert.ok(!html.includes('定向处理下一组'))
  assert.ok(html.includes('无需继续点击补充'))
  const actionable = render({stage:'published',status:'partial',summary:{pending_total:3,repairable_items:2,
    backlog:{total:3,actionable:2},readiness:'ready'}})
  assert.ok(actionable.includes('定向处理下一组待办（最多 4 项）'))
})

test('partial completion preserves gaps, document conflicts and source version', () => {
  const html = render({ id: 'fixture', stage: 'published', status: 'partial', summary: { documents: 6, reviewed_findings: 2, pending_items: 4 }, conflicts: ['api.md'] })
  assert.ok(html.includes('已建立部分项目理解'))
  assert.ok(html.includes('待确认内容'))
  assert.ok(html.includes('已保留原文'))
  assert.ok(html.includes('查看项目文档'))
  assert.ok(html.includes('收起初始化摘要'))
  assert.ok(!html.includes('重新初始化'))
  const saved = render({stage:'published',status:'partial',summary:{documents:7,reviewed_findings:2},research:{status:'failed',error:{code:'invalid_cognition_reference',message:'check.evidence_ids 包含不属于允许集合的引用。'}}})
  assert.ok(saved.includes('调查未完整完成'))
  assert.ok(!saved.includes('initialization-spinner'))
  assert.ok(!saved.includes('check.evidence_ids'))
  assert.ok(!saved.includes('现在可以直接'))
})

test('unknown responses offer reconciliation and publication failure only resumes local writing', () => {
  const unknown = render({ status: 'pending_execution' })
  assert.ok(unknown.includes('不会自动重发'))
  assert.ok(unknown.includes('核对并继续原初始化'))
  const publication = render({ status: 'publication_failed' })
  assert.ok(publication.includes('整理已有调查结果'))
  assert.ok(!publication.includes('开始初始化'))
  const stopped = render({status:'publication_pending',research:{error:{code:'invalid_cognition_reference',message:'internal reference detail'}}})
  assert.ok(stopped.includes('调查已停止'))
  assert.ok(stopped.includes('不会再次调用模型'))
  assert.ok(stopped.includes('未通过引用或格式核对'))
  assert.ok(!stopped.includes('initialization-spinner'))
  assert.ok(!stopped.includes('internal reference detail'))
})

test('context expansion includes the actual unsent body and explicit decisions', () => {
  const html = render({ status: 'awaiting_context_approval' }, null, [{ actor: 'T1', sequence: 1, status: 'awaiting_approval', chars: 34000, authorization: { request: { body_text: 'fixture-complete-body' } } }])
  assert.ok(html.includes('fixture-complete-body'))
  assert.ok(html.includes('允许这项请求'))
  assert.ok(html.includes('跳过这项请求'))
  assert.ok(html.includes('1 项请求等待确认'))
})

test('partial understanding offers a separately reviewed supplement and shows gap categories', () => {
  const partial = render({stage:'published', status:'partial', summary:{pending_items:3, pending_categories:[
    {title:'尚未完成自动核对', count:2}, {title:'需继续读取的项目资料', count:1},
  ]}})
  assert.ok(partial.includes('补全未完成的项目理解'))
  assert.ok(partial.includes('不需要逐条人工确认'))
  assert.ok(partial.includes('尚未完成自动核对 2 项'))
  const waiting = render({...prepared, repair_source_run_id:'old'}, {envelope:{repair_context:{leads:[{}],questions:[{}],failed_tasks:[{}]}}})
  assert.ok(waiting.includes('开始补全'))
  assert.ok(waiting.includes('尚未发送新的模型请求'))
  assert.ok(waiting.includes('1 条已有线索'))
  assert.ok(render({status:'repair_preparing'}).includes('继续准备补全资料'))
})
