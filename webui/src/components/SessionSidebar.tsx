
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";

import {
  HoverCard,
  IconBranchOutline16,
  IconCloseFill14,
  IconEditOutline16,
  IconEllipsisOutline16,
  IconNewChatOutline16,
  IconPanelLeftOutline16,
  IconSearchOutline16,
  IconSettingsOutline16,
  IconTrashOutline16,
  IconUserOutline16,
  Input,
  Menu,
  Modal,
  Button,
  StateDot,
  Tooltip,
} from "@/ui/primitives";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import type { SessionId, SessionSummary } from "@/types";
import css from "./Sidebar.module.css";

const COLLAPSE_SETTLE_MS = 150;
const EXPAND_SLIDE_MS = 300;
const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_QUERY_MAX_CODE_UNITS = 500;
/** 指针离开侧栏后滚动条保留显示的时长（DSH SCROLLBAR_LINGER_MS）。 */
const SCROLLBAR_LINGER_MS = 2000;

function sanitizeSearchQuery(value: string): string {
  const withoutNul = value.replaceAll("\0", "");
  if (withoutNul.length <= SEARCH_QUERY_MAX_CODE_UNITS) return withoutNul;
  let end = SEARCH_QUERY_MAX_CODE_UNITS;
  const last = withoutNul.charCodeAt(end - 1);
  const next = withoutNul.charCodeAt(end);
  if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) end--;
  return withoutNul.slice(0, end);
}

