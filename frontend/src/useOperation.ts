import { useEffect, useRef, useState } from 'react'
import { ApiError, prepareCommand, readApi, writeApi, type Connection, type Row } from './api.ts'

export function useOperation(connection: Connection | null) {
  const [operation, setOperation] = useState<Row | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [recoveryId, setRecoveryId] = useState('')
  const running = useRef(false), controller = useRef<AbortController | null>(null)
  const key = `zest-web-pending-${connection?.port || ''}`
  useEffect(() => {
    const lookup = new AbortController()
    try {
      const saved = localStorage.getItem(key) || ''
      if (/^[a-f0-9-]{36}$/.test(saved)) {
        setRecoveryId(saved)
        if (connection) void readApi<Row>(connection, `/api/operations/${saved}`, lookup.signal)
          .then(record => { if (!lookup.signal.aborted && !running.current) setOperation(record) }).catch(() => {})
      }
    } catch { /* Optional storage. */ }
    return () => { lookup.abort(); controller.current?.abort() }
  }, [key, connection])
  async function follow(record: Row, signal: AbortSignal): Promise<Row | undefined> {
    if (!connection) return
    setOperation(record)
    while (record.status === 'queued' || record.status === 'running') {
      await new Promise(resolve => setTimeout(resolve, 1000))
      record = await readApi<Row>(connection, `/api/operations/${record.id}`, signal)
      setOperation(record)
    }
    try { localStorage.removeItem(key) } catch { /* Optional storage. */ }
    setRecoveryId('')
    if (record.status === 'completed') return record.result as Row
    setError(String((record.error as Row | undefined)?.message || '本次操作需要核对，请查看保存的记录。'))
  }
  async function run(action: string, parameters: Row): Promise<Row | undefined> {
    if (!connection || running.current || recoveryId) return
    running.current = true; setBusy(true); setError('')
    const command = prepareCommand(action, parameters)
    setOperation({id: command.request_id, action, status:'submitting', created_at:new Date().toISOString()})
    const abort = new AbortController(); controller.current = abort
    try { localStorage.setItem(key, command.request_id) } catch { /* Keep only the UUID. */ }
    try {
      const record = await writeApi<Row>(connection, '/api/operations', command)
      return await follow(record, abort.signal)
    } catch (e) {
      if (e instanceof ApiError && [400, 403, 413, 422].includes(e.status)) {
        try { localStorage.removeItem(key) } catch { /* Optional storage. */ }
        if (e.status === 400) {
          setRecoveryId(command.request_id)
          try { localStorage.setItem(key, command.request_id) } catch { /* Optional storage. */ }
          try {
            const saved = await readApi<Row>(connection, `/api/operations/${command.request_id}`)
            setOperation(saved); setRecoveryId(command.request_id)
          } catch (lookupError) {
            if (lookupError instanceof ApiError && lookupError.status === 404) {
              setRecoveryId(''); try { localStorage.removeItem(key) } catch { /* Optional storage. */ }
            }
          }
        }
      } else setRecoveryId(command.request_id)
      if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '操作失败。')
    } finally { running.current = false; setBusy(false) }
  }
  async function recover(): Promise<{ action: string; result?: Row } | undefined> {
    if (!connection || running.current || !recoveryId) return
    running.current = true; setBusy(true); setError('')
    const abort = new AbortController(); controller.current = abort
    try {
      const saved = await readApi<Row>(connection, `/api/operations/${recoveryId}`, abort.signal)
      return { action: String(saved.action), result: await follow(saved, abort.signal) }
    }
    catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        try { localStorage.removeItem(key) } catch { /* Optional storage. */ }
        setRecoveryId(''); setError('未找到原请求，可重新发送。')
      } else if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '核对失败。')
    } finally { running.current = false; setBusy(false) }
  }
  async function startQueued() {
    if (!connection || operation?.status !== 'queued') return
    try { await writeApi(connection, `/api/operations/${operation.id}/start`, {}) }
    catch (e) { setError(e instanceof Error ? e.message : '启动失败。') }
  }
  return { run, recover, startQueued, operation, busy, error, recoveryId, blocked: busy || Boolean(recoveryId), clearError: () => setError('') }
}
