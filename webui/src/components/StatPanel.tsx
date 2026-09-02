// 尾部统计 pill + 点击弹窗（DSH TurnUsagePanel/TurnTimePanel 合并简化版）：
// 用量 pill（IconDatabase + 总量）点击展开本轮用量明细弹窗。
// maid 无 per-turn 路由/推理 token 明细，展示可用字段。

import { useEffect, useRef, useState, type CSSProperties } from "react";

import { IconClockOutline16, IconDatabaseOutline16 } from "@/ui/primitives/icons";
import { useDismissOnOutsidePointer } from "@/ui/chrome/dismiss";
import { formatExactTokens, formatTokens, formatCacheHitPercent, formatRunDuration } from "@/ui/chrome/format";

export interface StatUsage {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  totalTokens: number;
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

/**
 * 用量 pill 簇：仅在有 usage 数据时渲染。
 * DSH 中用量和时间是两个 pill；maid 只有 usage 事件（无 turn 起止计时数据可靠来源），
 * 时间 pill 仅在 runMs 提供时出现。
 */
export function StatPanel(props: { usage: StatUsage; runMs?: number }) {
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

  const usage = props.usage;
  const billedInput =
    (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0) + (usage.cacheWriteTokens ?? 0);
  const total = usage.totalTokens ?? billedInput + (usage.outputTokens ?? 0);

  return (
    <span ref={rootRef} style={{ display: "inline-flex", minWidth: 0 }}>
      <button
        type="button"
        className="stat-pill"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <IconDatabaseOutline16 size={15} />
        <span className="stat-label">用量 {formatTokens(total)}</span>
      </button>
      {props.runMs !== undefined ? (
        <span className="stat-pill" style={{ cursor: "default" }} aria-hidden="false">
          <IconClockOutline16 size={15} />
          <span className="stat-label">用时 {formatRunDuration(props.runMs)}</span>
        </span>
      ) : null}
      {open ? (
        <div
          ref={panelRef}
          className="stat-panel"
          role="dialog"
          aria-label="本轮用量"
          style={pos ?? { visibility: "hidden", left: 0, top: 0 }}
        >
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
          {(() => {
            const hit = formatCacheHitPercent(usage.cacheReadTokens ?? 0, billedInput);
            return hit !== null ? (
              <>
                <div className="stat-panel-rule" aria-hidden />
                <dl>
                  <dt>缓存命中</dt>
                  <dd>{hit}%</dd>
                </dl>
              </>
            ) : null;
          })()}
        </div>
      ) : null}
    </span>
  );
}
