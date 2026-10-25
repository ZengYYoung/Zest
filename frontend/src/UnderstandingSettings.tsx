import { useEffect, useState } from 'react'
import { readApi, type Connection, type Row } from './api.ts'

export function UnderstandingSettings({connection, projectId, refresh, busy, onCommand}: {
  connection: Connection; projectId: string; refresh: number; busy: boolean
  onCommand(action: string, parameters: Row): Promise<void> | void
}) {
  const [value, setValue] = useState<Row | null>(null), [error, setError] = useState('')
  useEffect(() => {
    const abort = new AbortController()
    readApi<Row>(connection, `/api/projects/${projectId}/understanding-approval`, abort.signal)
      .then(next => { if (!abort.signal.aborted) { setValue(next); setError('') } })
      .catch(e => { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '无法读取自动批准设置。') })
    return () => abort.abort()
  }, [connection, projectId, refresh])
  return <UnderstandingSettingsCard value={value} busy={busy} error={error}
    onChange={enabled => onCommand('understanding.configure', {project_id: projectId, enabled})} />
}

export function UnderstandingSettingsCard({value, busy, error = '', onChange}: {
  value: Row | null; busy: boolean; error?: string; onChange(enabled: boolean): Promise<void> | void
}) {
  const policy = value?.policy as Row | undefined
  return <section className="settings-section" aria-label="项目理解设置"><h3>项目理解</h3>
    <label className="understanding-toggle"><span>自动批准项目理解</span><input type="checkbox" role="switch"
      aria-label="自动批准项目理解" checked={Boolean(value?.enabled)} disabled={busy || !value}
      onChange={event => { void onChange(event.target.checked) }} /></label>
    <p className="muted">只对当前项目生效。开启后，初始化、补全、增量更新连续执行；项目范围内的检索、补读与请求分批自动处理，无需逐次点击。</p>
    <p className="muted">项目的安全资料会发送至已配置的模型；每轮最多 {String(policy?.max_model_calls || 32)} 次调用、{Math.round(Number(policy?.max_seconds || 1800) / 60)} 分钟。仅阅读 Git 已提交资料，不运行或修改项目。</p>
    <p className="muted">同一模型下更新密钥或内部调查规则无需重新开启。模型目标或资料访问权限变化时重新确认。关闭后不再自动开始新任务，已批准的任务继续；结果未知的请求不会自动重发。</p>
    {value?.scope_mode === 'legacy_package' && Boolean(value?.active) && <p className="muted">当前沿用旧版固定资料包授权，重新开启后使用项目范围读取。</p>}
    {value?.scope_mode === 'legacy_package' && Boolean(value?.active) && <button className="quiet-button" disabled={busy} onClick={() => { void onChange(true) }}>启用项目范围自动理解</button>}
    {value && <p role="status">{String(value.message || (value.active ? '已开启自动批准。' : '尚未开启自动批准。'))}</p>}
    {Boolean(value?.enabled) && !value?.active && <button className="quiet-button" disabled={busy} onClick={() => { void onChange(true) }}>按当前模型重新开启</button>}
    {error && <p role="alert" className="error">{error}</p>}
  </section>
}
