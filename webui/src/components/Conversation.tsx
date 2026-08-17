import { useState } from "react";
import clsx from "clsx";

import { Menu, Tooltip } from "@/ui/primitives";
import { IconAgentPresetOutline16, IconChevronDownOutline14, IconPanelLeftOutline16 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import { ChatView } from "@/components/ChatView";
import { Composer } from "@/components/Composer";
import css from "./Conversation.module.css";

/** 常驻对话骨架：hero 标题、composer 定位
 *  与输入条跨 无会话/会话 过渡保持挂载 —— data-phase 驱动布局，不重构树。
 *  相位：hero（空会话，卡居中）/ active（卡吸附底部）/ settling（历史回放中）。 */
export function ConversationRoot(props: {
  booting: boolean;
  detailsOpen?: boolean;
  onToggleDetails?: () => void;
}) {
  const current = useApp((s) => s.current);
  const summary = useApp((s) => (s.current ? s.sessions.get(s.current) : undefined));
  const historyLoaded = useApp((s) =>
    s.current ? (s.byId.get(s.current)?.historyLoaded ?? false) : false,
  );
  const running = Boolean(summary?.running);

  // hero = 没有会话，或会话尚未有过回合（summary.blank），或历史回放完仍为空。
  const hero =
    current === undefined ||
    summary?.blank === true ||
    (historyLoaded && (summary?.blank ?? true));
  // settling = 非空会话还在回放历史：座位保持挂载但不可见，避免相位闪烁。
  const settling = !hero && !historyLoaded && !props.booting;
  const phase = props.booting || hero ? "hero" : settling ? "settling" : "active";

  return (
    <div className={css.root} data-phase={phase}>
      {props.onToggleDetails ? (
        <Tooltip label={props.detailsOpen ? "收起详情" : "会话详情"} side="bottom" delayMs={500}>
          <button
            type="button"
            className={css.detailsToggle}
            aria-label={props.detailsOpen ? "收起详情" : "会话详情"}
            aria-pressed={props.detailsOpen}
            onClick={props.onToggleDetails}
          >
            {/* 面板图标镜像朝右（详情列在右） */}
            <span style={{ display: "inline-flex", transform: "scaleX(-1)" }}>
              <IconPanelLeftOutline16 size={16} />
            </span>
          </button>
        </Tooltip>
      ) : null}
      <div className={css.scrollBody} data-conversation-scroll="">
        {!hero && !props.booting ? (
          <div className={css.viewArea}>
            <ChatView />
          </div>
        ) : null}
        <div className={css.composerSeat} data-composer-seat="">
          <div className={clsx(css.composerStack, phase === "hero" && css.composerHero)}>
            {phase === "hero" ? (
              <HeroHeadline text={props.booting ? "正在连接 AstrBot…" : "派一个新任务"} />
            ) : null}
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

/** hero 标题：居中大标题。 */
function HeroHeadline(props: { text: string }) {
  return (
    <div className={css.headline}>
      <span className={css.headlineText}>{props.text}</span>
    </div>
  );
}

/** hero 芯片行：agent 预设选择。
 *  未建会话时写预选（createSession 带上）；会话内走 agentPreset.select。 */
function PresetChipRow() {
  const presets = useApp((s) => s.presets);
  const current = useApp((s) => s.current);
  const summary = useApp((s) => (s.current ? s.sessions.get(s.current) : undefined));
  // 订阅版本号即可，选中项在渲染期读取（chosenPreset 依赖模块级 presetChoice）
  useApp((s) => s.presets.length);
  const [open, setOpen] = useState(false);

  if (presets.length === 0) return null;
  const activeId = summary?.agentPreset ?? app.chosenPreset();
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
