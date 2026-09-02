import { createContext, memo, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { DiffBlock, DisclosureRow, StateDot, TerminalBlock, Tooltip, diffTotals } from "@/ui/primitives";
import {
  IconBranchOutline16,
  IconCheckOutline16,
  IconChevronDownOutline14,
  IconCopyOutline16,
  IconThinkOutline14,
  IconDatabaseOutline16,
  IconClockOutline16,
  IconSearchOutline16,
  IconBrowseOutline16,
  IconApiOutline14,
  IconEditOutline16,
  IconCodeOutline16,
  IconSparkle16,
} from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import { getSnapshot } from "@/store/app";
import { EMPTY_FOLD, type ChatNode, type ToolNode } from "@/store/conversation";
import { formatMessageClock, formatRunDuration } from "@/ui/chrome/format";
import { toolRowModel, type ToolRowVariant } from "@/ui/chrome/tool-row-model";
import { TurnRail } from "@/components/TurnRail";
import { StatPanel } from "@/components/StatPanel";

// Markdown 渲染栈（micromark + katex + shiki）体积大，首屏 hero 用不到，
// 懒加载到首个助手消息出现时再拉取；加载期间先以纯文本兜底。
const MarkdownText = lazy(() =>
  import("@/ui/primitives/markdown/MarkdownText.tsx").then((m) => ({ default: m.MarkdownText })),
);

const COPY_RESET_MS = 1000;

/** 变体图标（DSH VARIANT_ICONS，14px 入 16px 盒）。 */
const VARIANT_ICONS: Record<ToolRowVariant, ReactNode> = {
  search: <IconSearchOutline16 size={14} />,
  read: <IconBrowseOutline16 size={14} />,
  bash: <IconApiOutline14 size={14} />,
  write: <IconEditOutline16 size={14} />,
  edit: <IconEditOutline16 size={14} />,
  code: <IconCodeOutline16 size={14} />,
  others: <IconSparkle16 size={14} />,
};

/** 复制按钮：写剪贴板成功后短暂换成对勾（DSH MessageIconActions onCopy）。 */
function CopyAction(props: { text: string }) {
  const [copied, setCopied] = useState(false);
  const pendingRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    pendingRef.current = false;
    if (timerRef.current !== null) clearTimeout(timerRef.current);
  }, []);
  const onCopy = useCallback(() => {
    if (copied || pendingRef.current) return;
    pendingRef.current = true;
    void navigator.clipboard.writeText(props.text).then(
      () => {
        pendingRef.current = false;
        setCopied(true);
        timerRef.current = window.setTimeout(() => {
          timerRef.current = null;
          setCopied(false);
        }, COPY_RESET_MS);
      },
      () => {
        pendingRef.current = false;
      },
    );
  }, [copied, props.text]);
  return (
    <Tooltip label={copied ? "已复制" : "复制"} side="bottom">
      <button
        type="button"
        className="icon-action"
        aria-label={copied ? "已复制" : "复制"}
        onClick={onCopy}
      >
        {copied ? <IconCheckOutline16 size={15} /> : <IconCopyOutline16 size={15} />}
      </button>
    </Tooltip>
  );
}

/** 用户/助手共用的 IconActions 行（DSH MessageIconActions）。 */
function IconActions(props: {
  text: string;
  time?: number;
  clock: "start" | "end";
  revealHover?: boolean;
  className?: string;
  extra?: ReactNode;
}) {
  const clockEl =
    props.time === undefined ? null : (
      <span className={props.clock === "start" ? "time-start" : "time-end"}>
        {formatMessageClock(props.time)}
      </span>
    );
  const classes = props.className === undefined || props.className === ""
    ? "icon-actions"
    : `icon-actions ${props.className}`;
  return (
    <div className={classes}>
      {props.clock === "start" ? clockEl : null}
      <CopyAction text={props.text} />
      {props.extra}
      {props.clock === "end" ? clockEl : null}
    </div>
  );
}

/** 每轮过程折叠状态（DSH turn-process）：key=turn 号，true=展开。默认折叠。 */
const TurnOpenContext = createContext<{
  isOpen: (turn: number) => boolean;
  toggle: (turn: number) => void;
}>({ isOpen: () => true, toggle: () => {} });

