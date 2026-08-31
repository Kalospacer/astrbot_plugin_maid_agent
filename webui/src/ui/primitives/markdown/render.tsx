
import { Fragment, createElement } from 'react'
import type { Key, ReactNode } from 'react'
import type * as Md from 'mdast'
import type {} from 'mdast-util-math'
import { normalizeUri } from 'micromark-util-sanitize-uri'
import { CodeBlock } from './CodeBlock.tsx'
import { renderTexToReact } from './katex.tsx'
import type { PositionedBlock } from './incremental.ts'
import css from './MarkdownText.module.css'

export interface MarkdownCodeLabels {
  copyLabel?: string | undefined
  copiedLabel?: string | undefined
}

function sanitizeUrl(url: string): string {
  try {
    switch (new URL(url).protocol) {
      case 'http:':
      case 'https:':
      case 'mailto:':
        return url
      default:
        return ''
    }
  } catch {
    return ''
  }
}

function remoteImageUrl(url: string): string | undefined {
  try {
    const protocol = new URL(url).protocol
    return protocol === 'http:' || protocol === 'https:' ? url : undefined
  } catch {
    return undefined
  }
}

export interface ReferenceTargets {
  definitions: Map<string, Md.Definition>
  footnotes: Map<string, Md.FootnoteDefinition>
}

export function createReferenceTargets(): ReferenceTargets {
  return { definitions: new Map(), footnotes: new Map() }
}

export function collectReferenceTargets(
  nodes: readonly Md.RootContent[],
  targets: ReferenceTargets,
): void {
  for (const node of nodes) {
    if (node.type === 'definition') {
      const id = node.identifier.toUpperCase()
      if (!targets.definitions.has(id)) targets.definitions.set(id, node)
    } else if (node.type === 'footnoteDefinition') {
      const id = node.identifier.toUpperCase()
      if (!targets.footnotes.has(id)) targets.footnotes.set(id, node)
    }
    if ('children' in node) collectReferenceTargets(node.children, targets)
  }
}

export interface MarkdownFileMentions {
  resolve(value: string): { open: () => void; label: string; title: string } | undefined
}

export interface MarkdownRenderContext {
  readonly streaming: boolean
  readonly codeLabels: MarkdownCodeLabels | undefined
  readonly fileMentions: MarkdownFileMentions | undefined
  readonly inLink?: boolean
  readonly targets: ReferenceTargets
  readonly footnoteOrder: string[]
  readonly footnoteCounts: Map<string, number>
}

export function renderBlocks(
  blocks: readonly PositionedBlock[],
  context: MarkdownRenderContext,
): ReactNode[] {
  return blocks
    .map(block => renderNode(block.node, block.key, context))
    .filter(element => element !== null)
}

export function wrapBlockChildren(elements: readonly ReactNode[], edges: boolean): ReactNode[] {
  const wrapped: ReactNode[] = []
  for (const element of elements) {
    if (edges || wrapped.length > 0) wrapped.push('\n')
    wrapped.push(element)
  }
  if (edges && elements.length > 0) wrapped.push('\n')
  return wrapped
}

type BlockEntry = { paragraph: ReactNode[] } | { element: ReactNode }

function renderBlockEntries(
  blocks: readonly Md.RootContent[],
  context: MarkdownRenderContext,
): BlockEntry[] {
  const entries: BlockEntry[] = []
  for (const [index, block] of blocks.entries()) {
    if (block.type === 'paragraph') {
      entries.push({ paragraph: renderChildren(block.children, context) })
    } else {
      const element = renderNode(block, index, context)
      if (element !== null) entries.push({ element })
    }
  }
  return entries
}

function renderChildren(
  nodes: readonly Md.RootContent[],
  context: MarkdownRenderContext,
): ReactNode[] {
  return nodes.map((node, index) => renderNode(node, index, context))
}

