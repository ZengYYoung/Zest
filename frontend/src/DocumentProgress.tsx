import { shortCommit, type Row } from './api.ts'

const labels: Record<string, string> = { pending: '待撰写', writing: '正在撰写', reviewing: '正在复核', ready: '待发布',
  completed: '已发布', retained: '沿用已有正文', retired: '已归档，历史正文保留', blocked: '旧事实待复核，保留原文',
  skipped: '保留原文，已生成候选', failed: '未完成', unknown: '请求结果待核对' }
const reasons: Record<string, string> = { cognition_call_budget: '本轮调用预算已用完', cognition_time_budget: '本轮运行时间已用完',
  cognition_prompt_budget: '必要资料超过单次请求容量', document_review_rejected: '正文未通过核对',
  invalid_document_output: '页面结构或引用不完整', document_write_failed: '页面未写入', document_context_changed: '页面依据已变化',
  document_artifact_changed: '页面存在人工改动或已经缺失' }

function reason(value: unknown) {
  if (!value || typeof value !== 'object') return ''
  const message = (value as Row).message
  if (typeof message === 'string' && message.trim()) return message
  return reasons[String((value as Row).code)] || '详细原因已保存在本机页面记录'
}

export function DocumentProgress({ value, onOpen }: { value: unknown; onOpen?(path: string): void }) {
  if (!value || typeof value !== 'object') return null
  const generation = value as Row
  const knowledge = generation.knowledge && typeof generation.knowledge === 'object' ? generation.knowledge as Row : null
  const pages = Array.isArray(generation.pages) ? generation.pages as Row[] : []
  const recovery = generation.recovery && typeof generation.recovery === 'object' ? generation.recovery as Row : null
  return <>{Boolean(generation.index_candidate) && onOpen && <button className="quiet-button" onClick={() => onOpen(String(generation.index_candidate))}>查看本轮新文档目录</button>}<details className="document-progress" open={!pages.length || ['new_round', 'inspect'].includes(String(recovery?.action))}><summary>{pages.length ? <>文档页面 · 已发布 {Number(generation.completed || 0)} / {Number(generation.total || 0)}</> : '文档页面 · 尚未生成页面计划'}{Number(generation.retained) > 0 ? ` · 沿用 ${generation.retained}` : ''}</summary>
    {Boolean(generation.index_candidate) && <p>原目录有人工修改，已保留；本轮生成的新目录可单独查看。</p>}
    {recovery?.message && generation.status !== 'completed' ? <p className="initialization-warning">{String(recovery.message)}</p> : null}
    <p className="muted">每页独立撰写、复核和保存。页面完成不代表项目知识已穷尽。</p>
    {knowledge && <p>事实记录：核对 {Number(knowledge.verified || 0)} · 沿用 {Number(knowledge.inherited || 0)} · 待复核 {Number(knowledge.needs_review || 0) + Number(knowledge.unresolved || 0)} · 撤回 {Number(knowledge.retracted || 0)}。</p>}
    {pages.length ? <ul>{pages.map(page => <li key={String(page.slug)}>{String(page.title)} · {labels[String(page.status)] || '待核对'}{page.error ? `：${reason(page.error)}` : ''}
      {page.status === 'retained' && <span> · 核验 {shortCommit(page.last_verified_commit)} / 证据检查 {shortCommit(page.evidence_checked_commit)}</span>}</li>)}</ul> : <p>页面规划尚未完成，调查事实已保留。{reason(generation.error)}</p>}
    {Number(generation.unknown) > 0 || generation.status === 'unknown' ? <p>有请求结果不明，须先核对原调用，系统不会自动重发。</p> : null}
  </details></>
}
