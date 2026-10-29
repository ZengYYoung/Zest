import { useEffect, useRef, useState } from 'react'
import { readApi, shortCommit, type Connection, type Project, type Row } from './api.ts'
import { rows } from './Conversation.tsx'
import { TextContent } from './TextContent.tsx'
import { Icon } from './Icons.tsx'
import { MaintenanceFeedback } from './MaintenanceFeedback.tsx'
import { MaintenanceActions } from './Maintenance.tsx'
import { InitializationCard } from './Initialization.tsx'
import { DocumentProgress } from './DocumentProgress.tsx'

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

export function ProjectStatus({ connection, project, refresh, busy, onCommand, onCheck, checking, maintenance, checkError, commandError }: {
  connection: Connection; project: Project; refresh: number; busy: boolean; onCommand(action: string, p: Row): void
  onCheck?(): void; checking?: boolean
  maintenance?: Row | null; checkError?: string; commandError?: string
}) {
  const [overview, setOverview] = useState<Row>({}), [documents, setDocuments] = useState<Row[]>([])
  const [path, setPath] = useState(''), [file, setFile] = useState<Row | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true)
  const [readingMore, setReadingMore] = useState(false)
  const [initialization, setInitialization] = useState<Row | null>(null), [knowledgeOpen, setKnowledgeOpen] = useState(false)
  const [version, setVersion] = useState<Row | null>(null)
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
  return <div className="project-status-view">
    <div className="project-status-summary"><strong>{loading ? '正在读取分支…' : String(git?.branch || '分支待核对')}</strong><span>{git ? shortCommit(git.head_commit) : '版本待核对'}</span><span>{loading ? '正在检查…' : !git ? '状态待核对' : git.dirty ? '有本地改动' : '工作区干净'}</span></div>
    {!loading && <VersionSelection version={version} />}
    {onCheck && <button className="update-check-button" aria-busy={Boolean(checking)} disabled={busy || checking} onClick={onCheck}><Icon name="refresh" size={16} />{checking ? '正在检查更新…' : '检查更新'}</button>}
    {onCheck && <MaintenanceFeedback value={maintenance} checking={checking} error={checkError} />}
    <MaintenanceActions connection={connection} value={maintenance || null} busy={busy || Boolean(checking)} onCommand={onCommand} />
    {commandError && <p className="error" role="alert">{commandError}</p>}
    <DocumentProgress value={maintenance?.document_generation} onOpen={next => { setPath(next); setError('') }} />
    {onCheck && <p className="muted">打开、切换项目或任务完成时自动检查，也可以手动检查。空闲时不定时扫描。检查本地 Git，其他成员的更新请先同步到本机。</p>}
    <p className="dialog-description">这里是当前工作区的项目文档，包含尚未提交的修改。团队成员在提交、推送并同步后看到更新。</p>
    {error && <p className="error" role="alert">{error}</p>}
    {initialization?.stage === 'published' && <details className="project-knowledge-record" open={knowledgeOpen} onToggle={event => setKnowledgeOpen(event.currentTarget.open)}><summary>初始化记录与知识缺口</summary>
      <p className="muted">这是初始化时的调查记录。后续版本更新请查看上方状态与当前项目文档，知识缺口不阻止正常提问。</p>
      <InitializationCard value={initialization} preview={null} busy={busy} onCommand={onCommand} onSettings={() => {}}
        onDocuments={() => setKnowledgeOpen(false)} onDismiss={() => setKnowledgeOpen(false)} />
    </details>}
    {loading ? <p className="muted">正在读取项目状态…</p> : documents.length ? <div className="document-workspace">
      <div className="document-list" role="navigation" aria-label="项目文档">{documents.map(doc => <button key={String(doc.path)} title={String(doc.path)} className={path === doc.path ? 'selected' : ''} aria-current={path === doc.path ? 'page' : undefined} onClick={() => { setPath(String(doc.path)); setError('') }}>{String(doc.title || documentTitle(String(doc.path)))}</button>)}</div>
      <article className="document-reader"><div className="document-caption">{path.replace('.zest/docs/', '')}<span>工作区版本</span></div>{file ? <TextContent text={String(file.content)} onLink={followLink} /> : <p className="muted">正在读取文档…</p>}
        {Boolean(file?.more_after) && <button className="quiet-button" disabled={readingMore} onClick={more}>{readingMore ? '正在读取…' : '继续阅读'}</button>}</article>
    </div> : <div className="document-empty"><h3>把项目知识留在这里。</h3><p>建立 .zest/docs/，迁入已有文档，保留来源供你检查。</p><button className="solid-button" disabled={busy} onClick={() => onCommand('project.documents.init', { project_id: project.id })}>建立项目文档</button></div>}
  </div>
}
