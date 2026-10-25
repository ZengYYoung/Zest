import { useEffect, useRef, useState, type FormEvent } from 'react'
import { defaultPort, projectName, readApi, statusLabels, validateConnection, type Connection, type Project, type Row } from './api.ts'
import { Conversation, rows } from './Conversation.tsx'
import { ConversationSidebar } from './ConversationSidebar.tsx'
import { Dialog } from './Dialog.tsx'
import { Icon } from './Icons.tsx'
import { ModelConnection } from './ModelConnection.tsx'
import { ProjectStatus } from './ProjectStatus.tsx'
import { ProjectImport } from './ProjectImport.tsx'
import { OperationStatus, operationTitle } from './OperationStatus.tsx'
import { Initialization } from './Initialization.tsx'
import { UnderstandingSettings } from './UnderstandingSettings.tsx'
import { MaintenanceNotice, useProjectMaintenance } from './Maintenance.tsx'
import { RecordDetail } from './RecordDetail.tsx'
import { CognitionDetail } from './CognitionDetail.tsx'
import { RecordActions, type View } from './WorkspaceActions.tsx'
import { useOperation } from './useOperation.ts'

export function App() {
  const [connection, setConnection] = useState<Connection | null>(null), [projects, setProjects] = useState<Project[]>([])
  const [port, setPort] = useState(String(defaultPort())), [error, setError] = useState(''), [loading, setLoading] = useState(true)
  async function connect(event?: FormEvent) {
    event?.preventDefault(); setLoading(true); setError('')
    try {
      const candidate = validateConnection(Number(port))
      setProjects(await readApi<Project[]>(candidate, '/api/projects')); setConnection(candidate)
    } catch (e) { setError(e instanceof Error ? e.message : '连接失败。') }
    finally { setLoading(false) }
  }
  useEffect(() => { void connect() }, [])
  if (connection) return <Workspace key={connection.port} connection={connection} initialProjects={projects} onDisconnect={() => setConnection(null)} />
  return <main className="connection-page"><div className="brand"><span className="brand-mark">z</span>Zest</div><section>
    <h1>你的项目，<br />有位熟悉它的伙伴。</h1><p>无需登录，连接本机工作区即可开始。</p>
    <form className="connection-form" onSubmit={connect}><label>本地服务端口<input aria-label="API 端口" inputMode="numeric" value={port} onChange={event => setPort(event.target.value)} /></label><button className="solid-button" disabled={loading}>{loading ? '正在连接…' : '连接工作区'}</button></form>
    {error && <p className="error" role="alert">{error}</p>}<p className="connection-help">启动服务：<code>uv run zest web serve</code></p>
  </section></main>
}

