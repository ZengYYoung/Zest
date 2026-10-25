import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from './Icons.tsx'
import { TextContent } from './TextContent.tsx'
import { RecordDetail } from './RecordDetail.tsx'
import { shortCommit, statusLabels, type Connection, type Row } from './api.ts'

const object = (v: unknown): Row => v && typeof v === 'object' && !Array.isArray(v) ? v as Row : {}
export const rows = (v: unknown): Row[] => Array.isArray(v) ? v.map(object) : []

export function Conversation({ connection, value, busy, onSend, onCommand, onOlder, draft, onDraft, children, loading = false, answering = false, draftScope = '' }: {
  connection: Connection; value: Row | null; busy: boolean; onSend(question: string): Promise<boolean>
  onCommand(action: string, p: Row): void; onOlder(): void
  draft: string; onDraft(value: string): void
  children?: ReactNode; loading?: boolean; answering?: boolean; draftScope?: string
}) {
  const question = draft, setQuestion = onDraft
  const [sending, setSending] = useState(false)
  const currentScope = useRef(draftScope); currentScope.current = draftScope
  const viewport = useRef<HTMLDivElement>(null)
  const turns = rows(value?.turns), last = turns.at(-1)
  const pending = last && ['awaiting_approval', 'pending_execution', 'unknown', 'preparing', 'approved'].includes(String(last.status))
  useEffect(() => { if (turns.length && !loading) viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'smooth' }) }, [turns.length, last?.status, loading])
  const disabled = busy || sending || loading || Boolean(pending)
  async function send() {
    if (disabled || !question.trim()) return
    const submitted = question.trim()
    const submittedScope = draftScope
    setQuestion('')
    setSending(true)
    try { if (!await onSend(submitted) && currentScope.current === submittedScope) setQuestion(submitted) } finally { setSending(false) }
  }
  return <div className={`conversation-view ${turns.length ? 'has-messages' : ''}`}>
    <div className="conversation-scroll" ref={viewport}>
    {children}
    {loading ? <div className="conversation-loading" role="status">正在恢复对话…</div> : <>
    {!turns.length && <section className="chat-welcome"><div className="welcome-mark">Zest</div><h1>一起，把项目做好。</h1><p>你的专属私人项目管家</p></section>}
    <div className="message-list" aria-live="polite" aria-relevant="additions">
      {Boolean(value?.has_earlier) && <button className="quiet-button earlier" onClick={onOlder}>查看更早的消息</button>}
      {turns.map(turn => <Turn key={String(turn.id)} connection={connection} turn={turn} busy={busy} answering={answering && turn.id === last?.id} onCommand={onCommand} />)}
    </div>
    </>}
    </div>
    <div className="composer-dock"><form className="chat-composer" onSubmit={event => { event.preventDefault(); void send() }}>
      <textarea aria-label="和 Zest 对话" placeholder="说说你想了解什么，或想实现什么…" value={question} maxLength={2000} disabled={sending}
        onChange={event => setQuestion(event.target.value)} onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send() }
        }} />
      <div className="composer-bottom"><span>{answering ? '正在回答，完成后可以继续交流' : pending ? '先处理上一条消息，随后继续交流' : '需求不清楚，我们就一起聊清楚。'}</span>
        <button className="send-button" disabled={disabled || !question.trim()} aria-label="发送消息"><Icon name="arrow" /></button></div>
    </form><p className="composer-footnote">{busy ? '正在处理，记录会保存在本地。' : 'Shift + Enter 换行 · 项目事实以来源为依据'}</p></div>
  </div>
}

export function Turn({ turn, busy, onCommand, answering = false }: { connection: Connection; turn: Row; busy: boolean; onCommand(action: string, p: Row): void; answering?: boolean }) {
  const task = object(turn.task), result = object(task.result), context = object(task.context), target = object(task.target)
  const awaiting = task.status === 'awaiting_approval', sources = rows(context.sources)
  return <article className="conversation-turn">
    <div className="user-message"><TextContent text={String(turn.question || '')} /></div>
    <div className="assistant-message"><span className="assistant-mark" aria-hidden="true">z</span><div className="assistant-body">
      {typeof result.answer === 'string' ? <TextContent text={result.answer} /> : answering ? <p className="assistant-thinking" role="status"><span className="initialization-spinner" />正在结合项目资料回答…</p> : awaiting ? <div className="answer-permission">
        <p>这条消息尚未回答。点击继续，后续提问会在发送后直接回答。</p>
        <details className="request-preview"><summary>查看本次资料 · {sources.length} 份 · {shortCommit(context.commit)}</summary><RecordDetail kind="tasks" value={task} onClose={() => {}} /><details className="json-details"><summary>完整请求与对话上下文</summary><pre>{JSON.stringify({ target, context, payload_sha256: task.payload_sha256 }, null, 2)}</pre></details></details>
          <div className="inline-actions"><button className="solid-button" disabled={busy || !turn.conversation_id || !turn.id} onClick={() => onCommand('conversation.answer', { id: turn.conversation_id, message_id: turn.id })}>继续回答</button>
            <button className="quiet-button" disabled={busy} onClick={() => onCommand('task.decide', { id: task.task_id, approved: false })}>取消本轮</button></div>
      </div> : <p className="muted">{task.status === 'rejected' ? '本轮已取消，你可以继续补充或换个问题。' : statusLabels[String(turn.status)] || String(turn.status)}</p>}
      {Array.isArray(result.unknowns) && result.unknowns.length > 0 && <details className="message-evidence"><summary>待补充的信息</summary><ul>{result.unknowns.map((item, i) => <li key={i}>{String(item)}</li>)}</ul></details>}
      {Boolean(turn.error || task.error) && <p className="error" role="alert">{String(object(turn.error || task.error).message || '本轮处理失败。')}</p>}
      {Boolean(object(context.conversation_context).truncated) && <p className="muted">本轮使用最近的部分前文，更早消息仍保存在对话记录中。</p>}
      {Boolean(task.task_id) && !awaiting && <details className="message-evidence"><summary>来源与记录{context.commit ? ` · ${shortCommit(context.commit)}` : ''}</summary><RecordDetail kind="tasks" value={task} onClose={() => {}} /></details>}
    </div></div>
  </article>
}
