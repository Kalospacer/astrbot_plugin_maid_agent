// 尾部统计 pill + 点击弹窗（对齐 DSH TurnUsagePanel / TurnTimePanel）：
// 用量 pill 展开本轮 token 明细；用时 pill 展开本轮总用时 / TPS / TTFT。
// 文案取自 DSH zh 词条（message.turnUsage.* / message.turnTime.*）。

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { IconClockOutline16, IconDatabaseOutline16 } from "@/ui/primitives/icons";
import { useDismissOnOutsidePointer } from "@/ui/chrome/dismiss";
import {
  formatExactTokens,
  formatTokens,
  formatCacheHitPercent,
  formatDuration,
  formatRunDuration,
  formatThroughput,
} from "@/ui/chrome/format";

export interface StatUsage {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  totalTokens: number;
}

export interface StatTiming {
  /** 本轮墙上时间，pill 的标签。 */
  runMs: number;
  /** 本轮解码吞吐，已知时作为弹窗一行。 */
  tokensPerSecond?: number | undefined;
  /** 本轮首个 step 的 TTFT，已知时作为弹窗一行。 */
  ttftMs?: number | undefined;
}

/** 触发器旁弹窗定位：固定于触发器上方 8px，钳制在视口内。 */
function usePanelPosition(open: boolean) {
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<CSSProperties | null>(null);
  useEffect(() => {
    if (!open) return;
    const root = rootRef.current;
    const panel = panelRef.current;
    if (root === null || panel === null) return;
    const measure = () => {
      const trigger = root.getBoundingClientRect();
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      let left = trigger.left + trigger.width / 2 - width / 2;
      left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
      let top = trigger.top - height - 8;
      if (top < 12) top = Math.min(trigger.bottom + 8, window.innerHeight - height - 12);
      setPos({ left, top });
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open]);
  return { rootRef, panelRef, pos };
}

/** pill 触发器 + 受控弹窗的共用外壳（两个面板的几何/关闭行为完全一致）。 */
function StatDisclosure(props: {
  icon: ReactNode;
  label: string;
  dialogLabel: string;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const { rootRef, panelRef, pos } = usePanelPosition(open);
  useDismissOnOutsidePointer(rootRef, open, setOpen, panelRef);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <span ref={rootRef} style={{ display: "inline-flex", minWidth: 0 }}>
      <button
        type="button"
        className="stat-pill"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {props.icon}
        <span className="stat-label">{props.label}</span>
      </button>
      {open ? (
        <div
          ref={panelRef}
          className="stat-panel"
          role="dialog"
          aria-label={props.dialogLabel}
          style={pos ?? { visibility: "hidden", left: 0, top: 0 }}
        >
          {props.children(() => setOpen(false))}
        </div>
      ) : null}
    </span>
  );
}

/** 本轮用量面板（DSH TurnUsagePanel）。 */
export function StatPanel(props: { usage: StatUsage; timing?: StatTiming }) {
  const usage = props.usage;
  const billedInput =
    (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0);
  const total = usage.totalTokens ?? billedInput + (usage.outputTokens ?? 0);
  const hit = formatCacheHitPercent(usage.cacheReadTokens ?? 0, billedInput);

  return (
    <>
      <StatDisclosure
        icon={<IconDatabaseOutline16 size={15} />}
        label={`用量 ${formatTokens(total)}`}
        dialogLabel="本轮用量"
      >
        {() => (
          <>
            <div className="stat-panel-title">
              <span className="stat-panel-label">
                <IconDatabaseOutline16 size={14} />
                本轮用量
              </span>
              <span className="stat-panel-value">{formatExactTokens(total)} tok</span>
            </div>
            <div className="stat-panel-rule" aria-hidden />
            <dl>
              <dt>未缓存输入</dt>
              <dd>{formatExactTokens(usage.inputTokens ?? 0)}</dd>
              {usage.cacheReadTokens !== undefined ? (
                <>
                  <dt>缓存读取</dt>
                  <dd>{formatExactTokens(usage.cacheReadTokens)}</dd>
                </>
              ) : null}
              {usage.cacheWriteTokens !== undefined ? (
                <>
                  <dt>缓存写入</dt>
                  <dd>{formatExactTokens(usage.cacheWriteTokens)}</dd>
                </>
              ) : null}
              <dt>输出</dt>
              <dd>{formatExactTokens(usage.outputTokens ?? 0)}</dd>
            </dl>
            {hit !== null ? (
              <>
                <div className="stat-panel-rule" aria-hidden />
                <dl>
                  <dt>缓存命中</dt>
                  <dd>{hit}%</dd>
                </dl>
              </>
            ) : null}
          </>
        )}
      </StatDisclosure>
      {props.timing !== undefined ? <TimePanel timing={props.timing} /> : null}
    </>
  );
}

/** 本轮用时和速度面板（DSH TurnTimePanel）。 */
function TimePanel(props: { timing: StatTiming }) {
  const { runMs, tokensPerSecond, ttftMs } = props.timing;
  return (
    <StatDisclosure
      icon={<IconClockOutline16 size={15} />}
      label={`用时 ${formatRunDuration(runMs)}`}
      dialogLabel="本轮用时和速度"
    >
      {() => (
        <>
          <div className="stat-panel-title">
            <span className="stat-panel-label">
              <IconClockOutline16 size={14} />
              本轮用时和速度
            </span>
          </div>
          <div className="stat-panel-rule" aria-hidden />
          <dl>
            <dt>本轮总用时</dt>
            <dd>{formatRunDuration(runMs)}</dd>
            {tokensPerSecond !== undefined ? (
              <>
                <dt>输出速度（TPS）</dt>
                <dd>{formatThroughput(tokensPerSecond)}</dd>
              </>
            ) : null}
            {ttftMs !== undefined ? (
              <>
                <dt>首 token 用时（TTFT）</dt>
                <dd>{formatDuration(ttftMs)}</dd>
              </>
            ) : null}
          </dl>
        </>
      )}
    </StatDisclosure>
  );
}
