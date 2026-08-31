
import Anser from 'anser'
import type { CSSProperties } from 'react'

interface AnsiChunk {
  content: string
  fg: string | null
  bg: string | null
  decorations: readonly string[]
}

export interface AnsiSpan {
  text: string
  style: CSSProperties | undefined
}

export type AnsiLine = readonly AnsiSpan[]

const TOKEN_BY_BASIC_RGB: Record<string, string> = {
  '0,0,0': 'var(--maid-alias-label-primary)',
  '255,255,255': 'var(--maid-alias-label-primary)',
  '85,85,85': 'var(--maid-alias-label-tertiary)',
  '187,0,0': 'var(--maid-alias-state-error-primary)',
  '255,85,85': 'var(--maid-alias-state-error-secondary)',
  '0,187,0': 'var(--maid-alias-state-success-primary)',
  '0,255,0': 'var(--maid-alias-state-success-secondary)',
  '187,187,0': 'var(--maid-alias-state-warn-primary)',
  '255,255,85': 'var(--maid-alias-state-warn-secondary)',
  '0,0,187': 'var(--maid-alias-state-business-primary)',
  '85,85,255': 'var(--maid-static-blue-400)',
}

const STYLE_BY_DECORATION: Record<string, CSSProperties | undefined> = {
  bold: { fontWeight: 700 },
  dim: { opacity: 0.7 },
  italic: { fontStyle: 'italic' },
  underline: { textDecoration: 'underline' },
  strikethrough: { textDecoration: 'line-through' },
  hidden: { visibility: 'hidden' },
}

const OSC_SEQUENCE = /\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)?/g

