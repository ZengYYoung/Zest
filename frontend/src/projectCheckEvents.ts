export type CheckReason = 'open' | 'switch' | 'manual'

/** Event dispatcher only: no timers, focus listeners, or checks while idle. */
export class ProjectCheckEvents<T> {
  private selected = ''
  private pending = new Map<string, Promise<T>>()
  constructor(private request: (projectId: string, reason: CheckReason) => Promise<T>) {}
  select(projectId: string): Promise<T | undefined> {
    if (!projectId || this.selected === projectId) return Promise.resolve(undefined)
    const reason = this.selected ? 'switch' : 'open'
    this.selected = projectId
    return this.check(projectId, reason)
  }
  check(projectId: string, reason: CheckReason): Promise<T> {
    const existing = this.pending.get(projectId)
    if (existing) return existing
    const promise = this.request(projectId, reason).finally(() => this.pending.delete(projectId))
    this.pending.set(projectId, promise)
    return promise
  }
}
