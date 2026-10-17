import { useEffect, useRef, useState, type FormEvent } from 'react'
import { defaultPort, projectName, readApi, shortCommit, statusLabels, validateConnection, type Connection, type Project, type Row } from './api.ts'
import { RecordDetail } from './RecordDetail.tsx'
import { ProjectStructure } from './ProjectStructure.tsx'
import { CognitionDetail } from './CognitionDetail.tsx'
import { WorkspaceActions } from './WorkspaceActions.tsx'

type Tab = 'overview' | 'structure' | 'cognition' | 'tasks' | 'repairs' | 'verifications' | 'knowledge' | 'releases' | 'evaluations' | 'resolutions'
const tabs: [Tab, string, string][] = [['overview', '项目概览', '◈'], ['structure', '源码地图', '⌁'], ['cognition', '项目认知', '◉'], ['tasks', '调查与建议', '◎'], ['repairs', '候选修改', '⌘'], ['verifications', '测试验证', '✓'], ['knowledge', '项目知识', '◇'], ['releases', '版本历史', '↺'], ['evaluations', '独立评分', '◷'], ['resolutions', '补查结案', '◇']]

function Status({ value }: { value: unknown }) {
  const key = String(value || 'unknown')
  return <span className={`status ${['passed', 'completed', 'applied', 'confirmed'].includes(key) ? 'good' : ['failed', 'error', 'invalidated'].includes(key) ? 'bad' : ''}`}>{statusLabels[key] || key}</span>
}

function JsonDetails({ value, title = '查看来源与完整记录' }: { value: unknown; title?: string }) {
  return <details className="json-details"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>
}

