import type { ReactNode } from 'react'
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    settings: <><path d="M9 4 10 2h4l1 2 3 2 2 1 1 4-2 2-1 3v2l-3 3-3-1-3 1-3-3v-3l-2-2-1-4 2-2 3-1Z" /><circle cx="12" cy="12" r="3" /></>,
    overview: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
    tasks: <><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2A8.5 8.5 0 0 1 21 11.5Z" /><path d="M7 9h9M7 13h6" /></>,
    structure: <><path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16" /></>,
    cognition: <><path d="M12 3a4 4 0 0 0-4 3 4 4 0 0 0-3 6 4 4 0 0 0 3 6 4 4 0 0 0 4 3m0-18a4 4 0 0 1 4 3 4 4 0 0 1 3 6 4 4 0 0 1-3 6 4 4 0 0 1-4 3V3Z" /><path d="M8 9h4m0 6h4" /></>,
    repairs: <><path d="m14 6 4-4 4 4-4 4m-4-4L3 17v4h4L18 10" /></>,
    verifications: <><path d="m7 12 3 3 7-7" /><rect x="3" y="3" width="18" height="18" rx="5" /></>,
    knowledge: <><path d="M12 6c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V5c-4-1-7-1-10 1Zm0 0v15" /></>,
    releases: <><path d="M3 11a9 9 0 1 1 2 7M3 4v7h7m2-4v5l3 2" /></>,
    evaluations: <><path d="M4 20V10m8 10V4m8 16v-7M2 21h20" /></>,
    resolutions: <><path d="m6 12 4 4L20 6M4 3h11M4 21h16" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" /></>,
    chevron: <path d="m9 5 7 7-7 7" />,
    newChat: <><path d="M12 4H6a3 3 0 0 0-3 3v11a3 3 0 0 0 3 3h11a3 3 0 0 0 3-3v-6" /><path d="m14 3 3-2 4 4-2 3-8 8-5 1 1-5 7-9Z" /></>,
    folderOpen: <><path d="M3 17V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2" /><path d="M6 11h15l-3 9H3l3-9Z" /></>,
    sidebar: <><rect x="3" y="3" width="18" height="18" rx="3" /><path d="M9 3v18M5.5 8h1M5.5 12h1" /></>,
    arrow: <path d="m12 19 0-14m-6 6 6-6 6 6" />,
    refresh: <><path d="M20 7a8 8 0 1 0 1 8M20 2v6h-6" /></>,
    model: <><rect x="5" y="5" width="14" height="14" rx="4" /><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4m-14-6h4v4h-4Z" /></>,
    folder: <path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z" />,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.folder}</svg>
}
