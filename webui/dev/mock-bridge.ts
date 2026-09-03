
type Handlers = {
  onOpen?: () => void;
  onMessage: (event: { raw: string }) => void;
  onError?: () => void;
};

interface MockBridge {
  ready(): Promise<unknown>;
  apiGet(endpoint: string, params?: unknown): Promise<any>;
  apiPost(endpoint: string, body?: unknown): Promise<any>;
  upload(endpoint: string, file: File): Promise<any>;
  download(endpoint: string, params?: unknown, filename?: string): Promise<any>;
  subscribeSSE(endpoint: string, handlers: Handlers, params?: unknown): Promise<string>;
  unsubscribeSSE(subscriptionId: string): Promise<void>;
}

let rpcSeq = 0;

function rpcId(): string {
  return `rpc_${Date.now().toString(36)}_${++rpcSeq}`;
}

interface MockSession {
  sessionId: string;
  updatedAt: number;
  running: boolean;
  blank: boolean;
  agentPreset: string;
  title: string | null;
  events: any[];
  turns: number;
  steps: number;
  usage: { inputTokens: number; outputTokens: number };
  umo: string;
}

const sessions = new Map<string, MockSession>();

function newSession(agentPreset = "butler", umo = "dashboard:WebId:dashboard"): MockSession {
  const sessionId = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  const session: MockSession = {
    sessionId,
    updatedAt: Date.now(),
    running: false,
    blank: true,
    agentPreset,
    title: null,
    events: [],
    turns: 0,
    steps: 0,
    usage: { inputTokens: 0, outputTokens: 0 },
    umo,
  };
  sessions.set(sessionId, session);
  return session;
}

function append(session: MockSession, type: string, data: any): any {
  const event = { type, seq: session.events.length, time: Date.now(), data };
  session.events.push(event);
  broadcast("mux", { type: "session/event", sessionId: session.sessionId, event });
  return event;
}

function projectionsBlock(session: MockSession) {
  return {
    asOfSeq: session.events.length - 1,
    values: {
      title: session.title,
      sessionStats: { turns: session.turns, steps: session.steps, llmMs: 1200, toolMs: 800, ttftMs: 240, ttftSteps: 1, decodeMs: 1200, decodeTokens: 0 },
      tokenUsage: { uncachedInputTokens: session.usage.inputTokens, outputTokens: session.usage.outputTokens, cacheReadTokens: 0, cacheWriteTokens: 0 },
    },
  };
}

function summary(session: MockSession) {
  return {
    sessionId: session.sessionId,
    updatedAt: session.updatedAt,
    running: session.running,
    blank: session.blank,
    agentPreset: session.agentPreset,
    umo: session.umo,
    projections: projectionsBlock(session),
  };
}

