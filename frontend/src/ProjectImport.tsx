import type { FormEvent } from 'react'
import { Icon } from './Icons.tsx'
import { OperationStatus } from './OperationStatus.tsx'
import type { Row } from './api.ts'

export function ProjectImport({ path, onPath, busy, choosing, onChoose, onImport, error, operation = null, onStart }: {
  path: string; onPath(value: string): void; busy: boolean; choosing: boolean
  onChoose(): void; onImport(path: string): void; error: string
  operation?: Row | null
  onStart?(): void
}) {
  function submit(event: FormEvent) { event.preventDefault(); if (!busy && path.trim()) onImport(path) }
  return <form className="project-form" onSubmit={submit} aria-busy={busy}>
    <p className="dialog-description">选择本机 Git 项目根目录，点击后自动建立 .zest/、整理资料、理解项目并生成文档，无需逐条批准。安全的已提交资料会发送至你配置的 DeepSeek，每轮最多 32 次调用、30 分钟。</p>
    <button className="folder-choose-button" type="button" disabled={busy} onClick={onChoose}><Icon name="folder" />{choosing ? '请在系统窗口中选择…' : '在 Finder 中选择文件夹'}</button>
    <label>项目根目录<input autoComplete="off" name="path" required disabled={busy} value={path} onChange={event => onPath(event.target.value)} placeholder="也可以粘贴项目的完整路径" /></label>
    <button className="solid-button" disabled={busy || !path.trim()}>{busy && operation?.action === 'project.import' ? '正在接入项目…' : '接入并初始化'}</button>
    <OperationStatus operation={operation} busy={busy} onStart={onStart} />
    {error && <p className="error" role="alert">{error}</p>}
  </form>
}
