/**
 * 应用状态：会话列表 + 每会话（事件/视图/投影/队列镜像）+ 连接态。
 *
 * 外部 store + useSyncExternalStore，
 * 投影 higher-seq-wins，事件按 seq 有序落位。
 */

import { call, ConnectionController, type ConnectionState } from "@/api/client";
import type {
  AgentPresetEntry,
  HistoryEntry,
  HostFrame,
  Message,
  MuxFrame,
  PromptContentPart,
  QueuedInboxItem,
  SessionId,
  SessionSummary,
  SettingsNamespaceView,
  ToolEventView,
} from "@/types";

export interface SessionState {
  sessionId: SessionId;
  summary: SessionSummary;
  events: Map<number, any>; // seq -> SessionEvent
  views: Map<number, ToolEventView>; // seq -> view
  projections: Record<string, unknown>;
  projectionsSeq: number;
  queue: QueuedInboxItem[];
  historyLoaded: boolean;
  hasMore: boolean;
  models: SessionModels | null; // session.models RPC（composer 模型芯片）
}

export interface SessionModels {
  current: { provider: string; model: string; override: boolean };
  providers: { id: string; model: string; type: string }[];
}

export interface AppState {
  connection: ConnectionState;
  booting: boolean;
  sessions: Map<SessionId, SessionSummary>;
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

function emit(): void {
  version += 1;
  for (const listener of listeners) listener();
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

/* ---------------------------------------------------------------- 会话 */

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
    if (fresh) session.summary = fresh;
    else state.byId.delete(sid);
  }
  if (state.current !== undefined && !state.sessions.has(state.current)) {
    state.current = undefined;
  }
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
  emit();
}

function applyProjections(sessionId: SessionId, values: Record<string, unknown>, seq: number): void {
  const session = ensureSession(sessionId);
  if (seq < session.projectionsSeq) return; // higher-seq-wins
  session.projectionsSeq = seq;
  session.projections = { ...session.projections, ...values };
}

/* ---------------------------------------------------------------- 帧 */

export function applyMuxFrame(frame: MuxFrame): void {
  if (frame.type === "session/event") {
    const session = ensureSession(frame.sessionId);
    if (session.events.has(frame.event.seq)) return; // 重连重放去重
    session.events.set(frame.event.seq, frame.event);
    if (frame.view) session.views.set(frame.event.seq, frame.view);
    session.summary.updatedAt = Math.max(session.summary.updatedAt, frame.event.time);
    if (frame.event.type === "turn/start") session.summary.blank = false;
    if (state.current === frame.sessionId) emit();
    return;
  }
  if (frame.type === "session/subscribed") {
    const session = ensureSession(frame.sessionId);
    // truncate到 lastSeq：重连后的权威基线
    for (const seq of [...session.events.keys()]) {
      if (seq > frame.lastSeq) session.events.delete(seq);
    }
    return;
  }
  if (frame.type === "session/queue") {
    const session = ensureSession(frame.sessionId);
    session.queue = frame.items;
    emit();
    return;
  }
  if (frame.type === "session/projection") {
    applyProjections(frame.sessionId, { [frame.key]: frame.value }, frame.seq);
    const summary = state.sessions.get(frame.sessionId);
    if (summary?.projections && frame.key === "title") {
      summary.projections.values.title = frame.value;
    }
    emit();
  }
}

export function applyHostFrame(frame: HostFrame): void {
  if (frame.type === "host/session-status") {
    const summary = state.sessions.get(frame.sessionId);
    if (summary) {
      summary.running = frame.running;
      if (frame.running) summary.blank = false;
    }
    const session = state.byId.get(frame.sessionId);
    if (session) session.summary.running = frame.running;
    emit();
  }
  if (frame.type === "host/session-added") {
    void refreshSessions();
  }
  if (frame.type === "host/session-removed") {
    state.sessions.delete(frame.sessionId);
    state.byId.delete(frame.sessionId);
    if (state.current === frame.sessionId) state.current = undefined;
    emit();
  }
}

export function setConnection(connection: ConnectionState): void {
  state.connection = connection;
  emit();
}

/* ---------------------------------------------------------------- 动作 */

export async function createSession(agentPreset?: string): Promise<SessionId> {
  const { sessionId } = await call<{ sessionId: SessionId }>("session.create", {
    agentPreset: agentPreset || chosenPreset() || undefined,
    umo: currentUmo(),
  });
  await refreshSessions();
  await selectSession(sessionId);
  return sessionId;
}

const LEGACY_WEBID_UMO = "dashboard:WebId:dashboard"; // 2.0 初版的非法默认值（MessageType 无 WebId）
export const DEFAULT_UMO = "dashboard:FriendMessage:dashboard";

let umoChoice = "";
try {
  umoChoice = localStorage.getItem("maid-umo") ?? "";
  if (umoChoice === LEGACY_WEBID_UMO) umoChoice = "";
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
  // 切换来源后，不属于该来源的当前会话一并收起（发送即落到新来源的新会话）
  const currentSummary = state.current ? state.sessions.get(state.current) : undefined;
  if (currentSummary && (currentSummary.umo || DEFAULT_UMO) !== currentUmo()) {
    state.current = undefined;
  }
  emit();
}

/** 未建会话时的预选 preset（hero 芯片选择）；建会话后走 agentPreset.select。 */
let presetChoice = "";
export function chosenPreset(): string {
  return presetChoice || state.presets.find((p) => p.isDefault)?.id || "";
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
      target = await createSession(state.presets.find((p) => p.isDefault)?.id);
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

/** 远端内容搜索（session.search）：标题匹配在组件渲染期本地合并。 */
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
  if (session) session.summary.agentPreset = agentPreset;
  await refreshSessions();
}

export async function refreshSettings(): Promise<void> {
  const described = await call<{ namespaces: SettingsNamespaceView[] }>("settings.describe", {});
  state.settings = described.namespaces[0] ?? null;
  emit();
}

/* ---------------------------------------------------------------- 模型 */

export async function loadModels(sessionId: SessionId): Promise<void> {
  const models = await call<SessionModels>("session.models", { sessionId });
  const session = ensureSession(sessionId);
  session.models = models;
  emit();
}

/** provider 传空串 = 清除会话级覆盖（跟随 subagent 配置 / umo 当前 provider）。 */
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
  // 暗色样式按属性存在性匹配 body[data-maid-dark-theme]：亮色必须移除属性
  if (state.theme === "dark") document.body.setAttribute("data-maid-dark-theme", "");
  else document.body.removeAttribute("data-maid-dark-theme");
}

/* ---------------------------------------------------------------- 启动 */

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
