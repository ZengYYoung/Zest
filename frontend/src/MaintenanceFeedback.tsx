import { shortCommit, type Row } from './api.ts'
import { MaintenanceProgress, maintenanceRunning, maintenanceStage } from './MaintenanceProgress.tsx'

const object = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {}
const changes: Record<string, string> = { added: '新增', modified: '修改', deleted: '删除', type_changed: '类型变化' }
const stages: Record<string, string> = { queued: '正在排队更新项目理解', preparing: '正在整理变更资料', planning: '正在规划增量调查',
  investigating: '正在分析变更内容', reviewing: '正在核对项目理解', publishing: '正在更新项目文档',
  completed: '项目理解已更新', partial: '本轮更新已结束，仍有待处理内容', failed: '项目理解更新未完成', blocked: '增量理解未开始：资料过多',
  pending_execution: '项目理解更新需要核对', interrupted: '项目理解更新已中断', publication_failed: '项目文档尚未更新完成',
  awaiting_approval: '项目理解更新等待确认', awaiting_context_approval: '项目理解更新等待补充范围确认', permission_required: '发现更新，尚未更新项目理解' }

export function MaintenanceFeedback({ value, checking = false, error = '' }: { value?: Row | null; checking?: boolean; error?: string }) {
  if (checking) return <section className="maintenance-feedback" role="status" aria-label="更新检查进度">
    <strong><span className="initialization-spinner" />正在检查更新…</strong><p>正在比较本地 Git、工作区状态和项目理解版本。</p>
  </section>
  if (error) return <section className="maintenance-feedback check-failed" role="alert"><strong>{maintenanceRunning.has(String(value?.status)) ? '更新进度暂时无法读取' : '检查更新未完成'}</strong><p>{error}</p><p>{maintenanceRunning.has(String(value?.status)) ? '后台可能仍在执行，页面会继续尝试读取进度；不会重新发起更新。' : '可以再次点击“检查更新”核对当前状态。'}</p></section>
  if (!value || value.status === 'not_checked') return null
  const check = object(value.last_check), summary = object(check.summary), counts = object(summary.counts)
  const files = Array.isArray(summary.files) ? summary.files.map(object) : []
  const subjects = Array.isArray(summary.commit_subjects) ? summary.commit_subjects : []
  const date = typeof check.at === 'string' ? new Date(check.at) : null
  const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''
  const same = summary.base_commit && summary.base_commit === summary.target_commit
  const countText = Object.entries(changes).filter(([kind]) => Number(counts[kind]) > 0).map(([kind, label]) => `${label} ${counts[kind]}`).join('、')
  const active = maintenanceRunning.has(String(value.status))
  const success = ['completed', 'current', 'generated_only'].includes(String(value.status))
  const recovery = object(object(value.document_generation).recovery)
  const generation = object(value.document_generation)
  const title = !active && generation.status === 'completed' && generation.index_candidate ? '本轮文档已生成，原目录已保留' : !active && recovery.action === 'new_round' ? '文档生成需要新一轮' : value.status === 'completed' && object(value.research).document_continuation ? '项目文档已更新' : stages[maintenanceStage(value)] || '检查完成'
  return <section className={`maintenance-feedback${active ? ' maintenance-running' : success ? ' maintenance-completed' : ' maintenance-paused'}`} role="status" aria-label="更新检查结果" aria-busy={active}>
    <div className="check-result-heading"><strong>{active ? <span className="initialization-spinner maintenance-spinner" aria-hidden="true" /> : <span className="maintenance-state-icon" aria-hidden="true">{success ? '✓' : '!'}</span>}{title}</strong>{time && <span className="check-caption">检查于 <time dateTime={String(check.at)}>{time}</time></span>}</div>
    {active && <MaintenanceProgress key={String(value.active_run_id || value.project_id || 'maintenance')} value={value} />}
    {same ? <p>未发现新提交，本地版本检查已完成。</p> : Number(summary.file_count) > 0 ? <>
      <p>相对上次项目理解，{Number(summary.file_count)} 个文件发生变化{countText ? `：${countText}` : ''}。</p>
      {subjects.length > 0 && <><p className="check-caption">近期提交说明</p><ul className="check-commit-subjects">{subjects.map((title, i) => <li key={i}>{String(title)}</li>)}</ul></>}
    </> : summary.base_commit ? <p>版本已变化，未发现需要重新调查的项目资料{Number(summary.excluded_count) > 0 ? '，生成资料已跳过' : ''}。</p> : null}
    {(!same || value.status !== 'current') && Boolean(value.message) && <p>{String(value.message)}</p>}
    {Boolean(check.dirty) && <p className="check-worktree">另有本地未提交改动，尚未纳入本轮项目理解。</p>}
    {summary.base_commit && summary.target_commit && summary.base_commit !== summary.target_commit ? <p className="check-caption">比较版本 {shortCommit(summary.base_commit)} → {shortCommit(summary.target_commit)}</p> : Boolean(check.commit) && <p className="check-caption">检查版本 {shortCommit(check.commit)}</p>}
    {files.length > 0 && <details className="check-files"><summary>查看变更文件 · {Number(summary.file_count)} 个</summary><ul>{files.map((file, i) => <li key={i}><span>{changes[String(file.change)] || '变化'}</span><code>{String(file.path)}</code></li>)}</ul>{Number(summary.more_files) > 0 && <p>另有 {Number(summary.more_files)} 个文件未展开。</p>}</details>}
    {Boolean(value.error) && <p className="error">{String(object(value.error).message || '项目理解更新未完成，请查看原更新记录。')}</p>}
  </section>
}