const NON_CSI_ESCAPE = /\u001b(?!\[)[\u0020-\u002f]*[\u0030-\u007e]?/g

const INERT_CONTROL = /[\u0000-\u0007\u000b-\u001a\u001c-\u001f\u007f]/g

const NEEDS_REPLAY = /\r|\u0008|\u001b\[[\u0030-\u003f]*[\u0020-\u002f]*K/

const SGR_SEQUENCE = /\u001b\[([\u0030-\u003f]*)[\u0020-\u002f]*m/g

const TAB_WIDTH = 8

const ZERO_WIDTH = /^[\p{Mn}\p{Me}\p{Cf}\u200b-\u200f\u2060]$/u

const WIDE_CHAR = new RegExp(
  '\\p{Script=Han}|\\p{Script=Hiragana}|\\p{Script=Katakana}|\\p{Script=Hangul}'
  + '|\\p{Emoji_Presentation}'
  + '|[\\uff01-\\uff60\\u3000-\\u303e]',
  'u',
)

function isWide(char: string): boolean {
  const code = char.codePointAt(0)
  if (code === undefined || code < 0x1100) return false
  return WIDE_CHAR.test(char)
}

interface SgrState {
  fg: string
  bg: string
  attrs: readonly string[]
}

const SGR_NONE: SgrState = { fg: '', bg: '', attrs: [] }

const ATTR_CLOSERS: Record<string, readonly string[]> = {
  22: ['1', '2'], 23: ['3'], 24: ['4'], 25: ['5', '6'], 27: ['7'], 28: ['8'], 29: ['9'],
}

function foldSgr(state: SgrState, params: string): SgrState {
  const codes = params === '' ? ['0'] : params.split(';')
  let next = state
  for (let index = 0; index < codes.length; index++) {
    const code = String(codes[index])
    if (code === '' || code === '0') { next = SGR_NONE; continue }
    if (code === '38' || code === '48') {
      const kind = codes[index + 1] ?? ''
      const span = kind === '2' ? 4 : kind === '5' ? 2 : 0
      const value = codes.slice(index, index + span + 1).join(';')
      next = code === '38' ? { ...next, fg: value } : { ...next, bg: value }
      index += span
      continue
    }
    const closes = ATTR_CLOSERS[code]
    if (closes !== undefined) {
      next = { ...next, attrs: next.attrs.filter(attr => !closes.includes(attr)) }
      continue
    }
    const numeric = Number(code)
    if (code === '39') { next = { ...next, fg: '' }; continue }
    if (code === '49') { next = { ...next, bg: '' }; continue }
    if ((numeric >= 30 && numeric <= 37) || (numeric >= 90 && numeric <= 97)) { next = { ...next, fg: code }; continue }
    if ((numeric >= 40 && numeric <= 47) || (numeric >= 100 && numeric <= 107)) { next = { ...next, bg: code }; continue }
    if (!next.attrs.includes(code)) next = { ...next, attrs: [...next.attrs, code] }
  }
  return next
}

function openSgr(state: SgrState): string {
  const codes = [...state.attrs]
  if (state.fg !== '') codes.push(state.fg)
  if (state.bg !== '') codes.push(state.bg)
  return codes.length === 0 ? '' : `\u001b[${codes.join(';')}m`
}

function sameSgr(a: SgrState, b: SgrState): boolean {
  return a.fg === b.fg && a.bg === b.bg && a.attrs.length === b.attrs.length
    && a.attrs.every((attr, index) => attr === b.attrs[index])
}

function replayLine(line: string, entrySgr: SgrState): { text: string; sgr: SgrState } {
  const csi = /\u001b\[([\u0030-\u003f]*)[\u0020-\u002f]*([\u0040-\u007e])/g
  const columns: (Cell | undefined)[] = []
  let cursor = 0
  let sgr = entrySgr
  let at = 0

  const clear = (index: number, fill: string): void => {
    const cell = columns[index]
    if (cell?.spacer === true && index > 0) columns[index - 1] = { sgr, char: fill }
    else if (cell !== undefined && isWide(cell.char) && columns[index + 1]?.spacer === true) {
      columns[index + 1] = { sgr, char: fill }
    }
    columns[index] = { sgr, char: fill }
  }

  const consume = (chunk: string): void => {
    for (const char of chunk) {
      if (char === '\r') { cursor = 0; continue }
      if (char === '\u0008') { cursor = Math.max(0, cursor - 1); continue }
      if (char === '\t') {
        const stop = cursor + TAB_WIDTH - (cursor % TAB_WIDTH)
        for (; cursor < stop; cursor++) columns[cursor] ??= { sgr, char: ' ' }
        continue
      }
      if (ZERO_WIDTH.test(char)) {
        const base = cursor > 0 ? columns[cursor - 1] : undefined
        if (base !== undefined) columns[cursor - 1] = { sgr: base.sgr, char: base.char + char }
        continue
      }
      clear(cursor, ' ')
      columns[cursor] = { sgr, char }
      cursor++
      if (isWide(char)) { columns[cursor] = { sgr, char: '', spacer: true }; cursor++ }
    }
  }

  for (const match of line.matchAll(csi)) {
    consume(line.slice(at, match.index))
    at = match.index + match[0].length
    const params = String(match[1])
    const final = String(match[2])
    if (final === 'K') {
      const mode = String(params.split(';')[0])
      if (mode === '1') for (let index = 0; index <= cursor; index++) clear(index, ' ')
      else columns.length = mode === '2' ? 0 : cursor
      continue
    }
    if (final !== 'm') continue
    sgr = foldSgr(sgr, params)
  }
  consume(line.slice(at))

  let out = ''
  let active = entrySgr
  for (let index = 0; index < columns.length; index++) {
    const column = columns[index] ?? { sgr: SGR_NONE, char: ' ' }
    if (!sameSgr(column.sgr, active)) {
      if (!sameSgr(active, SGR_NONE)) out += '\u001b[0m'
      out += openSgr(column.sgr)
      active = column.sgr
    }
    const leadIntact = index > 0 && isWide(columns[index - 1]?.char ?? '')
    out += column.spacer === true && !leadIntact ? ' ' : column.char
  }
  if (!sameSgr(active, sgr)) {
    if (!sameSgr(active, SGR_NONE)) out += '\u001b[0m'
    out += openSgr(sgr)
  }
  return { text: out, sgr }
}

interface Cell {
  sgr: SgrState
  char: string
  spacer?: boolean
}

function applyCursorMovements(text: string): string {
  const replayed: string[] = []
  let sgr = SGR_NONE
  for (const raw of text.split('\n')) {
    const line = raw.replace(/\r+$/, '')
    if (NEEDS_REPLAY.test(line)) {
      const result = replayLine(line, sgr)
      replayed.push(result.text)
      sgr = result.sgr
      continue
    }
    replayed.push(line)
    for (const match of line.matchAll(SGR_SEQUENCE)) sgr = foldSgr(sgr, String(match[1]))
  }
  return replayed.join('\n')
}

function sanitize(text: string): string {
  const escaped = text.replace(OSC_SEQUENCE, '').replace(NON_CSI_ESCAPE, '')
  return applyCursorMovements(escaped).replace(INERT_CONTROL, '')
}

function resolveStyle(chunk: AnsiChunk): CSSProperties | undefined {
  const style: CSSProperties = {}
  const background = chunk.bg === null ? undefined : `rgb(${chunk.bg})`
  if (background !== undefined) style.backgroundColor = background
  if (chunk.fg !== null) {
    const literal = `rgb(${chunk.fg})`
    style.color = background === undefined
      ? TOKEN_BY_BASIC_RGB[chunk.fg.replace(/\s+/g, '')] ?? literal
      : literal
  }
  for (const decoration of chunk.decorations) Object.assign(style, STYLE_BY_DECORATION[decoration])
  return Object.keys(style).length === 0 ? undefined : style
}

export function parseAnsiLines(text: string): AnsiLine[] {
  let current: AnsiSpan[] = []
  const lines: AnsiSpan[][] = [current]
  for (const chunk of Anser.ansiToJson(sanitize(text), { json: true, remove_empty: true })) {
    const style = resolveStyle(chunk)
    for (const [index, part] of chunk.content.split('\n').entries()) {
      if (index > 0) {
        current = []
        lines.push(current)
      }
      if (part !== '') current.push({ text: part, style })
    }
  }
  return lines
}