type Panel = 'settings' | 'status' | 'project' | 'record' | 'remove-project' | null
export function Workspace({ connection, initialProjects, onDisconnect }: { connection: Connection; initialProjects: Project[]; onDisconnect(): void }) {
  const [projects, setProjects] = useState(initialProjects)
  const [projectId, setProjectId] = useState(() => {
    try { const saved = localStorage.getItem(`zest-project-${connection.port}`); if (initialProjects.some(p => p.id === saved)) return saved! } catch { /* Optional preferences. */ }
    return initialProjects[0]?.id || ''
  })
  const [panel, setPanel] = useState<Panel>(null), [sidebarOpen, setSidebarOpen] = useState(false)
  const sidebarTrigger = useRef<HTMLButtonElement>(null), sidebarWasOpen = useRef(false)
  const [conversations, setConversations] = useState<Row[]>([]), [conversationId, setConversationId] = useState(''), [conversation, setConversation] = useState<Row | null>(null)
  const [conversationLists, setConversationLists] = useState<Record<string, Row[]>>({})
  const requestedConversation = useRef<{ projectId: string; id: string } | null>(null)
  const [refresh, setRefresh] = useState(0), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  const [draft, setDraft] = useState('')
  const [projectPath, setProjectPath] = useState('')
  const [removalTarget, setRemovalTarget] = useState<Project | null>(null)
  const [record, setRecord] = useState<{ kind: View; value: Row } | null>(null)
  const operation = useOperation(connection)
  const answering = operation.busy && ['conversation.send', 'conversation.answer'].includes(String(operation.operation?.action))
  const current = useRef({ projectId, conversationId }); current.current = { projectId, conversationId }
  const project = projects.find(p => p.id === projectId)
  const maintenance = useProjectMaintenance(connection, projectId, refresh, () => setRefresh(v => v + 1))
  const conversationKey = `zest-conversation-${connection.port}-${projectId}`
  useEffect(() => {
    if (!sidebarOpen && sidebarWasOpen.current) sidebarTrigger.current?.focus()
    sidebarWasOpen.current = sidebarOpen
  }, [sidebarOpen])
  useEffect(() => {
    setConversationId(''); setConversation(null); setConversations([]); setError(''); setDraft('')
    if (!projectId) return
    const abort = new AbortController(); setLoading(true)
    try { localStorage.setItem(`zest-project-${connection.port}`, projectId) } catch { /* Optional preferences. */ }
    readApi<Row[]>(connection, `/api/projects/${projectId}/conversations`, abort.signal).then(list => {
      if (abort.signal.aborted) return
      setConversations(list)
      setConversationLists(previous => ({ ...previous, [projectId]: list }))
      let saved = ''; try { saved = localStorage.getItem(conversationKey) || '' } catch { /* Optional preferences. */ }
      const requested = requestedConversation.current
      if (requested?.projectId === projectId) requestedConversation.current = null
      const preferred = requested?.projectId === projectId ? requested.id : saved
      setConversationId(String(list.find(c => c.id === preferred)?.id || list[0]?.id || ''))
    }).catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '读取失败。') })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [connection, projectId, conversationKey])
  useEffect(() => {
    setConversation(null)
    if (!conversationId) return
    const abort = new AbortController(); setLoading(true); setError('')
    try { localStorage.setItem(conversationKey, conversationId) } catch { /* Optional preferences. */ }
    readApi<Row>(connection, `/api/conversations/${conversationId}`, abort.signal).then(setConversation)
      .catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '读取失败。') })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [connection, conversationId, conversationKey])
  useEffect(() => {
    if (!answering || !conversationId) return
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>
    async function followAnswer() {
      try {
        const value = await readApi<Row>(connection, `/api/conversations/${conversationId}`, abort.signal)
        if (!abort.signal.aborted && current.current.conversationId === conversationId && current.current.projectId === value.project_id) setConversation(value)
      } catch { /* The durable operation reports errors; this only refreshes text. */ }
      if (!abort.signal.aborted) timer = setTimeout(followAnswer, 1000)
    }
    void followAnswer()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [connection, conversationId, answering])
  async function reloadConversation(id = current.current.conversationId) {
    if (!id) return
    const value = await readApi<Row>(connection, `/api/conversations/${id}`)
    if (current.current.conversationId === id && current.current.projectId === value.project_id) setConversation(value)
  }
  async function updateAfterCommand(action: string, result?: Row) {
    setRefresh(v => v + 1)
    if (action === 'project.choose-directory') {
      if (result?.path && !result.cancelled) { setProjectPath(String(result.path)); setPanel('project') }
      return
    }
    if (action === 'project.remove' && result?.removed && result.project_id) {
      const removed = String(result.project_id), remaining = projects.filter(p => p.id !== removed)
      setProjects(remaining)
      setConversationLists(previous => { const next = { ...previous }; delete next[removed]; return next })
      requestedConversation.current = null
      try {
        localStorage.removeItem(`zest-conversation-${connection.port}-${removed}`)
        if (projectId === removed) localStorage.removeItem(`zest-project-${connection.port}`)
      } catch { /* Optional preferences. */ }
      if (projectId === removed) setProjectId(remaining[0]?.id || '')
      setRemovalTarget(null); setPanel(null); setError('')
      return
    }
    if ((action === 'project.import' || action === 'project.init') && result) {
      const list = await readApi<Project[]>(connection, '/api/projects'); setProjects(list)
      const created = result.project as Project | undefined
      setProjectId(String(created?.id || result.id || projectId)); setPanel(null)
    } else {
      if (action === 'conversation.create' && result?.id && result.project_id === projectId) {
        current.current.conversationId = String(result.id); setConversationId(String(result.id)); setConversation(result)
      }
      await reloadConversation()
      if (record && panel === 'record') {
        const updated = await readApi<Row>(connection, `/api/${record.kind}/${record.value.task_id || record.value.id}`)
        setRecord({ ...record, value: updated })
      }
    }
  }
  async function command(action: string, p: Row) {
    const result = await operation.run(action, p)
    try { await updateAfterCommand(action, result) } catch (e) { setError(e instanceof Error ? e.message : '刷新失败。') }
  }
  async function send(question: string) {
    setError('')
    try {
      let id = conversationId
      if (!id) {
        const created = await operation.run('conversation.create', { project_id: projectId })
        if (!created) return false
        id = String(created.id); current.current.conversationId = id
        setConversationId(id); setConversation(created); setConversations(list => [created, ...list])
        setConversationLists(previous => ({ ...previous, [projectId]: [created, ...(previous[projectId] || [])] }))
      }
      const result = await operation.run('conversation.send', { id, question, message_id: crypto.randomUUID(), automatic_answer:true })
      await reloadConversation(id)
      const list = await readApi<Row[]>(connection, `/api/projects/${projectId}/conversations`)
      if (current.current.projectId === projectId) {
        setConversations(list)
        setConversationLists(previous => ({ ...previous, [projectId]: list }))
      }
      return Boolean(result)
    } catch (e) { setError(e instanceof Error ? e.message : '消息未完成，请核对记录。'); return false }
  }
  async function older() {
    const first = rows(conversation?.turns)[0]
    if (!first) return
    try {
      const value = await readApi<Row>(connection, `/api/conversations/${conversationId}?before=${first.sequence}`)
      if (current.current.conversationId === value.id) setConversation(previous => ({ ...value, turns: [...rows(value.turns), ...rows(previous?.turns)] }))
    } catch (e) { setError(e instanceof Error ? e.message : '读取失败。') }
  }
  function newConversation() { setConversationId(''); setConversation(null); setSidebarOpen(false); setDraft(''); try { localStorage.removeItem(conversationKey) } catch { /* Optional preferences. */ } }
  async function openRecord(kind: View, id: unknown) {
    try { setRecord({ kind, value: await readApi<Row>(connection, `/api/${kind}/${id}`) }); setPanel('record') }
    catch (e) { setError(e instanceof Error ? e.message : '读取失败。') }
  }
  const active = operation.operation
  function openProjectImport() { operation.clearError(); setError(''); setSidebarOpen(false); setPanel('project') }
  const conversationNavigation = <ConversationSidebar projects={projects} projectId={projectId} conversations={conversations}
    savedLists={conversationLists} activeId={conversationId} busy={operation.blocked} loading={loading} onNew={newConversation}
    onAdd={openProjectImport} onProject={id => { requestedConversation.current = null; setProjectId(id) }}
    onRemove={target => { operation.clearError(); setError(''); setRemovalTarget(target); setSidebarOpen(false); setPanel('remove-project') }}
    onSelect={(targetProject, id) => {
      if (targetProject === projectId) setConversationId(id)
      else { requestedConversation.current = { projectId: targetProject, id }; setProjectId(targetProject) }
      setSidebarOpen(false)
    }} />
  const operationFeedback = <>
    {panel !== 'project' && <OperationStatus operation={operation.operation} busy={operation.busy} onStart={operation.startQueued} />}
    {operation.recoveryId && <div className="chat-notice">{operationTitle(operation.operation?.action)}尚待核对。<button disabled={operation.busy} onClick={async () => { const recovered = await operation.recover(); try { if (recovered) await updateAfterCommand(recovered.action, recovered.result) } catch { /* The saved result stays available. */ } }}>核对原操作</button></div>}
  </>
  return <div className="zest-app">
    <header className="zest-header"><div className="header-identity"><button ref={sidebarTrigger} className="icon-button mobile-history-toggle" aria-label="打开项目与对话" aria-haspopup="dialog" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}><Icon name="sidebar" size={19} /></button><span className="brand-wordmark">Zest</span></div>
      <div className="header-project" title={project?.root_path}>{project && <><Icon name="folder" size={16} /><span>{projectName(project)}</span></>}</div>
      <div className="header-actions"><button className="quiet-button" aria-label="项目状态" title="项目状态" disabled={!project} onClick={() => setPanel('status')}><Icon name="knowledge" size={17} /><span>项目状态</span></button><button className="icon-button" aria-label="设置" title="设置" onClick={() => setPanel('settings')}><Icon name="settings" size={19} /></button></div>
    </header>
    <div className="workspace-body">
    <aside className="workspace-sidebar" aria-label="项目与对话">{conversationNavigation}</aside>
    <main className="chat-main">
      {project ? <Conversation connection={connection} value={conversation} busy={operation.blocked} answering={answering} loading={loading} onSend={send} onCommand={command} onOlder={older} draft={draft} draftScope={projectId} onDraft={setDraft}>
        {operationFeedback}
        <div className="conversation-toolbar"><h1 title={String(conversation?.title || '新对话')}>{String(conversation?.title || '新对话')}</h1></div>
        <Initialization key={projectId} connection={connection} projectId={projectId} refresh={refresh} busy={operation.blocked} onCommand={command} onSettings={() => setPanel('settings')} onDocuments={() => setPanel('status')} onPublished={() => setRefresh(v => v + 1)} />
        <MaintenanceNotice connection={connection} value={maintenance.value} visible={maintenance.visible} error={maintenance.error} busy={operation.blocked || maintenance.checking} onDismiss={maintenance.dismiss} onCommand={command} />
        {(error || operation.error) && <div className="chat-error" role="alert">{operation.error && `${operationTitle(operation.operation?.action)}：`}{error || operation.error}</div>}
      </Conversation> : <>{operationFeedback}<section className="first-project"><div className="welcome-mark">Zest</div><h1>从你的项目开始。</h1><p>选择项目根目录，让 Zest 陪你理解和推进它。</p><button className="solid-button" onClick={() => setPanel('project')}><Icon name="folder" />选择项目</button></section></>}
    </main>
    </div>
    <footer className="zest-footer"><span><i className="connection-dot" />本地工作区</span><span>{operation.busy ? '正在处理…' : 'Zest'}</span></footer>
    {sidebarOpen && <Dialog title="项目与对话" onClose={() => setSidebarOpen(false)}>{conversationNavigation}</Dialog>}
    {panel === 'remove-project' && removalTarget && <Dialog title="删除项目" onClose={() => setPanel(null)}>
      <div className="project-remove-confirm"><h3>从 Zest 移除 {projectName(removalTarget)}？</h3>
        <p className="project-remove-path">{removalTarget.root_path}</p>
        <p>仅从当前项目列表移除，磁盘上的源码、.zest 文档和历史记录会保留。重新接入同一目录即可恢复。</p>
        <p className="muted">已启动的后台任务会继续完成。</p>
        {(operation.error || error) && <p className="error" role="alert">{operation.error || error}</p>}
        <div className="inline-actions"><button disabled={operation.busy} onClick={() => setPanel(null)}>取消</button>
          <button className="danger-button" disabled={operation.blocked} onClick={() => { void command('project.remove', { project_id: removalTarget.id }) }}>{operation.busy ? '正在移除…' : '删除项目'}</button></div>
      </div>
    </Dialog>}
    {panel === 'project' && <Dialog title="接入项目" onClose={() => setPanel(null)}><ProjectImport path={projectPath} onPath={setProjectPath}
      busy={operation.blocked} choosing={operation.busy && operation.operation?.action === 'project.choose-directory'}
      onChoose={() => { void command('project.choose-directory', {}) }} onImport={path => { void command('project.import', { path, automatic_understanding: true }) }} operation={operation.operation} onStart={operation.startQueued}
      error={String(operation.operation?.action || '').startsWith('project.') ? operation.error : ''} />
      {operation.recoveryId && <p className="chat-notice">有一条操作尚待核对。<button disabled={operation.busy} onClick={async () => { const recovered = await operation.recover(); if (recovered) await updateAfterCommand(recovered.action, recovered.result) }}>核对原操作</button></p>}
    </Dialog>}
    {panel === 'status' && project && <Dialog title="项目状态" wide onClose={() => setPanel(null)}><ProjectStatus connection={connection} project={project} refresh={refresh} busy={operation.blocked} onCommand={command} checking={maintenance.checking} maintenance={maintenance.value} checkError={maintenance.error} commandError={operation.error} onCheck={() => { void maintenance.check().catch(() => {}) }} /></Dialog>}
    {panel === 'settings' && <Dialog title="设置" onClose={() => setPanel(null)}><section className="settings-section"><h3>工作区连接</h3><p className="muted">本地服务 · 端口 {connection.port}</p><button className="quiet-button" disabled={operation.blocked} onClick={onDisconnect}>更换连接</button></section>
      <ModelConnection connection={connection} disabled={operation.blocked} refresh={refresh} check={String(active?.action || '').startsWith('model.') ? active?.result as Row || null : null} onCommand={command} onConfigured={() => { setRefresh(v => v + 1); if (project) void command('initialization.initialize', { project_id: project.id, resume_setup: true }) }} />
      {project && <UnderstandingSettings key={projectId} connection={connection} projectId={projectId} refresh={refresh} busy={operation.blocked} onCommand={command} />}
      {operation.error && <p className="error" role="alert">{operation.error}</p>}
      <details className="settings-advanced"><summary>历史工作记录</summary>{project && <LegacyRecords connection={connection} projectId={projectId} refresh={refresh} onOpen={openRecord} />}</details>
      <details className="settings-advanced"><summary>本次操作记录</summary>{active ? <><p>{statusLabels[String(active.status)] || String(active.status)}</p>{active.status === 'queued' && <button onClick={operation.startQueued}>启动已保存的操作</button>}<pre>{JSON.stringify(active, null, 2)}</pre></> : <p className="muted">本次打开后尚无操作。</p>}</details>
    </Dialog>}
    {panel === 'record' && record && <Dialog title="工作记录" wide onClose={() => setPanel('settings')}>
      {record.kind === 'cognition' ? <CognitionDetail value={record.value} onClose={() => setPanel('settings')} /> : ['tasks', 'repairs', 'verifications', 'knowledge', 'releases'].includes(record.kind) ? <RecordDetail kind={record.kind as 'tasks'} value={record.value} onClose={() => setPanel('settings')} /> : <pre>{JSON.stringify(record.value, null, 2)}</pre>}
      <RecordActions connection={connection} tab={record.kind} record={record.value} disabled={operation.blocked} submit={command} />
      {Boolean((record.value.result as Row | undefined)?.repair_id) && <button className="quiet-button" onClick={() => openRecord('repairs', (record.value.result as Row).repair_id)}>查看候选修改</button>}
    </Dialog>}
  </div>
}

