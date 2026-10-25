import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Icon } from './Icons.tsx'
import { ModelConnection } from './ModelConnection.tsx'
import { ApiError, prepareCommand, readApi, writeApi, statusLabels, type Connection, type Row, type WebCommand } from './api.ts'

export type View = 'overview' | 'structure' | 'cognition' | 'tasks' | 'repairs' | 'verifications' | 'knowledge' | 'releases' | 'evaluations' | 'resolutions' | 'model'
const obj = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const list = (value: unknown): Row[] => Array.isArray(value) ? value.map(obj) : []
const operationLabels: Record<string, string> = { 'task.create': '准备问题与来源', 'task.decide': '执行已审阅的问答', 'model.prepare': '准备模型连接测试', 'model.decide': '真实模型连接测试', 'project.import': '导入项目', 'project.init': '创建项目', 'cognition.create': '准备项目认知', 'cognition.decide': '后台项目调查', 'knowledge.add': '保存知识草稿' }
const lines = (value: string) => value.split('\n').map(v => v.trim()).filter(Boolean)

export function WorkspaceActions({ connection, projectId, tab, detail, onResult, onRefresh, conversationVersion = 0 }: {
  connection: Connection; projectId: string; tab: View; detail: unknown
  onResult(action: string, result: Row): void; onRefresh(): void; conversationVersion?: number
}) {
  const [question, setQuestion] = useState('')
  const [taskPurpose, setTaskPurpose] = useState('question')
  useEffect(() => { setQuestion(''); setTaskPurpose('question') }, [projectId, conversationVersion])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [operation, setOperation] = useState<Row | null>(null)
  const [history, setHistory] = useState<Row[]>([])
  const [pending, setPending] = useState<WebCommand | null>(null)
  const [recoveryId, setRecoveryId] = useState('')
  const inFlight = useRef(false)
  const storageKey = `zest-web-pending-${connection.port}`

  useEffect(() => {
    try {
      const saved = localStorage.getItem(storageKey) || ''
      if (/^[a-f0-9-]{36}$/.test(saved)) setRecoveryId(saved)
    } catch { /* An unavailable local store must not block the console. */ }
    const controller = new AbortController()
    readApi<Row[]>(connection, '/api/operations', controller.signal).then(setHistory).catch(() => {})
    return () => controller.abort()
  }, [connection, storageKey])

  async function recoverReceipt() {
    setBusy(true); setError('')
    try {
      setOperation(await readApi<Row>(connection, `/api/operations/${recoveryId}`))
      setRecoveryId(''); localStorage.removeItem(storageKey)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setRecoveryId(''); localStorage.removeItem(storageKey); setError('该请求未保存，可重新准备操作。')
      } else setError(e instanceof Error ? e.message : '核对失败')
    } finally { setBusy(false) }
  }

  const operationId = String(operation?.id || '')
  const active = operation?.status === 'queued' || operation?.status === 'running'
  useEffect(() => {
    if (!operationId || !active) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const record = await readApi<Row>(connection, `/api/operations/${operationId}`, controller.signal)
        setOperation(record)
        if (record.status === 'queued' || record.status === 'running') timer = setTimeout(poll, 1500)
        else {
          onRefresh()
          void readApi<Row[]>(connection, '/api/operations').then(setHistory).catch(() => {})
        }
      } catch (e) {
        if (!controller.signal.aborted) { setError(e instanceof Error ? e.message : '读取失败'); timer = setTimeout(poll, 4000) }
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
    // onRefresh intentionally excluded: polling depends only on operation identity.
  }, [connection, operationId, active])

  async function submit(action: string, parameters: Row, retry?: WebCommand) {
    if (inFlight.current || active || (pending && !retry)) return
    inFlight.current = true; setBusy(true); setError('')
    const command = retry || prepareCommand(action, parameters)
    // Persist only the UUID before the first POST; keep all input text in memory.
    setPending(command)
    try { localStorage.setItem(storageKey, command.request_id) } catch { /* In-memory identity remains available. */ }
    try {
      const record = await writeApi<Row>(connection, '/api/operations', command)
      setOperation(record); setPending(null)
      try { localStorage.removeItem(storageKey) } catch { /* Optional storage. */ }
      if (record.status !== 'queued' && record.status !== 'running') onRefresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交失败')
      if (e instanceof ApiError && [400, 403, 413, 422].includes(e.status)) {
        setPending(null)
        try { localStorage.removeItem(storageKey) } catch { /* Optional storage. */ }
      }
    }
    finally { inFlight.current = false; setBusy(false) }
  }

  const disabled = busy || active || Boolean(pending) || Boolean(recoveryId)
  const record = obj(detail)
  const id = String(record.task_id || record.id || '')
  const fields = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); return new FormData(event.currentTarget) }
  return <>
    <section className={`panel action-panel ${tab === 'tasks' ? 'conversation-actions' : ''} ${tab === 'model' ? 'model-actions' : ''}`}>
      {tab !== 'tasks' && tab !== 'model' && <div className="panel-heading"><h2>{tab === 'overview' ? '项目工作区' : '工作流操作'}</h2><span className="muted">准备 · 审阅 · 批准</span></div>}
      {tab === 'model' && <ModelConnection connection={connection} disabled={disabled} refresh={history.length}
        check={String(operation?.action || '').startsWith('model.') && operation?.result ? obj(operation.result) : null} onCommand={(a, p) => submit(a, p)} />}
      {tab === 'tasks' && <div className="conversation-welcome"><div className="welcome-symbol"><Icon name="tasks" size={27} /></div><div className="section-kicker">YOUR PROJECT, UNDERSTOOD</div><h2>接下来，想了解什么？</h2><p>理解代码，梳理思路，或者一起完成下一次修改。</p></div>}
      {tab === 'overview' && <details><summary>接入项目</summary><form className="action-form" onSubmit={event => {
        const f = fields(event), mode = String(f.get('mode'))
        void submit(`project.${mode}`, { path: f.get('path'), ...(mode === 'init' ? { name: f.get('name'), goal: f.get('goal'), ...(f.get('author_name') || f.get('author_email') ? { author_name: f.get('author_name'), author_email: f.get('author_email') } : {}) } : {}) })
      }}><label>接入方式<select name="mode"><option value="import">导入已有 Git 项目</option><option value="init">创建新项目</option></select></label>
        <label>本机绝对路径<input name="path" required placeholder="/absolute/path/to/project" /></label>
        <label>新项目名称（创建时填写）<input name="name" maxLength={100} /></label>
        <label>新项目目标（创建时填写）<textarea name="goal" maxLength={4000} /></label>
        <label>Git 作者姓名（可选，与邮箱同时填写）<input name="author_name" maxLength={200} /></label>
        <label>Git 作者邮箱（可选）<input name="author_email" maxLength={200} /></label>
        <p className="review-note">接入会建立项目 .zest 资源目录；创建新项目会写入 Git 基线。准备完成后可在网页审阅调查资料。</p>
        <button disabled={disabled}>接入项目</button></form></details>}
      {projectId && tab === 'tasks' && <>
        <form key={`${projectId}-${conversationVersion}`} className="composer" onSubmit={event => {
          const f = fields(event)
          void submit('task.create', { project_id: projectId, question: f.get('question'), purpose: taskPurpose,
            files: lines(String(f.get('files') || '')), new_files: taskPurpose === 'repair_proposal' ? lines(String(f.get('new_files') || '')) : [] })
        }}><label className="sr-only" htmlFor="question">问题或需求</label><textarea id="question" name="question" required maxLength={4000} value={question} onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !disabled && question.trim()) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} placeholder="问问这个项目，或者描述你想做的修改…" />
          <details className="composer-sources"><summary><Icon name="plus" size={15} /> 添加上下文与文件范围</summary><div className="action-form">
            <label>源码路径（可选，每行一个；修改建议需明确范围）<textarea name="files" placeholder="src/app.py" /></label>
            {taskPurpose === 'repair_proposal' && <label>拟新建路径（每行一个）<textarea name="new_files" /></label>}
          </div></details>
          <div className="composer-footer"><label className="composer-mode"><Icon name="tasks" size={15} /><span className="sr-only">任务类型</span><select name="purpose" value={taskPurpose} onChange={e => setTaskPurpose(e.target.value)}><option value="question">项目问答</option><option value="repair_proposal">修复 / 开发建议</option></select></label>
            <span className="composer-hint">发送前可审阅来源</span><button className="send-button" disabled={disabled || !question.trim()} aria-label="准备请求并审阅" title="准备请求并审阅"><Icon name="arrow" size={19} /></button></div>
        </form><div className="prompt-suggestions">{['梳理项目的核心流程', '这个项目如何启动和测试？', '帮我定位一个问题'].map(text => <button key={text} onClick={() => setQuestion(text)}>{text}</button>)}</div>
      </>}
      {projectId && tab === 'cognition' && <div className="action-form"><p>先生成完整本地资料包，审阅模型、来源和预算后再批准后台调查。</p>
        <div className="action-buttons"><button disabled={disabled} onClick={() => submit('cognition.create', { project_id: projectId })}>准备项目认知</button>
          <button disabled={disabled} onClick={() => submit('cognition.refresh', { project_id: projectId })}>准备版本增量调查</button></div>
        <ReadButton connection={connection} path={`/api/projects/${projectId}/refresh-preview`} label="查看版本变化" />
      </div>}
      {projectId && tab === 'evaluations' && <form className="action-form" onSubmit={event => {
        const f = fields(event)
        try { void submit('evaluation.create', { id: f.get('id'), criteria: JSON.parse(String(f.get('criteria'))), max_calls: Number(f.get('max_calls')) }) }
        catch { setError('评分参考需为有效 JSON，格式见 Web 使用说明。') }
      }}><label>认知记录完整 ID<input name="id" required /></label><label>独立评分参考（JSON）<textarea name="criteria" required placeholder="使用项目独立参考与证据位置，不把待评答案当参考。" /></label>
        <label>调用预算<input name="max_calls" type="number" min={1} max={128} defaultValue={32} required /></label><button disabled={disabled}>准备独立评分包</button></form>}
      {projectId && tab === 'resolutions' && <form className="action-form" onSubmit={event => {
        const f = fields(event); void submit('resolution.create', { id: f.get('id') })
      }}><label>定向补查记录完整 ID<input name="id" required /></label><button disabled={disabled}>准备补查结案对照</button></form>}
      {projectId && tab === 'releases' && <details><summary>导出与核验管理源码</summary><form className="action-form" onSubmit={event => {
        const f = fields(event); void submit('release.export', { project_id: projectId, destination: f.get('destination') })
      }}><label>新导出目录绝对路径<input name="destination" required /></label><button disabled={disabled}>导出当前管理源码</button></form>
        <form className="action-form" onSubmit={event => { const f = fields(event); void submit('release.verify-export', { path: f.get('path') }) }}>
          <label>已有导出目录绝对路径<input name="path" required /></label><button disabled={disabled}>核验导出清单</button></form></details>}
      {projectId && tab === 'knowledge'  && <form className="action-form" onSubmit={event => {
        const f = fields(event); void submit('knowledge.add', { project_id: projectId, title: f.get('title'), body: f.get('body'), kind: f.get('kind'), scope: f.get('scope') })
      }}><label>标题<input name="title" required maxLength={200} /></label><label>内容<textarea name="body" required maxLength={8000} /></label>
        <label>类型<select name="kind"><option value="decision">设计决策 / 目标</option><option value="constraint">约束</option><option value="maintenance">维护记录</option></select></label>
        <label>适用范围<select name="scope"><option value="snapshot">当前选定版本</option><option value="project">整个项目</option></select></label>
        <button disabled={disabled}>保存知识草稿</button></form>}
      {projectId && tab === 'overview' && <div className="action-buttons"><ReadButton connection={connection} path={`/api/projects/${projectId}/context`} label="查看项目管理总览" />
        <button disabled={disabled} onClick={() => submit('index.build', { project_id: projectId })}>构建词法索引</button></div>}
      {['structure', 'repairs', 'verifications', 'releases'].includes(tab) && <p className="review-note">打开记录后可审阅并继续下一步。候选修改由修复 / 开发建议生成。</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {recoveryId && <div className="notice">有一条请求尚待核对：<code>{recoveryId}</code><button disabled={busy} onClick={recoverReceipt}>核对原请求回执</button></div>}
      {pending && <div className="notice">请求身份：<code>{pending.request_id}</code><p>可用同一身份核对回执；服务端不会重复执行相同请求。</p>
        <button disabled={busy} onClick={() => submit(pending.action, pending.parameters, pending)}>按原身份提交 / 核对回执</button></div>}
      {operation && <div className="operation" aria-live="polite"><p><span className="operation-indicator" />{statusLabels[String(operation.status)] || String(operation.status)}<span className="operation-name">{operationLabels[String(operation.action)] || String(operation.action)}</span></p>
        <details className="request-identity"><summary>请求身份</summary><code>{operationId}</code></details>
        {Boolean(operation.error) && <p className="error">{String(obj(operation.error).message)}</p>}
        {operation.status === 'queued' && <button disabled={busy} onClick={async () => {
          setBusy(true)
          try { await writeApi(connection, `/api/operations/${operationId}/start`, {}); setError('') }
          catch (e) { setError(e instanceof Error ? e.message : '启动失败') }
          finally { setBusy(false) }
        }}>启动已保存的排队操作</button>}
        {operation.status === 'completed' && <><button onClick={() => onResult(String(operation.action), obj(operation.result))}>打开结果 / 审阅下一步</button>
          <details className="json-details"><summary>本次操作完整结果</summary><pre>{JSON.stringify(operation.result, null, 2)}</pre></details></>}
      </div>}
      <details className="json-details"><summary onClick={() => readApi<Row[]>(connection, '/api/operations').then(setHistory).catch(e => setError(String(e)))}>最近网页操作</summary>
        {history.map(item => <div className="operation" key={String(item.id)}><span>{String(item.action)} · {statusLabels[String(item.status)] || String(item.status)}</span>
          <button onClick={async () => {
            try { setOperation(await readApi<Row>(connection, `/api/operations/${item.id}`)); setError('') }
            catch (e) { setError(e instanceof Error ? e.message : '读取失败') }
          }}>查看回执</button></div>)}
      </details>
    </section>
    {id && tab !== 'model' && <RecordActions key={`${tab}-${id}-${String(record.status)}`} connection={connection} tab={tab} record={record} disabled={disabled}
      submit={(action, p) => submit(action, p)} />}
  </>
}