async function respond(method: string, payload: any): Promise<any> {
  switch (method) {
    case "session.list":
      return { items: [...sessions.values()].sort((a, b) => b.updatedAt - a.updatedAt).map(summary) };
    case "session.create": {
      const session = newSession(payload.agentPreset, String(payload.umo ?? "") || "dashboard:WebId:dashboard");
      broadcast("host", { type: "host/session-added", sessionId: session.sessionId, blank: true, agentPreset: session.agentPreset });
      return { sessionId: session.sessionId, agentPreset: session.agentPreset };
    }
    case "session.history": {
      const session = sessions.get(payload.sessionId);
      if (!session) throw Object.assign(new Error("会话不存在"), { code: "session-not-found" });
      return {
        events: session.events.map((event) => ({ event })),
        hasMore: false,
        projections: projectionsBlock(session),
      };
    }
    case "session.prompt": {
      const session = sessions.get(payload.sessionId) ?? newSession();
      void runMockTurn(session, payload.content ?? []);
      return { accepted: true };
    }
    case "session.cancel": {
      const session = sessions.get(payload.sessionId);
      if (session) session.running = false;
      return { accepted: true };
    }
    case "session.rename": {
      const session = sessions.get(payload.sessionId);
      if (session) {
        session.title = payload.title;
        append(session, "session/title", { title: payload.title, source: { kind: "user" } });
      }
      return { title: payload.title, seq: 0 };
    }
    case "session.fork": {
      const source = sessions.get(payload.sessionId) ?? newSession();
      const child = newSession(source.agentPreset, source.umo);
      child.events = source.events.map((e, i) => ({ ...e, seq: i }));
      child.blank = source.blank;
      child.title = source.title;
      child.turns = source.turns;
      child.steps = source.steps;
      return { sessionId: child.sessionId };
    }
    case "session.delete": {
      const session = sessions.get(payload.sessionId);
      if (!session) throw Object.assign(new Error("会话不存在"), { code: "session-not-found" });
      if (session.running) throw Object.assign(new Error("运行中的会话不能删除，请先停止任务。"), { code: "session-running" });
      sessions.delete(payload.sessionId);
      broadcast("host", { type: "host/session-removed", sessionId: payload.sessionId });
      return { deleted: true };
    }
    case "session.models":
      return {
        current: { provider: "mock", model: "mock-1", override: false },
        providers: [],
      };
    case "session.search": {
      const query = String(payload.query ?? "").trim().toLowerCase();
      if (!query) return { items: [], hasMore: false };
      const items = [];
      for (const session of sessions.values()) {
        const text = session.events
          .flatMap((e) => (e.data?.message?.content ?? e.data?.content ?? []))
          .filter((b: any) => b?.type === "text")
          .map((b: any) => String(b.text ?? ""))
          .join("\n");
        const idx = text.toLowerCase().indexOf(query);
        if (idx < 0) continue;
        const start = Math.max(0, idx - 30);
        items.push({ sessionId: session.sessionId, snippet: text.slice(start, idx + query.length + 50) });
        if (items.length >= 20) break;
      }
      return { items, hasMore: false };
    }
    case "session.selectModel":
      return { selected: { provider: "mock", model: payload.model ?? "mock-1" } };
    case "session.attachment":
      return { attachment: { attachmentId: payload.attachmentId, mediaType: "image/png", byteLength: 0 }, data: "" };
    case "session.updateQueue":
      return { accepted: true };
    case "agentPreset.list":
      return {
        presets: [
          { id: "butler", trust: "system", isDefault: true, name: "butler" },
          { id: "muiceagent", trust: "system", isDefault: false, name: "muiceagent" },
        ],
        authorable: false,
        hasDocument: false,
      };
    case "agentPreset.select":
      return { agentPreset: payload.agentPreset };
    case "settings.describe":
      return {
        writable: true,
        hasDocument: false,
        namespaces: [
          {
            ns: "maid",
            schema: { type: "object", properties: {} },
            value: {
              default_agent_name: "butler",
              allowed_agent_names: [],
              hide_native_tools: true,
              hide_transfer_tools: true,
              include_raw_user_input: false,
              log_raw_llm_io: false,
              dispatch_prompt_template: "",
              foreground_timeout_seconds: 50,
              memory_agent_names: [],
              max_active_per_umo: 5,
              max_active_global: 20,
              retention_days: 30,
            },
            applies: "live",
            secrets: [],
            revision: 1,
          },
        ],
      };
    case "settings.update":
      return { ok: true };
    case "host.describe":
      return { version: "mock", cwd: "/mock", attachedSessions: 0, canOpenPath: false };
    default:
      throw Object.assign(new Error(`未知方法: ${method}`), { code: "bad-request" });
  }
}

