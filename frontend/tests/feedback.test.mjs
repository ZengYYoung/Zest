import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ProjectImport } from '../src/ProjectImport.tsx'
import { ContextApprovals, ScopeReview } from '../src/RequestReview.tsx'

const render = (component, props) => renderToStaticMarkup(createElement(component, props))
test('import waiting explains actual stage, elapsed time and disabled submission', () => {
  const html = render(ProjectImport, {path:'/tmp/project', busy:true, choosing:false, error:'', onPath(){}, onChoose(){}, onImport(){},
    operation:{action:'project.import',status:'running',created_at:new Date(Date.now()-30000).toISOString(),progress:{stage:'index',message:'正在建立源码检索索引'}}})
  for (const text of ['aria-busy="true"','正在接入项目','已等待','正在建立源码检索索引','检查 Git 项目','不会重复提交']) assert.ok(html.includes(text), text)
  assert.ok(!html.includes('%'))
})
test('pending approvals explain the task and show a bounded current-batch action', () => {
  const request = {actor:'T1',sequence:0,status:'awaiting_approval',chars:34000,default_chars:32000,
    description:{title:'接口调查',goal:'确认参数和返回值',purpose:'阅读与分析项目资料',model:'fixture-model',source_paths:['api/orders.py'],source_count:1},
    authorization:{request:{body_text:'<script>inert</script>'}}}
  const html = render(ContextApprovals,{requests:[request,{...request,actor:'T2'}],busy:false,onDecision(){},onBatch(){}})
  for (const text of ['2 项请求等待确认','接口调查','确认参数和返回值','api/orders.py','fixture-model','允许上面这 2 项请求并继续','后续新请求仍会单独询问']) assert.ok(html.includes(text),text)
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('&lt;script&gt;'))
  const remaining = render(ContextApprovals,{requests:[{...request,status:'approved'},{...request,actor:'T2'}],busy:false,onDecision(){},onBatch(){}})
  assert.ok(remaining.includes('1 项请求等待确认'))
  assert.ok(!remaining.includes('允许上面这 2 项'))
})
test('fixed scope has a readable inventory and retains the full authorization package', () => {
  const html = render(ScopeReview,{envelope:{commit:'a'.repeat(40),target:{model:'fixture-model'},policy:{max_model_calls:32},files:[{id:'S1',evidence:{path:'api/orders.py'},body:'source-text'}]}})
  for (const text of ['这次要做什么','api/orders.py','32 次','完整冻结资料包','source-text']) assert.ok(html.includes(text),text)
})
