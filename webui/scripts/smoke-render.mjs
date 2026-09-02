// 运行时冒烟测试：在 jsdom 中加载构建产物 console.js，验证 React 应用
// 能完整渲染首屏（hero 阶段），并模拟一次 mock 会话流式回合。
// 用法: node scripts/smoke-render.mjs
import { JSDOM } from "jsdom";
import { setTimeout as delay } from "node:timers/promises";

const dom = new JSDOM(`<!doctype html><html><body><div id="root"></div></body></html>`, {
  url: "http://localhost/",
  pretendToBeVisual: true,
});

const { window } = dom;

// 把 jsdom 的浏览器全局挂到 Node 全局上
const globals = [
  "window", "document", "navigator", "HTMLElement", "HTMLDivElement", "HTMLTextAreaElement",
  "HTMLInputElement", "Element", "Node", "CustomEvent", "Event", "KeyboardEvent", "MouseEvent",
  "PointerEvent", "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame",
  "localStorage", "sessionStorage", "FileReader", "File", "Blob", "MutationObserver",
  "DOMParser", "XMLSerializer", "DocumentFragment", "Text", "Comment", "Range",
  "requestIdleCallback", "cancelIdleCallback", "queueMicrotask", "fetch",
];
for (const key of globals) {
  if (key in window && !(key in globalThis)) {
    globalThis[key] = window[key];
  }
}
globalThis.window = window;
globalThis.document = window.document;
globalThis.localStorage = window.localStorage;
Object.defineProperty(globalThis, "navigator", { value: window.navigator, configurable: true });
globalThis.requestAnimationFrame = window.requestAnimationFrame?.bind(window) ?? ((cb) => setTimeout(cb, 16));
globalThis.cancelAnimationFrame = window.cancelAnimationFrame?.bind(window) ?? clearTimeout;

// jsdom 缺失的 API 打桩
window.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
globalThis.ResizeObserver = window.ResizeObserver;
window.IntersectionObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
globalThis.IntersectionObserver = window.IntersectionObserver;
window.matchMedia ??= () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
window.scrollTo ??= () => {};
Element.prototype.scrollTo ??= () => {};
Element.prototype.setPointerCapture ??= () => {};
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.hasPointerCapture ??= () => false;

// ---- 注入与 dev/mock-bridge.ts 等价的极简 mock 桥（生产构建没有 DEV 分支）----
const sessions = new Map();
const streams = new Map();
let rpcSeq = 0;
let subSeq = 0;

function rpcId() {
  return `rpc_${Date.now().toString(36)}_${++rpcSeq}`;
}

function broadcast(kind, payload) {
  for (const stream of streams.values()) {
    if (!stream.endpoint.endsWith(kind === "mux" ? "events.mux" : "events.host")) continue;
    stream.handlers.onMessage({
      raw: JSON.stringify({ type: "server-request", rpcId: rpcId(), method: payload.type, payload }),
    });
  }
}

function newSession(umo = "dashboard:FriendMessage:dashboard") {
  const sessionId = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  const s = { sessionId, updatedAt: Date.now(), running: false, blank: true, events: [], umo, title: null };
  sessions.set(sessionId, s);
  return s;
}

function append(session, type, data) {
  const event = { type, seq: session.events.length, time: Date.now(), data };
  session.events.push(event);
  broadcast("mux", { type: "session/event", sessionId: session.sessionId, event });
  return event;
}

function projections(session) {
  return {
    asOfSeq: session.events.length - 1,
    values: {
      title: session.title,
      sessionStats: { turns: 1, steps: 1, llmMs: 1200, toolMs: 300, ttftMs: 200 },
      tokenUsage: { uncachedInputTokens: 120, outputTokens: 42, cacheReadTokens: 0 },
    },
  };
}

function summary(s) {
  return {
    sessionId: s.sessionId, updatedAt: s.updatedAt, running: s.running, blank: s.blank,
    agentPreset: "butler", umo: s.umo, projections: projections(s),
  };
}

async function respond(method, payload) {
  switch (method) {
    case "session.list":
      return { items: [...sessions.values()].map(summary) };
    case "session.create": {
      const s = newSession(String(payload.umo ?? ""));
      broadcast("host", { type: "host/session-added", sessionId: s.sessionId });
      return { sessionId: s.sessionId, agentPreset: "butler" };
    }
    case "session.history": {
      const s = sessions.get(payload.sessionId);
      return { events: (s?.events ?? []).map((event) => ({ event })), hasMore: false, projections: projections(s) };
    }
    case "session.prompt": {
      const s = sessions.get(payload.sessionId);
      void runMockTurn(s);
      return { accepted: true };
    }
    case "session.cancel": return { accepted: true };
    case "session.models": return { current: { provider: "mock", model: "mock-1", override: false }, providers: [] };
    case "agentPreset.list":
      return { presets: [{ id: "butler", trust: "system", isDefault: true, name: "butler" }], authorable: false };
    case "settings.describe":
      return { namespaces: [{ ns: "maid", schema: { type: "object", properties: {} }, value: {}, applies: "live", secrets: [], revision: 1 }] };
    default:
      return {};
  }
}

