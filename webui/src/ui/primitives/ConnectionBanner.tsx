
import css from './ConnectionBanner.module.css'

export function ConnectionBanner({ reconnecting, label = '连接已断开，正在重连…' }: {
  reconnecting: boolean
  label?: string | undefined
}) {
  if (!reconnecting) return null
  return <div className={css.banner}>{label}</div>
}
