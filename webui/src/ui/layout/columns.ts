
export interface Columns { sidebar: number; center: number }

export const CENTER_MIN = 640
export const SIDEBAR_MIN = 264
export const SIDEBAR_MAX = 420
export const SIDEBAR_DEFAULT = 280
export const SIDEBAR_COLLAPSED = 56
export const SIDEBAR_AUTO_COLLAPSE = 1024

export function clampWidth(px: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(px)))
}

export function computeColumns(viewport: number, sidebar: number): Columns {
  const s = sidebar === 0 ? SIDEBAR_COLLAPSED : clampWidth(sidebar, SIDEBAR_MIN, SIDEBAR_MAX)

  if (s + CENTER_MIN <= viewport) return { sidebar: s, center: viewport - s }

  return { sidebar: s, center: Math.max(0, viewport - s) }
}
