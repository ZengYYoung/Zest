import { useEffect, useState } from 'react'
import type { Row } from './api.ts'

export const maintenanceRunning = new Set(['queued', 'preparing', 'planning', 'investigating', 'reviewing', 'publishing'])
const object = (value: unknown): Row => value && typeof value === 'object' ? value as Row : {}
const steps = ['准备资料', '规划调查', '分析变更', '核对理解', '更新文档']
const stage: Record<string, number> = { queued: 0, preparing: 0, planning: 1, investigating: 2, reviewing: 3, publishing: 4 }

export function maintenanceStage(value: Row) {
  const status = String(value.status), pages = object(value.document_generation)
  return maintenanceRunning.has(status) && Boolean(value.active_run_id) && ['planning', 'writing'].includes(String(pages.status))
    && pages.run_id === value.active_run_id ? 'publishing' : status
}

function timestamp(value: unknown) {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN
  return Number.isFinite(parsed) ? parsed : undefined
}

export function waitingDuration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds))
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${String(value % 60).padStart(2, '0')} 秒`
}

export function MaintenanceProgress({ value }: { value: Row }) {
  const [clock, setClock] = useState(Date.now), [openedAt] = useState(Date.now)
  const research = object(value.research), pages = object(value.document_generation)
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const startedAt = timestamp(research.started_at) ?? timestamp(research.created_at) ?? openedAt
  const receivedAt = timestamp(value.progress_received_at)
  const jobs = Array.isArray(research.jobs) ? research.jobs.map(object) : []
  const index = stage[maintenanceStage(value)]
  return <div className="maintenance-progress">
    <div className="maintenance-waiting" aria-live="off">
      <span>已等待 {waitingDuration((clock - startedAt) / 1000)}</span>
      {receivedAt !== undefined && <span>最近读取 {waitingDuration((clock - receivedAt) / 1000)}前</span>}
    </div>
    <div className="maintenance-motion" aria-hidden="true"><span /></div>
    <ol className="maintenance-steps" aria-label="更新阶段">{steps.map((label, i) => <li key={label} aria-current={i === index ? 'step' : undefined}><span aria-hidden="true">{i + 1}</span>{label}</li>)}</ol>
    <p className="maintenance-wait-hint">后台正在处理，模型响应可能需要一些时间。完成、暂停或失败后会自动显示结果，可以收起窗口稍后查看。</p>
    {(typeof research.call_count === 'number' || jobs.length > 0 || Number(pages.total) > 0) && <p className="check-caption">
      {typeof research.call_count === 'number' && <span>模型调用 {research.call_count} 次 </span>}
      {jobs.length > 0 && <span>· 调查已完成 {jobs.filter(job => job.status === 'completed').length} / {jobs.length} 项 </span>}
      {Number(pages.total) > 0 && <span>· 文档已发布 {Number(pages.completed || 0)} / {Number(pages.total)} 页</span>}
    </p>}
  </div>
}
