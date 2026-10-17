import { useEffect, useState } from 'react'
import { readApi, statusLabels, type Connection, type Row } from './api.ts'
import { Icon } from './Icons.tsx'

const object = (v: unknown): Row => v && typeof v === 'object' ? v as Row : {}
export function ModelConnection({ connection, check, disabled, onCommand, refresh }: {
  connection: Connection; check: Row | null; disabled: boolean; onCommand(action: string, p: Row): void; refresh: number
}) {
  const [state, setState] = useState<Row>({}), [error, setError] = useState(''), [consent, setConsent] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    readApi<Row>(connection, '/api/model', controller.signal).then(setState).catch(e => { if (!controller.signal.aborted) setError(String(e)) })
    return () => controller.abort()
  }, [connection, refresh, check])
  useEffect(() => { setConsent(false) }, [check?.id])
  const config = object(state.configuration), latest = object(state.latest_check)
  const current = check || (latest.id ? latest : null), envelope = object(current?.envelope), result = object(current?.result)
  const passed = Boolean(state.connected)
  return <section className="model-settings">
    <div className="section-kicker">MODEL CONNECTION</div><h2>让工作台连接真实模型。</h2><p>服务在本机运行，回答来自你配置的模型 API。密钥留在服务端。</p>
    <div className="model-summary"><div className="model-emblem"><Icon name="model" size={24} /></div><div><strong>{String(config.name || '尚未配置模型')}</strong><span>{String(config.base_url || '在 .env 中填写模型配置')}</span></div>
      <span className={`connection-badge ${passed ? 'connected' : ''}`}>{passed ? '最近连接测试通过' : config.configured ? '已配置 · 待实测' : '未配置'}</span></div>
    <div className="model-help"><h3>一次有记录的真实请求</h3><p>只发送内置的两行加法示例，不包含项目源码。完整请求可先审阅；最多调用 1 次，不自动重试。</p>
      <button className="solid-button" disabled={disabled || !config.configured} onClick={() => onCommand('model.prepare', {})}>准备真实连接测试</button></div>
    {!config.configured && <pre className="config-example">{'ZEST_MODEL_PROVIDER=deepseek\nZEST_MODEL_NAME=你的模型名\nZEST_MODEL_BASE_URL=https://api.deepseek.com\nZEST_MODEL_API_KEY=在本地填写\nZEST_MODEL_TIMEOUT=60'}</pre>}
    {current && <div className="model-check"><div className="section-heading"><h3>连接测试</h3><span className="status">{statusLabels[String(current.status)] || String(current.status)}</span></div>
      <div className="check-facts"><span>真实调用上限 <strong>1 次</strong></span><span>项目资料 <strong>不发送</strong></span><span>自动重试 <strong>关闭</strong></span></div>
      <details className="json-details"><summary>审阅完整测试请求、来源与批准身份</summary><pre>{JSON.stringify(envelope, null, 2)}</pre><code>{String(current.approval_sha256)}</code></details>
      {current.status === 'awaiting_approval' && <><label className="consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />我已审阅，允许向上述接口发送这个合成示例，最多调用一次。</label>
        <button className="solid-button" disabled={disabled || !consent} onClick={() => onCommand('model.decide', { id: current.id, approved: true, sha256: current.approval_sha256 })}>执行真实连接测试</button>
        <button className="quiet-button" disabled={disabled} onClick={() => onCommand('model.decide', { id: current.id, approved: false })}>取消测试</button></>}
      {current.status === 'passed' && <div className="connection-result"><strong>真实模型已返回有效回答</strong><p>{String(object(result.answer).answer)}</p><div className="check-facts"><span>耗时 <strong>{String(result.elapsed_seconds)} 秒</strong></span><span>总 token <strong>{String(object(result.usage).total_tokens ?? '未返回')}</strong></span></div></div>}
      {Boolean(current.error) && <p className="error">{String(object(current.error).message)}</p>}
      {Boolean(result.receipt) && <details className="json-details"><summary>实际模型响应与用量收据</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
    </div>}
    {error && <p className="error">{error}</p>}
  </section>
}
