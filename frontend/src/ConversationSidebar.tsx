import { useEffect, useState } from 'react'
import { Icon } from './Icons.tsx'
import { projectName, type Project, type Row } from './api.ts'

export function ConversationSidebar({ projects, projectId, conversations, savedLists, activeId, busy, loading, onProject, onSelect, onNew, onAdd, onRemove }: {
  projects: Project[]; projectId: string; conversations: Row[]; savedLists: Record<string, Row[]>
  activeId: string; busy: boolean; loading: boolean
  onProject(id: string): void; onSelect(projectId: string, id: string): void; onNew(): void; onAdd(): void; onRemove(project: Project): void
}) {
  const [expanded, setExpanded] = useState(() => new Set([projectId]))
  useEffect(() => { if (projectId) setExpanded(previous => new Set([...previous, projectId])) }, [projectId])
  function choose(id: string) {
    setExpanded(previous => {
      const next = new Set(previous)
      if (id === projectId && next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    onProject(id)
  }
  return <nav className="conversation-sidebar" aria-label="项目与对话导航">
    <button className="sidebar-new" disabled={busy || !projectId} onClick={onNew}><Icon name="newChat" size={20} />新对话</button>
    <div className="sidebar-section-heading"><h2>项目</h2><button className="icon-button" aria-label="接入项目" title="接入项目" disabled={busy} onClick={onAdd}><Icon name="plus" size={17} /></button></div>
    <div className="sidebar-projects">
      {projects.map(project => {
        const selected = project.id === projectId, open = expanded.has(project.id)
        const list = selected ? conversations : savedLists[project.id] || []
        return <section className="project-nav-group" key={project.id}>
          <div className="sidebar-project-row"><button className="sidebar-project-button" title={project.root_path} aria-label={`选择项目 ${projectName(project)}`}
            aria-expanded={open} aria-current={selected ? 'true' : undefined} disabled={busy} onClick={() => choose(project.id)}>
            <Icon name={open ? 'folderOpen' : 'folder'} size={19} /><span>{projectName(project)}</span><Icon name="chevron" size={13} />
          </button><button className="icon-button project-remove-button" aria-label={`删除项目 ${projectName(project)}`} title="从 Zest 移除项目" disabled={busy} onClick={() => onRemove(project)}><Icon name="trash" size={15} /></button></div>
          {open && <div className="sidebar-conversations" role="group" aria-label={`${projectName(project)}的对话`}>
            {list.length ? list.map(item => {
              const title = String(item.title || '新对话')
              return <button key={String(item.id)} title={title} aria-current={selected && item.id === activeId ? 'page' : undefined}
                disabled={busy} onClick={() => onSelect(project.id, String(item.id))}><span>{title}</span></button>
            }) : <p className="sidebar-empty" role={selected && loading ? 'status' : undefined}>{selected && loading ? '正在读取对话…' : '还没有对话，开始聊聊吧。'}</p>}
          </div>}
        </section>
      })}
      {!projects.length && <p className="sidebar-empty">接入项目，开始你的第一个对话。</p>}
    </div>
  </nav>
}
