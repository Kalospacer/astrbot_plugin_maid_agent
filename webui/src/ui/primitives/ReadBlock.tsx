
import { useCallback, useMemo, useState, useSyncExternalStore } from 'react'
import clsx from 'clsx'
import { writeClipboard } from './clipboard.ts'
import {
  grammarLoadCount,
  highlightLines,
  subscribeGrammarLoaded,
  type HighlightSpan,
} from './markdown/highlight.ts'
import css from './ReadBlock.module.css'

export const DEFAULT_READ_MAX_LINES = 16

export interface ReadBlockLine {
  number: number
  text: string
}

export interface ReadBlockProps {
  label?: string | undefined
  lines: readonly ReadBlockLine[]
  totalLines: number
  lang?: string | undefined
  maxLines?: number | undefined
  className?: string | undefined
}

function renderSpans(spans: readonly HighlightSpan[]) {
  return spans.map((span, index) => <span key={index} style={span.style}>{span.text}</span>)
}

export function ReadBlock({
  label,
  lines,
  totalLines,
  lang,
  maxLines = DEFAULT_READ_MAX_LINES,
  className,
}: ReadBlockProps) {
  const raw = useMemo(() => lines.map(line => line.text).join('\n'), [lines])
  const loaded = useSyncExternalStore(subscribeGrammarLoaded, grammarLoadCount, grammarLoadCount)
  const highlighted = useMemo(() => highlightLines(raw, lang), [raw, lang, loaded])
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(raw).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, 1000)
    })
  }, [copied, raw])

  const onToggle = useCallback(() => { setExpanded(value => !value) }, [])

  const hidden = lines.length - maxLines
  const capped = hidden > 0 && !expanded
  const headLines = Math.ceil(maxLines / 2)
  const tailLines = maxLines - headLines
  const windowed = lines.length < totalLines

  const rows = (slice: readonly (readonly [ReadBlockLine, readonly HighlightSpan[] | undefined])[]) =>
    slice.map(([line, spans]) => (
      <div key={line.number} className={css.line}>
        <span className={css.gutter} aria-hidden>{line.number}</span>
        <span className={css.content}>{spans === undefined ? line.text : renderSpans(spans)}</span>
      </div>
    ))

  const paired = lines.map((line, index): readonly [ReadBlockLine, readonly HighlightSpan[] | undefined] =>
    [line, highlighted?.[index]])

  return (
    <div className={clsx(css.block, className)} data-read="">
      <div className={css.banner}>
        <div className={css.label}>{label ?? ''}</div>
        <div className={css.action}>
          {windowed && (
            <span className={css.count}>{`显示 ${lines.length} / ${totalLines} 行`}</span>
          )}
          <span className={css.lang}>{lang ?? ''}</span>
          {lines.length > 0 && (
            <button type="button" className={css.copyButton} onClick={onCopy}>
              {copied ? '复制成功' : '复制'}
            </button>
          )}
        </div>
      </div>
      <div className={css.body}>
        {rows(capped ? paired.slice(0, headLines) : paired)}
        {hidden > 0 && (
          <button
            type="button"
            className={css.expand}
            aria-expanded={expanded}
            aria-label={expanded ? '收起内容' : `展开其余 ${hidden} 行`}
            onClick={onToggle}
          >
            {expanded ? '收起' : `… 其余 ${hidden} 行`}
          </button>
        )}
        {capped && rows(paired.slice(paired.length - tailLines))}
      </div>
    </div>
  )
}
