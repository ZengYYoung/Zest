import { useEffect, useRef, type ReactNode } from 'react'

export function Dialog({ title, onClose, children, wide = false }: { title: string; onClose(): void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => { if (dialog?.open) dialog.close() }
  }, [])
  return <dialog ref={ref} className={`workspace-dialog ${wide ? 'wide' : ''}`} aria-label={title} onCancel={onClose} onClick={event => { if (event.target === ref.current) onClose() }}>
    <div className="dialog-heading"><h2>{title}</h2><button autoFocus className="icon-button" aria-label="关闭" onClick={onClose}>×</button></div>
    <div className="dialog-body">{children}</div>
  </dialog>
}
