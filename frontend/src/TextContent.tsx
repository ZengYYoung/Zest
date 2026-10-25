import { Fragment, createElement, type ReactNode } from 'react'

function inline(text: string, onLink?: (target: string) => void) {
  return text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g).map((part, index) => {
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
    if (link) return onLink ? <button key={index} className="document-link" onClick={() => onLink(link[2])}>{link[1]}</button> : <span key={index}>{link[1]} ({link[2]})</span>
    return part.startsWith('`') && part.endsWith('`')
    ? <code key={index}>{part.slice(1, -1)}</code> : part.startsWith('**') && part.endsWith('**')
      ? <strong key={index}>{part.slice(2, -2)}</strong> : part
  })
}

function cells(line: string) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'))
}

function prose(text: string, onLink?: (target: string) => void) {
  const lines = text.split('\n'), nodes: ReactNode[] = []
  const heading = (line: string) => line.match(/^(#{1,4})\s+(.+)$/)
  const list = (line: string) => line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)$/)
  const table = (i: number) => i + 1 < lines.length && lines[i].includes('|') && cells(lines[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell))
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i++; continue }
    const h = heading(lines[i])
    if (h) { nodes.push(createElement(`h${Math.min(6, h[1].length + 2)}`, { key: i }, inline(h[2], onLink))); i++; continue }
    if (table(i)) {
      const key = i, headers = cells(lines[i]), body: string[][] = []; i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { body.push(cells(lines[i])); i++ }
      nodes.push(<div className="document-table" key={key}><table><thead><tr>{headers.map((cell, n) => <th key={n} scope="col">{inline(cell, onLink)}</th>)}</tr></thead>
        <tbody>{body.map((row, n) => <tr key={n}>{headers.map((_, col) => <td key={col}>{inline(row[col] || '', onLink)}</td>)}</tr>)}</tbody></table></div>); continue
    }
    const item = list(lines[i])
    if (item) {
      const key = i, ordered = !item[1], items: ReactNode[] = []
      while (i < lines.length) {
        const row = list(lines[i]); if (!row || Boolean(!row[1]) !== ordered) break
        items.push(<li key={i}>{inline(row[2], onLink)}</li>); i++
      }
      nodes.push(ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>); continue
    }
    const key = i, paragraph = [lines[i++]]
    while (i < lines.length && lines[i].trim() && !heading(lines[i]) && !list(lines[i]) && !table(i)) paragraph.push(lines[i++])
    nodes.push(<p key={key}>{inline(paragraph.join('\n'), onLink)}</p>)
  }
  return nodes
}

// Render engineering Markdown as inert React text. No HTML or URL execution.
export function TextContent({ text, onLink }: { text: string; onLink?: (target: string) => void }) {
  const content = text.replace(/^---\n(?:title|description|observed_commit):[^\n]*\n(?:(?:title|description|observed_commit):[^\n]*\n)*---\n/, '')
  const blocks = content.split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g)
  return <div className="text-content">{blocks.map((block, i) => block.startsWith('```')
    ? <pre key={i}><code>{block.replace(/^```[^\n]*\n/, '').replace(/```$/, '')}</code></pre>
    : <Fragment key={i}>{prose(block, onLink)}</Fragment>)}</div>
}
