import { useState } from 'react'
import { shortCommit, type Row } from './api.ts'

const obj = (v: unknown): Row => v && typeof v === 'object' && !Array.isArray(v) ? v as Row : {}
const list = (v: unknown): Row[] => Array.isArray(v) ? v.map(obj) : []

export function ScopeReview({ envelope }: { envelope: Row }) {
  const files = list(envelope.files), target = obj(envelope.target), policy = obj(envelope.policy)
  const repair = obj(envelope.repair_context)
  return <div className="request-readable"><p><strong>这次要做什么</strong>：{envelope.repair_context ? '补读已有调查的缺口，核对未完成结论，保留原始记录。' : '了解项目目标、模块、接口、开发方式和已知约束。'}</p>
    {Boolean(envelope.repair_context) && <p>复用 {list(repair.leads).length} 条已有线索、{list(repair.questions).length} 项调查问题及 {list(repair.failed_tasks).length} 个失败任务的目标。旧结论需重新读取原文核对，尚未解决的历史问题会保留。</p>}
    <p>发送给 {String(target.model || '已配置模型')} · 版本 {shortCommit(envelope.commit)} · {files.length} 个文件</p>
    <p>调用上限 {String(policy.max_model_calls || '见上方')} 次；只阅读和整理，完成后生成项目文档。</p>
    <details><summary>项目资料清单 · {files.length} 个文件</summary><ul className="request-paths">{files.map((file, i) => <li key={String(file.id || i)}><code>{String(obj(file.evidence).path || file.id || '项目资料')}</code></li>)}</ul></details>
    {Boolean(envelope.source_scope) && <p>项目范围内按需读取同一版本源码，目录分页、继续补读和请求分批自动处理。</p>}
    <details><summary>{envelope.source_scope ? '技术详情：固定版本目录与请求规则' : '技术详情：完整冻结资料包'}</summary><pre>{JSON.stringify(envelope, null, 2)}</pre></details>
  </div>
}

export function ContextApprovals({ requests, busy, onDecision, onBatch }: {
  requests: Row[]; busy: boolean; onDecision(parameters: Row): Promise<void> | void; onBatch(parameters: Row): Promise<void> | void
}) {
  const [error, setError] = useState(''), [submitting, setSubmitting] = useState(false)
  const pending = requests.filter(item => item.status === 'awaiting_approval')
  async function decide(action: () => Promise<void> | void) {
    if (busy || submitting) return
    setSubmitting(true); setError('')
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : '决定未完成，请核对原记录。') }
    finally { setSubmitting(false) }
  }
  const disabled = busy || submitting
  return <div className="context-approvals" aria-busy={disabled}>
    <p><strong>{pending.length ? `${pending.length} 项请求等待确认` : '正在核对最新请求状态…'}</strong></p>
    <p className="muted">展开查看用途和资料，也可一起处理当前这批。全部处理后继续，已有调查保留。</p>
    <div className="approval-request-list">{pending.map((request, index) => {
      const description = obj(request.description), authorization = obj(request.authorization), original = obj(authorization.request)
      const title = String(description.title || ({coordinator: '规划项目调查'}[String(request.actor)] || (String(request.actor).startsWith('review_') ? '核对结论与源码证据' : `模块调查 ${request.actor}`)))
      const parameters = {actor: request.actor, sequence: request.sequence, sha256: request.request_sha256}
      return <details className="approval-request" key={`${request.actor}:${request.sequence}`}>
        <summary><strong>{index + 1}. {title}</strong><span>{String(description.goal || '继续分析当前固定版本的项目资料。')}</span>
          <small>{String(description.model || obj(original.target).model || '已配置模型')} · 1 个请求 · {Number(request.chars || 0).toLocaleString()} 字符</small></summary>
        {Boolean(description.purpose) && <p>操作：{String(description.purpose)}</p>}
        {Array.isArray(description.topics) && description.topics.length > 0 && <ul>{description.topics.map((topic, i) => <li key={i}>{String(topic)}</li>)}</ul>}
        <p className="muted">{String(description.reason || '完整资料超过默认请求长度，需要你确认本次长度扩展。')}</p>
        <p>{String(description.model || obj(original.target).model || '已配置模型')} · 本次 1 个请求 · {Number(request.chars || 0).toLocaleString()} 字符（默认 {Number(request.default_chars || 0).toLocaleString()}）</p>
        <p className="muted">不增加可读取的文件范围，不执行项目代码。</p>
        {Array.isArray(description.source_paths) && description.source_paths.length > 0 && <details><summary>涉及资料 · {String(description.source_count || description.source_paths.length)} 个文件</summary><ul className="request-paths">{description.source_paths.map((path, i) => <li key={i}><code>{String(path)}</code></li>)}</ul></details>}
        <details><summary>技术详情：本次完整请求</summary><pre>{String(original.body_text || JSON.stringify(authorization, null, 2))}</pre></details>
        <div className="inline-actions"><button disabled={disabled} onClick={() => { void decide(() => onDecision({...parameters, approved:true})) }}>允许这项请求</button>
          <button className="quiet-button" disabled={disabled} onClick={() => { void decide(() => onDecision({...parameters, approved:false})) }}>跳过这项请求</button></div>
      </details>
    })}</div>
    {pending.length > 1 && <div className="approval-batch"><p>已列出当前全部 {pending.length} 项，可以一起处理；后续新请求仍会单独询问。</p>
      <button className="solid-button" disabled={disabled} onClick={() => { void decide(() => onBatch({approved:true, requests:pending.map(item => ({actor:item.actor, sequence:item.sequence, sha256:item.request_sha256}))})) }}>允许上面这 {pending.length} 项请求并继续</button>
      <button className="quiet-button" disabled={disabled} onClick={() => { void decide(() => onBatch({approved:false, requests:pending.map(item => ({actor:item.actor, sequence:item.sequence, sha256:item.request_sha256}))})) }}>跳过这 {pending.length} 项</button>
    </div>}
    {submitting && <p role="status">正在保存决定并刷新请求状态…</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </div>
}
