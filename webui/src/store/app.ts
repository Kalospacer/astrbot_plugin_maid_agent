
import { call, ConnectionController, type ConnectionState } from "@/api/client";
import { ConversationFolder } from "@/store/conversation";
import type {
  AgentPresetEntry,
  HistoryEntry,
  HostFrame,
  Message,
  MuxFrame,
  PromptContentPart,
  QueuedInboxItem,
  SessionEvent,
  SessionId,
  SessionRuntimeMetadata,
  SessionSummary,
  SettingsNamespaceView,
  ToolEventView,
} from "@/types";

export interface SessionState {
  sessionId: SessionId;
  summary: SessionSummary;
  events: Map<number, SessionEvent>;
  views: Map<number, ToolEventView>;
  projections: Record<string, unknown>;
  projectionsSeq: number;
  queue: QueuedInboxItem[];
  historyLoaded: boolean;
  hasMore: boolean;
  models: SessionModels | null;
  /** 任何 session 内部数据（events/views/queue/summary/projections/models）变化时 +1，
   *  供 useApp 选择器以原始值订阅，避免原地修改导致更新丢失。 */
  stamp: number;
  /** 增量折叠器：跨渲染复用，流式时只 fold 新增事件。 */
  folder: ConversationFolder;
}

export interface SessionModels {
  current: { provider: string; model: string; override: boolean };
  providers: { id: string; model: string; type: string }[];
}

export interface AppState {
  connection: ConnectionState;
  booting: boolean;
  sessions: Map<SessionId, SessionSummary>;
  /** 会话列表或其内部任何 summary 变化时 +1（summary 对象是原地修改的）。 */
  sessionsStamp: number;
  byId: Map<SessionId, SessionState>;
  current: SessionId | undefined;
  presets: AgentPresetEntry[];
  settings: SettingsNamespaceView | null;
  theme: "light" | "dark";
  busy: boolean;
}

type Listener = () => void;

const state: AppState = {
  connection: "connecting",
  booting: true,
  sessions: new Map(),
  sessionsStamp: 0,
  byId: new Map(),
  current: undefined,
  presets: [],
  settings: null,
  theme: readTheme(),
  busy: false,
};

const listeners = new Set<Listener>();
let version = 0;

