
import { useCallback, useMemo, useRef, useState } from 'react'
import clsx from 'clsx'
import { writeClipboard } from '../clipboard.ts'
import { highlightToHtml } from './highlight.ts'
import css from './CodeBlock.module.css'

export interface CodeBlockProps {
  code: string
  lang?: string | undefined
  className?: string | undefined
  copyLabel?: string | undefined
  copiedLabel?: string | undefined
}

export function CodeBlock({ code, lang, className, copyLabel = '复制', copiedLabel = '复制成功' }: CodeBlockProps) {
  const trimmed = code.endsWith('\n') ? code.slice(0, -1) : code
  const html = useMemo(() => highlightToHtml(trimmed, lang), [trimmed, lang])
  const rootRef = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)

  const onCopy = useCallback(() => {
    if (copied) return
    const text = rootRef.current?.querySelector('pre')?.textContent ?? trimmed
    void writeClipboard(text).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, 1000)
    })
  }, [copied, trimmed])

  const body = html === undefined
    ? (
      <pre className={css.plain}><code>{trimmed}</code></pre>
    )
    : (
      <div dangerouslySetInnerHTML={{ __html: html }} />
    )

  return (
    <div ref={rootRef} className={clsx(css.block, 'md-code-block', className)}>
      <div className={css.bannerWrap}>
        <div className={css.banner}>
          <div className={css.infostring}>{lang ?? ''}</div>
          <div className={css.action}>
            <button type="button" className={css.copyButton} onClick={onCopy}>
              {copied ? copiedLabel : copyLabel}
            </button>
          </div>
        </div>
      </div>
      {body}
    </div>
  )
}
