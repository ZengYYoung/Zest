import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { SectionLayout } from '../src/SectionLayout.tsx'
import { ProjectStatus } from '../src/ProjectStatus.tsx'

const sections = [
  { id: 'overview', title: '概览', content: createElement('p', null, '概览内容') },
  { id: 'index', title: '索引', content: createElement('input', { defaultValue: '草稿' }) },
]

test('section navigation selects one visible panel and keeps other panels mounted', () => {
  const html = renderToStaticMarkup(createElement(SectionLayout, { label: '测试栏目', sections, initialSection: 'index' }))
  assert.match(html, /aria-label="测试栏目"/)
  assert.match(html, /aria-current="page"[^>]*>索引/)
  assert.match(html, /hidden="" aria-label="概览"/)
  assert.match(html, /aria-label="索引"/)
  assert.ok(html.includes('value="草稿"'))
})

test('project status separates overview, document progress, index and documents', () => {
  const html = renderToStaticMarkup(createElement(ProjectStatus, {
    connection: { port: 8000 }, project: { id: 'p', root_path: '/tmp/project', git: {} },
    refresh: 0, busy: false, onCommand() {}, maintenance: { document_generation: { total: 1, pages: [{ slug: 'x', title: '页面', status: 'blocked' }] } },
  }))
  for (const title of ['项目概览', '文档更新进度', '语义索引增量更新', '项目文档']) assert.ok(html.includes(title), title)
  assert.ok(html.includes('旧事实待复核，保留原文'))
  assert.ok(!html.includes('<summary>语义索引增量更新</summary>'))
})
