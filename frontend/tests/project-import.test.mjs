import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ProjectImport } from '../src/ProjectImport.tsx'

const props = { path: '', busy: false, choosing: false, error: '', onPath() {}, onChoose() {}, onImport() {} }
test('native folder selection and explicit import are separate; manual paths remain available', () => {
  const html = renderToStaticMarkup(createElement(ProjectImport, props))
  assert.match(html, /type="button"[^>]*>.*在 Finder 中选择文件夹/)
  assert.ok(html.includes('name="path"'))
  assert.match(html, /disabled="">接入并初始化/)
  assert.ok(!html.includes('type="file"'))
})
test('chosen paths are editable and escaped; waiting for the native dialog blocks import', () => {
  const html = renderToStaticMarkup(createElement(ProjectImport, {...props, path: '/tmp/项目 <team>', busy: true, choosing: true}))
  assert.ok(html.includes('/tmp/项目 &lt;team&gt;'))
  assert.ok(html.includes('请在系统窗口中选择…'))
  assert.match(html, /disabled="">接入并初始化/)
})
