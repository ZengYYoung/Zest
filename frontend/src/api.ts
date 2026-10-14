export type Connection = { token: string; port: number }
export type Project = { id: string; mode: string; root_path: string; git: { head_commit: string | null; branch: string | null; dirty: boolean } }
export type Row = Record<string, unknown>

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

export function validateConnection(token: string, port: number): Connection {
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(token) || /^(sk-|gh[pousr]_|github_pat_)/.test(token)) {
    throw new ApiError('请输入独立的 Web 访问令牌，请勿使用模型或 GitHub 密钥。', 0)
  }
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new ApiError('API 端口须为 1024–65535。', 0)
  return { token, port }
}

export async function readApi<T>(connection: Connection, path: string, signal?: AbortSignal): Promise<T> {
  if (!path.startsWith('/api/') || path.startsWith('//') || path.includes('://')) throw new ApiError('无效的本地接口路径。', 0)
  let response: Response
  try {
    response = await fetch(`http://127.0.0.1:${connection.port}${path}`, {
      method: 'GET', headers: { Authorization: `Bearer ${connection.token}` },
      credentials: 'omit', cache: 'no-store', redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
    })
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error
    throw new ApiError('无法连接本地 API。请确认服务已启动，端口及 CORS 配置正确。', 0)
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status === 401 ? '访问令牌无效，请重新连接。' : body.error?.message || '读取失败。', response.status)
  return body.data as T
}

export function projectName(project: Project): string { return project.root_path.split('/').filter(Boolean).at(-1) || '未命名项目' }
export function shortCommit(value: unknown): string { return typeof value === 'string' ? value.slice(0, 12) : '尚无提交' }
export const statusLabels: Record<string, string> = {
  awaiting_approval: '等待批准', completed: '已完成', proposed: '建议已生成', candidate_ready: '候选就绪',
  passed: '验证通过', failed: '失败', error: '异常', applied: '已选择', rejected: '已拒绝',
  pending_execution: '待核对执行', draft: '待确认', confirmed: '已确认', invalidated: '已失效',
  not_applicable: '未运行（不适用）', timed_out: '执行超时',
}
