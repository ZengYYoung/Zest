import { useEffect, useRef, useState } from 'react'
import { readApi, shortCommit, type Connection, type Project, type Row } from './api.ts'
import { rows } from './Conversation.tsx'
import { TextContent } from './TextContent.tsx'
import { Icon } from './Icons.tsx'
import { MaintenanceFeedback } from './MaintenanceFeedback.tsx'
import { MaintenanceActions } from './Maintenance.tsx'
import { InitializationCard } from './Initialization.tsx'
import { DocumentProgress } from './DocumentProgress.tsx'
import { SectionLayout } from './SectionLayout.tsx'

function documentTitle(path: string) {
  const titles: Record<string, string> = { 'initialization/index.md': '项目理解总览', 'initialization/purpose.md': '项目目标与功能',
    'initialization/architecture.md': '模块与架构', 'initialization/api.md': '接口与业务流程', 'initialization/dependencies.md': '数据与外部依赖',
    'initialization/development.md': '开发与验证', 'initialization/decisions.md': '设计约束与待确认', 'index.md': '项目文档目录', 'imports.json': '资料来源记录' }
  const relative = path.replace('.zest/docs/', '')
  return titles[relative] || relative.replace(/^imported\//, '原有资料 / ')
}

export function VersionSelection({ version }: { version: Row | null }) {
  if (!version) return <p className="muted">项目理解版本暂时无法读取。</p>
  if (!version.selected_commit) return <p className="muted">仓库尚无提交，提交后默认使用当前本地版本。</p>
  const managed = Boolean(version.managed_commit)
  return <p className="muted">项目理解使用{managed ? '已选择的管理版本' : '当前工作区提交（HEAD）'} {shortCommit(version.selected_commit)}。
    {managed && ' 主动选择的版本保持有效；本地 HEAD 单独显示。'}{' '}未提交的修改不包含在项目理解中；远程更新需要先同步到本机。</p>
}

export function SemanticIndexControl({ connection, projectId, refresh, busy, onCommand }: {
  connection: Connection; projectId: string; refresh: number; busy: boolean; onCommand(action: string, p: Row): void
}) {
  const [status, setStatus] = useState<Row | null>(null)
  const [plan, setPlan] = useState<Row | null>(null), [cleanupPlan, setCleanupPlan] = useState<Row | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  useEffect(() => {
    const abort = new AbortController(); setStatus(null)
    readApi<Row>(connection, `/api/projects/${projectId}/semantic-index/status`, abort.signal).then(setStatus).catch(() => {})
    return () => abort.abort()
  }, [connection, projectId, refresh])
  const previewVersion = useRef(0)
  useEffect(() => { previewVersion.current++; setPlan(null); setCleanupPlan(null); setError(''); setLoading(false) }, [projectId])
  async function preview() {
    const requestVersion = ++previewVersion.current
    setLoading(true); setError(''); setPlan(null)
    try { const value = await readApi<Row>(connection, `/api/projects/${projectId}/semantic-index/preview`, undefined, 60000); if (requestVersion === previewVersion.current) setPlan(value) }
    catch (e) { if (requestVersion === previewVersion.current) setError(e instanceof Error ? e.message : '无法预览语义索引。') }
    finally { if (requestVersion === previewVersion.current) setLoading(false) }
  }
  async function previewCleanup() {
    const requestVersion = ++previewVersion.current
    setLoading(true); setError(''); setCleanupPlan(null)
    try { const value = await readApi<Row>(connection, `/api/projects/${projectId}/semantic-index/cleanup-preview`, undefined, 60000); if (requestVersion === previewVersion.current) setCleanupPlan(value) }
    catch (e) { if (requestVersion === previewVersion.current) setError(e instanceof Error ? e.message : '无法预览清理。') }
    finally { if (requestVersion === previewVersion.current) setLoading(false) }
  }
  const sources = rows(plan?.pending_sources)
  return <div className="semantic-index-control">
    {status && <p role="status">当前版本 {shortCommit(status.commit)} · {status.lexical === 'ready' ? '词法索引可用' : '缺少词法索引'} · {status.semantic === 'published_unverified' ? '语义索引记录存在（未检查 Qdrant）' : status.semantic === 'invalid' ? '语义索引记录无效' : '语义索引待更新'} · {status.hybrid_enabled ? '混合检索已开启' : '混合检索未开启'}</p>}
    <p className="muted">先构建当前提交的本地词法索引，再预览要复用和发送的片段。未提交代码不会发送。</p>
    <div className="inline-actions"><button disabled={busy || loading} onClick={() => onCommand('index.build', { project_id: projectId })}>更新本地词法索引</button>
      <button disabled={busy || loading} onClick={() => { void preview() }}>{loading ? '正在预览…' : '预览语义更新'}</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    {plan && <div><p>版本 {shortCommit(plan.commit)} · 总片段 {String(plan.chunk_count)} · 可复用 {String(plan.reused_chunks)} · 新向量 {String(plan.new_vectors)}</p>
      <p>待发送 {String(plan.pending_characters)} 字符到 {String(plan.embedding_url)} · {String(plan.embedding_model)}。可能产生费用；不包含未提交内容。</p>
      {!plan.embedding_configured && <p className="error">尚未配置 Embedding 密钥，无法建立索引。</p>}
      <details><summary>查看待发送的 {sources.length} 个去重片段</summary>{sources.map((source, i) => {
        const evidence = source.evidence && typeof source.evidence === 'object' ? source.evidence as Row : {}
        return <details key={String(source.text_sha256 || i)}><summary>{String(evidence.path || '')} · 行 {String(evidence.start_line || '')}–{String(evidence.end_line || '')}</summary><pre className="source-code">{String(source.text || '')}</pre></details>
      })}</details>
      <button className="solid-button" disabled={busy || loading || !plan.embedding_configured || !plan.plan_sha256} onClick={() => {
        onCommand('index.build-semantic', { project_id: projectId, plan_sha256: plan.plan_sha256 }); setPlan(null)
      }}>确认并增量更新语义索引</button>
      <p className="muted">若版本、配置或缓存变化，需要重新预览；向量按内容共享，新格式只保留最近两份版本映射；旧格式集合不会自动删除。</p></div>}
    <div className="inline-actions"><button disabled={busy || loading} onClick={() => { void previewCleanup() }}>预览旧索引清理</button></div>
    {cleanupPlan && <div><p>计划清理无引用向量 {Array.isArray(cleanupPlan.pool_remove_keys) ? cleanupPlan.pool_remove_keys.length : 0} 条、缓存 {Array.isArray(cleanupPlan.cache_remove_keys) ? cleanupPlan.cache_remove_keys.length : 0} 条、旧词法索引 {rows(cleanupPlan.lexical_indexes).length} 个。</p>
      <p>保留当前版本、最近两个语义版本及最近两个词法版本；不会删除 Git、源码或旧格式 Qdrant 集合。清理缓存后将来可能需要重新付费生成向量。</p>
      {Boolean(cleanupPlan.legacy_lexical_indexes_protected) && <p>检测到旧格式语义索引：为保护历史读取，暂不清理任何旧词法索引。</p>}
      <details><summary>查看受影响版本和内容哈希</summary><pre>{JSON.stringify({ lexical_indexes: cleanupPlan.lexical_indexes, pool_keys: cleanupPlan.pool_remove_keys, cache_keys: cleanupPlan.cache_remove_keys }, null, 2)}</pre></details>
      <button className="solid-button" disabled={busy || loading || !cleanupPlan.plan_sha256} onClick={() => { onCommand('index.cleanup', { project_id: projectId, plan_sha256: cleanupPlan.plan_sha256 }); setCleanupPlan(null) }}>确认清理这些派生索引</button></div>}
  </div>
}

export function ProjectStatus({ connection, project, refresh, busy, onCommand, onCheck, checking, maintenance, checkError, commandError, initialSection }: {
  connection: Connection; project: Project; refresh: number; busy: boolean; onCommand(action: string, p: Row): void
  onCheck?(): void; checking?: boolean
  maintenance?: Row | null; checkError?: string; commandError?: string; initialSection?: string
}) {
  const [overview, setOverview] = useState<Row>({}), [documents, setDocuments] = useState<Row[]>([])
  const [path, setPath] = useState(''), [file, setFile] = useState<Row | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true)
  const [readingMore, setReadingMore] = useState(false)
  const [initialization, setInitialization] = useState<Row | null>(null)
  const [version, setVersion] = useState<Row | null>(null)
  const [requestedSection, setRequestedSection] = useState<{id: string; nonce: number} | null>(null)
  const activePath = useRef(path); activePath.current = path
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError(''); setVersion(null)
    Promise.all([readApi<Row>(connection, `/api/projects/${project.id}/overview`, abort.signal), readApi<Row>(connection, `/api/projects/${project.id}/documents/workspace`, abort.signal),
      readApi<Row>(connection, `/api/projects/${project.id}/initialization`, abort.signal).catch(() => null),
      readApi<Row>(connection, `/api/projects/${project.id}`, abort.signal).catch(() => null)])
      .then(([state, catalog, initial, details]) => {
        setInitialization(initial)
        setVersion((details?.version as Row | undefined) || null)
        setOverview(state); const docs = rows(catalog.documents); setDocuments(docs)
        setPath(previous => docs.some(d => d.path === previous) ? previous : String(docs.find(d => d.path === '.zest/docs/initialization/index.md')?.path || docs.find(d => d.path === '.zest/docs/index.md')?.path || docs[0]?.path || ''))
        if (rows(catalog.warnings).length) setError('文档目录达到当前显示容量，部分文档未列出。')
      }).catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '读取失败。') })
      .finally(() => { if (!abort.signal.aborted) setLoading(false) })
    return () => abort.abort()
  }, [connection, project.id, refresh])
  useEffect(() => {
    setFile(null)
    if (!path) return
    const abort = new AbortController()
    readApi<Row>(connection, `/api/projects/${project.id}/documents/read?${new URLSearchParams({ path })}`, abort.signal)
      .then(setFile).catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '读取失败。') })
    return () => abort.abort()
  }, [connection, project.id, path, refresh])
  async function more() {
    if (!file) return
    setReadingMore(true)
    try {
      const start = Number(file.end_line) + 1
      const next = await readApi<Row>(connection, `/api/projects/${project.id}/documents/read?${new URLSearchParams({ path, start_line: String(start), end_line: String(start + 199) })}`)
      if (activePath.current !== path) return
      if (next.content_sha256 !== file.content_sha256) { setError('文档已变化，请重新打开以读取最新内容。'); return }
      setFile({ ...next, content: String(file.content) + String(next.content) })
    } catch (e) { setError(e instanceof Error ? e.message : '读取失败。') }
    finally { setReadingMore(false) }
  }
  const git = overview.git as Row | undefined
  function followLink(target: string) {
    try {
      if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('/')) throw new Error()
      const destination = decodeURIComponent(new URL(target, `https://zest.local/${path}`).pathname.slice(1))
      if (!documents.some(doc => doc.path === destination)) throw new Error()
      setPath(destination); setError('')
    } catch { setError('此处只打开当前项目 .zest/docs/ 中的文档，请从文档列表选择。') }
  }
  return <div className="project-status-view"><SectionLayout label="项目状态栏目" initialSection={initialSection} requestedSection={requestedSection} sections={[
    { id: 'overview', title: '项目概览', description: '查看当前版本、更新进度与待处理事项。', content: <>
    <div className="project-status-summary"><strong>{loading ? '正在读取分支…' : String(git?.branch || '分支待核对')}</strong><span>{git ? shortCommit(git.head_commit) : '版本待核对'}</span><span>{loading ? '正在检查…' : !git ? '状态待核对' : git.dirty ? '有本地改动' : '工作区干净'}</span></div>
    {!loading && <VersionSelection version={version} />}
    {onCheck && <button className="update-check-button" aria-busy={Boolean(checking)} disabled={busy || checking} onClick={onCheck}><Icon name="refresh" size={16} />{checking ? '正在检查更新…' : '检查更新'}</button>}
    {onCheck && <MaintenanceFeedback value={maintenance} checking={checking} error={checkError} />}
    <MaintenanceActions connection={connection} value={maintenance || null} busy={busy || Boolean(checking)} onCommand={onCommand} />
    {commandError && <p className="error" role="alert">{commandError}</p>}
    {onCheck && <p className="muted">打开、切换项目或任务完成时自动检查，也可以手动检查。空闲时不定时扫描。检查本地 Git，其他成员的更新请先同步到本机。</p>}
    </> },
    { id: 'progress', title: '文档更新进度', description: '查看本轮文档和事实核对结果。', content: maintenance?.document_generation ? <DocumentProgress standalone value={maintenance.document_generation} onOpen={next => { setPath(next); setError(''); setRequestedSection({ id: 'documents', nonce: Date.now() }) }} /> : <p className="muted">暂无文档更新记录。</p> },
    { id: 'index', title: '语义索引增量更新', description: '管理当前版本的词法与语义索引。', content: <SemanticIndexControl connection={connection} projectId={project.id} refresh={refresh} busy={busy || Boolean(checking)} onCommand={onCommand} /> },
    { id: 'documents', title: '项目文档', description: '浏览工作区中的项目知识与资料。', content: <>
    <p className="dialog-description">这里是当前工作区的项目文档，包含尚未提交的修改。团队成员在提交、推送并同步后看到更新。</p>
    {error && <p className="error" role="alert">{error}</p>}
    {loading ? <p className="muted">正在读取项目状态…</p> : documents.length ? <div className="document-workspace">
      <div className="document-list" role="navigation" aria-label="项目文档">{documents.map(doc => <button key={String(doc.path)} title={String(doc.path)} className={path === doc.path ? 'selected' : ''} aria-current={path === doc.path ? 'page' : undefined} onClick={() => { setPath(String(doc.path)); setError('') }}>{String(doc.title || documentTitle(String(doc.path)))}</button>)}</div>
      <article className="document-reader"><div className="document-caption">{path.replace('.zest/docs/', '')}<span>工作区版本</span></div>{file ? <TextContent text={String(file.content)} onLink={followLink} /> : <p className="muted">正在读取文档…</p>}
        {Boolean(file?.more_after) && <button className="quiet-button" disabled={readingMore} onClick={more}>{readingMore ? '正在读取…' : '继续阅读'}</button>}</article>
    </div> : <div className="document-empty"><h3>把项目知识留在这里。</h3><p>建立 .zest/docs/，迁入已有文档，保留来源供你检查。</p><button className="solid-button" disabled={busy} onClick={() => onCommand('project.documents.init', { project_id: project.id })}>建立项目文档</button></div>}
    </> },
    ...(initialization?.stage === 'published' ? [{ id: 'initialization', title: '初始化记录与知识缺口', description: '查看初始化调查记录和待确认内容。', content: <>
      <p className="muted">这是初始化时的调查记录。后续版本更新请查看项目概览与当前项目文档，知识缺口不阻止正常提问。</p>
      <InitializationCard value={initialization} preview={null} busy={busy} onCommand={onCommand} onSettings={() => {}} onDocuments={() => setRequestedSection({ id: 'documents', nonce: Date.now() })} onDismiss={() => setRequestedSection({ id: 'overview', nonce: Date.now() })} />
    </> }] : []),
  ]} /></div>
}
