import { shortCommit, statusLabels, type Row } from './api.ts'

type Kind = 'tasks' | 'repairs' | 'verifications' | 'knowledge' | 'releases'
const object = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const records = (value: unknown): Row[] => Array.isArray(value) ? value.map(object) : []
const text = (value: unknown, fallback = '—') => typeof value === 'string' ? value : fallback

function Field({ label, value }: { label: string; value: unknown }) {
  return <div className="review-field"><span>{label}</span><code>{text(value)}</code></div>
}

function Sources({ sources, citations }: { sources: Row[]; citations: unknown }) {
  const cited = Array.isArray(citations) ? citations : []
  return <div className="review-sources">{sources.map((source, index) => {
    const evidence = object(source.evidence)
    const planned = evidence.kind === 'planned_new_file'
    const knowledge = evidence.kind === 'confirmed_knowledge'
    const content = typeof source.content === 'string' ? source.content : Array.isArray(source.lines)
      ? records(source.lines).map(line => `${String(line.number).padStart(4)}  ${text(line.text, '')}`).join('\n') : text(source.body, '来源未保存正文。')
    return <details key={text(source.id, String(index))} className="source-entry">
      <summary><span className="evidence-id">{text(source.id)}</span><strong>{text(evidence.path, text(source.title, '知识记录'))}</strong>
        <span className="muted">{planned ? '待创建路径' : knowledge ? '已确认知识' : `行 ${evidence.start_line}–${evidence.end_line}`}</span>
        {cited.includes(source.id) && <span className="cited">结果引用</span>}</summary>
      <div className="source-metadata"><Field label={knowledge ? '观察版本' : '来源版本'} value={evidence.commit || evidence.observed_commit} />
        {knowledge ? <Field label="知识记录 ID" value={evidence.knowledge_id} /> : <Field label="原文件 SHA-256" value={source.content_sha256} />}</div>
      {planned ? <p className="review-note">这是批准范围内的待创建路径，不代表已有文件或代码证据。</p> : <pre className="source-code">{content}</pre>}
    </details>
  })}</div>
}

function Task({ record }: { record: Row }) {
  const context = object(record.context)
  const result = object(record.result)
  const target = object(record.target)
  const policy = object(context.execution_policy)
  return <>
    <p className="review-question">{text(context.question, '任务未保存问题。')}</p>
    <div className="review-fields"><Field label="任务状态" value={statusLabels[text(record.status)] || record.status} /><Field label="来源版本" value={context.commit} />
      <Field label="模型 / 接口" value={`${text(target.model)} · ${text(target.base_url)}`} /><Field label="数据批准 SHA-256" value={record.payload_sha256} /></div>
    {record.status === 'awaiting_approval' && <p className="review-note">目前等待这份数据请求的批准。下面列出将发送的来源；数据批准后仍需独立审阅具体修改，才能创建候选版本。</p>}
    {record.status === 'pending_execution' && <p className="review-note">执行结果尚未核对，不能视为完成，也不能自动重试可能已计费的请求。</p>}
    {result.status && <Field label="结果类型" value={result.status === 'insufficient_evidence' ? '证据不足' : result.status === 'proposed' ? '建议已生成 · 待独立修改批准' : '模型分析 · 需核对引用'} />}
    {result.answer && <><h3>调查结论</h3><div className="review-answer">{text(result.answer)}</div><p className="review-note">这是模型分析；引用可定位来源，仍需检查来源是否支持结论。</p></>}
    {result.summary && <><h3>维护建议</h3><div className="review-answer">{text(result.summary)}</div>
      <Field label="具体修改方案 ID" value={result.repair_id} /><Field label="具体方案 SHA-256" value={result.repair_plan_sha256} />
      <p className="review-note">建议生成与修改执行是两次独立批准。请到候选修改中审阅完整 diff；这里不代表修改已经应用或验证通过。</p></>}
    {Array.isArray(result.unknowns) && result.unknowns.length > 0 && <><h3>待补充信息</h3><ul className="unknowns">{result.unknowns.map((value, i) => <li key={i}>{text(value)}</li>)}</ul></>}
    {record.error && <div role="alert" className="error">{text(object(record.error).message, '任务异常。')}<br /><code>{text(object(record.error).code)}</code></div>}
    <h3>本次来源 · {records(context.sources).length} 项</h3><Sources sources={records(context.sources)} citations={result.citations} />
    <p className="review-note">调用上限：{Number(policy.max_model_calls || 0)} 次。所有批准和执行仍通过 CLI。</p>
    {Array.isArray(result.specialists) && <details className="json-details"><summary>查看协作 Agent 分析（不是新增事实证据）</summary><pre>{JSON.stringify(result.specialists, null, 2)}</pre></details>}
  </>
}

function Repair({ record }: { record: Row }) {
  const plan = object(record.plan)
  return <><p className="review-question">{text(plan.summary)}</p><div className="review-fields"><Field label="方案状态" value={statusLabels[text(record.status)] || record.status} />
    <Field label="基础版本" value={plan.base_commit} /><Field label="候选版本" value={record.candidate_commit} /><Field label="具体修改批准 SHA-256" value={record.plan_sha256} /></div>
    <p className="review-note">候选提交不改动原工作区。候选就绪仍须独立验证；只有通过验证的候选才可申请管理源码选择。</p>
    {records(plan.changes).map((change, i) => <section className="change-entry" key={text(change.path, String(i))}><div className="change-heading"><strong>{text(change.path)}</strong>
      <span>{change.operation === 'create' ? '创建文件' : change.operation === 'edit' ? '精确锚点替换' : '替换正文'}</span></div>
      <div className="review-fields"><Field label="原文 SHA-256" value={change.expected_sha256} /><Field label="新文 SHA-256" value={change.new_sha256} /></div>
      <pre className="diff-code">{text(change.diff, '没有保存 diff。').split('\n').map((line, n) => <span className={line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('@@') ? 'hunk' : ''} key={n}>{line}{'\n'}</span>)}</pre>
      <details className="json-details"><summary>查看完整候选正文</summary><pre>{text(change.content, '')}</pre></details></section>)}
    {record.error && <div className="error" role="alert">{text(object(record.error).message)}</div>}</>
}

