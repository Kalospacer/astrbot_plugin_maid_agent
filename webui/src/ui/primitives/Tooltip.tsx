
import { cloneElement, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FocusEventHandler, MouseEventHandler, MutableRefObject, ReactElement, Ref } from 'react'
import css from './Tooltip.module.css'

export type TooltipSide = 'right' | 'bottom' | 'top'

interface AnchorProps {
  ref?: Ref<HTMLElement> | undefined
  onMouseEnter?: MouseEventHandler | undefined
  onMouseLeave?: MouseEventHandler | undefined
  onFocus?: FocusEventHandler | undefined
  onBlur?: FocusEventHandler | undefined
}

type TooltipLabel = string | (() => string)

export function Tooltip({ label, side = 'right', delayMs = 0, disabled = false, maxWidth, children }: { label: TooltipLabel; side?: TooltipSide; delayMs?: number; disabled?: boolean; maxWidth?: number; children: ReactElement<AnchorProps> }) {
  const anchor = useRef<HTMLElement | null>(null)
  const childRef = (children as ReactElement<AnchorProps> & { ref?: Ref<HTMLElement> }).ref
  const mergedRef = useCallback((el: HTMLElement | null) => {
    anchor.current = el
    if (typeof childRef === 'function') childRef(el)
    else if (childRef != null) (childRef as MutableRefObject<HTMLElement | null>).current = el
  }, [childRef])
  const [pos, setPos] = useState<{ x: number; top: number; bottom: number } | null>(null)
  const [placement, setPlacement] = useState<TooltipSide>(side)
  const bubble = useRef<HTMLSpanElement | null>(null)
  const resolvedLabel = pos === null
    ? null
    : typeof label === 'function' ? label() : label
  const y = pos === null
    ? 0
    : placement === 'right'
      ? pos.top + (pos.bottom - pos.top) / 2
      : placement === 'top' ? pos.top - 8 : pos.bottom + 8
  const EDGE_MARGIN = 12
  useLayoutEffect(() => {
    if (pos === null) return
    const fit = () => {
      const el = bubble.current
      if (el === null) return
      el.style.left = `${pos.x}px`
      const r = el.getBoundingClientRect()
      let dx = 0
      if (r.right > window.innerWidth - EDGE_MARGIN) dx = window.innerWidth - EDGE_MARGIN - r.right
      if (r.left + dx < EDGE_MARGIN) dx = EDGE_MARGIN - r.left
      el.style.left = `${pos.x + dx}px`
      if (side === 'right') return
      const fitsBelow = pos.bottom + 8 + r.height <= window.innerHeight - EDGE_MARGIN
      const fitsAbove = pos.top - 8 - r.height >= EDGE_MARGIN
      if (placement === 'bottom' && !fitsBelow && fitsAbove) setPlacement('top')
      if (placement === 'top' && !fitsAbove && fitsBelow) setPlacement('bottom')
    }
    fit()
    window.addEventListener('resize', fit)
    return () => { window.removeEventListener('resize', fit) }
  }, [placement, pos, resolvedLabel, side])
  const showTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const triggers = useRef({ hover: false, focus: false })

  const cancelShow = useCallback(() => {
    if (showTimer.current === null) return
    clearTimeout(showTimer.current)
    showTimer.current = null
  }, [])
  useEffect(() => {
    if (disabled) {
      cancelShow()
      triggers.current = { hover: false, focus: false }
      setPos(null)
    }
    return cancelShow
  }, [cancelShow, disabled])

  const show = () => {
    if (disabled) return
    const el = anchor.current
    if (el === null) return
    const r = el.getBoundingClientRect()
    setPlacement(side)
    setPos({ x: side === 'right' ? r.right + 10 : r.left + r.width / 2, top: r.top, bottom: r.bottom })
  }
  const showAfterHoverDelay = () => {
    cancelShow()
    if (delayMs <= 0) {
      show()
      return
    }
    showTimer.current = setTimeout(() => {
      showTimer.current = null
      show()
    }, delayMs)
  }
  const hide = () => {
    cancelShow()
    if (!triggers.current.hover && !triggers.current.focus) setPos(null)
  }

  return (
    <>
      {cloneElement(children, {
        ref: mergedRef,
        onMouseEnter: (e) => { children.props.onMouseEnter?.(e); triggers.current.hover = true; showAfterHoverDelay() },
        onMouseLeave: (e) => { children.props.onMouseLeave?.(e); triggers.current.hover = false; cancelShow(); setPos(null) },
        onFocus: (e) => { children.props.onFocus?.(e); triggers.current.focus = true; cancelShow(); show() },
        onBlur: (e) => { children.props.onBlur?.(e); triggers.current.focus = false; hide() },
      })}
      {pos !== null && (
        <span
          ref={bubble}
          className={css.bubble}
          data-side={placement}
          style={{ left: pos.x, top: y, ...maxWidth === undefined ? {} : { maxWidth } }}
          role="tooltip"
        >
          {resolvedLabel}
        </span>
      )}
    </>
  )
}
