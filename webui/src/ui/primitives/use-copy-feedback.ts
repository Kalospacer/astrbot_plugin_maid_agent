
import { useCallback, useState } from 'react'
import { writeClipboard } from './clipboard.ts'

const COPIED_FEEDBACK_MS = 1000

export interface CopyFeedback {
  copied: boolean
  onCopy: () => void
}

export function useCopyFeedback(text: string): CopyFeedback {
  const [copied, setCopied] = useState(false)
  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(text).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, COPIED_FEEDBACK_MS)
    })
  }, [copied, text])
  return { copied, onCopy }
}
