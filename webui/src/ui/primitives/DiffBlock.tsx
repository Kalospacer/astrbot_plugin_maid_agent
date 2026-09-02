
import { useCallback, useMemo, useState } from 'react'
import clsx from 'clsx'
import { writeClipboard } from './clipboard.ts'
import css from './DiffBlock.module.css'

export const DEFAULT_DIFF_MAX_LINES = 16

export interface DiffHunk {
  path: string
  oldText: string | null
  newText: string
}

export interface DiffBlockProps {
  diffs: DiffHunk[]
  maxLines?: number | undefined
  className?: string | undefined
}

interface DiffRow {
  kind: 'path' | 'del' | 'add' | 'gap'
  text: string
}

function assertNever(value: never): never {
  throw new Error(`unreachable diff row kind: ${String(value)}`)
}

const ROW_CLASS: Record<DiffRow['kind'], string | undefined> = {
  path: css.path,
  del: css.del,
  add: css.add,
  gap: css.gap,
}

/** 行级 +/− 计数（DSH diffTotals）：折叠摘要的 +n -m 尾标。 */
export function diffTotals(diffs: DiffHunk[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const diff of diffs) {
    if (diff.oldText !== null) removed += contentLines(diff.oldText).length
    added += contentLines(diff.newText).length
  }
  return { added, removed }
}

function buildRows(diffs: DiffHunk[]): { rows: DiffRow[]; added: number; removed: number; files: number } {
  const rows: DiffRow[] = []
  const paths = new Set<string>()
  let added = 0
  let removed = 0
  let prevPath: string | undefined
  for (const diff of diffs) {
    paths.add(diff.path)
    if (diff.path !== prevPath) rows.push({ kind: 'path', text: diff.path })
    else rows.push({ kind: 'gap', text: '⋯' })
    prevPath = diff.path
    if (diff.oldText !== null) {
      for (const line of contentLines(diff.oldText)) {
        rows.push({ kind: 'del', text: line })
        removed++
      }
    }
    for (const line of contentLines(diff.newText)) {
      rows.push({ kind: 'add', text: line })
      added++
    }
  }
  return { rows, added, removed, files: paths.size }
}

function contentLines(text: string): string[] {
  if (text === '') return []
  const body = text.endsWith('\n') ? text.slice(0, -1) : text
  return body.split('\n')
}

function copyText(rows: DiffRow[]): string {
  return rows.map((row) => {
    switch (row.kind) {
      case 'del': return `- ${row.text}`
      case 'add': return `+ ${row.text}`
      case 'path': return row.text
      case 'gap': return row.text
      default: return assertNever(row.kind)
    }
  }).join('\n')
}

export function DiffBlock({ diffs, maxLines = DEFAULT_DIFF_MAX_LINES, className }: DiffBlockProps) {
  const { rows, added, removed, files } = useMemo(() => buildRows(diffs), [diffs])
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(copyText(rows)).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, 1000)
    })
  }, [copied, rows])

  const onToggle = useCallback(() => { setExpanded(value => !value) }, [])

  if (rows.length === 0) return null

  const hidden = rows.length - maxLines
  const capped = hidden > 0 && !expanded
  const headLines = Math.ceil(maxLines / 2)
  const tailLines = maxLines - headLines
  const head = capped ? rows.slice(0, headLines) : rows
  const tail = capped ? rows.slice(rows.length - tailLines) : []

  return (
    <div className={clsx(css.block, className)} data-diff="">
      <button type="button" className={css.copyButton} onClick={onCopy}>
        {copied ? '复制成功' : '复制'}
      </button>
      <div className={css.body}>
        {head.map((row, index) => (
          <div key={index} className={clsx(css.line, ROW_CLASS[row.kind])}>{row.text}</div>
        ))}
        {hidden > 0 && (
          <button
            type="button"
            className={css.expand}
            aria-expanded={expanded}
            aria-label={expanded ? '收起差异' : `展开其余 ${hidden} 行差异`}
            onClick={onToggle}
          >
            {expanded ? '收起' : `… 其余 ${hidden} 行`}
          </button>
        )}
        {tail.map((row, index) => (
          <div key={index} className={clsx(css.line, ROW_CLASS[row.kind])}>{row.text}</div>
        ))}
      </div>
      <div className={css.footer}>└ +{added} -{removed} · {files} file{files === 1 ? '' : 's'}</div>
    </div>
  )
}
