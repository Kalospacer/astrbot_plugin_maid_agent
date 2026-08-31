
import { useMemo, useState } from 'react'
import css from './JsonBlock.module.css'

const MAX_CHARS = 20_000

function defaultTruncatedLabel(total: number): string {
  return `… 已截断，共 ${total} 字符`
}

export function JsonBlock({ label, payload, defaultOpen = false, truncatedLabel = defaultTruncatedLabel }: {
  label: string
  payload: unknown
  defaultOpen?: boolean
  truncatedLabel?: ((total: number) => string) | undefined
}) {
  const [open, setOpen] = useState(defaultOpen)
  const body = useMemo(() => {
    if (!open) return ''
    let s: string
    try {
      s = JSON.stringify(payload, null, 2) ?? String(payload)
    } catch {
      s = String(payload)
    }
    return s.length > MAX_CHARS ? `${s.slice(0, MAX_CHARS)}\n${truncatedLabel(s.length)}` : s
  }, [open, payload, truncatedLabel])
  return (
    <div className={css.root}>
      <button type="button" className={css.toggle} onClick={() => { setOpen(v => !v) }}>
        {open ? '▾' : '▸'} {label}
      </button>
      {open && <pre className={css.body}>{body}</pre>}
    </div>
  )
}
