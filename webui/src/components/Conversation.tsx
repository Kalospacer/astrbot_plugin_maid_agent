import { useCallback, useRef, useState } from "react";
import clsx from "clsx";

import { Menu } from "@/ui/primitives";
import { IconAgentPresetOutline16, IconChevronDownOutline14 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import { ChatView } from "@/components/ChatView";
import { Composer } from "@/components/Composer";
import css from "./Conversation.module.css";

export function ConversationRoot(props: {
  booting: boolean;
}) {
  const current = useApp((s) => s.current);
  // summary 原地修改，订阅派生原始值
  const summaryBlank = useApp((s) => (s.current ? s.sessions.get(s.current)?.blank : undefined));
  const running = useApp((s) => (s.current ? Boolean(s.sessions.get(s.current)?.running) : false));
  const historyLoaded = useApp((s) =>
    s.current ? (s.byId.get(s.current)?.historyLoaded ?? false) : false,
  );

  // 发布输入区座高度与滚动口高度（DSH ConversationRoot seatObserver）：
  // 回底按钮与轮次导航轨据此避开 sticky 输入区，并居中于剩余可见带。
  const seatObserver = useRef<ResizeObserver | null>(null);
  const seatResizeRef = useCallback((seat: HTMLDivElement | null): void => {
    seatObserver.current?.disconnect();
    seatObserver.current = null;
    const scroller = seat?.parentElement ?? null;
    if (seat === null || scroller === null) return;
    seatObserver.current = new ResizeObserver(() => {
      scroller.style.setProperty("--maid-composer-height", `${seat.offsetHeight}px`);
      scroller.style.setProperty("--maid-conversation-viewport-height", `${scroller.clientHeight}px`);
    });
    seatObserver.current.observe(seat);
    seatObserver.current.observe(scroller);
  }, []);

  const hero =
    current === undefined ||
    summaryBlank === true ||
    (historyLoaded && (summaryBlank ?? true));
  const settling = !hero && !historyLoaded && !props.booting;
  const phase = props.booting || hero ? "hero" : settling ? "settling" : "active";

  return (
    <div className={css.root} data-phase={phase}>
      <div className={css.scrollBody} data-conversation-scroll="">
        {!hero && !props.booting ? (
          <div className={css.viewArea}>
            <ChatView />
          </div>
        ) : null}
        <div className={css.composerSeat} data-composer-seat="" ref={seatResizeRef}>
          <div className={clsx(css.composerStack, phase === "hero" && css.composerHero)}>
            {phase === "hero" && !props.booting ? <HeroHeadline text="派一个新任务" /> : null}
            {phase === "hero" && !props.booting ? <PresetChipRow /> : null}
            <Composer
              variant={phase === "hero" ? "hero" : "composer"}
              running={running}
              disabled={props.booting}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function HeroHeadline(props: { text: string }) {
  return (
    <div className={css.headline}>
      <span className={css.headlineText}>{props.text}</span>
    </div>
  );
}

function PresetChipRow() {
  const presets = useApp((s) => s.presets);
  const current = useApp((s) => s.current);
  const activePresetId = useApp((s) => (s.current ? s.sessions.get(s.current)?.agentPreset : undefined));
  const [open, setOpen] = useState(false);

  if (presets.length === 0) return null;
  const activeId = activePresetId ?? app.chosenPreset();
  const active = presets.find((p) => p.id === activeId);

  return (
    <div className={css.heroWorkspaceRow}>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        portal
        anchor={
          <button
            type="button"
            className={css.presetChip}
            aria-label="选择代理预设"
            aria-haspopup="menu"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <span className={css.presetIcon}>
              <IconAgentPresetOutline16 size={16} />
            </span>
            <span className={css.presetLabel}>{active?.name ?? activeId ?? "选择预设"}</span>
            <span className={css.presetChevron}>
              <IconChevronDownOutline14 size={12} />
            </span>
          </button>
        }
        items={presets.map((p) => ({ id: p.id, label: p.name ?? p.id }))}
        selectedId={activeId || undefined}
        onSelect={(id) => {
          setOpen(false);
          if (current) void app.selectPreset(current, id).catch(() => undefined);
          else app.setPresetChoice(id);
        }}
      />
    </div>
  );
}
