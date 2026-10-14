import { useState } from 'react'
import { shortCommit, type Row } from './api.ts'

const rows = (value: unknown): Row[] => Array.isArray(value) ? value as Row[] : []
const object = (value: unknown): Row => value && typeof value === 'object' ? value as Row : {}

function importLabel(item: Row) {
  const level = typeof item.level === 'number' ? item.level : 0
  const names = rows(item.names).map(name => String(name.name) + (name.alias ? ` as ${name.alias}` : '')).join(', ')
  return item.kind === 'from' ? `from ${'.'.repeat(Math.min(Math.max(level, 0), 32))}${level > 32 ? '…' : ''}${item.module || ''} import ${names}` : `import ${names}`
}

export function ProjectStructure({ value }: { value: unknown }) {
  const record = object(value)
  const [filter, setFilter] = useState('')
  const modules = rows(record.modules)
  const needle = filter.trim().toLocaleLowerCase()
  const filtered = modules.map(module => ({ ...module, path: module.path, imports: rows(module.imports), symbols: rows(module.symbols).filter(symbol => !needle || String(module.path).toLocaleLowerCase().includes(needle) || String(symbol.lexical_name).toLocaleLowerCase().includes(needle)) }))
    .filter(module => !needle || String(module.path).toLocaleLowerCase().includes(needle) || module.symbols.length > 0)
  const skipped = Object.values(object(record.skipped_counts)).reduce<number>((total, count) => total + (typeof count === 'number' ? count : 0), 0)
  return <section className="panel"><div className="panel-heading"><h2>Python 声明地图</h2><span className="muted">{shortCommit(record.commit)}</span></div>
    <div className="review-body"><p className="review-note">只记录固定版本的模块、类、函数与 import 声明。动态调用、真实依赖和业务行为尚未解析；分析不执行项目源码。</p>
      <div className="cards"><div><span>已解析模块</span><strong>{modules.length}</strong></div><div><span>符号声明</span><strong>{modules.reduce((total, module) => total + rows(module.symbols).length, 0)}</strong></div><div><span>跳过项</span><strong>{skipped}</strong></div></div>
      {rows(record.warnings).length > 0 && <div className="error" role="status">部分来源未覆盖，请检查下方范围说明与跳过计数。</div>}
      <label>查找模块或符号<input aria-label="查找模块或符号" value={filter} onChange={event => setFilter(event.target.value)} placeholder="例如 Service.method 或 app.py" /></label>
      {!filtered.length && <p className="review-note">当前范围没有匹配的 Python 模块或符号；这不说明项目没有其他语言的实现。</p>}
      {filtered.map(module => <details className="map-module" key={String(module.path)}><summary><strong>{String(module.path)}</strong><span className="muted">{module.symbols.length} 个匹配符号</span></summary>
        <div className="map-symbols">{module.symbols.map((symbol, i) => { const evidence = object(symbol.evidence); return <div key={i}><code>{String(symbol.lexical_name)}</code><span className="muted">{String(symbol.kind)} · 行 {String(evidence.start_line)}–{String(evidence.end_line)}</span></div> })}</div>
        <div className="map-imports"><h3>import 声明</h3>{rows(module.imports).length ? rows(module.imports).map((item, i) => <pre key={i}>{importLabel(item)}<span className="muted"> · 行 {String(object(item.evidence).start_line)} · {String(item.scope || '模块级')}</span></pre>) : <p className="review-note">没有静态 import 声明。</p>}</div>
        <details className="json-details"><summary>查看此模块的完整来源身份</summary><pre>{JSON.stringify(module, null, 2)}</pre></details></details>)}
      <details className="json-details"><summary>查看范围、解析器与跳过原因</summary><pre>{JSON.stringify({ commit: record.commit, parser_python: record.parser_python, files_considered: record.files_considered, bytes_read: record.bytes_read, skipped_counts: record.skipped_counts, warnings: record.warnings, limitations: record.limitations }, null, 2)}</pre></details>
    </div></section>
}
