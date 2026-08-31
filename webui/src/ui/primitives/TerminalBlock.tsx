
import { useCallback, useMemo, useState } from 'react'
import clsx from 'clsx'
import { parseAnsiLines, type AnsiLine } from './ansi.ts'
import { headTailCap } from './head-tail-cap.ts'
import { useCopyFeedback } from './use-copy-feedback.ts'
import { Pill } from './Pill.tsx'
import { StateDot, type StateDotState } from './StateDot.tsx'
import css from './TerminalBlock.module.css'

export const DEFAULT_TERMINAL_MAX_LINES = 16

export interface TerminalBlockLabels {
  signal: (signal: string) => string
  exitCode: (exitCode: number) => string
  running: string
  failed: string
  done: string
  copy: string
  copied: string
  noOutput: string
  collapseAria: string
  collapse: string
  expandAria: (hidden: number) => string
  expand: (hidden: number) => string
}

const DEFAULT_LABELS: TerminalBlockLabels = {
  signal: signal => `信号 ${signal}`,
  exitCode: exitCode => `退出码 ${exitCode}`,
  running: '运行中',
  failed: '失败',
  done: '已完成',
  copy: '复制',
  copied: '复制成功',
  noOutput: '无输出',
  collapseAria: '收起输出',
  collapse: '收起',
  expandAria: hidden => `展开其余 ${hidden} 行输出`,
  expand: hidden => `… 其余 ${hidden} 行`,
}

export interface TerminalBlockProps {
  command: string
  cwd?: string | undefined
  home?: string | undefined
  output?: string | undefined
  exitCode?: number | undefined
  signal?: string | undefined
  running?: boolean | undefined
  maxLines?: number | undefined
  className?: string | undefined
  labels?: Partial<TerminalBlockLabels> | undefined
}

function promptLabel(cwd: string, home: string | undefined): string {
  const trimmed = cwd.replace(/[/\\]+$/, '')
  if (home !== undefined && trimmed === home.replace(/[/\\]+$/, '')) return '~'
  const segment = trimmed.split(/[/\\]/).pop()
  return segment === undefined || segment === '' ? cwd : segment
}

function statusText(
  exitCode: number | undefined,
  signal: string | undefined,
  labels: TerminalBlockLabels,
): string | undefined {
  if (signal !== undefined) return labels.signal(signal)
  if (exitCode !== undefined && exitCode !== 0) return labels.exitCode(exitCode)
  return undefined
}

function runState(
  running: boolean,
  exitCode: number | undefined,
  signal: string | undefined,
  labels: TerminalBlockLabels,
): { state: StateDotState; label: string } {
  if (running) return { state: 'ongoing', label: labels.running }
  if (statusText(exitCode, signal, labels) !== undefined) return { state: 'error', label: labels.failed }
  return { state: 'done', label: labels.done }
}

function renderLine(line: AnsiLine) {
  return line.map((span, index) => span.style === undefined
    ? span.text
    : <span key={index} style={span.style}>{span.text}</span>)
}

export function TerminalBlock({
  command,
  cwd,
  home,
  output,
  exitCode,
  signal,
  running = false,
  maxLines = DEFAULT_TERMINAL_MAX_LINES,
  className,
  labels,
}: TerminalBlockProps) {
  const copy = useMemo<TerminalBlockLabels>(
    () => (labels === undefined ? DEFAULT_LABELS : { ...DEFAULT_LABELS, ...labels }),
    [labels],
  )
  const text = output ?? ''
  const lines = useMemo(() => {
    const parsed = parseAnsiLines(text)
    const last = parsed[parsed.length - 1]
    const terminated = parsed.length > 1 && last !== undefined
      && last.every(span => span.text === '')
    return terminated ? parsed.slice(0, -1) : parsed
  }, [text])
  const [expanded, setExpanded] = useState(false)
  const { copied, onCopy } = useCopyFeedback(text)

  const onToggle = useCallback(() => { setExpanded(value => !value) }, [])

  const status = statusText(exitCode, signal, copy)
  const state = runState(running, exitCode, signal, copy)
  const commandLines = useMemo(() => {
    const body = command.endsWith('\n') ? command.slice(0, -1) : command
    return body.split('\n')
  }, [command])
  const empty = lines.every(line => line.every(span => span.text.trim() === ''))
  const { hidden, capped, headLines, tailLines } = headTailCap(lines.length, maxLines, expanded)

  return (
    <div className={clsx(css.block, className)} data-terminal="" data-running={running ? '' : undefined}>
      <div className={css.header}>
        <div className={css.prompt}>
          <span className={css.runStateLabel}>{state.label}</span>
          {commandLines.map((line, index) => (
            <div key={index} className={css.promptLine}>
              {index === 0 && <StateDot state={state.state} className={css.runState} />}
              <span className={css.cwd}>
                {index > 0 || cwd === undefined ? '$' : promptLabel(cwd, home)}
              </span>
              <span className={css.command}>{line}</span>
            </div>
          ))}
        </div>
        {status !== undefined && <Pill className={css.status}>{status}</Pill>}
        {!running && !empty && (
          <button type="button" className={css.copyButton} onClick={onCopy}>
            {copied ? copy.copied : copy.copy}
          </button>
        )}
      </div>
      {!running && (empty
        ? <div className={css.empty}>{copy.noOutput}</div>
        : (
          <div className={css.output}>
            {(capped ? lines.slice(0, headLines) : lines).map((line, index) => (
              <div key={index} className={css.line}>{renderLine(line)}</div>
            ))}
            {hidden > 0 && (
              <button
                type="button"
                className={css.expand}
                aria-expanded={expanded}
                aria-label={expanded ? copy.collapseAria : copy.expandAria(hidden)}
                onClick={onToggle}
              >
                {expanded ? copy.collapse : copy.expand(hidden)}
              </button>
            )}
            {capped && lines.slice(lines.length - tailLines).map((line, index) => (
              <div key={index} className={css.line}>{renderLine(line)}</div>
            ))}
          </div>
        ))}
    </div>
  )
}
