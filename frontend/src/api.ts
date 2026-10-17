export type Connection = { port: number }
export type Project = { id: string; mode: string; root_path: string; git: { head_commit: string | null; branch: string | null; dirty: boolean } }
export type Row = Record<string, unknown>

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

export function validateConnection(port: number): Connection {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new ApiError('API 端口须为 1024–65535。', 0)
  return { port }
}

export function defaultPort(): number {
  if (typeof window === 'undefined') return 8000
  const port = Number(window.location.port)
  return port >= 1024 && port !== 5173 ? port : 8000
}

async function requestApi<T>(connection: Connection, path: string, options: RequestInit, signal?: AbortSignal, timeoutMs = 15000): Promise<T> {
  validateConnection(connection.port)
  if (!path.startsWith('/api/') || path.startsWith('//') || path.includes('://')) throw new ApiError('无效的本地接口路径。', 0)
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) throw new ApiError('无效的本地读取时限。', 0)
  const host = typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'localhost' : '127.0.0.1'
  let response: Response
  try {
    response = await fetch(`http://${host}:${connection.port}${path}`, {
      ...options, credentials: 'omit', cache: 'no-store', redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError(options.method === 'POST'
      ? '未收到操作回执。请按原请求身份核对网页操作记录，避免重复创建。'
      : '无法连接本地 API。请确认服务已启动且端口正确。', 0)
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(body.error?.message || '请求失败。', response.status)
  return body.data as T
}

export function readApi<T>(connection: Connection, path: string, signal?: AbortSignal, timeoutMs = 15000): Promise<T> {
  return requestApi(connection, path, { method: 'GET' }, signal, timeoutMs)
}

export function writeApi<T>(connection: Connection, path: string, body: unknown): Promise<T> {
  return requestApi(connection, path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}

export type WebCommand = { request_id: string; action: string; parameters: Row }
export function prepareCommand(action: string, parameters: Row): WebCommand {
  return { request_id: crypto.randomUUID(), action, parameters }
}

export function projectName(project: Project): string { return project.root_path.split('/').filter(Boolean).at(-1) || '未命名项目' }
export function shortCommit(value: unknown): string { return typeof value === 'string' ? value.slice(0, 12) : '尚无提交' }
export const statusLabels: Record<string, string> = {
  awaiting_context_approval: '完整请求待扩展批准',
  awaiting_approval: '等待批准', completed: '已完成', proposed: '建议已生成', candidate_ready: '候选就绪',
  passed: '验证通过', failed: '失败', error: '异常', applied: '已选择', rejected: '已拒绝',
  pending_execution: '待核对执行', draft: '待确认', confirmed: '已确认', invalidated: '已失效',
  not_applicable: '未运行（不适用）', timed_out: '执行超时',
  unknown: '结果待核对', queued: '已排队', planning: '正在规划', investigating: '后台调查', reviewing: '正在核对',
  partial: '部分完成', running: '进行中', blocked: '依赖阻塞', pending_review: '待核对',
  auto_reviewed: '自动核对通过', needs_investigation: '仍需调查',
}