function ReadButton({ connection, path, label }: { connection: Connection; path: string; label: string }) {
  const [value, setValue] = useState<unknown>(null), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  useEffect(() => { setValue(null); setError('') }, [path])
  return <div><button disabled={loading} onClick={async () => {
    setLoading(true); setError('')
    try { setValue(await readApi(connection, path, undefined, 60000)) }
    catch (e) { setError(e instanceof Error ? e.message : '读取失败') }
    finally { setLoading(false) }
  }}>{loading ? '读取中…' : label}</button>{error && <p className="error">{error}</p>}{value !== null && <pre className="source-code">{JSON.stringify(value, null, 2)}</pre>}</div>
}

export function RecordActions({ connection, tab, record, disabled, submit }: {
  connection: Connection; tab: View; record: Row; disabled: boolean; submit(action: string, parameters: Row): void
}) {
  const [preview, setPreview] = useState<Row | null>(null), [consent, setConsent] = useState(false)
  const [error, setError] = useState(''), [reading, setReading] = useState(false), [reason, setReason] = useState('')
  const [extended, setExtended] = useState(false)
  const id = String(record.task_id || record.id)
  const segment = tab === 'tasks' ? 'task' : tab === 'repairs' ? 'repair' : tab === 'verifications' ? 'verification' : tab === 'releases' ? 'release' : tab === 'evaluations' ? 'evaluation' : tab === 'resolutions' ? 'resolution' : tab
  const pending = record.status === 'awaiting_approval'
  async function loadPreview() {
    setReading(true); setError(''); setConsent(false); setExtended(false)
    try {
      setPreview(await readApi<Row>(connection, `/api/${tab}/${id}${['cognition', 'tasks', 'evaluations', 'resolutions'].includes(tab) ? '/preview' : ''}`, undefined, 60000))
    } catch (e) { setError(e instanceof Error ? e.message : '读取失败') }
    finally { setReading(false) }
  }
  const sha256 = preview?.approval_sha256 || preview?.payload_sha256 || preview?.plan_sha256
  return <section className="panel action-panel"><h2>记录操作 · {id.slice(0, 12)}</h2>
    {pending && <><button disabled={reading || disabled} onClick={loadPreview}>{reading ? '读取中…' : '审阅完整请求 / 执行计划'}</button>
      {preview && <><p>批准身份：<code>{String(sha256)}</code></p><pre className="approval-preview">{JSON.stringify(preview, null, 2)}</pre>
        <label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我已审阅完整内容，批准本次{tab === 'tasks' || tab === 'cognition' ? '模型请求和预算' : '操作计划'}</label>
        {(tab === 'evaluations' || tab === 'resolutions') && (Boolean(preview.requires_extension) || list(preview.extended_requests).length > 0) && <label className="consent"><input type="checkbox" checked={extended} onChange={e => setExtended(e.target.checked)} />批准预览列出的精确扩展请求</label>}
        <button className="primary" disabled={disabled || !consent || !sha256 || ((tab === 'evaluations' || tab === 'resolutions') && (Boolean(preview.requires_extension) || list(preview.extended_requests).length > 0) && !extended)} onClick={() => submit(`${segment}.decide`, { id, approved: true, sha256, ...(['evaluations', 'resolutions'].includes(tab) ? { allow_extended_requests: extended } : {}) })}>批准本次操作</button></>}
      <button disabled={disabled} onClick={() => submit(`${segment}.decide`, { id, approved: false })}>拒绝本次操作</button></>}
    {(tab === 'evaluations' || tab === 'resolutions') && <>
      {record.status === 'queued' && <button disabled={disabled} onClick={() => submit(`${segment}.run`, { id })}>执行已批准的任务</button>}
      {['running', 'partial', 'pending_execution'].includes(String(record.status)) && <button disabled={disabled} onClick={() => submit(`${segment}.recover`, { id })}>核对中断的任务</button>}
      {tab === 'evaluations' && <ReadButton connection={connection} path={`/api/evaluations/${id}/report`} label="查看评分汇总及未评项" />}
      {tab === 'resolutions' && record.status === 'proposed' && !record.adoption && <><ReadButton connection={connection} path={`/api/resolutions/${id}/preview`} label="审阅结案对照与效果" />
        <label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />已核对逐项建议，确认采纳完整效果清单</label>
        <button disabled={disabled || !consent || !record.proposal_sha256} onClick={() => submit('resolution.adopt', { id, sha256: record.proposal_sha256 })}>采纳本次结案建议</button></>}
    </>}
    {tab === 'knowledge' && <>{record.status === 'draft' && <><pre className="approval-preview">{String(record.body)}</pre>
      <label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />确认这条知识及其适用范围</label>
      <button disabled={disabled || !consent} onClick={() => submit('knowledge.confirm', { id })}>确认知识</button></>}
      {record.status !== 'invalidated' && <div className="action-form"><label>失效原因<input value={reason} onChange={e => setReason(e.target.value)} maxLength={1000} /></label>
        <button disabled={disabled || !reason.trim()} onClick={() => submit('knowledge.invalidate', { id, reason })}>标记失效</button></div>}</>}
    {tab === 'repairs' && record.status === 'candidate_ready' && <form className="action-form" onSubmit={e => {
      e.preventDefault(); const f = new FormData(e.currentTarget); submit('verification.create', { id, image: f.get('image'), timeout: Number(f.get('timeout')) })
    }}><label>已安装的 Docker 镜像<input name="image" required placeholder="python:3.12-slim" /></label><label>单次测试时限（秒）<input name="timeout" type="number" min={1} max={120} defaultValue={30} required /></label>
      <button disabled={disabled}>准备独立验证计划</button></form>}
    {tab === 'verifications' && record.status === 'passed' && <button disabled={disabled} onClick={() => submit('release.promote', { id })}>准备管理源码选择</button>}
    {tab === 'releases' && record.status === 'applied' && <div className="action-buttons"><button disabled={disabled} onClick={() => submit('release.rollback', { id })}>准备管理源码回退</button>
      <button disabled={disabled} onClick={() => submit('release.capture', { id })}>生成维护案例草稿</button></div>}
    {tab === 'releases' && record.status === 'pending_execution' && <button disabled={disabled} onClick={() => submit('release.recover', { id })}>核对中断的源码操作</button>}
    {tab === 'cognition' && <>
      {record.status === 'queued' && <button disabled={disabled} onClick={() => submit('cognition.start', { id })}>启动已批准的调查</button>}
      {['planning', 'investigating', 'reviewing', 'pending_execution'].includes(String(record.status)) && !record.worker_active && <button disabled={disabled} onClick={() => submit('cognition.recover', { id })}>核对并恢复已知响应</button>}
      {Boolean(record.followup) && <button disabled={disabled} onClick={() => submit('resolution.create', { id })}>准备补查结案对照</button>}
      <ReadButton connection={connection} path={`/api/cognition/${id}/dossier`} label="查看六部分档案" />
      <ReadButton connection={connection} path={`/api/cognition/${id}/calls`} label="核对请求与调用记录" />
      {list(record.findings).some(f => f.status !== 'invalidated') && <details><summary>标记认知发现失效</summary><form className="action-form" onSubmit={e => {
        e.preventDefault(); const f = new FormData(e.currentTarget); submit('cognition.invalidate', { id, finding_id: f.get('finding_id'), reason: f.get('reason') })
      }}><label>需失效的发现<select name="finding_id">{list(record.findings).filter(f => f.status !== 'invalidated').map(f => <option key={String(f.id)} value={String(f.id)}>{String(f.id)} · {String(obj(f.finding).statement)}</option>)}</select></label>
        <label>失效原因<input name="reason" required maxLength={1000} /></label><button disabled={disabled}>标记该发现失效</button></form></details>}
      {record.status === 'awaiting_context_approval' && <ContextDecisions connection={connection} id={id} disabled={disabled} submit={submit} />}
      {list(record.findings).filter(f => ['pending_review', 'needs_investigation'].includes(String(f.status))).map(f => <div key={String(f.id)} className="operation">
        <p>{String(obj(f.finding).statement)}</p><button disabled={disabled} onClick={() => submit('cognition.followup', { id, finding_id: f.id })}>准备定向补查</button></div>)}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
    <p className="review-note">每一步独立准备和批准。执行中可离开页面，回到网页操作记录核对结果。</p>
  </section>
}

function ContextDecisions({ connection, id, disabled, submit }: { connection: Connection; id: string; disabled: boolean; submit(action: string, p: Row): void }) {
  const [records, setRecords] = useState<Row[]>([]), [error, setError] = useState(''), [consent, setConsent] = useState<Record<string, boolean>>({})
  useEffect(() => {
    const controller = new AbortController()
    readApi<Row[]>(connection, `/api/cognition/${id}/context-requests`, controller.signal).then(setRecords).catch(e => { if (!controller.signal.aborted) setError(String(e)) })
    return () => controller.abort()
  }, [connection, id])
  return <>{error && <p className="error">{error}</p>}{records.filter(r => r.status === 'awaiting_approval').map(r => {
    const key = `${r.actor}-${r.sequence}`
    return <div key={key}><h3>超出默认范围的完整请求</h3><pre className="approval-preview">{JSON.stringify(r, null, 2)}</pre>
      <label className="consent"><input type="checkbox" checked={Boolean(consent[key])} onChange={e => setConsent({ ...consent, [key]: e.target.checked })} />批准该请求的精确字符扩展</label>
      {[true, false].map(approved => <button key={String(approved)} disabled={disabled || (approved && !consent[key])} onClick={() => submit('cognition.context-decide', {
        id, approved, actor: r.actor, sequence: r.sequence, sha256: r.request_sha256,
      })}>{approved ? '批准本次扩展' : '拒绝本次扩展'}</button>)}</div>
  })}</>
}
