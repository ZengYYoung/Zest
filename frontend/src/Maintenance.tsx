import { useEffect, useMemo, useRef, useState } from 'react'
import { readApi, writeApi, shortCommit, type Connection, type Row } from './api.ts'
import { ProjectCheckEvents, type CheckReason } from './projectCheckEvents.ts'
import { ContextApprovals, ScopeReview } from './RequestReview.tsx'
import { MaintenanceFeedback } from './MaintenanceFeedback.tsx'
import { maintenanceRunning as running } from './MaintenanceProgress.tsx'
import { dismissNotice, noticeIdentity, wasDismissed } from './noticeHistory.ts'

const object = (value: unknown): Row => value && typeof value === 'object' ? value as Row : {}

export function useProjectMaintenance(connection: Connection, projectId: string, refresh: number, onChanged: () => void) {
  const [value, setValue] = useState<Row | null>(null), [checking, setChecking] = useState(false), [error, setError] = useState(''), [visible, setVisible] = useState(false)
  const current = useRef(projectId); current.current = projectId
  const changed = useRef(onChanged); changed.current = onChanged
  const notified = useRef('')
  const checkInFlight = useRef(false), checkRevision = useRef(0)
  const observedActive = useRef(new Set<string>())
  const lastKnownRunning = useRef(false)
  const events = useMemo(() => new ProjectCheckEvents<Row>(async (id, reason) => {
    if (id === current.current) { checkRevision.current++; checkInFlight.current = true; setChecking(true); setError('') }
    try {
      const result = await writeApi<Row>(connection, `/api/projects/${id}/check`, { reason })
      if (id === current.current) {
        if (running.has(String(result.status))) observedActive.current.add(String(result.active_run_id))
        const dismissed = wasDismissed(connection.port, id, 'maintenance', noticeIdentity('maintenance', result))
        const needsDecision = ['permission_required', 'awaiting_approval', 'awaiting_context_approval', 'pending_execution', 'interrupted'].includes(String(result.status))
        const fresh = observedActive.current.has(String(result.active_run_id))
        lastKnownRunning.current = running.has(String(result.status))
        setValue({ ...result, progress_received_at: new Date().toISOString() }); setVisible(reason === 'manual' || !dismissed && (running.has(String(result.status)) || needsDecision || fresh))
        changed.current()
      }
      return result
    } catch (e) {
      if (id === current.current) { setError(e instanceof Error ? e.message : '检查更新失败。'); setVisible(true) }
      throw e
    } finally { if (id === current.current) { checkRevision.current++; checkInFlight.current = false; setChecking(false) } }
  }), [connection])
  useEffect(() => {
    checkRevision.current++; checkInFlight.current = false
    observedActive.current.clear()
    lastKnownRunning.current = false
    setValue(null); setError(''); setVisible(false); setChecking(false)
    void events.select(projectId).catch(() => {})
  }, [events, projectId])
  useEffect(() => {
    if (!projectId) return
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>
    async function load() {
      const revision = checkRevision.current
      let retryDelay = 2000
      try {
        const result = await readApi<Row>(connection, `/api/projects/${projectId}/maintenance`, abort.signal)
        if (abort.signal.aborted || revision !== checkRevision.current || checkInFlight.current) return
        setValue({ ...result, progress_received_at: new Date().toISOString() }); setError('')
        lastKnownRunning.current = running.has(String(result.status))
        if (running.has(String(result.status))) {
          observedActive.current.add(String(result.active_run_id))
          setVisible(!wasDismissed(connection.port, projectId, 'maintenance', noticeIdentity('maintenance', result)))
          // Read the current job's progress; this endpoint does not check Git.
        } else if (['completed', 'partial', 'failed', 'blocked', 'publication_failed', 'awaiting_approval', 'awaiting_context_approval', 'pending_execution', 'interrupted'].includes(String(result.status))) {
          const dismissed = wasDismissed(connection.port, projectId, 'maintenance', noticeIdentity('maintenance', result))
          const requiresDecision = ['awaiting_approval', 'awaiting_context_approval', 'pending_execution', 'interrupted'].includes(String(result.status))
          const fresh = observedActive.current.has(String(result.active_run_id))
          setVisible(previous => !dismissed && (previous || requiresDecision || fresh))
          const key = `${result.active_run_id}:${result.status}`
          if (notified.current !== key) { notified.current = key; changed.current() }
        }
      } catch (e) {
        retryDelay = 5000
        if (!abort.signal.aborted && revision === checkRevision.current && !checkInFlight.current) {
          setError(e instanceof Error ? e.message : '无法读取更新进度。'); setVisible(true)
        }
      } finally {
        // Retry only GET progress for an observed active job, never the update command.
        if (!abort.signal.aborted && lastKnownRunning.current) timer = setTimeout(load, retryDelay)
      }
    }
    void load()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [connection, projectId, refresh])
  async function check(reason: CheckReason = 'manual') { if (projectId) return events.check(projectId, reason) }
  return { value, checking, error, visible, check, dismiss: () => {
    if (value) dismissNotice(connection.port, projectId, 'maintenance', noticeIdentity('maintenance', value))
    setVisible(false); setError('')
  } }
}

export function MaintenanceNotice({ value, error, visible, busy, onDismiss, onCommand, connection }: {
  value: Row | null; error: string; visible: boolean; busy: boolean; onDismiss(): void
  onCommand(action: string, parameters: Row): Promise<void> | void; connection: Connection
}) {
  const status = String(value?.status), check = object(value?.last_check)
  if (!visible && !error) return null
  return <section className="chat-notice maintenance-notice" aria-label="项目更新状态" aria-live="polite">
    <div className="initialization-heading"><strong>{error ? '更新状态待核对' : running.has(status) ? '正在增量更新项目理解' : '项目更新状态'}</strong><button className="icon-button" aria-label="收起更新提示" onClick={onDismiss}>×</button></div>
    {running.has(status) ? <MaintenanceFeedback value={value} error={error} /> : <><p>{error || String(value?.message || '本轮更新已结束，记录保存在项目状态。')}</p><details><summary>查看本轮更新详情</summary><MaintenanceFeedback value={value} error={error} /></details></>}
    {Boolean(check.commit) && <p className="muted">当前提交 {shortCommit(check.commit)} · 认知版本 {value?.knowledge_commit ? shortCommit(value.knowledge_commit) : '尚未建立'}{check.dirty ? ' · 有未提交改动' : ''}</p>}
    <MaintenanceActions connection={connection} value={value} busy={busy} onCommand={onCommand} />
  </section>
}

export function MaintenanceActions({ connection, value, busy, onCommand }: {
  connection: Connection; value: Row | null; busy: boolean
  onCommand(action: string, parameters: Row): Promise<void> | void
}) {
  const [preview, setPreview] = useState<Row | null>(null), [contexts, setContexts] = useState<Row[]>([])
  const status = String(value?.status), run = object(value?.research)
  const documentRecovery = object(object(value?.document_generation).recovery)
  const newRound = documentRecovery.action === 'new_round' && !running.has(status)
  useEffect(() => {
    setPreview(null); setContexts([])
    if (!value?.active_run_id || !['awaiting_approval', 'awaiting_context_approval'].includes(status)) return
    const abort = new AbortController()
    const path = `/api/cognition/${value.active_run_id}/${status === 'awaiting_approval' ? 'preview' : 'context-requests'}`
    readApi<Row | Row[]>(connection, path, abort.signal).then(result => { if (Array.isArray(result)) setContexts(result); else setPreview(result) }).catch(() => {})
    return () => abort.abort()
  }, [connection, value?.active_run_id, status, run.updated_at])
  const command = (action: string, parameters: Row = {}) => onCommand(`maintenance.${action}`, { project_id: value?.project_id, ...parameters })
  return <div className="maintenance-actions">
    {status === 'permission_required' && <><p>确认后按当前配置的模型开启此项目的自动理解，并立即开始增量更新。已提交的安全项目资料会发送至该模型，每轮最多 32 次模型调用、30 分钟；不运行或修改项目代码。</p><p className="muted">后续仅在打开、切换项目、任务完成或手动检查时更新，无需逐次批准。可在设置中关闭自动批准。</p><button className="solid-button" disabled={busy || !value?.project_id} onClick={() => onCommand('understanding.configure', { project_id: value?.project_id, enabled: true })}>确认并开始更新</button></>}
    {status === 'awaiting_approval' && <><details><summary>查看本轮增量资料与范围</summary>{preview ? <ScopeReview envelope={object(preview.envelope)} /> : '正在读取完整资料…'}</details><button disabled={busy || !preview} onClick={() => command('decide', { approved: true, sha256: run.approval_sha256 })}>允许本轮更新</button><button disabled={busy} onClick={() => command('decide', { approved: false })}>暂缓</button></>}
    {status === 'awaiting_context_approval' && <ContextApprovals requests={contexts} busy={busy} onDecision={p => command('context-decide', p)} onBatch={p => command('context-decide-many', p)} />}
    {['failed', 'pending_execution', 'interrupted', 'publication_failed'].includes(status) && !newRound && <button disabled={busy} onClick={() => command('resume')}>核对并继续原更新</button>}
    {status === 'partial' && object(value?.document_generation).status === 'partial' && !['new_round', 'inspect'].includes(String(documentRecovery.action)) && Boolean(value?.active_run_id) && <button disabled={busy} onClick={() => command('resume')}>继续未完成文档页面</button>}
    {newRound && <><p className="muted">此操作新建独立的文档生成轮次，复用已核对事实，不重复调查。新一轮最多 {Number(documentRecovery.max_model_calls || 32)} 次模型调用、{Math.round(Number(documentRecovery.max_seconds || 1800) / 60)} 分钟；原任务与调用记录保留。</p><button disabled={busy || !documentRecovery.source_run_id || !documentRecovery.source_sha256} onClick={() => command('documents-new-round', { source_run_id: documentRecovery.source_run_id, source_sha256: documentRecovery.source_sha256 })}>基于已有事实开始新一轮文档生成</button></>}
    {Boolean(value?.can_reprepare) && <button disabled={busy} onClick={() => command('reprepare')}>重新准备本次更新</button>}
  </div>
}
