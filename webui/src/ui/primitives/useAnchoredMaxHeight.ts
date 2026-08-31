import { useLayoutEffect, useState } from 'react'
import type { RefObject } from 'react'

const MARGIN = 12

export function useAnchoredMaxHeight(ref: RefObject<HTMLElement>, cap: number, signal: unknown): number {
  const [maxHeight, setMaxHeight] = useState(cap)
  useLayoutEffect(() => {
    const el = ref.current
    if (el === null) return
    const fit = () => {
      setMaxHeight(Math.min(cap, Math.max(0, el.getBoundingClientRect().bottom - MARGIN)))
    }
    fit()
    window.addEventListener('resize', fit)
    window.addEventListener('scroll', fit, true)
    return () => {
      window.removeEventListener('resize', fit)
      window.removeEventListener('scroll', fit, true)
    }
  }, [ref, cap, signal])
  return maxHeight
}
