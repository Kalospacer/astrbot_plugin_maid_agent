import { useEffect, useRef, useState } from "react";

import { DisclosureRow, MarkdownText, Pill, StateDot } from "@/ui/primitives";
import { IconThinkOutline14 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import { foldConversation, type ChatNode } from "@/store/conversation";
import { ToolNodeView } from "@/components/ToolNodeView";

export function ChatView() {
  const session = useApp((s) => (s.current ? s.byId.get(s.current) : undefined));
  const innerRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  // store 对 SessionState 就地变更，fold 不能用 [session] 记忆——直接每次重算（线性、量级小）
  const folded = session
    ? foldConversation([...session.events.values()], session.views)
    : { nodes: [] as ChatNode[], running: false };

  // 自动滚动：滚动盒是 ConversationRoot 的共享 scrollBody（贴底跟随，向上翻阅不打扰）
  useEffect(() => {
    const scroller = innerRef.current?.closest("[data-conversation-scroll]");
    if (!scroller) return;
    const onScroll = () => {
      pinnedRef.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [session?.sessionId]);

  useEffect(() => {
    const scroller = innerRef.current?.closest("[data-conversation-scroll]");
    if (scroller && pinnedRef.current) scroller.scrollTop = scroller.scrollHeight;
  });

  // hero 相位（无会话/空会话）由 ConversationRoot 持有，这里只渲染消息流
  if (!session) return null;

  // 工作中标识：turn 开着且宿主上报运行中（宿主是运行态的唯一事实源——历史里的
  // 孤儿 turn/start 不再单独点亮），且末尾不是正在流式的 assistant 块
  // （流式块自带"生成中…"），在时间线末尾补一个 "正在工作…" 行。
  const lastNode = folded.nodes[folded.nodes.length - 1];
  const working =
    folded.running &&
    session.summary.running &&
    lastNode?.kind !== "assistant-partial";

  return (
    <div className="chat-inner" ref={innerRef}>
      {session.hasMore && (
        <button
          type="button"
          className="muted"
          style={{ alignSelf: "center", border: "none", background: "none", cursor: "pointer", fontSize: 12.5 }}
          onClick={() => void app.loadOlder(session.sessionId)}
        >
          加载更早消息
        </button>
      )}
      {folded.nodes.map((node) => (
        <ChatNodeView key={node.key} node={node} sessionId={session.sessionId} />
      ))}
      {working ? (
        <div className="working-line" role="status">
          <StateDot state="ongoing" />
          <span>正在工作…</span>
        </div>
      ) : null}
    </div>
  );
}

function ChatNodeView(props: { node: ChatNode; sessionId: string }) {
  const node = props.node;
  if (node.kind === "user") {
    const text = node.message.content
      .filter((b) => b.type === "text")
      .map((b: any) => b.text)
      .join("\n");
    const images = node.message.content.filter((b) => b.type === "image") as any[];
    return (
      <div className="message-user">
        {text}
        {images.length > 0 && <UserImages sessionId={props.sessionId} refs={images.map((b) => b.attachment)} />}
      </div>
    );
  }
  if (node.kind === "assistant") {
    return <AssistantBody blocks={node.blocks} streaming={false} />;
  }
  if (node.kind === "assistant-partial") {
    const blocks = node.blocks.map((b) => ({ type: b.kind, text: b.text }));
    return <AssistantBody blocks={blocks} streaming />;
  }
  if (node.kind === "tool") {
    return <ToolNodeView node={node} />;
  }
  if (node.kind === "usage") {
    return (
      <div className="stats-line">
        <span>↑ {node.usage.inputTokens ?? 0}</span>
        <span>↓ {node.usage.outputTokens ?? 0}</span>
        {node.usage.cacheReadTokens ? <span>缓存 {node.usage.cacheReadTokens}</span> : null}
      </div>
    );
  }
  // turn-tail
  const reasonKind = node.reason?.kind ?? "completed";
  const label =
    reasonKind === "completed"
      ? "回合完成"
      : reasonKind === "aborted"
        ? "已停止"
        : reasonKind === "error"
          ? `失败：${node.reason?.error?.message ?? ""}`
          : reasonKind === "interrupted"
            ? "被中断"
            : reasonKind;
  return (
    <div className="turn-tail" data-reason={reasonKind}>
      <span>—</span>
      <span>{label}</span>
    </div>
  );
}

function UserImages(props: { sessionId: string; refs: { attachmentId: string }[] }) {
  if (!props.sessionId || props.refs.length === 0) return null;
  return (
    <div className="attach-rail" style={{ marginTop: 6 }}>
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

function AssistantBody(props: { blocks: any[]; streaming: boolean }) {
  const [reasoningOpen, setReasoningOpen] = useState(props.streaming);
  const reasoning = props.blocks.find((b) => b.type === "reasoning");
  const text = props.blocks
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  return (
    <div className="message-assistant">
      {reasoning?.text ? (
        <DisclosureRow
          title={props.streaming ? "思考中…" : "已深度思考"}
          icon={<IconThinkOutline14 />}
          expandable
          open={reasoningOpen}
          onToggle={() => setReasoningOpen((v) => !v)}
        >
          <div className="muted" style={{ fontSize: 12.5, whiteSpace: "pre-wrap", padding: "4px 0 8px" }}>
            {reasoning.text}
          </div>
        </DisclosureRow>
      ) : null}
      {text ? <MarkdownText text={text} streaming={props.streaming} /> : null}
      {props.streaming && !text && !reasoning?.text ? (
        <Pill>
          <span className="muted">生成中…</span>
        </Pill>
      ) : null}
    </div>
  );
}