export function App() {
  const [connection, setConnection] = useState<Connection | null>(null)
  const [port, setPort] = useState(String(defaultPort()))
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [tab, setTab] = useState<Tab>('overview')
  const [data, setData] = useState<unknown>(null)
  const [detail, setDetail] = useState<unknown>(null)
  const [resultSelection, setResultSelection] = useState<{ tab: Tab; id: string } | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [filePath, setFilePath] = useState('README.md')
  const [file, setFile] = useState<Row | null>(null)
  const recordRequest = useRef<AbortController | null>(null)
  const fileRequest = useRef<AbortController | null>(null)
  const viewRef = useRef({ tab, projectId, detail })
  viewRef.current = { tab, projectId, detail }
  const selected = projects.find(project => project.id === projectId)

  async function connect(event?: FormEvent) {
    event?.preventDefault(); setError(''); setLoading(true)
    try {
      const candidate = validateConnection(Number(port))
      const records = await readApi<Project[]>(candidate, '/api/projects')
      setProjects(records); setProjectId(records[0]?.id || ''); setConnection(candidate)
    } catch (error) { setError(error instanceof Error ? error.message : '连接失败。') }
    finally { setLoading(false) }
  }

  useEffect(() => { void connect() }, [])

  function disconnect() {
    recordRequest.current?.abort(); fileRequest.current?.abort()
    setConnection(null); setProjects([]); setProjectId(''); setData(null); setDetail(null); setFile(null); setError('')
  }

  async function refreshRecords() {
    if (!connection) return
    setError('')
    try {
      const records = await readApi<Project[]>(connection, '/api/projects')
      setProjects(records)
      setProjectId(current => records.some(project => project.id === current) ? current : records[0]?.id || '')
      setRefresh(value => value + 1)
      const view = viewRef.current
      const current = view.detail as Row | null
      const id = current?.task_id || current?.id
      if (id && view.tab !== 'overview' && view.tab !== 'structure') {
        const updated = await readApi(connection, `/api/${view.tab}/${id}`)
        const latest = viewRef.current
        const latestRecord = latest.detail as Row | null
        if (latest.tab === view.tab && latest.projectId === view.projectId && (latestRecord?.task_id || latestRecord?.id) === id) setDetail(updated)
      }
    } catch (error) { setError(error instanceof Error ? error.message : '刷新失败。') }
  }

  useEffect(() => {
    if (!connection || (!projectId && tab !== 'tasks')) return
    const controller = new AbortController()
    recordRequest.current?.abort(); fileRequest.current?.abort()
    setLoading(true); setError(''); setFile(null); setData(null)
    const paths = { overview: `/api/projects/${projectId}/overview`, structure: `/api/projects/${projectId}/structure`, tasks: `/api/tasks?project_id=${projectId}`,
      cognition: `/api/projects/${projectId}/cognition`, repairs: `/api/projects/${projectId}/repairs`, verifications: `/api/projects/${projectId}/verifications`, knowledge: `/api/projects/${projectId}/knowledge`, releases: `/api/projects/${projectId}/releases`, evaluations: `/api/projects/${projectId}/evaluations`, resolutions: `/api/projects/${projectId}/resolutions` }
    readApi(connection, paths[tab], controller.signal, tab === 'structure' ? 60000 : 15000).then(value => { setData(value); setLoading(false) }).catch(error => {
      if (error instanceof Error && error.name === 'AbortError') return
      setLoading(false); setError(error instanceof Error ? error.message : '读取失败。')
    })
    return () => controller.abort()
  }, [connection, projectId, tab, refresh])

  useEffect(() => { setDetail(null) }, [projectId, tab])

  useEffect(() => {
    if (!connection || !detail || tab !== 'cognition') return
    const row = detail as Row
    if (!['queued', 'planning', 'investigating', 'reviewing', 'awaiting_context_approval'].includes(String(row.status))) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      readApi(connection, `/api/cognition/${row.id}`, controller.signal).then(setDetail).catch(() => {})
    }, 2000)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [connection, detail, tab])

  async function openOperationResult(action: string, result: Row) {
    await refreshRecords()
    setError('')
    const project = result.project as Project | undefined
    if (action.startsWith('project.')) { setProjectId(project?.id || String(result.id)); setTab('overview'); return }
    if (action === 'release.capture') result = result.note as Row
    if (typeof (result.plan as Row | undefined)?.project_id === 'string') setProjectId(String((result.plan as Row).project_id))
    if (typeof result.project_id === 'string') setProjectId(result.project_id)
    let target: Tab | undefined
    if (action.startsWith('task.')) target = 'tasks'
    if (action.startsWith('cognition.')) target = 'cognition'
    if (action.startsWith('repair.')) target = 'repairs'
    if (action.startsWith('verification.')) target = 'verifications'
    if (action.startsWith('release.')) target = action === 'release.capture' ? 'knowledge' : 'releases'
    if (action.startsWith('evaluation.')) target = 'evaluations'
    if (action.startsWith('resolution.')) target = 'resolutions'
    if (action.startsWith('knowledge.')) target = 'knowledge'
    if (target) {
      setTab(target)
      const id = result.task_id || result.evaluation_id || result.id
      if (id) setResultSelection({ tab: target, id: String(id) })
    }
  }

  useEffect(() => {
    if (!connection || !resultSelection || resultSelection.tab !== tab) return
    const controller = new AbortController()
    const segment = tab === 'knowledge' ? 'knowledge' : tab
    readApi(connection, `/api/${segment}/${resultSelection.id}`, controller.signal).then(setDetail).catch(e => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '读取失败')
    })
    return () => controller.abort()
  }, [connection, resultSelection, tab])

  async function openRecord(row: Row) {
    if (!connection) return
    const id = String(row.task_id || row.id)
    if (tab === 'knowledge') { setDetail(row); return }
    recordRequest.current?.abort()
    const controller = new AbortController(); recordRequest.current = controller
    const segment = tab === 'releases' ? 'releases' : tab
    try { setDetail(await readApi(connection, `/api/${segment}/${encodeURIComponent(id)}`, controller.signal)); setError('') }
    catch (error) { if (error instanceof Error && error.name === 'AbortError') return; setError(error instanceof Error ? error.message : '读取失败。') }
  }

  async function readFile(event: FormEvent) {
    event.preventDefault()
    if (!connection || !projectId) return
    fileRequest.current?.abort()
    const controller = new AbortController(); fileRequest.current = controller
    try {
      const query = new URLSearchParams({ path: filePath, start_line: '1', end_line: '80' })
      setFile(await readApi<Row>(connection, `/api/projects/${projectId}/file?${query}`, controller.signal)); setError('')
    } catch (error) { if (error instanceof Error && error.name === 'AbortError') return; setError(error instanceof Error ? error.message : '文件读取失败。') }
  }

  if (!connection) return <div className="connect-screen">
    <div className="connect-story"><div className="brand"><span className="brand-mark">Z</span>Zest</div>
      <div className="eyebrow">PROJECT MAINTENANCE WORKSPACE</div><h1>理解一个项目，<br />陪它持续成长。</h1>
      <p>把代码、知识与维护记录放在同一个上下文中。每一个建议有来源，每一次修改有批准，每一个版本有记录。</p>
      <div className="story-tags"><span>项目上下文</span><span>证据与知识</span><span>维护历史</span></div>
      <div className="story-footer"><span className="dot" /> 本地项目工作台</div>
    </div>
    <div className="connect-form"><div className="eyebrow">WELCOME BACK</div><h2>连接你的工作台</h2><p>无需登录，连接本机服务即可开始试用。</p>
      <form onSubmit={connect}><label>API 端口<input aria-label="API 端口" inputMode="numeric" value={port} onChange={event => setPort(event.target.value)} /></label>
        {error && <div className="error" role="alert">{error}</div>}<button className="primary" disabled={loading}>{loading ? '正在连接…' : '连接工作台 →'}</button>
      </form><p className="small">启动命令：uv run zest web serve。首次使用先构建 frontend，即可由 API 服务直接打开网页。</p>
    </div>
  </div>

  const overview = data && !Array.isArray(data) ? data as Row : null
  const source = overview?.source as Row | undefined
  const git = overview?.git as Row | undefined
  const rows = Array.isArray(data) ? data as Row[] : []
  return <div className="workspace">
    <aside><div className="brand"><span className="brand-mark">Z</span>Zest</div><div className="sidebar-note">长期项目维护</div>
      <nav>{tabs.map(([key, label, icon]) => <button className={tab === key ? 'active' : ''} key={key} onClick={() => { setResultSelection(null); setTab(key) }}><span>{icon}</span>{label}</button>)}</nav>
      <div className="sidebar-bottom"><span className="dot" /> 本地 API 已连接<small>127.0.0.1:{connection.port}</small><button onClick={disconnect}>断开连接</button></div>
    </aside>
    <main><header><div><div className="eyebrow">YOUR PROJECT, IN CONTEXT</div><h1>{tabs.find(([key]) => key === tab)?.[1]}</h1></div>
      <div className="toolbar"><span className="readonly">本地试用</span><button onClick={refreshRecords}>↻ 刷新</button></div></header>
      <div className="project-bar"><span className="project-icon">⌁</span><select aria-label="选择项目" value={projectId} onChange={event => { setResultSelection(null); setProjectId(event.target.value) }}>
        {!projects.length && <option value="">尚未登记项目</option>}{projects.map(project => <option value={project.id} key={project.id}>{projectName(project)}</option>)}</select>
        {selected && <><span className="muted">{selected.mode === 'development' ? '开发模式' : '导入模式'}</span><code>{selected.root_path}</code></>}
      </div>
      {error && <div className="error" role="alert">{error}</div>}
      <WorkspaceActions connection={connection} projectId={projectId} tab={tab} detail={detail} onRefresh={refreshRecords} onResult={openOperationResult} />
      {loading ? <div className="empty">正在读取项目记录…</div> : !projects.length ? <div className="empty"><h3>从第一个项目开始</h3><p>在上方“接入项目”中导入本机仓库或创建新项目。</p></div> : <>
        {tab === 'overview' && overview && <>
          <div className="cards"><div><span>当前选定源码</span><strong>{shortCommit(source?.commit)}</strong><small>每一条证据都保留版本来源</small></div>
            <div><span>工作区 HEAD</span><strong>{shortCommit(git?.head_commit)}</strong><small>{git?.dirty ? '包含未提交工作 · 未纳入读取' : '工作区干净'}</small></div>
            <div><span>已识别文档</span><strong>{Array.isArray(overview.documents) ? overview.documents.length : 0}<em>份</em></strong><small>已提交版本的文档位置</small></div></div>
          <section className="panel"><div className="panel-heading"><h2>项目证据</h2><span className="muted">先读原文，再判断结论</span></div>
            <form className="file-form" onSubmit={readFile}><input aria-label="源码相对路径" value={filePath} onChange={event => setFilePath(event.target.value)} placeholder="README.md 或 src/app.py" /><button>读取前 80 行</button></form>
            {file && <><div className="file-caption"><code>{String((file.evidence as Row)?.path || '')}</code><span>{shortCommit((file.evidence as Row)?.commit)}</span></div><pre className="source-code">{String(file.content || '')}</pre></>}
            <JsonDetails value={overview} title="查看概览来源、声明与提示" /></section>
          <div className="notice">当前选定源码与工作区分别报告。这里的版本选择不代表运行服务已经发布。</div>
        </>}
        {tab === 'structure' && data !== null && <ProjectStructure key={projectId} value={data} />}
        {tab !== 'overview' && tab !== 'structure' && <section className="panel"><div className="panel-heading"><h2>{tabs.find(([key]) => key === tab)?.[1]}</h2><span className="muted">{rows.length} 条记录</span></div>
          {!rows.length ? <div className="empty compact"><h3>还没有相关记录</h3><p>使用上方操作入口创建记录；准备与执行进度会保存在网页操作中。</p></div> : <div className="table-wrap"><table><thead><tr><th>记录</th><th>状态</th><th>类型 / 来源</th><th>创建时间</th><th /></tr></thead><tbody>
            {rows.map((row, index) => <tr key={String(row.id || row.task_id || index)}><td><strong title={String(row.title || row.task_id || row.id)}>{String(row.title || row.task_id || row.id).slice(0, row.title ? 36 : 12)}</strong></td><td><Status value={row.status} /></td>
              <td>{String(tab === 'cognition' ? `项目理解 · ${shortCommit(row.commit)} · ${Number(row.call_count || 0)} 次调用` : row.purpose === 'repair_proposal' ? '修复 / 开发建议' : row.purpose === 'question' ? '调查问答' : row.kind || row.operation || '候选修改')}</td><td>{row.created_at ? new Date(String(row.created_at)).toLocaleString('zh-CN', { hour12: false }) : '—'}</td><td><button onClick={() => openRecord(row)}>查看 →</button></td></tr>)}
          </tbody></table></div>}
          <div className="notice">打开记录即可审阅和继续操作；模型请求、候选修改、测试和源码选择分别批准。</div></section>}
        {detail && tab === 'cognition' && <CognitionDetail value={detail} onClose={() => { recordRequest.current?.abort(); setDetail(null) }} />}
        {detail && tab !== 'overview' && tab !== 'structure' && tab !== 'cognition' && tab !== 'evaluations' && tab !== 'resolutions' && <RecordDetail kind={tab} value={detail} onClose={() => { recordRequest.current?.abort(); setDetail(null) }} />}
        {detail && (tab === 'evaluations' || tab === 'resolutions') && <section className="panel"><div className="panel-heading"><h2>{tab === 'evaluations' ? '评分结果与未评项' : '逐项结案建议'}</h2><button onClick={() => setDetail(null)}>关闭</button></div><pre className="approval-preview">{JSON.stringify(detail, null, 2)}</pre></section>}
      </>}
      <footer>Zest · 为一个项目保留连续的上下文<span>有来源 · 可审阅 · 有记录</span></footer>
    </main>
  </div>
}
