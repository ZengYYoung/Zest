import { useEffect, useState, type ReactNode } from 'react'

export type Section = { id: string; title: string; description?: string; content: ReactNode }

/** Keep panels mounted so navigation does not discard drafts or in-flight operation state. */
export function SectionLayout({ label, sections, requestedSection, initialSection, onSectionChange }: { label: string; sections: Section[]; initialSection?: string; onSectionChange?(id: string): void; requestedSection?: { id: string; nonce: number } | null }) {
  const [selected, setSelected] = useState(initialSection || sections[0]?.id || '')
  useEffect(() => { if (requestedSection) setSelected(requestedSection.id) }, [requestedSection])
  const active = sections.some(section => section.id === selected) ? selected : sections[0]?.id
  return <div className="section-layout">
    <nav className="section-navigation" aria-label={label}>
      {sections.map(section => <button type="button" key={section.id} className={section.id === active ? 'selected' : ''}
        aria-current={section.id === active ? 'page' : undefined} onClick={() => { setSelected(section.id); onSectionChange?.(section.id) }}>{section.title}</button>)}
    </nav>
    <div className="section-panels">{sections.map(section => <section key={section.id} className="section-panel" hidden={section.id !== active} aria-label={section.title}>
      <div className="section-panel-heading"><h3>{section.title}</h3>{section.description && <p>{section.description}</p>}</div>
      {section.content}
    </section>)}</div>
  </div>
}
