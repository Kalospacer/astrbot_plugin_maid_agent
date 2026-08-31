
import { useCallback, useState, type ReactNode } from 'react'
import clsx from 'clsx'
import { headTailCap } from './head-tail-cap.ts'
import { useCopyFeedback } from './use-copy-feedback.ts'
import css from './SearchBlock.module.css'

export const DEFAULT_SEARCH_MAX_LINES = 16

export interface SearchBlockLineMatch {
  lineNumber: number
  line: string
}

export interface SearchFileGroup {
  path: string
  matches: SearchBlockLineMatch[]
}

interface SearchBlockCommon {
  truncated: boolean
  total: number
  maxLines?: number | undefined
  className?: string | undefined
}

export interface SearchMatchesBlockProps extends SearchBlockCommon {
  kind: 'matches'
  files: SearchFileGroup[]
}

export interface SearchPathsBlockProps extends SearchBlockCommon {
  kind: 'paths'
  paths: string[]
}

export type SearchBlockProps = SearchMatchesBlockProps | SearchPathsBlockProps

type SearchRow =
  | { type: 'file'; path: string; count: number; index: number; collapsed: boolean }
  | { type: 'match'; lineNumber: number; line: string; key: string; fileIndex: number }
  | { type: 'path'; path: string }

function copyText(props: SearchBlockProps): string {
  if (props.kind === 'paths') return props.paths.join('\n')
  return props.files
    .map(file => [file.path, ...file.matches.map(m => `${m.lineNumber}: ${m.line}`)].join('\n'))
    .join('\n\n')
}

function shownCount(props: SearchBlockProps): number {
  return props.kind === 'paths'
    ? props.paths.length
    : props.files.reduce((sum, file) => sum + file.matches.length, 0)
}

function summaryText(props: SearchBlockProps, shown: number, truncated: boolean, total: number): string {
  const count = truncated ? `显示 ${shown} / 共 ${total}` : `${shown}`
  return props.kind === 'paths'
    ? `${count} 个路径`
    : `${count} 处匹配 · ${props.files.length} 个文件`
}

function toRows(props: SearchBlockProps, collapsed: ReadonlySet<number>): SearchRow[] {
  if (props.kind === 'paths') return props.paths.map((path): SearchRow => ({ type: 'path', path }))
  const rows: SearchRow[] = []
  props.files.forEach((file, index) => {
    const isCollapsed = collapsed.has(index)
    rows.push({ type: 'file', path: file.path, count: file.matches.length, index, collapsed: isCollapsed })
    if (isCollapsed) return
    for (const match of file.matches) {
      rows.push({ type: 'match', lineNumber: match.lineNumber, line: match.line, key: `${index}:${match.lineNumber}`, fileIndex: index })
    }
  })
  return rows
}

function rowKey(row: SearchRow): string {
  switch (row.type) {
    case 'match': return `match:${row.key}`
    case 'file': return `file:${row.index}`
    case 'path': return `path:${row.path}`
  }
}

export function SearchBlock(props: SearchBlockProps) {
  const { truncated, total, maxLines = DEFAULT_SEARCH_MAX_LINES, className } = props
  const [expanded, setExpanded] = useState(false)
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set())

  const rows = toRows(props, collapsed)
  const shown = shownCount(props)
  const empty = rows.length === 0
  const { copied, onCopy } = useCopyFeedback(copyText(props))

  const onToggle = useCallback(() => { setExpanded(value => !value) }, [])

  const toggleFile = useCallback((index: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }, [])

  const { hidden, capped, headLines, tailLines } = headTailCap(rows.length, maxLines, expanded)
  const head = capped ? rows.slice(0, headLines) : rows
  const naturalTail = capped ? rows.slice(rows.length - tailLines) : []
  const tailLead = naturalTail[0]
  const tailHeader = tailLead?.type === 'match'
    && !head.some(row => row.type === 'file' && row.index === tailLead.fileIndex)
    ? rows.find((row): row is Extract<SearchRow, { type: 'file' }> =>
      row.type === 'file' && row.index === tailLead.fileIndex)
    : undefined
  const tail = tailHeader === undefined ? naturalTail : naturalTail.slice(1)

  const renderRow = (row: SearchRow): ReactNode => {
    if (row.type === 'path') return <div className={css.line}>{row.path}</div>
    if (row.type === 'match') {
      return (
        <div className={css.line}>
          <span className={css.lineNumber}>{row.lineNumber}: </span>
          {row.line}
        </div>
      )
    }
    return (
      <button
        type="button"
        className={css.fileHeader}
        aria-expanded={!row.collapsed}
        onClick={() => { toggleFile(row.index) }}
      >
        <span className={css.filePath}>{row.path}</span>
        <span className={css.fileCount}>{row.count}</span>
      </button>
    )
  }

  return (
    <div className={clsx(css.block, className)} data-search={props.kind}>
      <div className={css.header}>
        <span className={css.summary}>{summaryText(props, shown, truncated, total)}</span>
        {!empty && (
          <button type="button" className={css.copyButton} onClick={onCopy}>
            {copied ? '复制成功' : '复制'}
          </button>
        )}
      </div>
      {empty
        ? <div className={css.empty}>无结果</div>
        : (
          <div className={css.body}>
            {head.map(row => (
              <div key={rowKey(row)}>{renderRow(row)}</div>
            ))}
            {hidden > 0 && (
              <button
                type="button"
                className={css.expand}
                aria-expanded={expanded}
                aria-label={expanded ? '收起结果' : `展开其余 ${hidden} 行结果`}
                onClick={onToggle}
              >
                {expanded ? '收起' : `… 其余 ${hidden} 行`}
              </button>
            )}
            {tailHeader !== undefined && (
              <div key={`tailHeader:${rowKey(tailHeader)}`}>{renderRow(tailHeader)}</div>
            )}
            {tail.map(row => (
              <div key={rowKey(row)}>{renderRow(row)}</div>
            ))}
          </div>
        )}
    </div>
  )
}
