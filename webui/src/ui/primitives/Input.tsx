
import type { InputHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import css from './Input.module.css'

export function Input({ icon, className, ...rest }: {
  icon?: ReactNode
  className?: string
} & InputHTMLAttributes<HTMLInputElement>) {
  return (
    <span className={clsx(css.wrap, className)}>
      {icon != null && <span className={css.icon}>{icon}</span>}
      <input className={css.input} {...rest} />
    </span>
  )
}