function renderNode(node: Md.RootContent, key: Key, context: MarkdownRenderContext): ReactNode {
  switch (node.type) {
    case 'text':
      return node.value
    case 'paragraph':
      return <p key={key}>{renderChildren(node.children, context)}</p>
    case 'heading':
      return createElement(`h${node.depth}`, { key }, ...renderChildren(node.children, context))
    case 'blockquote':
      return (
        <blockquote key={key}>
          {wrapBlockChildren(renderChildren(node.children, context).filter(child => child !== null), true)}
        </blockquote>
      )
    case 'thematicBreak':
      return <hr key={key} />
    case 'break':
      return <Fragment key={key}><br />{'\n'}</Fragment>
    case 'strong':
      return <strong key={key}>{renderChildren(node.children, context)}</strong>
    case 'emphasis':
      return <em key={key}>{renderChildren(node.children, context)}</em>
    case 'delete':
      return <del key={key}>{renderChildren(node.children, context)}</del>
    case 'inlineCode': {
      const value = node.value.replace(/\r?\n|\r/g, ' ')
      const href = inlineCodeHttpUrl(value)
      if (href !== undefined) return <code key={key}>{renderSafeLink(href, [value], 'link')}</code>
      const mention = context.inLink === true ? undefined : context.fileMentions?.resolve(value)
      if (mention !== undefined) {
        return (
          <code key={key}>
            <button
              type="button"
              className={css.fileMention}
              title={mention.title}
              aria-label={mention.label}
              onClick={mention.open}
            >
              {value}
            </button>
          </code>
        )
      }
      return <code key={key}>{value}</code>
    }
    case 'html':
      return renderInlineHtml(node.value, key)
    case 'code':
      return renderCode(node, key, context)
    case 'math':
      return <Fragment key={key}>{renderTexToReact(node.value, true)}</Fragment>
    case 'inlineMath':
      return <Fragment key={key}>{renderTexToReact(node.value, false)}</Fragment>
    case 'list':
      return renderList(node, key, context)
    case 'listItem':
      return renderListItem(node, listItemLoose(node), key, context)
    case 'table':
      return renderTable(node, key, context)
    case 'link':
      return renderAnchor(node.url, renderChildren(node.children, { ...context, inLink: true }), key)
    case 'linkReference':
      return renderLinkReference(node, key, context)
    case 'image':
      return renderImage(node.url, node.alt ?? '', key)
    case 'imageReference':
      return renderImageReference(node, key, context)
    case 'footnoteReference':
      return renderFootnoteReference(node, key, context)
    case 'definition':
    case 'footnoteDefinition':
      return null
    default:
      return null
  }
}

function renderCode(node: Md.Code, key: Key, context: MarkdownRenderContext): ReactNode {
  const language = node.lang ?? undefined
  if (node.value === '') {
    return (
      <pre key={key}>
        <code className={language === undefined ? undefined : `language-${language}`} />
      </pre>
    )
  }
  const lang = language === undefined ? undefined : /^[\w-]+/.exec(language)?.[0]
  if (!context.streaming && lang === 'math') {
    return <Fragment key={key}>{renderTexToReact(`${node.value}\n`, true)}</Fragment>
  }
  return (
    <CodeBlock
      key={key}
      code={`${node.value}\n`}
      lang={context.streaming ? undefined : lang}
      copyLabel={context.codeLabels?.copyLabel}
      copiedLabel={context.codeLabels?.copiedLabel}
    />
  )
}

function listLoose(list: Md.List): boolean {
  return (list.spread ?? false) || list.children.some(listItemLoose)
}

function listItemLoose(item: Md.ListItem): boolean {
  return item.spread ?? item.children.length > 1
}

function renderList(node: Md.List, key: Key, context: MarkdownRenderContext): ReactNode {
  const loose = listLoose(node)
  const properties: { start?: number; className?: string } = {}
  if (typeof node.start === 'number' && node.start !== 1) properties.start = node.start
  if (node.children.some(item => typeof item.checked === 'boolean')) {
    properties.className = 'contains-task-list'
  }
  return createElement(
    node.ordered === true ? 'ol' : 'ul',
    { key, ...properties },
    ...node.children.map((item, index) => renderListItem(item, loose, index, context)),
  )
}

