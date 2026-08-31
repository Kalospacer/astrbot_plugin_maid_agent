
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import css from './Button.module.css'

export type ButtonVariant = 'primary' | 'ghost' | 'outline' | 'toolbar'

export function Button({ variant = 'ghost', size = 'md', icon, className, children, ...rest }: {
  variant?: ButtonVariant
  size?: 'md' | 'sm'
  icon?: ReactNode
  className?: string | undefined
  children?: ReactNode
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" className={clsx(css.button, css[variant], css[size], className)} {...rest}>
      {icon != null && <span className={css.icon}>{icon}</span>}
      {children}
    </button>
  )
}
