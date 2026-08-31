
import { useCallback, useEffect, useRef } from 'react'

export const POINTER_GRACE_MS = 200

export interface PointerGrace {
  arm: () => void
  cancel: () => void
}

export function usePointerGrace(close: () => void): PointerGrace {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closeRef = useRef(close)
  closeRef.current = close

  const cancel = useCallback(() => {
    if (timerRef.current === null) return
    clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const arm = useCallback(() => {
    cancel()
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      closeRef.current()
    }, POINTER_GRACE_MS)
  }, [cancel])

  useEffect(() => cancel, [cancel])

  return { arm, cancel }
}
