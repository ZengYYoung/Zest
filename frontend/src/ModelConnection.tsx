import { useEffect, useState } from 'react'
import { readApi, writeApi, statusLabels, type Connection, type Row } from './api.ts'
import { Icon } from './Icons.tsx'

const object = (v: unknown): Row => v && typeof v === 'object' ? v as Row : {}
export function ModelConnection({ connection, check, disabled, onCommand, refresh, onConfigured }: {
  connection: Connection; check: Row | null; disabled: boolean; onCommand(action: string, p: Row): void; refresh: number
  onConfigured?(): void
}) {
  const [state, setState] = useState<Row>({}), [error, setError] = useState(''), [consent, setConsent] = useState(false)
  const [key, setKey] = useState(''), [saving, setSaving] = useState(false), [saved, setSaved] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    readApi<Row>(connection, '/api/model', controller.signal).then(setState).catch(e => { if (!controller.signal.aborted) setError(String(e)) })
    return () => controller.abort()
  }, [connection, refresh, check])
  useEffect(() => { setConsent(false) }, [check?.id])
  const config = object(state.configuration), latest = object(state.latest_check)
  const current = check || (latest.id ? latest : null), envelope = object(current?.envelope), result = object(current?.result)
  const passed = Boolean(state.connected)
  const target = object(envelope.target)
  const changed = Boolean(config.configured && current && (envelope.configuration_revision !== config.revision ||
    target.provider !== config.provider || target.model !== config.name || target.base_url !== config.base_url))
  async function save(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true); setError(''); setSaved(false)
    try {
      const configuration = await writeApi<Row>(connection, '/api/model/settings', { api_key: key })
      setKey(''); setConsent(false); setSaved(true)
      setState(previous => ({ ...previous, configuration, connected: false, check_matches_target: false }))
      onConfigured?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败，请重新打开设置核对。')
    } finally { setSaving(false) }
  }
  return <section className="model-settings">
    <div className="section-kicker">MODEL CONNECTION</div><h2>连接 DeepSeek</h2><p>只需填写 API Key，保存后即可用于对话。密钥仅保存在本机服务端。</p>
    <form className="model-key-form" onSubmit={save}>
      <label htmlFor="deepseek-api-key">DeepSeek API Key</label>
      <input id="deepseek-api-key" type="password" autoComplete="off" spellCheck={false} minLength={8} maxLength={4096} required value={key}
        placeholder={config.configured ? '已配置，填写新密钥以替换' : 'sk-…'} disabled={disabled || saving}
        onChange={event => { setKey(event.target.value); setSaved(false) }} aria-describedby="model-key-help" />
      <p id="model-key-help" className="muted">内置 DeepSeek Flash 和官方接口地址，无需选择模型。</p>
      <button className="solid-button" disabled={disabled || saving || key.length < 8}>{saving ? '正在保存…' : '保存密钥'}</button>
      {saved && <p role="status">密钥已保存，可以返回对话使用。尚未进行真实调用验证。</p>}
    </form>
    <div className="model-summary"><div className="model-emblem"><Icon name="model" size={24} /></div><div><strong>{String(config.name || 'DeepSeek Flash')}</strong><span>{String(config.configured_base_url || 'https://api.deepseek.com')}</span></div>
      <span className={`connection-badge ${passed ? 'connected' : ''}`}>{passed ? '最近连接测试通过' : config.configured ? '已配置 · 待实测' : '未配置'}</span></div>
    <details className="model-test" open={Boolean(current)}><summary>连接测试（可选）</summary><div className="model-help"><h3>一次有记录的真实请求</h3><p>只发送内置的两行加法示例，不包含项目源码。完整请求可先审阅；最多调用 1 次，不自动重试。</p>
      <button className="solid-button" disabled={disabled || saving || !config.configured} onClick={() => onCommand('model.prepare', {})}>准备真实连接测试</button></div>
    {current && <div className="model-check"><div className="section-heading"><h3>连接测试</h3><span className="status">{statusLabels[String(current.status)] || String(current.status)}</span></div>
      {changed && <p className="muted">历史测试：配置已变化，请重新准备测试。</p>}
      <div className="check-facts"><span>真实调用上限 <strong>1 次</strong></span><span>项目资料 <strong>不发送</strong></span><span>自动重试 <strong>关闭</strong></span></div>
      <details className="json-details"><summary>审阅完整测试请求、来源与批准身份</summary><pre>{JSON.stringify(envelope, null, 2)}</pre><code>{String(current.approval_sha256)}</code></details>
      {current.status === 'awaiting_approval' && <><label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我已审阅，允许向上述接口发送这个合成示例，最多调用一次。</label>
        <button className="solid-button" disabled={disabled || saving || changed || !consent} onClick={() => onCommand('model.decide', { id: current.id, approved: true, sha256: current.approval_sha256 })}>执行真实连接测试</button>
        <button className="quiet-button" disabled={disabled} onClick={() => onCommand('model.decide', { id: current.id, approved: false })}>取消测试</button></>}
      {current.status === 'passed' && <div className="connection-result"><strong>真实模型已返回有效回答</strong><p>{String(object(result.answer).answer)}</p><div className="check-facts"><span>耗时 <strong>{String(result.elapsed_seconds)} 秒</strong></span><span>总 token <strong>{String(object(result.usage).total_tokens ?? '未返回')}</strong></span></div></div>}
      {Boolean(current.error) && <p className="error">{String(object(current.error).message)}</p>}
      {Boolean(result.receipt) && <details className="json-details"><summary>实际模型响应与用量收据</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
    </div>}</details>
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
