import { useEffect, useRef, useState } from 'react'
import { readApi, shortCommit, type Connection, type Row } from './api.ts'
import { rows } from './Conversation.tsx'
import { ContextApprovals, ScopeReview } from './RequestReview.tsx'
import { dismissNotice, noticeIdentity, wasDismissed } from './noticeHistory.ts'
import { DocumentProgress } from './DocumentProgress.tsx'

const object = (value: unknown): Row => value && typeof value === 'object' ? value as Row : {}
const active = new Set(['preparing', 'queued', 'planning', 'investigating', 'reviewing', 'publishing'])
const labels: Record<string, string> = { preparing: '正在整理项目资料', queued: '初始化已排队', planning: '正在了解项目目标', investigating: '正在阅读源码与文档', reviewing: '正在核对项目理解', publishing: '正在整理项目文档', publication_pending: '调查已结束，等待整理文档', awaiting_approval: '确认项目调查范围', awaiting_context_approval: '需要确认本次资料长度' }

export function Initialization({ connection, projectId, refresh, busy, onCommand, onSettings, onDocuments, onPublished }: {
  connection: Connection; projectId: string; refresh: number; busy: boolean; onCommand(action: string, parameters: Row): Promise<void> | void
  onSettings(): void; onDocuments(): void; onPublished(): void
}) {
  const [value, setValue] = useState<Row | null>(null), [preview, setPreview] = useState<Row | null>(null), [error, setError] = useState('')
  const [contexts, setContexts] = useState<Row[]>([]), [dismissed, setDismissed] = useState('')
  const notified = useRef('')
  const observedActive = useRef(new Set<string>())
  useEffect(() => {
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>
    async function load() {
      try {
        const next = await readApi<Row>(connection, `/api/projects/${projectId}/initialization`, abort.signal)
        if (abort.signal.aborted) return
        setValue(next); setError('')
        if (active.has(String(next.status))) observedActive.current.add(String(next.run_id || next.id))
        if (next.stage === 'published' && notified.current !== next.id) {
          notified.current = String(next.id); onPublished()
        }
        if (active.has(String(next.status))) timer = setTimeout(load, 2000)
      } catch (e) { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '无法读取初始化状态。') }
    }
    void load()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [connection, projectId, refresh])
  useEffect(() => {
    setPreview(null); setContexts([])
    if (!value?.run_id || !['awaiting_approval', 'awaiting_context_approval'].includes(String(value.status))) return
    const abort = new AbortController()
    const path = `/api/cognition/${value.run_id}/${value.status === 'awaiting_approval' ? 'preview' : 'context-requests'}`
    readApi<Row | Row[]>(connection, path, abort.signal).then(result => {
      if (Array.isArray(result)) setContexts(result); else setPreview(result)
    }).catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '无法读取资料范围。') })
    return () => abort.abort()
  }, [connection, value?.run_id, value?.status, object(value?.research).updated_at, refresh])
  if (!value) return error ? <div className="chat-notice" role="alert">{error}</div> : null
  const terminal = value.stage === 'published'
  const identity = noticeIdentity('initialization', value)
  // An already published initialization is history, not an outstanding task.
  if (terminal && (!observedActive.current.has(String(value.run_id || value.id)) || dismissed === identity
    || wasDismissed(connection.port, projectId, 'initialization', identity))) return null
  return <InitializationCard value={value} preview={preview} contexts={contexts} error={error} busy={busy} onCommand={onCommand}
    onSettings={onSettings} onDocuments={onDocuments} onDismiss={() => { dismissNotice(connection.port, projectId, 'initialization', identity); setDismissed(identity) }} />
}