export function ChatView() {
  // 订阅 stamp（原始值）：session 内部数据任何变化都会 +1；
  // 未变化时组件完全不重渲染，流式期间也不会波及兄弟组件。
  // 键入 sessionId 避免切换到 stamp 恰好相同的会话时不刷新。
  const stampKey = useApp((s) => {
    if (!s.current) return "";
    return `${s.current}:${s.byId.get(s.current)?.stamp ?? -1}`;
  });
  const state = getSnapshot();
  const session = state.current ? state.byId.get(state.current) : undefined;

  const innerRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [turnOpen, setTurnOpen] = useState<Map<number, boolean>>(() => new Map());

  // 值必须随 turnOpen 变化：ChatNodeView 被 memo 化，只有 context 值
  // 换新引用才会重渲染（否则点击折叠条状态翻转但界面纹丝不动）。
  const processCtx = useMemo(
    () => ({
      isOpen: (turn: number) => turnOpen.get(turn) ?? false,
      toggle: (turn: number) =>
        setTurnOpen((prev) => {
          const next = new Map(prev);
          next.set(turn, !(prev.get(turn) ?? false));
          return next;
        }),
    }),
    [turnOpen],
  );

  // 增量折叠：复用 session 上的 folder，只处理新增事件
  const folded = useMemo(
    () => (session ? session.folder.ingest(session.events, session.views) : EMPTY_FOLD),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [session, stampKey],
  );

  useEffect(() => {
    const scroller = innerRef.current?.closest("[data-conversation-scroll]");
    if (!scroller) return;
    const onScroll = () => {
      const isAtBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
      pinnedRef.current = isAtBottom;
      setAtBottom((prev) => (prev === isAtBottom ? prev : isAtBottom));
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [session?.sessionId]);

  useEffect(() => {
    const scroller = innerRef.current?.closest("[data-conversation-scroll]");
    if (scroller && pinnedRef.current) scroller.scrollTop = scroller.scrollHeight;
  }, [stampKey, session?.sessionId]);

  const toBottom = useCallback(() => {
    const scroller = innerRef.current?.closest("[data-conversation-scroll]");
    if (!scroller) return;
    scroller.scrollTop = scroller.scrollHeight;
    pinnedRef.current = true;
    setAtBottom(true);
  }, []);

  if (!session) return null;

  const lastNode = folded.nodes[folded.nodes.length - 1];
  const working =
    folded.running &&
    session.summary.running &&
    lastNode?.kind !== "assistant-partial";
  // 是否还有更早的用户行（决定 IconActions hover 显隐，DSH data-actions-reveal）
  const lastUserIndex = (() => {
    for (let i = folded.nodes.length - 1; i >= 0; i--) {
      if (folded.nodes[i]?.kind === "user") return i;
    }
    return -1;
  })();

  return (
    <TurnOpenContext.Provider value={processCtx}>
    <div className="chat-inner" ref={innerRef}>
      <TurnRail nodes={folded.nodes} scrollerQuery="[data-conversation-scroll]" />
      {session.hasMore && (
        <button
          type="button"
          className="muted"
          style={{ alignSelf: "center", border: "none", background: "none", cursor: "pointer", fontSize: 12.5 }}
          onClick={() => void app.loadOlder(session.sessionId)}
        >
          加载更早
        </button>
      )}
      {folded.nodes.map((node, index) => (
        <ChatNodeView
          key={node.key}
          node={node}
          sessionId={session.sessionId}
          revealHover={index < lastUserIndex}
        />
      ))}
      {working ? <TurnStatus /> : null}
      {!atBottom && (
        <div className="to-bottom-slot">
          <Tooltip label="回到底部" side="top" delayMs={500}>
            <button type="button" className="to-bottom" aria-label="回到底部" onClick={toBottom}>
              <IconChevronDownOutline14 size={16} />
            </button>
          </Tooltip>
        </div>
      )}
    </div>
    </TurnOpenContext.Provider>
  );
}

/** 运行中回合状态行：中性渐变 shimmer + ≥15s 计时器（布局对齐 DSH TurnStatus，文案走 Kimi）。 */
function TurnStatus(props: { startTime?: number }) {
  const [mountedAt] = useState(() => Date.now());
  const anchor = props.startTime ?? mountedAt;
  const [elapsed, setElapsed] = useState(() => Math.max(0, Date.now() - anchor));
  useEffect(() => {
    const tick = () => setElapsed(Math.max(0, Date.now() - anchor));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [anchor]);
  return (
    <div className="turn-status" role="status">
      正在工作...
      {elapsed >= 15_000 ? (
        <span className="turn-status-clock" aria-hidden>
          {formatRunDuration(elapsed)}
        </span>
      ) : null}
    </div>
  );
}

const ChatNodeView = memo(function ChatNodeView(props: {
  node: ChatNode;
  sessionId: string;
  revealHover: boolean;
}) {
  const node = props.node;
  const process = useContext(TurnOpenContext);
  if (node.kind === "user") {
    const text = node.message.content
      .filter((b) => b.type === "text")
      .map((b: any) => b.text)
      .join("\n");
    const images = node.message.content.filter((b) => b.type === "image") as any[];
    return (
      <div className="user-row" data-reveal-hover={props.revealHover || undefined} data-chat-anchor-key={node.key}>
        <div className="user-stack">
          {images.length > 0 ? (
            <UserImages sessionId={props.sessionId} refs={images.map((b) => b.attachment)} />
          ) : null}
          {text !== "" ? <div className="user-bubble">{text}</div> : null}
        </div>
        <IconActions text={text} time={node.time} clock="start" />
      </div>
    );
  }
  if (node.kind === "assistant") {
    return <AssistantBody blocks={node.blocks} streaming={false} usage={node.usage} time={node.time} interrupted={false} />;
  }
  if (node.kind === "assistant-partial") {
    const blocks = node.blocks.map((b) => ({ type: b.kind, text: b.text }));
    return <AssistantBody blocks={blocks} streaming usage={undefined} time={undefined} interrupted={false} />;
  }
  if (node.kind === "tool") {
    return <ToolRowView node={node} hidden={!process.isOpen(node.turn)} />;
  }
  if (node.kind === "usage") {
    return <UsageLine usage={node.usage} />;
  }
  const reasonKind = node.reason?.kind ?? "completed";
  if (reasonKind === "completed") {
    // 过程折叠条（DSH TurnProcessNodeView）：已完成轮的过程行（工具/中间消息）默认收起
    const open = process.isOpen(node.turn);
    const labels: string[] = [];
    if (node.toolCallCount > 0) labels.push(`${node.toolCallCount} 次工具调用`);
    if (node.messageCount > 0) labels.push(`${node.messageCount} 条消息`);
    const label = labels.length === 0 ? "已思考" : `已思考 · ${labels.join(" · ")}`;
    return (
      <button
        type="button"
        className="turn-process"
        data-open={open || undefined}
        aria-expanded={open}
        onClick={(event) => {
          event.currentTarget.focus();
          process.toggle(node.turn);
        }}
      >
        <span className="turn-process-label">{label}</span>
        <IconChevronDownOutline14 className="turn-process-chevron" />
      </button>
    );
  }
  if (reasonKind === "error") {
    return (
      <div className="turn-error-row" role="status">
        <StateDot state="error" className="state-dot" />
        <div className="turn-error-copy">
          <span className="turn-error-title">本轮运行失败</span>
          <span className="turn-error-message">{node.reason?.error?.message ?? ""}</span>
        </div>
        {node.reason?.error?.code !== undefined ? (
          <code className="turn-error-code">{node.reason.error.code}</code>
        ) : null}
      </div>
    );
  }
  if (reasonKind === "interrupted" || reasonKind === "aborted") {
    return (
      <div className="turn-error-row" role="status">
        <StateDot state="warning" className="state-dot" />
        <div className="turn-error-copy">
          <span className="turn-error-title">已停止</span>
          <span className="turn-error-message">本轮已被中断</span>
        </div>
      </div>
    );
  }
  if (reasonKind === "max-tokens") {
    return (
      <div className="turn-error-row" role="status">
        <StateDot state="warning" className="state-dot" />
        <div className="turn-error-copy">
          <span className="turn-error-title">已达到输出 token 上限</span>
          <span className="turn-error-message">
            回答被截断，已有输出保留在对话中。发送“继续”可让模型接着输出。
          </span>
        </div>
      </div>
    );
  }
  return null;
});

function UserImages(props: { sessionId: string; refs: { attachmentId: string }[] }) {
  if (!props.sessionId || props.refs.length === 0) return null;
  return (
    <div className="attach-rail" style={{ marginTop: 0 }}>
      {props.refs.map((ref, index) => (
        <AttachedImage key={ref.attachmentId ?? index} sessionId={props.sessionId} attachmentId={ref.attachmentId} />
      ))}
    </div>
  );
}

function AttachedImage(props: { sessionId: string; attachmentId: string }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let alive = true;
    void app
      .loadAttachmentImage(props.sessionId, props.attachmentId)
      .then((data) => {
        if (alive) setSrc(data);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [props.sessionId, props.attachmentId]);
  if (!src) return null;
  return <img src={src} alt="附件" style={{ maxWidth: 180, borderRadius: 8 }} />;
}

/**
 * 用量节点：DSH 中 usage 不单独渲染——它挂在本轮最后一条助手的尾部
 * IconActions 用量 pill 上（StatPills）。这里返回 null 避免双重展示。
 */
function UsageLine(props: { usage: any }) {
  return null;
}

function AssistantBody(props: {
  blocks: any[];
  streaming: boolean;
  usage?: any;
  time?: number;
  interrupted: boolean;
}) {
  const reasoning = props.blocks.find((b) => b.type === "reasoning");
  const text = props.blocks
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  return (
    <div className="message-assistant">
      {reasoning?.text ? <ThinkRow text={reasoning.text} running={props.streaming && !text} /> : null}
      {text ? (
        <Suspense fallback={<div className="message-plain">{text}</div>}>
          <MarkdownText text={text} streaming={props.streaming} />
        </Suspense>
      ) : null}
      {props.interrupted ? <span className="stopped-tag">已停止</span> : null}
      {!props.streaming && text !== "" ? (
        <IconActions
          text={text}
          time={props.time}
          clock="end"
          className="assistant-actions"
          extra={
            <StatPills
              usage={props.usage}
              onFork={undefined}
            />
          }
        />
      ) : null}
    </div>
  );
}

/** 尾部用量/时间 pill 簇（DSH TurnUsagePanel 触发器）；无 usage 时返回 null。 */
function StatPills(props: { usage?: any; onFork?: () => void }) {
  if (props.usage === undefined || props.usage === null) return null;
  const usage = props.usage;
  const billedInput =
    (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0);
  const total = billedInput + (usage.outputTokens ?? 0);
  return <StatPanel usage={{ ...usage, totalTokens: total }} />;
}

/** 思考行（DSH ReasoningRow）：24px 披露行 + 流式摘要跟随末行 + 扫光。 */
function ThinkRow(props: { text: string; running: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const summary = props.running
    ? latestLine(props.text)
    : firstLine(props.text);
  return (
    <div
      className="think-row"
      data-variant="think"
      data-state={props.running ? "running" : "ok"}
      data-expanded={expanded || undefined}
    >
      {props.running ? <span className="visually-hidden">运行中</span> : null}
      <DisclosureRow
        rowClassName="disclosure-row"
        leadingClassName="think-leading"
        titleClassName="think-title"
        chevronClassName="think-chevron"
        icon={<IconThinkOutline14 size={14} />}
        title="思考"
        open={expanded}
        expandable
        expandOnRowClick
        onToggle={() => setExpanded((v) => !v)}
        collapsedContent={
          summary !== "" ? (
            <>
              <span className="flow-sep" aria-hidden />
              <span className="think-summary" data-follow-end={props.running || undefined}>
                <span className="think-summary-text">{summary}</span>
              </span>
            </>
          ) : null
        }
      >
        <div className="think-body">{props.text}</div>
      </DisclosureRow>
    </div>
  );
}

function firstLine(text: string): string {
  const newline = text.indexOf("\n");
  return newline === -1 ? text : text.slice(0, newline);
}

function latestLine(text: string): string {
  const visible = text.trimEnd();
  const newline = visible.lastIndexOf("\n");
  return newline === -1 ? visible : visible.slice(newline + 1);
}

/** 工具行（DSH ToolRow）：变体图标+标题+点+摘要；失败替换错误首行；展开 IN/OUT 卡。 */
function ToolRowView(props: { node: ToolNode; hidden: boolean }) {
  const node = props.node;
  const [open, setOpen] = useState(false);
  const model = useMemo(() => toolRowModel(node), [node]);
  const variant = model.variant;
  const done = node.resultSeq !== undefined;

  const leading =
    model.state === "error" ? (
      <StateDot state="error" />
    ) : model.state === "stopped" ? (
      <StateDot state="warning" />
    ) : (
      VARIANT_ICONS[variant]
    );
  const status = model.state === "running" ? "运行中" : null;
  const failureLine = model.state === "error" ? model.errorSummary ?? null : null;
  const summaryText = failureLine ?? model.summary;
  const expandable =
    model.bodyRaw !== null || model.output !== null || done;
  // diff 卡折叠摘要的 +n -m 尾标（DSH diffStat）
  const diffStat = useMemo(() => {
    const diffView =
      node.resultView?.card === "diff"
        ? node.resultView
        : node.callView?.card === "diff"
          ? node.callView
          : undefined;
    if (diffView === undefined) return null;
    const { added, removed } = diffTotals(
      (diffView.diffs ?? []).map((d: any) => ({ path: d.path, oldText: d.oldText ?? "", newText: d.newText })),
    );
    return `+${added} -${removed}`;
  }, [node]);
  const suffix = failureLine === null ? diffStat : null;

  return (
    <div
      className="tool-row"
      data-variant={variant}
      data-state={model.state}
      hidden={props.hidden || undefined}
    >
      {status !== null ? <span className="visually-hidden">{status}</span> : null}
      <DisclosureRow
        rowClassName="disclosure-row"
        leadingClassName="tool-leading"
        titleClassName="tool-title"
        chevronClassName="tool-chevron"
        icon={leading}
        title={model.title}
        open={open}
        expandable={expandable}
        expandOnRowClick
        keepContentWhenOpen
        onToggle={() => setOpen((v) => !v)}
        collapsedContent={
          summaryText !== "" ? (
            <>
              <span className="flow-sep" aria-hidden />
              <span className={`tool-summary${failureLine !== null ? " error-summary" : ""}`}>
                {summaryText}
              </span>
              {suffix !== null ? <span className="tool-diff-stat">{suffix}</span> : null}
            </>
          ) : null
        }
      >
        <ToolBody node={node} variant={variant} error={model.state === "error"} />
      </DisclosureRow>
    </div>
  );
}

function ToolBody(props: { node: ToolNode; variant: ToolRowVariant; error: boolean }) {
  const node = props.node;
  const resultView = node.resultView;

  if (resultView?.card === "terminal") {
    return (
      <div className="tool-block-body terminal">
        <TerminalBlock
          command={resultView.title ?? node.name}
          output={resultView.output ?? node.resultText ?? ""}
          exitCode={resultView.exitCode}
        />
      </div>
    );
  }
  if (resultView?.card === "read") {
    return (
      <div className="tool-block-body">
        <Suspense
          fallback={
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12 }}>
              {(resultView.lines ?? []).map((l: any) => l.text ?? "").join("\n")}
            </pre>
          }
        >
          <ReadBlock
            label={resultView.path}
            lines={resultView.lines}
            totalLines={resultView.totalLines}
            lang={resultView.lang}
          />
        </Suspense>
      </div>
    );
  }
  const diffView =
    resultView?.card === "diff"
      ? resultView
      : node.callView?.card === "diff"
        ? node.callView
        : undefined;
  if (diffView) {
    return (
      <div className="tool-block-body">
        <DiffBlock
          diffs={diffView.diffs.map((d: any) => ({ path: d.path, oldText: d.oldText ?? "", newText: d.newText }))}
        />
      </div>
    );
  }
  // generic：IN/OUT 卡（DSH .ioCard）
  const bodyText = formatToolBodySafe(node.arguments ?? "");
  const outputText = node.resultText ?? null;
  return (
    <div className="tool-body-wrap">
      {bodyText !== null || outputText !== null ? (
        <div className="io-card">
          {bodyText !== null ? (
            <div className="io-section">
              <span className="io-label">输入</span>
              <span className="io-text">{bodyText}</span>
            </div>
          ) : null}
          {bodyText !== null && outputText !== null ? <span className="io-divider" aria-hidden /> : null}
          {outputText !== null ? (
            <div className="io-section">
              <span className="io-label">输出</span>
              <span className="io-text" data-error={props.error || undefined}>
                {outputText}
              </span>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const PARAMS_MAX_CHARS = 20_000;

function formatToolBodySafe(raw: string): string | null {
  let text = raw;
  try {
    text = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    /* 非 JSON 原样展示 */
  }
  if (text === "") return null;
  return text.length > PARAMS_MAX_CHARS
    ? `${text.slice(0, PARAMS_MAX_CHARS)}\n… 已截断，共 ${text.length} 字符`
    : text;
}

const ReadBlock = lazy(() =>
  import("@/ui/primitives/ReadBlock.tsx").then((m) => ({ default: m.ReadBlock })),
);
