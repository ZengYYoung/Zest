import { useEffect, useState } from 'react'
import type { Row } from './api.ts'

export const operationTitle = (action: unknown) => ({
  'project.import':'接入项目', 'project.choose-directory':'选择项目文件夹', 'initialization.prepare':'准备项目资料',
  'project.remove':'移除项目',
  'initialization.decide':'启动项目初始化', 'initialization.context-decide':'确认调查请求', 'initialization.context-decide-many':'确认本批调查请求',
  'initialization.start':'继续项目初始化', 'initialization.recover':'核对原初始化', 'task.decide':'生成本轮回答',
  'initialization.repair':'准备补全项目理解', 'initialization.republish':'整理已有项目文档',
  'initialization.initialize':'自动初始化项目',
  'understanding.configure':'保存项目理解自动批准设置',
  'conversation.send':'生成回答', 'conversation.answer':'继续回答', 'conversation.create':'建立对话',
  'maintenance.context-decide':'确认增量调查请求', 'maintenance.context-decide-many':'确认本批增量请求',
  'maintenance.reprepare':'重新准备增量更新',
  'maintenance.documents-new-round':'开启新一轮文档生成',
}[String(action)] || '处理项目操作')

export function OperationStatus({ operation, busy, onStart }: { operation: Row | null; busy: boolean; onStart?(): void }) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => { if (!busy) return; setClock(Date.now()); const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer) }, [busy, operation?.id])
  if (!busy || !operation || !['submitting','queued','running'].includes(String(operation.status))) return null
  const progress = operation.progress as Row | undefined, elapsed = Math.max(0, Math.floor((clock - Date.parse(String(operation.created_at))) / 1000))
  const importing = operation.action === 'project.import'
  const stages = ['repository','documents','index','scope','ready'], current = stages.indexOf(String(progress?.stage))
  return <section className="operation-status" role="status" aria-live="polite">
    <div className="initialization-progress"><span className="initialization-spinner" /><strong>{operationTitle(operation.action)}中{Number.isFinite(elapsed) ? ` · 已等待 ${elapsed} 秒` : ''}</strong></div>
    <p>{String(progress?.message || (operation.status === 'queued' ? '已保存操作，等待本机后台开始。' : importing ? '正在检查目录、整理文档并建立源码索引。较大的项目需要一些时间。' : operation.action === 'project.choose-directory' ? '请在 macOS 文件夹窗口完成选择，或点击取消。' : '正在处理，完成后会自动更新页面。'))}</p>
    {importing && <ol className="import-stages">{['检查 Git 项目','整理已有文档','建立源码索引','准备调查范围'].map((label, i) => <li key={label} className={current > i ? 'done' : current === i ? 'active' : ''}>{current > i ? '✓ ' : current === i ? '◉ ' : '○ '}{label}</li>)}</ol>}
    {elapsed >= 20 && <p className="muted">仍在等待后台结果；不会重复提交。你可以收起此窗口，稍后核对原操作。</p>}
    {elapsed >= 5 && operation.status === 'queued' && onStart && <button type="button" onClick={onStart}>启动已保存的排队操作</button>}
  </section>
}
