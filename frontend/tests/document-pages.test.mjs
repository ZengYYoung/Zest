import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { TextContent } from '../src/TextContent.tsx'
import { DocumentProgress } from '../src/DocumentProgress.tsx'
import { InitializationCard } from '../src/Initialization.tsx'

test('engineering pages render headings, tables, steps and hide generated metadata', () => {
  const text = '---\ntitle: "模块说明"\ndescription: "阅读价值"\nobserved_commit: "abc"\n---\n\n# 模块说明\n## 协作方式\n模块先处理输入，然后返回结果。\n\n| 模块 | 职责 |\n| --- | --- |\n| `add` | 返回 `a + b` |\n\n1. 准备参数\n2. 调用函数\n\n### 边界\n尚未验证部署。'
  const html = renderToStaticMarkup(createElement(TextContent, { text }))
  assert.ok(html.includes('<h3>模块说明</h3>'))
  assert.ok(html.includes('<h4>协作方式</h4>'))
  assert.ok(html.includes('<h5>边界</h5>'))
  assert.ok(html.includes('<table>'))
  assert.ok(html.includes('<th scope="col">模块</th>'))
  assert.ok(html.includes('<td><code>add</code></td>'))
  assert.ok(html.includes('<ol><li>准备参数</li><li>调用函数</li></ol>'))
  assert.ok(!html.includes('observed_commit:'))
})

test('table content and unsafe links remain inert React text', () => {
  const html = renderToStaticMarkup(createElement(TextContent, {
    text: '| 名称 | 内容 |\n| --- | --- |\n| <img src=x onerror=alert(1)> | [链接](javascript:alert) |', onLink() {} }))
  assert.ok(html.includes('&lt;img'))
  assert.ok(!html.includes('<img'))
  assert.ok(!html.includes('href='))
})

test('page progress distinguishes completed, skipped and unknown from knowledge gaps', () => {
  const html = renderToStaticMarkup(createElement(DocumentProgress, {value: {total:3, completed:1, unknown:1, pages:[
    {slug:'flow',title:'核心流程',status:'completed'}, {slug:'api',title:'接口用法',status:'skipped'},
    {slug:'development',title:'开发说明',status:'unknown'}]}}))
  assert.ok(html.includes('已发布 1 / 3'))
  assert.ok(html.includes('核心流程 · 已发布'))
  assert.ok(html.includes('接口用法 · 保留原文'))
  assert.ok(html.includes('开发说明 · 请求结果待核对'))
  assert.ok(html.includes('不会自动重发'))
})

test('partial document generation does not claim initialization complete', () => {
  const html = renderToStaticMarkup(createElement(InitializationCard, {value: {stage:'published', status:'partial',
    summary:{readiness:'ready',documents:3,reviewed_findings:6}, document_generation:{status:'partial',total:6,completed:2,skipped:1}},
    preview:null,busy:false,onCommand(){},onSettings(){},onDocuments(){},onDismiss(){} }))
  assert.ok(!html.includes('初始化完成，可以开始使用'))
  assert.ok(html.includes('继续未完成页面'))
})

test('new page generation explains unsent writing while old runs retain local publication', () => {
  const html = renderToStaticMarkup(createElement(InitializationCard, {value:{status:'publication_pending',
    research:{status:'completed',document_generation_enabled:true}},preview:null,busy:false,
    onCommand(){},onSettings(){},onDocuments(){},onDismiss(){} }))
  assert.ok(html.includes('尚未发送的页面写作'))
  assert.ok(html.includes('继续生成项目文档'))
  assert.ok(!html.includes('不会再次调用模型'))
})

test('incremental progress distinguishes inherited evidence checks from semantic verification', () => {
  const html = renderToStaticMarkup(createElement(DocumentProgress, {value:{total:2,completed:1,retained:1,
    knowledge:{verified:3,inherited:3,needs_review:1,retracted:2},pages:[{slug:'beta',title:'Beta',status:'retained',
      last_verified_commit:'1234567890abcdef',evidence_checked_commit:'abcdef1234567890'}]}}))
  assert.ok(html.includes('沿用 1'))
  assert.ok(html.includes('沿用已有正文'))
  assert.ok(html.includes('核验 1234567'))
  assert.ok(html.includes('证据检查 abcdef1'))
  assert.ok(html.includes('事实记录：核对 3 · 沿用 3 · 待复核 1 · 撤回 2'))
})

test('zero pages expose failed planning and its budget reason instead of a misleading zero total', () => {
  const html = renderToStaticMarkup(createElement(DocumentProgress, {value: {status:'partial',total:0,pages:[],
    recovery:{action:'new_round',message:'本轮模型调用预算已用完（32 / 32 次），继续原任务不能增加预算。'}}}))
  assert.ok(html.includes('尚未生成页面计划'))
  assert.ok(html.includes('32 / 32 次'))
  assert.ok(html.includes('<details class="document-progress" open="">'))
  assert.ok(!html.includes('已发布 0 / 0'))
})

test('document failures show the exact local validation reason as escaped text', () => {
  const html = renderToStaticMarkup(createElement(DocumentProgress, {value:{status:'partial',total:0,pages:[],
    error:{code:'invalid_document_output',message:'页面数量超出计划容量。<script>inert</script>'}}}))
  assert.ok(html.includes('页面数量超出计划容量。&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('a protected manual directory exposes the newly generated reading entry', () => {
  const html=renderToStaticMarkup(createElement(DocumentProgress,{value:{status:'completed',total:5,completed:5,pages:[],
    index_candidate:'.zest/docs/initialization/candidates/run/index.md'},onOpen(){}}))
  assert.ok(html.includes('查看本轮新文档目录'))
  assert.ok(html.includes('原目录有人工修改，已保留'))
})