function renderListItem(
  item: Md.ListItem,
  loose: boolean,
  key: Key,
  context: MarkdownRenderContext,
): ReactNode {
  const entries = renderBlockEntries(item.children, context)
  const task = typeof item.checked === 'boolean'
  if (task) {
    const checkbox = <input key="task-checkbox" type="checkbox" checked={item.checked === true} disabled />
    const head = entries[0]
    if (head !== undefined && 'paragraph' in head) {
      head.paragraph = head.paragraph.length > 0 ? [checkbox, ' ', ...head.paragraph] : [checkbox]
    } else {
      entries.unshift({ paragraph: [checkbox] })
    }
  }
  const parts: ReactNode[] = []
  for (const [index, entry] of entries.entries()) {
    const isParagraph = 'paragraph' in entry
    if (loose || index !== 0 || !isParagraph) parts.push('\n')
    if (!isParagraph) parts.push(entry.element)
    else if (loose) parts.push(<p key={`p-${index}`}>{entry.paragraph}</p>)
    else parts.push(<Fragment key={`p-${index}`}>{entry.paragraph}</Fragment>)
  }
  const tail = entries[entries.length - 1]
  if (tail !== undefined && (loose || !('paragraph' in tail))) parts.push('\n')
  return (
    <li key={key} className={task ? 'task-list-item' : undefined}>
      {parts}
    </li>
  )
}

function renderTable(node: Md.Table, key: Key, context: MarkdownRenderContext): ReactNode {
  const align = node.align ?? null
  const [headRow, ...bodyRows] = node.children
  return (
    <div key={key} className={css.tableScroll}>
      <table>
        {headRow !== undefined && <thead>{renderTableRow(headRow, 'th', align, 0, context)}</thead>}
        {bodyRows.length > 0 && (
          <tbody>
            {bodyRows.map((row, index) => renderTableRow(row, 'td', align, index + 1, context))}
          </tbody>
        )}
      </table>
    </div>
  )
}

function renderTableRow(
  row: Md.TableRow,
  cellTag: 'th' | 'td',
  align: readonly Md.AlignType[] | null,
  key: Key,
  context: MarkdownRenderContext,
): ReactNode {
  const length = align === null ? row.children.length : align.length
  const cells: ReactNode[] = []
  for (let index = 0; index < length; index++) {
    const cell = row.children[index]
    const alignValue = align?.[index]
    cells.push(createElement(
      cellTag,
      { key: index, style: alignValue == null ? undefined : { textAlign: alignValue } },
      ...(cell === undefined ? [] : renderChildren(cell.children, context)),
    ))
  }
  return <tr key={key}>{cells}</tr>
}

function renderSafeLink(href: string, children: ReactNode[], key: Key): ReactNode {
  const safeHref = sanitizeUrl(href)
  if (safeHref === '') return <Fragment key={key}>{children}</Fragment>
  const external = ['http:', 'https:'].includes(new URL(safeHref).protocol)
  return (
    <a
      key={key}
      href={safeHref}
      {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
    >
      {children}
    </a>
  )
}

function renderAnchor(url: string, children: ReactNode[], key: Key): ReactNode {
  return renderSafeLink(normalizeUri(url), children, key)
}

function inlineCodeHttpUrl(value: string): string | undefined {
  if (value.trim() !== value) return undefined
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:' ? value : undefined
  } catch {
    return undefined
  }
}

const SAFE_HTML_TAGS = new Set(['details', 'summary', 'br', 'kbd', 'mark', 'sub', 'sup', 's', 'abbr'])
const DROPPED_HTML_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed', 'form'])

function renderInlineHtml(value: string, key: Key): ReactNode {
  const doc = new DOMParser().parseFromString(`<body>${value}</body>`, 'text/html')
  return (
    <Fragment key={key}>
      {renderSanitizedNodes(Array.from(doc.body.childNodes), key)}
    </Fragment>
  )
}

