import assert from 'node:assert/strict'
import test from 'node:test'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {UnderstandingSettingsCard} from '../src/UnderstandingSettings.tsx'

const render = value => renderToStaticMarkup(createElement(UnderstandingSettingsCard, {value, busy:false, onChange(){}}))
test('project understanding switch discloses scope and is disabled until loaded', () => {
  assert.match(render(null), /role="switch"[^>]*disabled/)
  const html = render({enabled:false, active:false})
  assert.ok(html.includes('只对当前项目生效'))
  assert.ok(html.includes('初始化、补全、增量更新'))
  assert.ok(html.includes('32'))
  assert.ok(html.includes('30'))
  assert.ok(html.includes('检索、补读与请求分批自动处理'))
  assert.ok(html.includes('结果未知的请求不会自动重发'))
  assert.ok(!html.includes('checked=""'))
})
test('enabled permission and changed model remain distinct and repairable', () => {
  assert.ok(render({enabled:true,active:true}).includes('checked=""'))
  const changed = render({enabled:true,active:false,message:'模型已变化，请重新开启。'})
  assert.ok(changed.includes('按当前模型重新开启'))
  assert.ok(changed.includes('模型已变化'))
  assert.ok(!render({enabled:true,active:true}).includes('按当前模型重新开启'))
  assert.ok(render({enabled:true,active:true,scope_mode:'legacy_package'}).includes('启用项目范围自动理解'))
  assert.ok(!render({enabled:true,active:true,scope_mode:'project'}).includes('启用项目范围自动理解'))
})
