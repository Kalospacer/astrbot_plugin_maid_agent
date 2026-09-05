// 轮次导航轨（DSH TurnNavigator）：右缘刻度线梯子 + 悬停/聚焦预览卡。
// maid 数据源是已折叠的 ChatNode 流：以 user 节点为轮锚点，向最近的
// turn 号聚组；未加载的轮在 maid 中没有 outline 投影，全部按已加载处理。

import { memo, useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";

import type { ChatNode } from "@/store/conversation";

/** 刻度间距 / 上下留白（DSH TURN_SPACING_PX / RAIL_INSET_PX）。 */
const TURN_SPACING_PX = 10;
const RAIL_INSET_PX = 6;

export interface TurnRailItem {
  turn: number;
  /** 锚行 key：点击滚动定位。 */
  anchorKey: string;
  prompt: string;
  response: string;
}

/** 从折叠节点流推导轮次梯子：每轮取首个 user 节点为锚，回复取该轮首个 assistant 文本。 */
export function deriveTurnRailItems(nodes: readonly ChatNode[]): TurnRailItem[] {
  const items: TurnRailItem[] = [];
  let current: TurnRailItem | null = null;
  let turnCount = 0;
  for (const node of nodes) {
    if (node.kind === "user") {
      if (current !== null) items.push(current);
      turnCount += 1;
      const text = node.message.content
        .filter((b) => b.type === "text")
        .map((b: any) => b.text)
        .join("\n");
      current = {
        turn: turnCount,
        anchorKey: node.key,
        prompt: firstLine(text),
        response: "",
      };
    } else if (current !== null && (node.kind === "assistant" || node.kind === "assistant-partial")) {
      const c = current;
      if (c.response === "") {
        const text = node.blocks
          .filter((b: any) => b.type === "text" || b.kind === "text")
          .map((b: any) => b.text)
          .join("");
        c.response = firstLine(text);
      }
    }
  }
  if (current !== null) items.push(current);
  return items;
}

function firstLine(text: string): string {
  const newline = text.indexOf("\n");
  return newline === -1 ? text : text.slice(0, newline);
}

type PositionStyle = CSSProperties & { "--turn-natural-position": string };
type FrameStyle = CSSProperties & { "--turn-natural-height": string; "--turn-scroll-top": string };

interface TurnRailProps {
  nodes: readonly ChatNode[];
  /**
   * nodes 的长度。folder 原地增长 nodes，引用恒定——只传 nodes 的话本组件
   * 的 memo 会永久 bail out，梯子在整个会话里再也不更新（首次挂载不足 2 轮
   * 就返回 null，之后永远不再出现）。size 变化即“多了一行”。
   */
  size: number;
  /**
   * 助手消息定稿计数。只看 size 不够：流式最后一步是用定稿节点等长替换 partial，
   * 预览里的回复会永远停在 partial 刚建时的空正文。
   */
  contentRevision: number;
  scrollerQuery: string;
}

/** 导航轨主体；少于 2 轮时不渲染。 */
export const TurnRail = memo(function TurnRail(props: TurnRailProps) {
  // 依赖 size + contentRevision 而非 nodes：nodes 引用恒定，逐 token 的
  // replaceNode 既不改长度也不改定稿计数，所以每个 token 不会重算；
  // 新增行（size）或某步正文定稿（contentRevision）才会。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const items = useMemo(
    () => deriveTurnRailItems(props.nodes),
    [props.nodes, props.size, props.contentRevision],
  );
  const [previewTurn, setPreviewTurn] = useState<number | null>(null);
  const [activeTurn, setActiveTurn] = useState<number | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [canScrollUp, setCanScrollUp] = useState(false);
  const [canScrollDown, setCanScrollDown] = useState(false);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const previewId = useId();

  // 内部梯子滚动状态：预览定位 + 两端渐隐（DSH syncScrollState/fade）
  const syncRailScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (el === null) return;
    setScrollTop(el.scrollTop);
    setCanScrollUp(el.scrollTop > 1);
    setCanScrollDown(el.scrollTop + el.clientHeight < el.scrollHeight - 1);
  }, []);

  useEffect(() => {
    syncRailScroll();
  }, [items, syncRailScroll]);

  // 活动轮：监听滚动口，找阅读线（顶部 20% 高度处）命中的轮
  useEffect(() => {
    const root = rootRef.current;
    if (root === null || items.length < 2) return;
    const scroller = root.closest(props.scrollerQuery);
    if (scroller === null) return;
    let frame = 0;
    const sync = () => {
      frame = 0;
      const rect = scroller.getBoundingClientRect();
      const line = rect.top + Math.min(96, rect.height * 0.2);
      let next: number | null = null;
      for (const item of items) {
        // 锚行由 ChatView 打在用户行上（data-chat-anchor-key），在滚动口里而不是
        // 梯子里；navigate() 用的也是同一个属性，两处必须保持一致。
        const el = scroller.querySelector<HTMLElement>(
          `[data-chat-anchor-key="${item.anchorKey}"]`,
        );
        if (el === null) continue; // 该轮尚未渲染（历史未加载），跳过而不是误判
        if (el.getBoundingClientRect().top > line) break;
        next = item.turn;
      }
      if (next === null) next = items[0]?.turn ?? null;
      setActiveTurn((prev) => (prev === next ? prev : next));
      syncRailScroll();
    };
    const onScroll = () => {
      if (frame !== 0) return;
      frame = requestAnimationFrame(sync);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    sync();
    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (frame !== 0) cancelAnimationFrame(frame);
    };
  }, [items, props.scrollerQuery]);

  if (items.length < 2) return null;

  const frameStyle: FrameStyle = {
    "--turn-natural-height": `${(items.length - 1) * TURN_SPACING_PX + 2 * RAIL_INSET_PX}px`,
    "--turn-scroll-top": `${scrollTop}px`,
  };
  const navigate = (item: TurnRailItem) => {
    const root = rootRef.current;
    const scroller = root?.closest(props.scrollerQuery);
    const anchor = scroller?.querySelector<HTMLElement>(
      `[data-chat-anchor-key="${item.anchorKey}"]`,
    );
    if (scroller && anchor) {
      scroller.scrollTop += anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 24;
    }
  };

  // 指针所在刻度（DSH itemAtPointer）：frame 整列接管鼠标输入，
  // 刻度按钮只作键盘焦点（pointer-events: none）。
  const itemAtPointer = (clientY: number, currentTarget: HTMLElement): TurnRailItem | undefined => {
    const rect = currentTarget.getBoundingClientRect();
    const offset = clientY - rect.top + scrollTop - RAIL_INSET_PX;
    const index = Math.max(0, Math.min(items.length - 1, Math.round(offset / TURN_SPACING_PX)));
    return items[index];
  };
  const fadeClasses = ["turn-rail-scroller"];
  if (canScrollUp) fadeClasses.push("turn-rail-fade-top");
  if (canScrollDown) fadeClasses.push("turn-rail-fade-bottom");

  return (
    <div className="turn-rail-slot" ref={rootRef}>
      <nav
        className="turn-rail-frame"
        style={frameStyle}
        aria-label="轮次导航"
        onClick={(event) => {
          const item = itemAtPointer(event.clientY, event.currentTarget);
          if (item) navigate(item);
        }}
        onPointerMove={(event) => {
          const item = itemAtPointer(event.clientY, event.currentTarget);
          setPreviewTurn(item?.turn ?? null);
        }}
        onPointerLeave={() => setPreviewTurn(null)}
      >
        <div ref={scrollerRef} className={fadeClasses.join(" ")} onScroll={syncRailScroll}>
          <div className="turn-rail-marks">
            {items.map((item, index) => {
              const style: PositionStyle = { "--turn-natural-position": `${index * TURN_SPACING_PX}px` };
              const active = item.turn === activeTurn;
              const showingPreview = item.turn === previewTurn;
              const cls = [
                "turn-rail-mark",
                active ? "active" : "",
                showingPreview ? "preview" : "",
  ].filter(Boolean).join(" ");
              return (
                <div key={item.turn} className="turn-rail-mark-position" style={style}>
                  <button
                    type="button"
                    className={cls}
                    aria-label={`跳转到第 ${item.turn} 轮`}
                    aria-current={active ? "true" : undefined}
                    aria-describedby={showingPreview ? previewId : undefined}
                    onClick={(event) => {
                      event.stopPropagation();
                      navigate(item);
                    }}
                    onFocus={() => setPreviewTurn(item.turn)}
                    onBlur={() => setPreviewTurn(null)}
                  />
                </div>
              );
            })}
          </div>
        </div>
        {previewTurn !== null && (() => {
          const previewIndex = items.findIndex((item) => item.turn === previewTurn);
          const preview = previewIndex >= 0 ? items[previewIndex] : undefined;
          if (preview === undefined) return null;
          const style: PositionStyle = { "--turn-natural-position": `${previewIndex * TURN_SPACING_PX}px` };
          return (
            <div id={previewId} role="tooltip" className="turn-rail-preview" style={style}>
              <div className="turn-rail-preview-prompt">
                {preview.prompt || `第 ${preview.turn} 轮`}
              </div>
              {preview.response !== "" ? (
                <div className="turn-rail-preview-response">{preview.response}</div>
              ) : null}
            </div>
          );
        })()}
      </nav>
    </div>
  );
});