function readTheme(): "light" | "dark" {
  try {
    return localStorage.getItem("maid-theme") === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

let flushScheduled = false;

function flush(): void {
  flushScheduled = false;
  version += 1;
  for (const listener of listeners) listener();
}

/**
 * 微任务合批的 emit：同一事件循环里的连续变更（如批量 RPC 回写、
 * SSE 帧连发）只触发一轮监听者通知，配合 useApp 的选择器缓存，
 * 把流式期间的渲染开销压到最小。
 */
function emit(): void {
  if (flushScheduled) return;
  flushScheduled = true;
  queueMicrotask(flush);
}

/** 标记某个 session 的内部数据已变化（供 useApp stamp 选择器感知）。 */
function touchSession(session: SessionState): void {
  session.stamp += 1;
}

/** 标记会话列表/摘要已变化。 */
function touchSessions(): void {
  state.sessionsStamp += 1;
}

function applyRuntimeMetadata(summary: SessionSummary, metadata: SessionRuntimeMetadata): void {
  const keys: (keyof SessionRuntimeMetadata)[] = [
    "executionMode",
    "sourceKind",
    "backgroundReason",
    "dispatchId",
    "agentId",
    "taskId",
    "foregroundLease",
    "deliveryStatus",
  ];
  for (const key of keys) {
    if (metadata[key] !== undefined) summary[key] = metadata[key] as never;
  }
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSnapshot(): AppState {
  return state;
}

export function getVersion(): number {
  return version;
}

function ensureSession(sessionId: SessionId): SessionState {
  let session = state.byId.get(sessionId);
  if (!session) {
    const summary: SessionSummary =
      state.sessions.get(sessionId) ?? {
        sessionId,
        updatedAt: 0,
        running: false,
        blank: true,
      };
    session = {
      sessionId,
      summary,
      events: new Map(),
      views: new Map(),
      projections: { title: null },
      projectionsSeq: -1,
      queue: [],
      historyLoaded: false,
      hasMore: false,
      models: null,
      stamp: 0,
      folder: new ConversationFolder(),
    };
    state.byId.set(sessionId, session);
  }
  return session;
}

export async function refreshSessions(): Promise<void> {
  const { items } = await call<{ items: SessionSummary[] }>("session.list", {});
  state.sessions = new Map(items.map((item) => [item.sessionId, item]));
  for (const [sid, session] of state.byId) {
    const fresh = items.find((item) => item.sessionId === sid);
    if (fresh) {
      session.summary = fresh;
      touchSession(session);
    } else state.byId.delete(sid);
  }
  if (state.current !== undefined && !state.sessions.has(state.current)) {
    state.current = undefined;
  }
  touchSessions();
  emit();
}

export async function selectSession(sessionId: SessionId | undefined): Promise<void> {
  state.current = sessionId;
  emit();
  if (!sessionId) return;
  await loadHistoryTail(sessionId);
}

export async function loadHistoryTail(sessionId: SessionId): Promise<void> {
  const session = ensureSession(sessionId);
  const page = await call<{ events: HistoryEntry[]; hasMore: boolean; projections?: { asOfSeq: number; values: Record<string, unknown> } }>(
    "session.history",
    { sessionId, maxMessages: 30 },
  );
  session.events = new Map(page.events.map((entry) => [entry.event.seq, entry.event]));
  session.views = new Map(
    page.events.filter((entry) => entry.view).map((entry) => [entry.event.seq, entry.view as ToolEventView]),
  );
  session.hasMore = page.hasMore;
  if (page.projections) applyProjections(sessionId, page.projections.values, page.projections.asOfSeq);
  session.historyLoaded = true;
  touchSession(session);
  emit();
}

export async function loadOlder(sessionId: SessionId): Promise<void> {
  const session = state.byId.get(sessionId);
  if (!session || !session.hasMore) return;
  const oldest = Math.min(...session.events.keys());
  const page = await call<{ events: HistoryEntry[]; hasMore: boolean }>(
    "session.history",
    { sessionId, beforeSeq: oldest, maxMessages: 30 },
  );
  for (const entry of page.events) {
    session.events.set(entry.event.seq, entry.event);
    if (entry.view) session.views.set(entry.event.seq, entry.view);
  }
  session.hasMore = page.hasMore;
  touchSession(session);
  emit();
}

function applyProjections(sessionId: SessionId, values: Record<string, unknown>, seq: number): void {
  const session = ensureSession(sessionId);
  if (seq < session.projectionsSeq) return;
  session.projectionsSeq = seq;
  session.projections = { ...session.projections, ...values };
}

export function applyMuxFrame(frame: MuxFrame): void {
  if (frame.type === "session/event") {
    const session = ensureSession(frame.sessionId);
    if (session.events.has(frame.event.seq)) return;
    session.events.set(frame.event.seq, frame.event);
    if (frame.view) session.views.set(frame.event.seq, frame.view);
    session.summary.updatedAt = Math.max(session.summary.updatedAt, frame.event.time);
    if (frame.event.type === "turn/start") session.summary.blank = false;
    if (frame.event.type === "maid/delivery") {
      // drivers.emit_delivery 写的键是 status/agentId/taskId（不是 deliveryStatus）
      const delivery = frame.event.data ?? {};
      applyRuntimeMetadata(session.summary, {
        deliveryStatus: typeof delivery.status === "string" ? delivery.status : undefined,
        agentId: delivery.agentId,
        taskId: delivery.taskId,
      });
    }
    touchSession(session);
    touchSessions();
    if (state.current === frame.sessionId) emit();
    return;
  }
  if (frame.type === "session/subscribed") {
    ensureSession(frame.sessionId);
    return;
  }
  if (frame.type === "session/queue") {
    const session = ensureSession(frame.sessionId);
    session.queue = frame.items;
    touchSession(session);
    emit();
    return;
  }
  if (frame.type === "session/projection") {
    applyProjections(frame.sessionId, { [frame.key]: frame.value }, frame.seq);
    const summary = state.sessions.get(frame.sessionId);
    if (summary?.projections && frame.key === "title") {
      summary.projections.values.title = frame.value;
    }
    const session = state.byId.get(frame.sessionId);
    if (session) touchSession(session);
    touchSessions();
    emit();
  }
}

export function applyHostFrame(frame: HostFrame): void {
  if (frame.type === "host/session-status") {
    const summary = state.sessions.get(frame.sessionId) ?? {
      sessionId: frame.sessionId,
      updatedAt: Date.now(),
      running: false,
      blank: true,
    };
    state.sessions.set(frame.sessionId, summary);
    summary.running = frame.running;
    if (frame.running) summary.blank = false;
    applyRuntimeMetadata(summary, frame);
    const session = state.byId.get(frame.sessionId);
    if (session) {
      session.summary.running = frame.running;
      applyRuntimeMetadata(session.summary, frame);
      touchSession(session);
    }
    touchSessions();
    emit();
  }
  if (frame.type === "host/session-added") {
    const summary = state.sessions.get(frame.sessionId) ?? {
      sessionId: frame.sessionId,
      updatedAt: Date.now(),
      running: false,
      blank: frame.blank,
      agentPreset: frame.agentPreset,
    };
    summary.blank = frame.blank;
    if (frame.agentPreset !== undefined) summary.agentPreset = frame.agentPreset;
    applyRuntimeMetadata(summary, frame);
    state.sessions.set(frame.sessionId, summary);
    touchSessions();
    emit();
    void refreshSessions();
  }
  if (frame.type === "host/session-removed") {
    state.sessions.delete(frame.sessionId);
    state.byId.delete(frame.sessionId);
    if (state.current === frame.sessionId) state.current = undefined;
    touchSessions();
    emit();
  }
}

export function setConnection(connection: ConnectionState): void {
  state.connection = connection;
  emit();
}

export async function createSession(agentPreset: string): Promise<SessionId> {
  if (!agentPreset.trim()) {
    throw new Error("请先选择 Agent，再创建控制台任务。");
  }
  const { sessionId } = await call<{ sessionId: SessionId }>("session.create", {
    agentPreset,
  });
  await refreshSessions();
  await selectSession(sessionId);
  return sessionId;
}

export const DEFAULT_UMO = "dashboard:FriendMessage:dashboard";

let umoChoice = "";
try {
  umoChoice = localStorage.getItem("maid-umo") ?? "";
} catch {
  /* sandboxed iframe */
}
export function currentUmo(): string {
  return umoChoice || DEFAULT_UMO;
}
export function setUmo(umo: string): void {
  umoChoice = umo.trim();
  try {
    localStorage.setItem("maid-umo", umoChoice);
  } catch {
    /* sandboxed iframe */
  }
  const currentSummary = state.current ? state.sessions.get(state.current) : undefined;
  if (currentSummary && (currentSummary.umo || DEFAULT_UMO) !== currentUmo()) {
    state.current = undefined;
  }
  emit();
}

let presetChoice = "";
export function chosenPreset(): string {
  return presetChoice;
}
export function setPresetChoice(id: string): void {
  presetChoice = id;
  emit();
}

export async function sendPrompt(
  sessionId: SessionId | undefined,
  parts: PromptContentPart[],
  mode: "queue" | "steer" = "queue",
): Promise<void> {
  state.busy = true;
  emit();
  try {
    let target = sessionId;
    if (!target) {
      target = await createSession(chosenPreset());
    }
    await call("session.prompt", { sessionId: target, mode, content: parts });
  } finally {
    state.busy = false;
    emit();
  }
}

export async function cancelTurn(sessionId: SessionId): Promise<void> {
  await call("session.cancel", { sessionId });
}

export async function renameSession(sessionId: SessionId, title: string): Promise<void> {
  await call("session.rename", { sessionId, title });
  await refreshSessions();
}

export async function deleteSession(sessionId: SessionId): Promise<void> {
  await call("session.delete", { sessionId });
  state.sessions.delete(sessionId);
  state.byId.delete(sessionId);
  if (state.current === sessionId) state.current = undefined;
  touchSessions();
  emit();
}

export async function forkSession(sessionId: SessionId): Promise<SessionId> {
  const { sessionId: child } = await call<{ sessionId: SessionId }>("session.fork", { sessionId });
  await refreshSessions();
  await selectSession(child);
  return child;
}

export async function loadAttachmentImage(
  sessionId: SessionId,
  attachmentId: string,
): Promise<string> {
  const { data, attachment } = await call<{ attachment: { mediaType: string }; data: string }>(
    "session.attachment",
    { sessionId, attachmentId },
  );
  return `data:${attachment.mediaType};base64,${data}`;
}

export async function searchSessions(
  query: string,
  umo?: string,
): Promise<{ sessionId: SessionId; snippet?: string }[]> {
  const { items } = await call<{ items: { sessionId: SessionId; snippet?: string }[] }>(
    "session.search",
    { query, umo: umo || currentUmo() },
  );
  return items;
}

export async function refreshPresets(): Promise<void> {
  const { presets } = await call<{ presets: AgentPresetEntry[] }>("agentPreset.list", {});
  state.presets = presets;
  emit();
}

export async function selectPreset(sessionId: SessionId, agentPreset: string): Promise<void> {
  await call("agentPreset.select", { sessionId, agentPreset });
  const session = state.byId.get(sessionId);
  if (session) {
    session.summary.agentPreset = agentPreset;
    touchSession(session);
  }
  touchSessions();
  await refreshSessions();
}

export async function refreshSettings(): Promise<void> {
  const described = await call<{ namespaces: SettingsNamespaceView[] }>("settings.describe", {});
  state.settings = described.namespaces[0] ?? null;
  emit();
}

export async function loadModels(sessionId: SessionId): Promise<void> {
  const models = await call<SessionModels>("session.models", { sessionId });
  const session = ensureSession(sessionId);
  session.models = models;
  touchSession(session);
  emit();
}

export async function selectModel(sessionId: SessionId, provider: string): Promise<void> {
  await call("session.selectModel", { sessionId, provider });
  await loadModels(sessionId);
}

export async function saveSettings(patch: Record<string, unknown>): Promise<void> {
  const updated = await call<SettingsNamespaceView>("settings.update", {
    ns: state.settings?.ns ?? "maid",
    patch,
  });
  state.settings = updated;
  emit();
}

export function toggleTheme(): void {
  setTheme(state.theme === "dark" ? "light" : "dark");
}

export function setTheme(theme: "light" | "dark"): void {
  if (state.theme === theme) return;
  state.theme = theme;
  try {
    localStorage.setItem("maid-theme", theme);
  } catch {
    /* sandboxed iframe */
  }
  applyTheme();
  emit();
}

export function applyTheme(): void {
  if (state.theme === "dark") document.body.setAttribute("data-maid-dark-theme", "");
  else document.body.removeAttribute("data-maid-dark-theme");
}

let controller: ConnectionController | null = null;

export async function boot(): Promise<void> {
  applyTheme();
  controller = new ConnectionController({
    onState: setConnection,
    onMuxFrame: applyMuxFrame,
    onHostFrame: applyHostFrame,
    onReconnected: async () => {
      await refreshSessions().catch(() => undefined);
      if (state.current) await loadHistoryTail(state.current).catch(() => undefined);
    },
  });
  await controller.start().catch(() => undefined);
  await Promise.allSettled([refreshSessions(), refreshPresets(), refreshSettings()]);
  if (state.current === undefined && state.sessions.size > 0) {
    const newest = [...state.sessions.values()].sort((a, b) => b.updatedAt - a.updatedAt)[0];
    if (!newest.blank) await selectSession(newest.sessionId);
    else state.current = undefined;
  }
  state.booting = false;
  emit();
}

export async function shutdown(): Promise<void> {
  await controller?.stop().catch(() => undefined);
  controller = null;
}
