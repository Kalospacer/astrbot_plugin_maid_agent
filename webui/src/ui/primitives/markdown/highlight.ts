
import { createHighlighterCoreSync, createCssVariablesTheme } from 'shiki/core'
import { createJavaScriptRegexEngine, defaultJavaScriptRegexConstructor } from 'shiki/engine/javascript'
import langTs from '@shikijs/langs/typescript'
import langBash from '@shikijs/langs/shellscript'
import langJson from '@shikijs/langs/json'
import langPython from '@shikijs/langs/python'
import langYaml from '@shikijs/langs/yaml'
import langToml from '@shikijs/langs/toml'
import langIni from '@shikijs/langs/ini'
import langMarkdown from '@shikijs/langs/markdown'
import langCss from '@shikijs/langs/css'
import langSql from '@shikijs/langs/sql'
import langGo from '@shikijs/langs/go'
import langRust from '@shikijs/langs/rust'
import langJava from '@shikijs/langs/java'
import type { HighlighterCore } from 'shiki/core'
import type { CSSProperties } from 'react'

/**
 * 语法表是取舍过的，别随手往里加语言。
 *
 * 宿主约束（见 vite.config.ts）逼着产物必须是单文件，`inlineDynamicImports`
 * 会把 `import('@shikijs/langs/x')` 直接内联——所以“按需加载语法”在这里
 * 一个字节都省不下来，反而全部变成顶层 `Object.freeze(JSON.parse(...))`
 * 在首屏同步求值。曾经的 26 门语言实测拖进 34 个语法、2.52 MB，占产物 76%。
 *
 * 更要命的是传递依赖：ruby 自己只有 52 KB，却经 haml → html → javascript/css
 * 一路拉进 cpp/java/glsl，闭包 1961 KB；php 472 KB、html 292 KB 同理。
 * 加语言前先算闭包，不要只看单文件大小。
 */
const LANGS = [
  langTs,
  langBash,
  langJson,
  langPython,
  langYaml,
  langToml,
  langIni,
  langMarkdown,
  langCss,
  langSql,
  langGo,
  langRust,
  langJava,
]

/** 只映射 LANGS 里真实注册过的语法；映射到未注册语言会让 shiki 抛错。 */
const LANG_ALIASES = new Map<string, string>([
  ['typescript', 'typescript'],
  ['ts', 'typescript'],
  ['tsx', 'typescript'],
  ['javascript', 'typescript'],
  ['js', 'typescript'],
  ['jsx', 'typescript'],
  ['mjs', 'typescript'],
  ['cjs', 'typescript'],
  ['shellscript', 'shellscript'],
  ['bash', 'shellscript'],
  ['sh', 'shellscript'],
  ['shell', 'shellscript'],
  ['zsh', 'shellscript'],
  ['console', 'shellscript'],
  ['json', 'json'],
  ['jsonc', 'json'],
  ['json5', 'json'],
  ['py', 'python'],
  ['python', 'python'],
  ['yaml', 'yaml'],
  ['yml', 'yaml'],
  ['toml', 'toml'],
  ['ini', 'ini'],
  ['conf', 'ini'],
  ['md', 'markdown'],
  ['markdown', 'markdown'],
  ['css', 'css'],
  ['sql', 'sql'],
  ['go', 'go'],
  ['golang', 'go'],
  ['rs', 'rust'],
  ['rust', 'rust'],
  ['java', 'java'],
])

const cssVariablesTheme = createCssVariablesTheme({
  name: 'css-variables',
  variablePrefix: '--shiki-',
  fontStyle: true,
})

const regexEngine = createJavaScriptRegexEngine({
  forgiving: true,
  regexConstructor: pattern => defaultJavaScriptRegexConstructor(pattern, {
    lazyCompileLength: Number.POSITIVE_INFINITY,
  }),
})

let singleton: HighlighterCore | undefined

const BOOT_GRAMMAR_WARMUPS = [
  { lang: 'typescript', code: 'const answer: number = 42' },
  { lang: 'shellscript', code: 'printf \'%s\\n\' "$HOME"' },
  { lang: 'json', code: '{"ready":true}' },
] as const

function highlighter(): HighlighterCore {
  singleton ??= createHighlighterCoreSync({
    themes: [cssVariablesTheme],
    langs: LANGS,
    engine: regexEngine,
  })
  return singleton
}

// 预热放到空闲期：正则编译是 lazyCompileLength: Infinity 强制惰性的，首个代码块
// 出现时才付钱，但那一下会卡在渲染里。空闲时先跑一遍把它挪出首屏关键路径。
const scheduleWarmup: (run: () => void) => void =
  typeof requestIdleCallback === 'function'
    ? run => { requestIdleCallback(run, { timeout: 2000 }) }
    : run => { setTimeout(run, 0) }

scheduleWarmup(() => {
  const instance = highlighter()
  for (const sample of BOOT_GRAMMAR_WARMUPS) {
    instance.codeToTokens(sample.code, {
      lang: sample.lang,
      theme: 'css-variables',
      tokenizeTimeLimit: 0,
    })
  }
})

export function highlightToHtml(code: string, lang: string | undefined): string | undefined {
  const resolved = lang === undefined ? undefined : LANG_ALIASES.get(lang.toLowerCase())
  if (resolved === undefined) return undefined
  return highlighter().codeToHtml(code, { lang: resolved, theme: 'css-variables' })
}

export interface HighlightSpan {
  text: string
  style: CSSProperties
}

export function highlightLines(code: string, lang: string | undefined): HighlightSpan[][] | undefined {
  const resolved = lang === undefined ? undefined : LANG_ALIASES.get(lang.toLowerCase())
  if (resolved === undefined) return undefined
  const { tokens } = highlighter().codeToTokens(code, { lang: resolved, theme: 'css-variables' })
  const last = tokens[tokens.length - 1]
  const lines = tokens.length > 1 && last !== undefined && last.length === 0
    ? tokens.slice(0, -1)
    : tokens
  return lines.map(line => line.map(token => ({ text: token.content, style: { color: token.color } })))
}
