
import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import clsx from 'clsx'
import { IconCloseOutline16 } from './icons/index.tsx'
import css from './Modal.module.css'

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])',
  'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * aria-modal 只是给辅助技术的声明，浏览器不会真的把焦点关在对话框里。
 * 没有这个陷阱的话 Tab 会走到背后的会话列表上，键盘用户会“掉出”弹窗。
 */
function useFocusTrap(open: boolean, dialogRef: React.RefObject<HTMLDivElement | null>): void {
  useEffect(() => {
    if (!open) return
    const restoreTo = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    // autoFocus 的元素优先，否则落到第一个可聚焦元素
    if (dialog !== null && !dialog.contains(document.activeElement)) {
      dialog.querySelector<HTMLElement>(FOCUSABLE)?.focus()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || dialog === null) return
      const items = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)]
        .filter(el => el.offsetParent !== null || el === document.activeElement)
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || !dialog.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      restoreTo?.focus?.()
    }
  }, [open, dialogRef])
}

export function Modal({
  open, onClose, title, closeLabel = 'Close', description, children, footer, className, contentClassName, headless = false,
}: {
  open: boolean
  onClose: () => void
  title: string
  closeLabel?: string
  description?: string
  children?: ReactNode
  footer?: ReactNode
  className?: string
  contentClassName?: string
  headless?: boolean
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => { document.removeEventListener('keydown', onKeyDown) }
  }, [open, onClose])

  useFocusTrap(open, dialogRef)

  if (!open) return null

  return createPortal((
    <div className={css.root} role="presentation">
      <div className={css.mask} aria-hidden="true" onClick={onClose} />
      <div
        ref={dialogRef}
        className={clsx(css.dialog, className)}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        {headless
          ? children
          : (
            <>
              <div className={clsx(css.content, contentClassName)}>
                <div className={css.header}>
                  <h2 className={css.title}>{title}</h2>
                  <button type="button" className={css.close} aria-label={closeLabel} onClick={onClose}>
                    <IconCloseOutline16 size={14} />
                  </button>
                </div>
                {description !== undefined && description !== '' && (
                  <p className={css.description}>{description}</p>
                )}
                {children !== undefined && <div className={css.body}>{children}</div>}
              </div>
              {footer !== undefined && <div className={css.footer}>{footer}</div>}
            </>
          )}
      </div>
    </div>
  ), document.body)
}