export function InitializationCard({ value, preview, contexts = [], error = '', busy, onCommand, onSettings, onDocuments, onDismiss }: {
  value: Row; preview: Row | null; contexts?: Row[]; error?: string; busy: boolean
  onCommand(action: string, parameters: Row): Promise<void> | void; onSettings(): void; onDocuments(): void; onDismiss(): void
}) {
  const research = object(value.research), summary = object(value.summary), policy = object(research.policy), target = object(research.target)
  const supplement = Boolean(value.repair_source_run_id)
  const status = String(value.status), terminal = value.stage === 'published'
  const ready = terminal && summary.readiness === 'ready' && (!value.document_generation || object(value.document_generation).status === 'completed')
  const backlog = object(summary.backlog)
  const repairProgress = object(summary.repair_progress)
  const pendingTotal = Number(summary.pending_total ?? (Number(summary.pending_items || 0) + Number(summary.unresolved_gaps || 0)))
  const repairable = summary.repairable_items === undefined ? undefined : Number(summary.repairable_items)
  const workflow = object(research.workflow)
  const steps = [['survey', '整理资料'], ['structure', '梳理结构'], ['research', '调查核心流程'], ['review', '核对与局部修正'], ['publish', '生成文档']]
  const currentStep = steps.findIndex(([key]) => key === String(workflow.stage || (status === 'preparing' || status === 'queued' ? 'survey' : status === 'planning' ? 'structure' : status === 'investigating' ? 'research' : status === 'reviewing' ? 'review' : 'publish')))
  const researchError = object(research.error)
  const expectedPause = status === 'awaiting_context_approval' && researchError.code === 'cognition_context_approval_required'
  const invalidReview = ['invalid_cognition_reference', 'invalid_cognition_output'].includes(String(researchError.code))
  const pageGeneration = Boolean(research.document_generation_enabled) && research.status !== 'failed'
  const send = (action: string, parameters: Row = {}) => onCommand(`initialization.${action}`, { project_id: value.project_id, ...parameters })
  return <section className={`initialization-card ${terminal ? 'initialization-finished' : ''}`} aria-label="项目初始化" aria-live="polite">
    <div className="initialization-heading"><strong>{terminal ? ready ? '初始化完成，可以开始使用' : summary.readiness === 'unavailable' ? '资料已整理，项目理解尚未建立' : status === 'completed' ? 'Zest 已建立初步项目理解' : status === 'failed' ? '初始化未完成' : '已建立部分项目理解' : labels[status] || '让 Zest 先熟悉这个项目'}</strong>
      {terminal && <button className="icon-button" aria-label="收起初始化摘要" onClick={onDismiss}>×</button>}</div>
    {terminal ? <><p>已整理 {String(summary.documents || 0)} 份文档，自动核对 {String(summary.reviewed_findings || 0)} 项发现。{!summary.backlog && <>保留 {pendingTotal} 项{summary.readiness ? '知识缺口' : '待确认内容'}。</>}{research.status === 'failed' || status === 'failed' ? '调查未完整完成，已有资料与待确认内容均已保留，可先查看项目文档。' : summary.readiness === 'unavailable' ? '尚无可复用的核对结论，可查看缺口后按需补充。' : '现在可以直接在下方提问。'}</p>
      {ready && <p>已形成项目用途、模块关系和核心流程的初步说明。知识缺口会保留，遇到相关问题时再补充，不需要反复初始化。</p>}
      {Number(repairProgress.processed) > 0 && <p>本轮定向处理 {Number(repairProgress.processed)} 项，已解决 {Number(repairProgress.resolved)} 项，仍缺依据 {Number(repairProgress.remaining)} 项。未解决项保留原因，相同资料下不再重复。</p>}
      {Boolean(summary.backlog) && pendingTotal > 0 && <details className="knowledge-backlog"><summary>查看待补充知识记录 · {pendingTotal} 项</summary>
        <p>可定向处理 {repairable || 0} 项；已尝试但仍缺依据 {Number(backlog.attempted || 0)} 项；需外部资料 {Number(backlog.external || 0)} 项；分析或待明确问题 {Number(backlog.deferred || 0)} 项。历史记录不等于需要反复初始化的任务。</p>
        {repairable === 0 ? <p>当前资料下没有新的自动补查任务。剩余问题保留，遇到相关问题时直接提问，无需继续点击补充。</p> : <button className="quiet-button" disabled={busy} onClick={() => send('repair', {automatic_understanding:true})}>定向处理下一组待办（最多 4 项）</button>}
        {Array.isArray(summary.pending_categories) && <p className="muted">{summary.pending_categories.map(item => `${String(object(item).title)} ${String(object(item).count)} 项`).join(' · ')}</p>}
      </details>}
      {!summary.backlog && Array.isArray(summary.pending_categories) && summary.pending_categories.length > 0 && <p className="muted">{summary.pending_categories.map(item => `${String(object(item).title)} ${String(object(item).count)} 项`).join(' · ')}。这些事项会区分调查未完成与需要补充资料，不需要逐条人工确认。</p>}
      <button className="quiet-button" onClick={onDocuments}>查看项目文档</button>
      <DocumentProgress value={value.document_generation} />
      {object(value.document_generation).status === 'partial' && Number(object(value.document_generation).skipped || 0) < Number(object(value.document_generation).total || 0) && <button className="quiet-button" disabled={busy} onClick={() => send('recover')}>继续未完成页面</button>}
      {status !== 'completed' && repairable === undefined && <button className="quiet-button" disabled={busy} onClick={() => send('repair', {automatic_understanding:true})}>{ready ? '按需补充项目知识' : '补全未完成的项目理解'}</button>}
      {Array.isArray(value.conflicts) && value.conflicts.length > 0 && <p>部分文档已有修改，已保留原文，请在项目状态核对。</p>}</>
      : status === 'not_started' ? <><p>点击后自动整理文档、建立源码索引，并使用已配置模型理解项目；完成后直接自然提问。每轮最多 32 次调用、30 分钟。</p><button className="solid-button" disabled={busy} onClick={() => send('initialize')}>初始化项目</button></>
      : status === 'needs_model' ? <><p>本地文档已准备好。填写并保存 DeepSeek API Key 后自动继续初始化。</p><button className="solid-button" onClick={onSettings}>配置 DeepSeek</button><button disabled={busy} onClick={() => send('initialize')}>继续初始化</button></>
      : status === 'needs_commit' ? <><p>已建立 .zest/ 文档目录。项目还没有 Git 提交；请先提交需要理解的源码，再继续初始化。</p><button disabled={busy} onClick={() => send('initialize')}>重新检查项目</button></>
      : status === 'awaiting_approval' ? <><p>从版本 {shortCommit(value.commit)} 读取 {String(research.file_count || 0)} 个安全文本文件，发送至 {String(target.model || '已配置模型')}。未提交改动不包含在本轮理解中。</p>
        {supplement && <p>本轮复用原资料包和已有调查线索，优先补读缺口、核对未完成结论。原调用和历史记录保留，尚未发送新的模型请求。</p>}
        <p className="muted">最多 {String(policy.max_model_calls || 0)} 次模型调用、{Math.round(Number(policy.max_seconds || 0) / 60)} 分钟；不运行项目，不提交或推送。</p>
        <details><summary>查看资料范围和调查方式</summary>{preview ? <ScopeReview envelope={object(preview.envelope)} /> : <p>正在读取完整资料范围…</p>}</details>
        <p className="muted">点击后自动完成项目理解，并开启当前项目的自动维护；范围内的较长请求自动继续。可在设置中关闭自动批准。</p><button className="solid-button" disabled={busy || Boolean(value.stale)} onClick={() => send('initialize')}>{supplement ? '开始补全' : '开始初始化'}</button>
        <button className="quiet-button" disabled={busy} onClick={() => send('decide', { approved: false })}>{supplement ? '暂不补全' : '暂不初始化'}</button>
        <button className="quiet-button" disabled={busy} onClick={() => send(supplement ? 'repair' : 'prepare', { replace_pending: true })}>重新准备资料</button></>
      : active.has(status) ? <><p>{status === 'queued' ? '任务已保存在本机，关闭页面后仍可在后台继续。' : '正在后台完成。你可以查看项目文档，刷新页面后会恢复进度。'}</p>
        <ol className="initialization-steps" aria-label="初始化步骤">{steps.map(([key, label], index) => <li key={key} aria-current={index === currentStep ? 'step' : undefined} className={index < currentStep ? 'done' : ''}>{label}</li>)}</ol>
        <div className="initialization-progress" role="status"><span className="initialization-spinner" />{labels[status]}{Number(research.call_count) > 0 && ` · 已调用 ${research.call_count} 次`}</div>
        <p className="muted">最多进行一轮定向补读，完成后保留具体缺口；格式问题只修正现有核对结果。</p>
        <DocumentProgress value={value.document_generation} />
        {status === 'preparing' && !value.preparation_active && <button disabled={busy} onClick={() => send('initialize')}>继续准备原初始化</button>}
        {status === 'queued' && !value.worker_active && <button disabled={busy} onClick={() => send('start')}>继续后台初始化</button>}
        {status === 'publishing' && !value.worker_active && <button disabled={busy} onClick={() => send('start')}>整理已有调查结果</button>}</>
      : status === 'awaiting_context_approval' ? <><p>部分资料较长，调查已暂停。已有结果保留；扩展请求尚未发送。</p><ContextApprovals requests={contexts} busy={busy} onDecision={p => send('context-decide', p)} onBatch={p => send('context-decide-many', p)} /></>
      : ['interrupted', 'pending_execution'].includes(status) ? <><p>后台执行已中断或请求结果待核对，不会自动重发。</p><button disabled={busy} onClick={() => send('recover')}>核对并继续原初始化</button></>
      : ['publication_pending', 'publication_failed'].includes(status) ? <><p>{pageGeneration ? '调查结果已保存在本机。继续保存已生成页面，并完成尚未发送的页面写作；结果不明的请求不会重发。' : '调查已停止，已有结果保存在本机。可以整理可用结论和待确认内容，不会再次调用模型。'}</p><button disabled={busy} onClick={() => send('start')}>{pageGeneration ? '继续生成项目文档' : '整理已有调查结果'}</button></>
      : status === 'rejected' ? <><p>本轮调查已暂缓；没有发送项目资料。</p><button disabled={busy} onClick={() => send(supplement ? 'repair' : 'initialize', supplement ? { replace_pending: true, automatic_understanding:true } : {})}>{supplement ? '重新准备补全资料' : '重新准备初始化'}</button></>
      : status === 'repair_preparing' ? <><p>补全资料尚未准备完，已有调查记录保留。</p><button disabled={busy} onClick={() => send('repair', {automatic_understanding:true})}>继续准备补全资料</button></>
      : <><p>资料整理未完成，已有记录保留。</p><button disabled={busy} onClick={() => send('initialize')}>继续准备初始化</button></>}
    {Boolean(value.stale) && <p className="initialization-warning">项目版本已变化，本轮理解对应 {shortCommit(value.commit)}；当前版本需要重新调查。</p>}
    {invalidReview && <p className="initialization-warning">有一批结论未通过引用或格式核对，已保留为待确认，不会作为已确认知识使用。</p>}
    {(error || Boolean(value.error) || Boolean(research.error) && !expectedPause && !invalidReview) && <p className="error" role="alert">{error || String(object(value.error).message || researchError.message || '')}</p>}
  </section>
}
