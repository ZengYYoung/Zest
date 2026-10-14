import { useEffect, useRef, useState, type FormEvent } from 'react'
import { projectName, readApi, shortCommit, statusLabels, validateConnection, type Connection, type Project, type Row } from './api.ts'
import { RecordDetail } from './RecordDetail.tsx'

type Tab = 'overview' | 'tasks' | 'repairs' | 'verifications' | 'knowledge' | 'releases'
const tabs: [Tab, string, string][] = [['overview', '项目概览', '◈'], ['tasks', '调查与建议', '◎'], ['repairs', '候选修改', '⌘'], ['verifications', '测试验证', '✓'], ['knowledge', '项目知识', '◇'], ['releases', '版本历史', '↺']]

function Status({ value }: { value: unknown }) {
  const key = String(value || 'unknown')
  return <span className={`status ${['passed', 'completed', 'applied', 'confirmed'].includes(key) ? 'good' : ['failed', 'error', 'invalidated'].includes(key) ? 'bad' : ''}`}>{statusLabels[key] || key}</span>
}

function JsonDetails({ value, title = '查看来源与完整记录' }: { value: unknown; title?: string }) {
  return <details className="json-details"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>
}

export function App() {
  const [connection, setConnection] = useState<Connection | null>(null)
  const [token, setToken] = useState('')
  const [port, setPort] = useState('8000')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [tab, setTab] = useState<Tab>('overview')
  const [data, setData] = useState<unknown>(null)
  const [detail, setDetail] = useState<unknown>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [filePath, setFilePath] = useState('README.md')
  const [file, setFile] = useState<Row | null>(null)
  const recordRequest = useRef<AbortController | null>(null)
  const fileRequest = useRef<AbortController | null>(null)
  const selected = projects.find(project => project.id === projectId)

  async function connect(event: FormEvent) {
    event.preventDefault(); setError(''); setLoading(true)
    try {
      const candidate = validateConnection(token, Number(port))
      const records = await readApi<Project[]>(candidate, '/api/projects')
      setProjects(records); setProjectId(records[0]?.id || ''); setConnection(candidate); setToken('')
    } catch (error) { setError(error instanceof Error ? error.message : '连接失败。') }
    finally { setLoading(false) }
  }

  function disconnect() {
    recordRequest.current?.abort(); fileRequest.current?.abort()
    setConnection(null); setToken(''); setProjects([]); setProjectId(''); setData(null); setDetail(null); setFile(null); setError('')
  }

  async function refreshRecords() {
    if (!connection) return
    setError('')
    try {
      const records = await readApi<Project[]>(connection, '/api/projects')
      setProjects(records)
      setProjectId(current => records.some(project => project.id === current) ? current : records[0]?.id || '')
      setRefresh(value => value + 1)
    } catch (error) { setError(error instanceof Error ? error.message : '刷新失败。') }
  }

  useEffect(() => {
    if (!connection || (!projectId && tab !== 'tasks')) return
    const controller = new AbortController()
    recordRequest.current?.abort(); fileRequest.current?.abort()
    setLoading(true); setError(''); setDetail(null); setFile(null); setData(null)
    const paths = { overview: `/api/projects/${projectId}/overview`, tasks: `/api/tasks?project_id=${projectId}`,
      repairs: `/api/projects/${projectId}/repairs`, verifications: `/api/projects/${projectId}/verifications`, knowledge: `/api/projects/${projectId}/knowledge`, releases: `/api/projects/${projectId}/releases` }
    readApi(connection, paths[tab], controller.signal).then(value => { setData(value); setLoading(false) }).catch(error => {
      if (error instanceof Error && error.name === 'AbortError') return
      setLoading(false); setError(error instanceof Error ? error.message : '读取失败。')
    })
    return () => controller.abort()
  }, [connection, projectId, tab, refresh])

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
      <div className="story-footer"><span className="dot" /> 本地连接 · 只读工作台</div>
    </div>
    <div className="connect-form"><div className="eyebrow">WELCOME BACK</div><h2>连接你的工作台</h2><p>使用本地 API 的独立访问令牌。</p>
      <form onSubmit={connect}><label>API 端口<input aria-label="API 端口" inputMode="numeric" value={port} onChange={event => setPort(event.target.value)} /></label>
        <label>Web 访问令牌<input aria-label="Web 访问令牌" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
        {error && <div className="error" role="alert">{error}</div>}<button className="primary" disabled={loading}>{loading ? '正在连接…' : '连接工作台 →'}</button>
      </form><p className="small">仅连接 127.0.0.1。令牌只保存在本次页面内存中，不写入本地存储；请勿使用模型 API 密钥。</p>
    </div>
  </div>

  const overview = data && !Array.isArray(data) ? data as Row : null
  const source = overview?.source as Row | undefined
  const git = overview?.git as Row | undefined
  const rows = Array.isArray(data) ? data as Row[] : []
  return <div className="workspace">
    <aside><div className="brand"><span className="brand-mark">Z</span>Zest</div><div className="sidebar-note">长期项目维护</div>
      <nav>{tabs.map(([key, label, icon]) => <button className={tab === key ? 'active' : ''} key={key} onClick={() => setTab(key)}><span>{icon}</span>{label}</button>)}</nav>
      <div className="sidebar-bottom"><span className="dot" /> 本地 API 已连接<small>127.0.0.1:{connection.port}</small><button onClick={disconnect}>断开连接</button></div>
    </aside>
    <main><header><div><div className="eyebrow">YOUR PROJECT, IN CONTEXT</div><h1>{tabs.find(([key]) => key === tab)?.[1]}</h1></div>
      <div className="toolbar"><span className="readonly">只读视图</span><button onClick={refreshRecords}>↻ 刷新</button></div></header>
      <div className="project-bar"><span className="project-icon">⌁</span><select aria-label="选择项目" value={projectId} onChange={event => setProjectId(event.target.value)}>
        {!projects.length && <option value="">尚未登记项目</option>}{projects.map(project => <option value={project.id} key={project.id}>{projectName(project)}</option>)}</select>
        {selected && <><span className="muted">{selected.mode === 'development' ? '开发模式' : '导入模式'}</span><code>{selected.root_path}</code></>}
      </div>
      {error && <div className="error" role="alert">{error}</div>}
      {loading ? <div className="empty">正在读取项目记录…</div> : !projects.length ? <div className="empty"><h3>从第一个项目开始</h3><p>先在 CLI 中创建或导入项目，再刷新工作台。</p><code>zest project import /path/to/project</code></div> : <>
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
        {tab !== 'overview' && <section className="panel"><div className="panel-heading"><h2>{tabs.find(([key]) => key === tab)?.[1]}</h2><span className="muted">{rows.length} 条记录</span></div>
          {!rows.length ? <div className="empty compact"><h3>还没有相关记录</h3><p>通过 CLI 完成调查、维护或知识确认后，记录会在这里展示。</p></div> : <div className="table-wrap"><table><thead><tr><th>记录</th><th>状态</th><th>类型 / 来源</th><th>创建时间</th><th /></tr></thead><tbody>
            {rows.map((row, index) => <tr key={String(row.id || row.task_id || index)}><td><strong title={String(row.title || row.task_id || row.id)}>{String(row.title || row.task_id || row.id).slice(0, row.title ? 36 : 12)}</strong></td><td><Status value={row.status} /></td>
              <td>{String(row.purpose === 'repair_proposal' ? '修复 / 开发建议' : row.purpose === 'question' ? '调查问答' : row.kind || row.operation || '候选修改')}</td><td>{row.created_at ? new Date(String(row.created_at)).toLocaleString('zh-CN', { hour12: false }) : '—'}</td><td><button onClick={() => openRecord(row)}>查看 →</button></td></tr>)}
          </tbody></table></div>}
          <div className="notice">这个工作台目前只展示记录。模型数据批准、具体修改、验证与版本选择仍通过 CLI 独立进行。</div></section>}
        {detail && tab !== 'overview' && <RecordDetail kind={tab} value={detail} onClose={() => { recordRequest.current?.abort(); setDetail(null) }} />}
      </>}
      <footer>Zest · 为一个项目保留连续的上下文<span>有来源 · 可审阅 · 有记录</span></footer>
    </main>
  </div>
}
