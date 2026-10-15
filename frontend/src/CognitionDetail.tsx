import { shortCommit, statusLabels, type Row } from './api.ts'

const object = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const rows = (value: unknown): Row[] => Array.isArray(value) ? value.map(object) : []
const text = (value: unknown) => typeof value === 'string' ? value : '—'
const sections = [['purpose', '项目目标与业务边界'], ['architecture', '模块与架构关系'], ['flows', '核心业务流程'], ['dependencies', '数据与外部依赖'], ['development', '开发与验证方式'], ['constraints', '约束与认知缺口']]

export function CognitionDetail({ value, onClose }: { value: unknown; onClose(): void }) {
  const record = object(value), plan = object(record.plan), policy = object(record.policy)
  const findings = rows(record.findings), jobs = rows(record.jobs)
  return <section className="panel"><div className="panel-heading"><h2>项目认知 · {shortCommit(record.commit)}</h2><button onClick={onClose}>关闭</button></div>
    <div className="review-fields"><div className="review-field"><span>阶段</span><strong>{statusLabels[text(record.status)] || text(record.status)}</strong></div>
      <div className="review-field"><span>模型调用预算</span><strong>{Number(record.call_count || 0)} / {Number(policy.max_model_calls || 0)}</strong></div>
      <div className="review-field"><span>后台进程</span><strong>{record.worker_active ? '正在执行' : '当前未执行'}</strong></div>
      <div className="review-field"><span>固定资料包</span><strong>{Number(record.file_count || 0)} 个文件</strong></div></div>
    <p className="review-note">自动核对表示模型检查了原文支持关系，仍可能出错。静态源码认知不代表实际运行验证；返回对话不会取消后台调查。</p>
    {Boolean(object(record.scope_review).requires_discussion) && <p className="review-note">范围待确认：已纳入 {Number(object(record.scope_review).source_count)} 个文件，规划清单展示 {Number(object(record.scope_review).catalog_count)} 个。{text(object(record.scope_review).message)}</p>}
    {Boolean(record.error) && <div className="error">{text(object(record.error).message)}</div>}
    {record.status === 'pending_execution' && <p className="review-note">存在结果未知的请求，请核对调用记录；不会自动重复发送。</p>}
    {rows(record.context_requests).filter(item => item.status === 'awaiting_approval').map(item => <p className="review-note" key={`${text(item.actor)}-${Number(item.sequence)}`}>
      {text(item.actor)} / {Number(item.sequence)}：完整请求 {Number(item.chars)} 字符，默认 {Number(item.default_chars)}，超出 {Number(item.over_percent)}%。尚未外发，请在终端或 CLI 审阅并决定本次扩展。
    </p>)}
    <h3>初步认知 · 待深入核对</h3><ul>{rows(plan.overview).map((entry, i) => <li key={i}>{text(entry.statement)} · {entry.kind === 'inferred' ? '推测' : '初步观察'}</li>)}</ul>
    <h3>调查任务</h3>{jobs.map(job => { const spec = object(job.spec), report = object(object(job.result).report), rejected = rows(object(object(job.result).report_validation).rejected_findings)
      return <details className="source-entry" key={text(job.id)}><summary><strong>{text(job.id)} · {text(spec.title)}</strong><span>{statusLabels[text(job.status)] || text(job.status)}</span></summary>
        <p>{text(spec.goal)}</p><p className="review-note">成果：{text(spec.deliverable)}</p>
        <p>依赖：{Array.isArray(spec.depends_on) && spec.depends_on.length ? spec.depends_on.join('、') : '无'}</p>
        <ul>{Array.isArray(spec.acceptance) && spec.acceptance.map((v, i) => <li key={i}>{text(v)}</li>)}</ul>
        <ul>{Array.isArray(report.unknowns) && report.unknowns.map((v, i) => <li key={i}>待核实：{text(v)}</li>)}</ul>
        <ul>{rejected.map((issue, i) => <li key={i}>候选未接收：{text(issue.field)} · {text(issue.code)} · {text(issue.message)}</li>)}</ul>
        <ul>{Array.isArray(report.scope_requests) && report.scope_requests.map((v, i) => <li key={i}>需要扩大授权：{text(v)}</li>)}</ul>
      </details> })}
    {sections.map(([id, title]) => <div key={id}><h3>{title}</h3>
      {!findings.some(item => object(item.finding).section === id) && <p className="review-note">尚无可展示发现，保持未知。</p>}
      {findings.filter(item => object(item.finding).section === id).map(item => { const finding = object(item.finding)
        return <details className="source-entry" key={text(item.id)}><summary><strong>{text(finding.statement)}</strong><span>{statusLabels[text(item.status)] || text(item.status)}</span></summary>
          <p>类别：{finding.kind === 'observed' ? '源码 / 文档观察' : '推测'} · 任务 {text(item.job_id)}</p><p>核对意见：{text(object(item.review).reason)}</p>
          {rows(finding.evidence).map((proof, i) => { const source = object(proof.evidence)
            return <div key={i}><p><code>{text(source.path)}</code> · 行 {Number(source.start_line)}–{Number(source.end_line)}</p><code>{text(source.commit)}</code><pre className="source-code">{text(proof.text)}</pre></div> })}
        </details> })}
      {rows(plan.gaps).filter(gap => gap.section === id).map((gap, i) => <p className="review-note" key={i}>计划缺口：{text(gap.question)}</p>)}
    </div>)}
    <details className="json-details"><summary>任务、事件与完整记录</summary><pre>{JSON.stringify(record, null, 2)}</pre></details>
  </section>
}
