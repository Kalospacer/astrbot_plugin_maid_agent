
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  clampWidth,
  computeColumns,
  DETAILS_DEFAULT,
  DETAILS_MAX,
  DETAILS_MIN,
  SIDEBAR_AUTO_COLLAPSE,
  SIDEBAR_DEFAULT,
  SIDEBAR_MAX,
  SIDEBAR_MIN,
} from "./columns.ts";
import css from "./AppFrame.module.css";

function CenterColumn(props: { children?: ReactNode }) {
  return <div className={css.centerCol}>{props.children}</div>;
}

function DetailsColumn(props: { children?: ReactNode }) {
  return <div className={css.detailsCol}>{props.children}</div>;
}

function DragHandle(props: { side: "sidebar" | "details"; left: number; onStart: () => void; onDrag: (dx: number) => void; onEnd: () => void }) {
  const [dragging, setDragging] = useState(false);
  const origin = useRef(0);
  const latest = useRef(0);
  const frame = useRef<number | null>(null);
  const callbacks = useRef({ onStart: props.onStart, onDrag: props.onDrag, onEnd: props.onEnd });
  callbacks.current = { onStart: props.onStart, onDrag: props.onDrag, onEnd: props.onEnd };

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    origin.current = e.clientX;
    latest.current = e.clientX;
    callbacks.current.onStart();
    setDragging(true);
  }, []);
  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    latest.current = e.clientX;
    frame.current ??= requestAnimationFrame(() => {
      frame.current = null;
      callbacks.current.onDrag(latest.current - origin.current);
    });
  }, []);
  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    if (frame.current !== null) {
      cancelAnimationFrame(frame.current);
      frame.current = null;
    }
    callbacks.current.onDrag(latest.current - origin.current);
    setDragging(false);
    callbacks.current.onEnd();
  }, []);

  return (
    <div
      className={css.handle}
      style={{ left: props.left }}
      data-side={props.side}
      data-dragging={dragging || undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    />
  );
}

export interface SidebarApi {
  collapsed: boolean;
  width: number;
  toggle: () => void;
}

export interface AppFrameProps {
  sidebar: ReactNode | ((api: SidebarApi) => ReactNode);
  conversation: ReactNode;
  details: ReactNode;
  overlay?: ReactNode;
  detailsActive: boolean;
}

interface LayoutPrefs {
  sidebar: number;
  details: number;
  narrowExpanded: boolean;
}

export function AppFrame(props: AppFrameProps) {
  const [panels, setPanels] = useState<LayoutPrefs>(() => ({
    sidebar: SIDEBAR_DEFAULT,
    details: DETAILS_DEFAULT,
    narrowExpanded: false,
  }));
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState(() => window.innerWidth);

  useEffect(() => {
    const el = frameRef.current;
    if (el === null) return;
    let raf: number | null = null;
    const observer = new ResizeObserver(() => {
      raf ??= requestAnimationFrame(() => {
        raf = null;
        const width = el.getBoundingClientRect().width;
        if (width > 0) setViewport(width);
      });
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      if (raf !== null) cancelAnimationFrame(raf);
    };
  }, []);

  const narrow = viewport < SIDEBAR_AUTO_COLLAPSE;
  const sidebarCollapsed = narrow ? !panels.narrowExpanded : panels.sidebar === 0;
  const sidebarPreference = sidebarCollapsed
    ? 0
    : panels.sidebar === 0
      ? SIDEBAR_DEFAULT
      : panels.sidebar;
  const cols = computeColumns(viewport, sidebarPreference, props.detailsActive ? panels.details : 0);
  const colsRef = useRef(cols);
  colsRef.current = cols;

  const toggleSidebar = useCallback(() => {
    setPanels((p) => {
      if (frameRef.current !== null && frameRef.current.getBoundingClientRect().width < SIDEBAR_AUTO_COLLAPSE) {
        return { ...p, narrowExpanded: !p.narrowExpanded };
      }
      return { ...p, sidebar: p.sidebar === 0 ? SIDEBAR_DEFAULT : 0 };
    });
  }, []);

  const sidebarBase = useRef(0);
  const detailsBase = useRef(0);
  const [dragging, setDragging] = useState(false);
  const onDragEnd = useCallback(() => {
    setDragging(false);
  }, []);
  const onSidebarStart = useCallback(() => {
    sidebarBase.current = colsRef.current.sidebar;
    setDragging(true);
  }, []);
  const onDetailsStart = useCallback(() => {
    detailsBase.current = colsRef.current.details;
    setDragging(true);
  }, []);
  const onSidebarDrag = useCallback((dx: number) => {
    setPanels((p) => ({ ...p, sidebar: clampWidth(sidebarBase.current + dx, SIDEBAR_MIN, SIDEBAR_MAX) }));
  }, []);
  const onDetailsDrag = useCallback((dx: number) => {
    setPanels((p) => ({ ...p, details: clampWidth(detailsBase.current - dx, DETAILS_MIN, DETAILS_MAX) }));
  }, []);

  return (
    <div
      ref={frameRef}
      className={css.frame}
      style={{ gridTemplateColumns: `${cols.sidebar}px minmax(0, 1fr) ${cols.details}px` }}
      data-sidebar-collapsed={sidebarCollapsed || undefined}
      data-details-collapsed={cols.details === 0 || undefined}
      data-dragging={dragging || undefined}
    >
      <div className={css.sidebarCol}>
        {typeof props.sidebar === "function"
          ? props.sidebar({ collapsed: sidebarCollapsed, width: cols.sidebar, toggle: toggleSidebar })
          : props.sidebar}
      </div>
      <>
        <CenterColumn>{props.conversation}</CenterColumn>
        <DetailsColumn>{props.details}</DetailsColumn>
      </>
      <div className={css.overlayLayer} data-shell-overlay>
        {props.overlay}
      </div>
      {!sidebarCollapsed && (
        <DragHandle side="sidebar" left={cols.sidebar} onStart={onSidebarStart} onDrag={onSidebarDrag} onEnd={onDragEnd} />
      )}
      {cols.details > 0 && (
        <DragHandle side="details" left={viewport - cols.details} onStart={onDetailsStart} onDrag={onDetailsDrag} onEnd={onDragEnd} />
      )}
    </div>
  );
}