function timeLabel(updatedAt: number, now: number): string {
  const MIN = 60_000;
  const HOUR = 3_600_000;
  const DAY = 86_400_000;
  const diff = Math.max(0, now - updatedAt);
  if (diff < MIN) return "刚刚";
  if (diff < HOUR) return `${Math.floor(diff / MIN)}分钟`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}小时`;
  if (diff < 30 * DAY) return `${Math.floor(diff / DAY)}天`;
  if (diff < 365 * DAY) return `${Math.floor(diff / (30 * DAY))}个月`;
  return `${Math.floor(diff / (365 * DAY))}年`;
}

function summaryTitle(item: SessionSummary): string {
  const title = String(
    (item.projections?.values as Record<string, unknown> | undefined)?.title ?? "",
  );
  return title || "新会话";
}

export function SessionSidebar(props: {
  collapsed: boolean;
  width: number;
  onToggle: () => void;
  onOpenSettings: () => void;
}) {
  const { collapsed, width } = props;
  const presets = useApp((s) => s.presets);
  const defaultPreset = presets.find((p) => p.isDefault)?.id;
  const startSession = () => {
    void app.createSession(defaultPreset).catch(() => undefined);
  };

  const [settled, setSettled] = useState(collapsed);
  useEffect(() => {
    if (!collapsed) {
      setSettled(false);
      return;
    }
    const timer = window.setTimeout(() => setSettled(true), COLLAPSE_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [collapsed]);
  const wide = !collapsed || !settled;

  const lastWideWidth = useRef(width);
  if (!collapsed) lastWideWidth.current = width;

  const everWide = useRef(!collapsed);
  if (!collapsed) everWide.current = true;

  // 指针跟随滚动条（DSH quietBars）：指针在列内时显示 thumb，
  // 离开后保留 2 秒再隐藏；离开判定看列的 BOX 而非 DOM 包含（portal 菜单也在内）。
  const columnRef = useRef<HTMLDivElement | null>(null);
  const [pointerInside, setPointerInside] = useState(false);
  const lingerRef = useRef<number | undefined>(undefined);
  const armLinger = () => {
    if (lingerRef.current !== undefined) return;
    lingerRef.current = window.setTimeout(() => {
      lingerRef.current = undefined;
      setPointerInside(false);
    }, SCROLLBAR_LINGER_MS);
  };
  const cancelLinger = () => {
    window.clearTimeout(lingerRef.current);
    lingerRef.current = undefined;
  };
  useEffect(() => {
    if (!pointerInside) return;
    const onMove = (event: PointerEvent) => {
      const rect = columnRef.current?.getBoundingClientRect();
      if (rect === undefined) return;
      const inside =
        event.clientX >= rect.left &&
        event.clientX < rect.right &&
        event.clientY >= rect.top &&
        event.clientY < rect.bottom;
      if (inside) cancelLinger();
      else armLinger();
    };
    document.addEventListener("pointermove", onMove);
    return () => {
      document.removeEventListener("pointermove", onMove);
      cancelLinger();
    };
  }, [pointerInside]);

  return (
    <div
      ref={columnRef}
      className={clsx(
        css.root,
        !wide && css.collapsed,
        !wide && everWide.current && css.railIn,
        collapsed && wide && css.fading,
        !pointerInside && css.quietBars,
      )}
      style={wide ? { width: collapsed ? lastWideWidth.current : width } : undefined}
      onPointerEnter={() => {
        cancelLinger();
        setPointerInside(true);
      }}
      onPointerLeave={() => {
        armLinger();
      }}
    >
      <div className={css.logoRow}>
        {wide && (
          <button
            type="button"
            className={clsx(css.brand, css.wide)}
            aria-label="新会话"
            onClick={startSession}
          >
            代理女仆
          </button>
        )}
        <Tooltip label={collapsed ? "展开侧边栏" : "收起侧边栏"} delayMs={500}>
          <button
            type="button"
            className={css.iconButton}
            aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
            onClick={props.onToggle}
          >
            <IconPanelLeftOutline16 size={wide ? 16 : 18} />
          </button>
        </Tooltip>
      </div>

      <Tooltip label="新会话" delayMs={500} disabled={wide}>
        <button type="button" className={css.newSession} aria-label="新会话" onClick={startSession}>
          <IconNewChatOutline16 size={wide ? 14 : 18} />
          {wide && <span className={clsx(css.newSessionLabel, css.wide)}>新会话</span>}
        </button>
      </Tooltip>

      <div className={css.regionArea}>
        <SessionBrowser wide={wide} expandSidebar={() => { if (collapsed) props.onToggle(); }} />
      </div>

      <div className={css.footArea}>
        <UmoSwitcher wide={wide} />
        <Tooltip label="设置" delayMs={500} disabled={wide}>
          <button type="button" className={css.settingsButton} aria-label="设置" onClick={props.onOpenSettings}>
            <IconSettingsOutline16 size={wide ? 16 : 18} />
            {wide && <span>设置</span>}
          </button>
        </Tooltip>
      </div>
    </div>
  );
}

function UmoSwitcher(props: { wide: boolean }) {
  // sessions Map 内部原地修改，订阅 stamp 后读取最新引用
  useApp((s) => s.sessionsStamp);
  const sessions = app.getSnapshot().sessions;
  const [menuOpen, setMenuOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [customValue, setCustomValue] = useState("");

  const umo = app.currentUmo();
  const known = [
    ...new Set([umo, ...[...sessions.values()].map((s) => s.umo).filter((u): u is string => Boolean(u))]),
  ];

  function confirmCustom() {
    const value = customValue.trim();
    if (value) app.setUmo(value);
    setCustomOpen(false);
    setCustomValue("");
  }

  return (
    <>
      <Menu
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        portal
        side="top"
        anchor={
          <Tooltip label={umo} delayMs={500} disabled={props.wide}>
            <button
              type="button"
              className={css.settingsButton}
              aria-label="切换消息来源"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <IconUserOutline16 size={props.wide ? 16 : 18} />
              {props.wide && <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{umo}</span>}
            </button>
          </Tooltip>
        }
        items={[
          ...known.map((u) => ({ id: u, label: u })),
          { type: "separator" as const, id: "sep" },
          { id: "__custom", label: "自定义来源…" },
        ]}
        selectedId={umo}
        onSelect={(id) => {
          setMenuOpen(false);
          if (id === "__custom") setCustomOpen(true);
          else app.setUmo(id);
        }}
      />
      <Modal
        open={customOpen}
        title="自定义消息来源"
        onClose={() => setCustomOpen(false)}
        footer={
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setCustomOpen(false)}>取消</Button>
            <Button variant="primary" onClick={confirmCustom} disabled={!customValue.trim()}>确定</Button>
          </div>
        }
      >
        <div style={{ display: "grid", gap: 8 }}>
          <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
            格式 platform:MessageType:SessionId，新建会话的任务结果会推送到该来源。
          </p>
          <Input
            value={customValue}
            placeholder="aiocqhttp:GroupMessage:123456789"
            autoFocus
            onChange={(e) => setCustomValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) confirmCustom();
            }}
          />
        </div>
      </Modal>
    </>
  );
}

interface RemoteSearchState {
  query: string;
  status: "idle" | "loading" | "ready" | "error";
  items: { sessionId: SessionId; snippet?: string }[];
}

function SessionBrowser(props: { wide: boolean; expandSidebar: () => void }) {
  const { wide } = props;
  // sessions Map 内部原地修改，订阅 stamp 后读取最新引用
  useApp((s) => s.sessionsStamp);
  const sessionMap = app.getSnapshot().sessions;
  const current = useApp((s) => s.current);
  const umo = app.currentUmo();

  const [query, setQuery] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const normalizedQuery = sanitizeSearchQuery(query).trim();
  const [remote, setRemote] = useState<RemoteSearchState>({ query: "", status: "idle", items: [] });
  const searchRoot = useRef<HTMLDivElement | null>(null);
  const searchInput = useRef<HTMLInputElement | null>(null);

  const [searchOnExpand, setSearchOnExpand] = useState(false);
  useEffect(() => {
    if (wide && searchOnExpand) {
      const timer = window.setTimeout(() => {
        searchInput.current?.focus({ preventScroll: true });
        setSearchOnExpand(false);
      }, EXPAND_SLIDE_MS);
      return () => window.clearTimeout(timer);
    }
  }, [wide, searchOnExpand]);

  useEffect(() => {
    if (!wide || !searchExpanded || searchOnExpand) return;
    searchInput.current?.focus({ preventScroll: true });
  }, [wide, searchExpanded, searchOnExpand]);

  useEffect(() => {
    if (!wide || !searchExpanded) return;
    const onClick = (event: MouseEvent): void => {
      if (!(event.target instanceof Node) || searchRoot.current?.contains(event.target) === true) return;
      searchInput.current?.blur();
      if (normalizedQuery !== "") return;
      setSearchExpanded(false);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, [normalizedQuery, wide, searchExpanded]);

  useEffect(() => {
    if (normalizedQuery === "") {
      setRemote({ query: "", status: "idle", items: [] });
      return;
    }
    const controller = { aborted: false };
    setRemote({ query: normalizedQuery, status: "loading", items: [] });
    const timer = window.setTimeout(() => {
      app
        .searchSessions(normalizedQuery, umo)
        .then((items) => {
          if (controller.aborted) return;
          setRemote({ query: normalizedQuery, status: "ready", items });
        })
        .catch(() => {
          if (controller.aborted) return;
          setRemote({ query: normalizedQuery, status: "error", items: [] });
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      controller.aborted = true;
      window.clearTimeout(timer);
    };
  }, [normalizedQuery, umo]);

  const sessions = [...sessionMap.values()]
    .filter((s) => (s.umo || app.DEFAULT_UMO) === umo)
    .sort((a, b) => b.updatedAt - a.updatedAt);
  const now = Date.now();

  return (
    <div className={clsx(css.browser, !wide && css.rail)}>
      <div className={css.sectionHeader}>
        {wide && (
          <span className={clsx(css.sectionLabel, css.wide, searchExpanded && css.sectionLabelHidden)}>
            会话
          </span>
        )}
        {wide && (
          <div className={clsx(css.searchSlot, searchExpanded && css.searchSlotExpanded)}>
            <div
              ref={searchRoot}
              className={clsx(css.search, searchExpanded && css.searchExpanded)}
              onClick={() => {
                setSearchExpanded(true);
                searchInput.current?.focus();
              }}
            >
              <Tooltip label="搜索" side="bottom" delayMs={500} disabled={searchExpanded}>
                <button
                  type="button"
                  className={css.searchButton}
                  aria-label="搜索会话"
                  aria-expanded={searchExpanded}
                  onClick={() => setSearchExpanded(true)}
                >
                  <IconSearchOutline16 size={searchExpanded ? 11 : 14} />
                </button>
              </Tooltip>
              <input
                ref={searchInput}
                className={css.searchInput}
                type="text"
                placeholder="搜索会话"
                maxLength={SEARCH_QUERY_MAX_CODE_UNITS}
                value={query}
                tabIndex={searchExpanded ? 0 : -1}
                onChange={(e) => setQuery(sanitizeSearchQuery(e.target.value))}
                onKeyDown={(e) => {
                  if (e.key !== "Escape") return;
                  setQuery("");
                  setSearchExpanded(false);
                }}
              />
              {searchExpanded && (
                <button
                  type="button"
                  className={css.clearButton}
                  aria-label="清空搜索"
                  onClick={(e) => {
                    e.stopPropagation();
                    setQuery("");
                    setSearchExpanded(false);
                  }}
                >
                  <IconCloseFill14 />
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {!wide && (
        <div className={css.search}>
          <Tooltip label="搜索">
            <button
              type="button"
              className={css.searchButton}
              aria-label="搜索会话"
              onClick={() => {
                setSearchExpanded(true);
                setSearchOnExpand(true);
                props.expandSidebar();
              }}
            >
              <IconSearchOutline16 size={18} />
            </button>
          </Tooltip>
        </div>
      )}

      <div className={css.listArea}>
        {wide && (
          normalizedQuery !== "" ? (
            <SearchResultList
              sessions={sessions}
              query={normalizedQuery}
              remote={remote}
              currentId={current}
            />
          ) : (
            <div className={css.treeBody}>
              <div className={css.list} role="tree" aria-label="会话">
                {sessions.length === 0 && <div className={css.empty}>暂无会话</div>}
                {sessions.map((item) => (
                  <SessionRow key={item.sessionId} item={item} currentId={current} now={now} />
                ))}
              </div>
              <span className={css.fade} />
            </div>
          )
        )}
      </div>
    </div>
  );
}

function SearchResultList(props: {
  sessions: SessionSummary[];
  query: string;
  remote: RemoteSearchState;
  currentId: SessionId | undefined;
}) {
  const { query, remote } = props;
  const byId = new Map(props.sessions.map((s) => [s.sessionId, s]));
  const lower = query.toLowerCase();

  const remoteItems = remote.query === query ? remote.items : [];
  const remoteIds = new Set(remoteItems.map((item) => item.sessionId));
  const localMatches = props.sessions.filter(
    (s) => summaryTitle(s).toLowerCase().includes(lower) && !remoteIds.has(s.sessionId),
  );
  const rows = [
    ...remoteItems.map((item) => ({
      id: item.sessionId,
      title: byId.has(item.sessionId) ? summaryTitle(byId.get(item.sessionId)!) : `${item.sessionId.slice(0, 8)}…`,
      running: Boolean(byId.get(item.sessionId)?.running),
      snippet: item.snippet,
    })),
    ...localMatches.map((s) => ({ id: s.sessionId, title: summaryTitle(s), running: Boolean(s.running), snippet: undefined })),
  ];

  const pending = remote.query === query && remote.status === "loading";
  const failed = remote.query === query && remote.status === "error";

  return (
    <div className={css.treeBody}>
      <div className={css.list}>
        {rows.map((row) => (
          <button
            key={row.id}
            type="button"
            className={clsx(css.searchResultRow, row.id === props.currentId && css.selected)}
            role="treeitem"
            aria-selected={row.id === props.currentId}
            onClick={() => void app.selectSession(row.id)}
          >
            <span className={css.searchResultHeading}>
              <span className={css.slot}>
                <StateDot state={row.running ? "ongoing" : "done"} />
              </span>
              <span className={css.searchResultTitle}>{row.title}</span>
            </span>
            {row.snippet !== undefined && <span className={css.searchResultSnippet}>{row.snippet}</span>}
          </button>
        ))}
        {pending && <div className={css.searchStatus} role="status">搜索中…</div>}
        {failed && <div className={css.searchStatus} role="status">内容搜索不可用，仅显示标题匹配</div>}
        {!pending && rows.length === 0 && <div className={css.empty}>没有匹配的会话</div>}
      </div>
      <span className={css.fade} />
    </div>
  );
}

function SessionRow(props: { item: SessionSummary; currentId: SessionId | undefined; now: number }) {
  const { item, now } = props;
  const [menuOpen, setMenuOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const selected = item.sessionId === props.currentId;
  const title = summaryTitle(item);
  const status = item.running ? "ongoing" : "done";

  const ownRow = (
    <div
      className={clsx(css.sessionRow, selected && css.selected, menuOpen && css.menuOpen)}
      role="treeitem"
      aria-selected={selected}
      onClick={() => void app.selectSession(item.sessionId)}
    >
      <span className={css.slot}>
        <StateDot state={status} />
      </span>
      <span className={css.rowTitle}>{title}</span>
      {!item.blank && <span className={css.time}>{timeLabel(item.updatedAt, now)}</span>}
      {!item.blank && (
        <span className={css.rowActions}>
          <Menu
            open={menuOpen}
            onClose={() => setMenuOpen(false)}
            items={[
              { id: "rename", label: "重命名", icon: <IconEditOutline16 /> },
              { id: "fork", label: "Fork", icon: <IconBranchOutline16 /> },
              { type: "separator" as const, id: "delete-separator" },
              {
                id: "delete",
                label: item.running ? "运行中无法删除" : "删除会话",
                icon: <IconTrashOutline16 />,
                danger: true,
                disabled: item.running,
              },
            ]}
            onSelect={(id) => {
              setMenuOpen(false);
              if (id === "rename") setRenameOpen(true);
              if (id === "fork") void app.forkSession(item.sessionId).catch(() => undefined);
              if (id === "delete") setDeleteOpen(true);
            }}
            portal
            anchor={
              <button
                type="button"
                className={css.rowIconButton}
                aria-label={`会话操作：${title}`}
                onClick={(e) => {
                  e.stopPropagation();
                  setMenuOpen(true);
                }}
              >
                <IconEllipsisOutline16 />
              </button>
            }
          />
        </span>
      )}
    </div>
  );

  return (
    <>
      <HoverCard
        anchor={ownRow}
        disabled={menuOpen}
        content={
          <div className={css.hoverContent}>
            <div className={css.hoverTitle}>{title}</div>
            {!item.blank && <div className={css.hoverTime}>{timeLabel(item.updatedAt, now)}前</div>}
            <div className={css.hoverStatus}>
              <StateDot state={status} />
              <span>{item.running ? "运行中" : item.blank ? "空会话" : "已完成"}</span>
            </div>
          </div>
        }
        copyText={item.blank ? undefined : title}
        copyLabel="复制"
        copiedLabel="已复制"
      />
      <RenameDialog
        open={renameOpen}
        sessionId={item.sessionId}
        currentTitle={title}
        onClose={() => setRenameOpen(false)}
      />
      <DeleteDialog
        open={deleteOpen}
        sessionId={item.sessionId}
        title={title}
        onClose={() => setDeleteOpen(false)}
      />
    </>
  );
}

function DeleteDialog(props: { open: boolean; sessionId: SessionId; title: string; onClose: () => void }) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!props.open) return;
    setDeleting(false);
    setError("");
  }, [props.open]);

  const confirm = () => {
    if (deleting) return;
    setDeleting(true);
    setError("");
    app.deleteSession(props.sessionId).then(props.onClose).catch((reason: unknown) => {
      setDeleting(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    });
  };

  return (
    <Modal
      open={props.open}
      onClose={() => { if (!deleting) props.onClose(); }}
      closeLabel="关闭"
      title="删除会话"
      footer={
        <>
          <Button variant="outline" disabled={deleting} onClick={props.onClose}>取消</Button>
          <Button variant="primary" disabled={deleting} onClick={confirm}>
            {deleting ? "删除中…" : "删除"}
          </Button>
        </>
      }
    >
      <p style={{ margin: 0, lineHeight: 1.6 }}>
        要删除“{props.title}”吗？该会话及其附件将无法恢复。
      </p>
      {error !== "" && <div role="alert" style={{ color: "var(--maid-alias-state-danger-label, #c00)", fontSize: 12.5, marginTop: 8 }}>{error}</div>}
    </Modal>
  );
}

function RenameDialog(props: { open: boolean; sessionId: SessionId; currentTitle: string; onClose: () => void }) {
  const [draft, setDraft] = useState(props.currentTitle);
  const [renaming, setRenaming] = useState(false);
  const [error, setError] = useState("");
  const composingRef = useRef(false);

  useEffect(() => {
    if (props.open) {
      setDraft(props.currentTitle);
      setError("");
    }
  }, [props.open, props.currentTitle]);

  const blocked = renaming || draft.trim() === "";

  const confirm = () => {
    if (blocked) return;
    setRenaming(true);
    setError("");
    app
      .renameSession(props.sessionId, draft.trim())
      .then(() => {
        setRenaming(false);
        props.onClose();
      })
      .catch((reason: unknown) => {
        setRenaming(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      });
  };

  return (
    <Modal
      open={props.open}
      onClose={() => { if (!renaming) props.onClose(); }}
      closeLabel="关闭"
      title="重命名会话"
      footer={
        <>
          <Button variant="outline" disabled={renaming} onClick={props.onClose}>
            取消
          </Button>
          <Button variant="primary" disabled={blocked} onClick={confirm}>
            重命名
          </Button>
        </>
      }
    >
      <input
        className={css.renameInput}
        value={draft}
        aria-label="会话名称"
        autoFocus
        disabled={renaming}
        onFocus={(e) => e.target.select()}
        onChange={(e) => {
          setDraft(e.target.value);
          setError("");
        }}
        onCompositionStart={() => { composingRef.current = true; }}
        onCompositionEnd={() => { composingRef.current = false; }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !composingRef.current) {
            e.preventDefault();
            confirm();
          }
        }}
      />
      {error !== "" && <div role="alert" style={{ color: "var(--maid-alias-state-danger-label, #c00)", fontSize: 12.5, marginTop: 8 }}>{error}</div>}
    </Modal>
  );
}
