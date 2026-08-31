
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import css from './Pill.module.css'

export function Pill({ active = false, className, children, onClick, ...rest }: {
  active?: boolean
  className?: string | undefined
  children?: ReactNode
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  if (!onClick) {
    return <span className={clsx(css.pill, active && css.active, className)}>{children}</span>
  }
  return (
    <button
      type="button"
      className={clsx(css.pill, css.interactive, active && css.active, className)}
      onClick={onClick}
      {...rest}
    >
      {children}
    </button>
  )
}