function LegacyRecords({ connection, projectId, refresh, onOpen }: { connection: Connection; projectId: string; refresh: number; onOpen(kind: View, id: unknown): void }) {
  const [records, setRecords] = useState<{ kind: View; row: Row }[]>([]), [error, setError] = useState('')
  useEffect(() => {
    const abort = new AbortController()
    const kinds: View[] = ['tasks', 'cognition', 'repairs', 'verifications', 'knowledge', 'releases', 'evaluations', 'resolutions']
    Promise.all(kinds.map(async kind => {
      const path = kind === 'tasks' ? `/api/tasks?project_id=${projectId}` : `/api/projects/${projectId}/${kind}`
      return (await readApi<Row[]>(connection, path, abort.signal)).map(row => ({ kind, row }))
    })).then(items => setRecords(items.flat().sort((a, b) => String(b.row.created_at).localeCompare(String(a.row.created_at))).slice(0, 100)))
      .catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '读取失败。') })
    return () => abort.abort()
  }, [connection, projectId, refresh])
  return <div className="legacy-records">{error && <p className="error">{error}</p>}{records.length ? records.map(({ kind, row }) => <button key={`${kind}:${row.id || row.task_id}`} onClick={() => onOpen(kind, row.id || row.task_id)}><span>{String(row.question || row.title || row.id || row.task_id).slice(0, 70)}</span><small>{statusLabels[String(row.status)] || String(row.status)}</small></button>) : <p className="muted">暂无历史工作记录。</p>}</div>
}
