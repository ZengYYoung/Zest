import type { Row } from './api.ts'

export function noticeIdentity(kind: 'initialization' | 'maintenance', value: Row): string {
  const error = value.error as Row | undefined
  return JSON.stringify(kind === 'initialization'
    ? [value.id, value.run_id, value.stage, value.status]
    : [value.active_run_id || '', value.target_commit || value.knowledge_commit || '', value.status, error?.code || ''])
}
const key = (port: number, projectId: string, kind: string) => `zest-notices-${port}-${projectId}-${kind}`
export function wasDismissed(port: number, projectId: string, kind: string, identity: string): boolean {
  try { const saved = JSON.parse(localStorage.getItem(key(port, projectId, kind)) || '[]'); return Array.isArray(saved) && saved.includes(identity) } catch { return false }
}
export function dismissNotice(port: number, projectId: string, kind: string, identity: string): void {
  try {
    const saved = JSON.parse(localStorage.getItem(key(port, projectId, kind)) || '[]')
    localStorage.setItem(key(port, projectId, kind), JSON.stringify([...new Set([...(Array.isArray(saved) ? saved.filter(v => typeof v === 'string') : []), identity])].slice(-32)))
  } catch { /* Preferences remain optional; saved project knowledge is unchanged. */ }
}