async function runMockTurn(session) {
  if (!session) return;
  session.running = true;
  session.blank = false;
  broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: true });
  append(session, "turn/start", { turn: 1 });
  append(session, "user/message", {
    id: "u1", role: "user",
    content: [{ type: "text", text: "你好，女仆" }],
    source: { kind: "user" },
  });
  append(session, "step/start", { turn: 1, step: 1 });
  const answer = "收到，**正在处理**。\n\n- 项目一\n- 项目二\n\n```python\nprint('ok')\n```";
  for (const char of answer) {
    append(session, "assistant/chunk", { turn: 1, step: 1, chunk: { type: "text-delta", index: 0, text: char } });
    await delay(4);
  }
  append(session, "assistant/message", {
    turn: 1, step: 1,
    message: { id: "a1", role: "assistant", content: [{ type: "text", text: answer }], source: { kind: "model" } },
    usage: { inputTokens: 120, outputTokens: answer.length },
  });
  append(session, "step/end", { turn: 1, step: 1 });
  // 工具调用一轮（真实后端形状：结果正文嵌在 tool-result 块内层）
  append(session, "step/start", { turn: 1, step: 2 });
  append(session, "tool/call", {
    turn: 1, step: 2, callId: "call_1", name: "web_search",
    arguments: JSON.stringify({ query: "今天天气" }),
  });
  append(session, "tool/result", {
    turn: 1, step: 2,
    message: {
      id: "t1", role: "user",
      content: [{
        type: "tool-result", toolCallId: "call_1", isError: false,
        content: [{ type: "text", text: "晴，26 度，适合出门。" }],
      }],
      source: { kind: "tool", callId: "call_1" },
    },
  });
  append(session, "step/end", { turn: 1, step: 2 });
  append(session, "turn/end", { turn: 1, reason: { kind: "completed" } });
  session.running = false;
  broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: false });
}

window.AstrBotPluginPage = {
  async ready() { return { pluginName: "smoke" }; },
  async apiGet() { return null; },
  async apiPost(endpoint, body) {
    if (endpoint.startsWith("api/")) {
      const envelope = body ?? {};
      try {
        const value = await respond(envelope.method, envelope.payload ?? {});
        return { type: "server-response", rpcId: envelope.rpcId, result: { ok: true, value } };
      } catch (error) {
        return { type: "server-response", rpcId: envelope.rpcId, result: { ok: false, error: { code: "internal", message: String(error) } } };
      }
    }
    return null;
  },
  async upload() { return {}; },
  async download() { return {}; },
  async subscribeSSE(endpoint, handlers) {
    const id = `smoke_${++subSeq}`;
    streams.set(id, { endpoint, handlers });
    handlers.onOpen?.();
    return id;
  },
  async unsubscribeSSE(id) { streams.delete(id); },
};

// ---- 加载构建产物 ----
const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.error ?? e.message)));
process.on("unhandledRejection", (e) => errors.push(`unhandledRejection: ${e?.stack ?? e}`));

await import("../../pages/console/assets/console.js");
await delay(800);

const rootText = document.getElementById("root").textContent;
console.log("== 首屏（hero）文本 ==");
console.log(rootText.slice(0, 200));
const heroOk = rootText.includes("派一个新任务");
console.log(heroOk ? "PASS hero 渲染" : "FAIL hero 未渲染");

// 模拟发送一条消息，走一遍流式回合
const textarea = document.querySelector("textarea");
if (!textarea) {
  console.log("FAIL 找不到输入框");
  process.exit(1);
}
const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
setter.call(textarea, "你好，女仆");
textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
textarea.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

await delay(2500);

const afterText = document.getElementById("root").textContent;
const checks = [
  ["用户消息上屏", afterText.includes("你好，女仆")],
  ["流式回复渲染", afterText.includes("正在处理")],
  // DSH 移植后 completed 轮渲染为过程折叠条（信息由用量 pill 承载）
  ["回合过程折叠条", afterText.includes("已思考")],
  ["工具行标题与摘要", afterText.includes("搜索") && afterText.includes("今天天气")],
  ["助手尾部操作行", afterText.includes("复制") || document.querySelector(".assistant-actions") !== null],
  ["用量 pill", afterText.includes("用量")],
];
for (const [label, ok] of checks) console.log(`${ok ? "PASS" : "FAIL"} ${label}`);

// 展开工具行验证输出卡（默认收起，io-card 不在 DOM）
let outputOk = false;
const toolRowBtn = document.querySelector(".tool-row .disclosure-row");
if (toolRowBtn) {
  toolRowBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await delay(300);
  outputOk = document.getElementById("root").textContent.includes("晴，26 度，适合出门。");
}
console.log(`${outputOk ? "PASS" : "FAIL"} 工具输出正文`);

// Node/jsdom 中懒加载 chunk 的 URL 解析依赖浏览器环境（fetch http://localhost/...），
// 属于测试环境限制而非应用缺陷——真实浏览器中这些 chunk 由 vite preview/插件页正常服务
// （已由 scripts/validate-chunks.mjs 全量核对）。此处单独归类为 envSkip。
const envSkip = errors.filter((e) => /fetch failed/.test(e));
const runtimeErrors = errors.filter((e) => !/fetch failed/.test(e) && !/favicon|resize|loop/i.test(e));
if (envSkip.length) {
  console.log(`SKIP ${envSkip.length} 个 chunk 动态加载（Node 环境限制，浏览器中正常）`);
}
if (runtimeErrors.length) {
  console.log("FAIL 运行时错误:");
  for (const e of runtimeErrors.slice(0, 5)) console.log("  " + e);
} else {
  console.log("PASS 无未捕获运行时错误");
}

const allOk = heroOk && checks.every(([, ok]) => ok) && outputOk && runtimeErrors.length === 0;
console.log(allOk ? "\nSMOKE OK" : "\nSMOKE FAILED");
process.exit(allOk ? 0 : 1);