async function runMockTurn(session: MockSession, content: any[]) {
  const text = content.filter((p) => p.type === "text").map((p) => p.text).join(" ");
  session.running = true;
  session.blank = false;
  session.updatedAt = Date.now();
  broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: true });
  const turn = ++session.turns;
  append(session, "turn/start", { turn });
  append(session, "user/message", {
    id: `u${turn}`, role: "user",
    content: [...content.filter((p) => p.type === "text").map((p) => ({ type: "text", text: p.text }))],
    source: { kind: "user" },
  });
  const step = ++session.steps;
  append(session, "step/start", { turn, step });

  const reasoning = "用户要我做点事。先看看有没有可用工具，再决定步骤。";
  const answer = `收到任务：「${text.slice(0, 40)}」。\n\n这是 **mock 回合** 的流式回复，用于本地开发验证打字机效果、ReasoningRow 与统计行。\n\n- 列表项一\n- 列表项二\n\n\`\`\`python\nprint("hello maid")\n\`\`\``;

  for (const char of reasoning) {
    append(session, "assistant/chunk", { turn, step, chunk: { type: "reasoning-delta", index: 0, text: char } });
    await sleep(18);
  }
  for (const char of answer) {
    append(session, "assistant/chunk", { turn, step, chunk: { type: "text-delta", index: 1, text: char } });
    await sleep(12);
  }

  const callId = `call_${turn}`;
  append(session, "tool/call", {
    turn, step, callId, name: "bash",
    arguments: JSON.stringify({ command: "echo mock" }),
  });
  await sleep(500);
  append(session, "tool/result", {
    turn, step,
    message: { id: `tr${turn}`, role: "user", content: [{ type: "tool-result", toolCallId: callId, content: [{ type: "text", text: "mock output" }] }], source: { kind: "tool", callId } },
  });

  session.usage.inputTokens += 120;
  session.usage.outputTokens += answer.length;
  append(session, "assistant/message", {
    turn, step,
    message: {
      id: `a${turn}`, role: "assistant",
      content: [
        { type: "reasoning", text: reasoning },
        { type: "text", text: answer },
      ],
      source: { kind: "model", provider: "mock", model: "mock-1" },
    },
    usage: { inputTokens: 120, outputTokens: answer.length },
  });
  append(session, "step/end", { turn, step });
  append(session, "turn/end", { turn, reason: { kind: "completed" } });
  session.title = text.slice(0, 12) || "新任务";
  append(session, "session/title", { title: session.title, source: { kind: "auto" } });

  const values = projectionsBlock(session).values;
  const seq = session.events.length - 1;
  for (const key of ["title", "sessionStats", "tokenUsage"] as const) {
    broadcast("mux", { type: "session/projection", sessionId: session.sessionId, key, value: values[key], seq });
  }

  session.running = false;
  session.updatedAt = Date.now();
  broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: false });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const streams = new Map<string, { endpoint: string; handlers: Handlers }>();
let subSeq = 0;

function broadcast(kind: "mux" | "host", payload: any): void {
  const method = payload.type;
  for (const stream of streams.values()) {
    if (!stream.endpoint.endsWith(kind === "mux" ? "events.mux" : "events.host")) continue;
    stream.handlers.onMessage({
      raw: JSON.stringify({ type: "server-request", rpcId: rpcId(), method, payload }),
    });
  }
}

const bridge: MockBridge = {
  async ready() {
    return { pluginName: "mock" };
  },
  async apiGet() {
    return null;
  },
  async apiPost(endpoint: string, body: any) {
    await sleep(120);
    if (endpoint.startsWith("api/")) {
      const method = endpoint.slice(4);
      const envelope = body ?? {};
      if (envelope.type !== "client-request") {
        return { type: "server-response", rpcId: "", result: { ok: false, error: { code: "bad-request", message: "bad envelope", details: {} } } };
      }
      try {
        const value = await respond(envelope.method, envelope.payload ?? {});
        return { type: "server-response", rpcId: envelope.rpcId, result: { ok: true, value } };
      } catch (error: any) {
        return {
          type: "server-response",
          rpcId: envelope.rpcId,
          result: { ok: false, error: { code: error?.code ?? "internal", message: error?.message ?? String(error), details: {} } },
        };
      }
    }
    return null;
  },
  async upload() {
    return { path: "mock", name: "mock", mime_type: "image/png", size: 0 };
  },
  async download() {
    return { filename: "mock" };
  },
  async subscribeSSE(endpoint: string, handlers: Handlers) {
    const id = `mock_sse_${++subSeq}`;
    streams.set(id, { endpoint, handlers });
    handlers.onOpen?.();
    if (endpoint.endsWith("events.mux")) {
      for (const session of sessions.values()) {
        handlers.onMessage({
          raw: JSON.stringify({
            type: "server-request", rpcId: rpcId(), method: "session/subscribed",
            payload: { type: "session/subscribed", sessionId: session.sessionId, lastSeq: session.events.length - 1 },
          }),
        });
      }
    }
    return id;
  },
  async unsubscribeSSE(subscriptionId: string) {
    streams.delete(subscriptionId);
  },
};

(window as any).AstrBotPluginPage = bridge;

export {}