function Knowledge({ record }: { record: Row }) {
  return <><p className="review-question">{text(record.title)}</p><div className="review-fields"><Field label="知识状态" value={statusLabels[text(record.status)] || record.status} />
    <Field label="适用范围" value={record.scope === 'project' ? '跨版本项目知识 · 历史信息仍须核对' : '限定观察版本'} /><Field label="观察版本" value={record.observed_commit} /><Field label="确认时间" value={record.confirmed_at} /></div>
    <div className="review-answer">{text(record.body)}</div><p className="review-note">草稿不进入 Agent 上下文；确认与失效通过 CLI 完成。确认记录也不保证它对未来版本始终适用。</p></>
}

function Release({ record }: { record: Row }) {
  const plan = object(record.plan)
  return <><p className="review-question">{plan.operation === 'rollback' ? '回退管理源码' : '选择管理源码'}：{shortCommit(plan.previous_commit)} → {shortCommit(plan.target_commit)}</p>
    <div className="review-fields"><Field label="操作状态" value={statusLabels[text(record.status)] || record.status} /><Field label="以前版本" value={plan.previous_commit} /><Field label="目标版本" value={plan.target_commit} /><Field label="操作批准 SHA-256" value={record.plan_sha256} /></div>
    <p className="review-note">这里只记录管理源码指针的选择，不代表服务部署或流量切换。回退可恢复有故障的历史基线，不恢复数据库或运行健康。</p></>
}

function Verification({ record }: { record: Row }) {
  const plan = object(record.plan)
  const result = object(record.result)
  const fixture = plan.backend !== 'docker'
  const describe = (value: unknown) => value === undefined ? '—' : String(value)
  return <><p className="review-question">候选测试验证</p>
    <p className="review-note">{fixture ? '离线或未知后端记录，不能作为正式管理源码选择的真实 Docker 证据。' : '受限 Docker 测试记录；通过只说明这套测试通过，仍需独立批准源码选择。'}</p>
    <div className="review-fields"><Field label="验证状态" value={statusLabels[text(record.status)] || record.status} /><Field label="执行后端" value={plan.backend} />
      <Field label="基础版本" value={plan.base_commit} /><Field label="候选版本" value={plan.candidate_commit} /><Field label="固定镜像 ID" value={plan.image_id} /><Field label="验证批准 SHA-256" value={record.plan_sha256} /></div>
    <Field label="固定命令" value={Array.isArray(plan.command) ? plan.command.join(' ') : undefined} />
    <p className="review-note">单次时限：{describe(plan.timeout_seconds)} 秒。{plan.baseline_strategy === 'not_applicable_initial_creation' ? '初始纯创建且基线无匹配测试：基线不运行，候选仍必须真实执行非空测试。' : '基线与候选分别测试，保留两份结果。'}</p>
    {(['baseline', 'candidate'] as const).map(key => {
      const run = object(result[key])
      if (!Object.keys(run).length) return <div className="review-answer" key={key}>{key === 'baseline' ? '基线' : '候选'}：尚无执行结果</div>
      return <section className="change-entry" key={key}><div className="change-heading"><strong>{key === 'baseline' ? '基线结果' : '候选结果'}</strong><span>{statusLabels[text(run.status)] || text(run.status)}</span></div>
        {run.status === 'not_applicable' && <p className="review-note">基线未运行，不是测试通过。</p>}
        <div className="review-fields"><Field label="测试数量" value={describe(run.tests_run)} /><Field label="退出码" value={describe(run.exit_code)} />
          <Field label="容器清理已确认" value={describe(run.cleanup_ok)} /><Field label="实际隔离配置已核对" value={describe(run.policy_verified)} />
          <Field label="OOM / 日志截断" value={`${describe(run.oom_killed)} / ${describe(run.output_truncated)}`} /></div>
        {Boolean(run.error) && <div className="error" role="alert">{text(object(run.error).message)}</div>}
        {typeof run.output === 'string' && <details className="json-details"><summary>查看保存的测试日志</summary><pre>{run.output}</pre></details>}</section>
    })}
    {result.error && <div className="error" role="alert">{text(object(result.error).message)}</div>}
    <details className="json-details"><summary>查看验证策略与文件清单</summary><pre>{JSON.stringify({ policy: plan.policy, base: plan.base_manifest, candidate: plan.candidate_manifest }, null, 2)}</pre></details>
  </>
}

export function RecordDetail({ kind, value, onClose }: { kind: Kind; value: unknown; onClose: () => void }) {
  const record = object(value)
  return <section className="panel"><div className="panel-heading"><h2>审阅记录</h2><button onClick={onClose}>收起</button></div>
    <div className="review-body">{kind === 'tasks' ? <Task record={record} /> : kind === 'repairs' ? <Repair record={record} /> : kind === 'verifications' ? <Verification record={record} /> : kind === 'knowledge' ? <Knowledge record={record} /> : <Release record={record} />}</div>
    <details className="json-details"><summary>查看完整原始记录</summary><pre>{JSON.stringify(value, null, 2)}</pre></details></section>
}