function renderSanitizedNodes(nodes: readonly ChildNode[], key: Key): ReactNode[] {
  const out: ReactNode[] = []
  for (const [index, child] of nodes.entries()) {
    if (child.nodeType === 3) {
      out.push(child.textContent ?? '')
      continue
    }
    if (child.nodeType !== 1) continue
    const el = child as HTMLElement
    const tag = el.tagName.toLowerCase()
    if (DROPPED_HTML_TAGS.has(tag)) continue
    if (tag === 'br') {
      out.push(<br key={`${key}-${index}`} />)
      continue
    }
    const children = renderSanitizedNodes(Array.from(el.childNodes), `${key}-${index}`)
    if (!SAFE_HTML_TAGS.has(tag)) {
      out.push(...children)
      continue
    }
    out.push(createElement(tag, { key: `${key}-${index}` }, ...children))
  }
  return out
}

function renderImage(url: string, alt: string, key: Key): ReactNode {
  const imageSrc = remoteImageUrl(sanitizeUrl(normalizeUri(url)))
  if (imageSrc === undefined) {
    return <span key={key} className={css.imageAlt}>{alt}</span>
  }
  return (
    <img
      key={key}
      className={css.image}
      src={imageSrc}
      alt={alt}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
    />
  )
}

function referenceSuffix(node: Md.LinkReference | Md.ImageReference): string {
  if (node.referenceType === 'collapsed') return '][]'
  if (node.referenceType === 'full') return `][${node.label ?? node.identifier}]`
  return ']'
}

function renderLinkReference(
  node: Md.LinkReference,
  key: Key,
  context: MarkdownRenderContext,
): ReactNode {
  const definition = context.targets.definitions.get(node.identifier.toUpperCase())
  if (definition === undefined) {
    return <Fragment key={key}>{'['}{renderChildren(node.children, context)}{referenceSuffix(node)}</Fragment>
  }
  return renderAnchor(definition.url, renderChildren(node.children, { ...context, inLink: true }), key)
}

function renderImageReference(
  node: Md.ImageReference,
  key: Key,
  context: MarkdownRenderContext,
): ReactNode {
  const definition = context.targets.definitions.get(node.identifier.toUpperCase())
  if (definition === undefined) return `![${node.alt ?? ''}${referenceSuffix(node)}`
  return renderImage(definition.url, node.alt ?? '', key)
}

function renderFootnoteReference(
  node: Md.FootnoteReference,
  key: Key,
  context: MarkdownRenderContext,
): ReactNode {
  const id = node.identifier.toUpperCase()
  const seen = context.footnoteCounts.get(id)
  if (seen === undefined) context.footnoteOrder.push(id)
  context.footnoteCounts.set(id, (seen ?? 0) + 1)
  return <sup key={key}>{String(context.footnoteOrder.indexOf(id) + 1)}</sup>
}

export function renderFootnoteSection(context: MarkdownRenderContext): ReactNode | null {
  const items: ReactNode[] = []
  for (const id of context.footnoteOrder) {
    const definition = context.targets.footnotes.get(id)
    if (definition === undefined) continue
    const count = context.footnoteCounts.get(id) ?? 0
    const backrefs: ReactNode[] = []
    for (let reference = 1; reference <= count; reference++) {
      if (backrefs.length > 0) backrefs.push(' ')
      backrefs.push('↩')
      if (reference > 1) backrefs.push(<sup key={`re-${reference}`}>{String(reference)}</sup>)
    }
    const entries = renderBlockEntries(definition.children, context)
    const tail = entries[entries.length - 1]
    const body: ReactNode[] = entries.map((entry, index) => (
      'paragraph' in entry
        ? (
          <p key={`p-${index}`}>
            {entry.paragraph}
            {entry === tail && <>{' '}{backrefs}</>}
          </p>
        )
        : entry.element
    ))
    if (tail === undefined || !('paragraph' in tail)) body.push(...backrefs)
    items.push(
      <li key={id} id={`user-content-fn-${normalizeUri(id.toLowerCase())}`}>
        {wrapBlockChildren(body, true)}
      </li>,
    )
  }
  if (items.length === 0) return null
  return (
    <section key="footnotes" data-footnotes className="footnotes">
      <h2 id="footnote-label" className="sr-only">Footnotes</h2>
      <ol>{items}</ol>
    </section>
  )
}
