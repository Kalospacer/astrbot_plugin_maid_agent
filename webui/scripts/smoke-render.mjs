// 运行时冒烟测试：在 jsdom 中加载构建产物 console.js，验证 React 应用
// 能完整渲染首屏（hero 阶段），并模拟一次 mock 会话流式回合。
// 用法: node scripts/smoke-render.mjs
import { JSDOM } from "jsdom";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
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

function newSession(agentPreset) {
  const sessionId = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  const umo = "dashboard:FriendMessage:dashboard";
  const sourceKind = "dashboard";
  const s = {
    sessionId, updatedAt: Date.now(), running: false, blank: true, events: [], umo, title: null,
    sourceKind,
    agentId: `agent_${sessionId.slice(0, 8)}`,
    taskId: `task_${sessionId.slice(0, 8)}`,
    deliveryStatus: "pending",
  };
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
    sourceKind: s.sourceKind, agentId: s.agentId, taskId: s.taskId,
    deliveryStatus: s.deliveryStatus,
  };
}

async function respond(method, payload) {
  switch (method) {
    case "session.list":
      return { items: [...sessions.values()].map(summary) };
    case "session.create": {
      if (typeof payload.agentPreset !== "string" || !payload.agentPreset.trim()) throw new Error("控制台任务必须显式选择 Agent。");
      const s = newSession(payload.agentPreset);
      broadcast("host", { type: "host/session-added", sessionId: s.sessionId, blank: true, agentPreset: payload.agentPreset, sourceKind: s.sourceKind });
      return { sessionId: s.sessionId, agentPreset: payload.agentPreset };
    }
    case "session.history": {
      const s = sessions.get(payload.sessionId);
      return { events: (s?.events ?? []).map((event) => ({ event })), hasMore: false, projections: projections(s) };
    }
    case "session.prompt": {
      const s = sessions.get(payload.sessionId);
      // 回显用户真实输入，否则多轮断言会被同一句话糊弄过去
      const text = (payload.content ?? [])
        .filter((part) => part?.type === "text")
        .map((part) => part.text)
        .join("\n");
      void runMockTurn(s, text || "你好，女仆", !text.startsWith("纯文本"));
      return { accepted: true };
    }
    case "session.cancel": return { accepted: true };
    case "session.models": return { current: { provider: "mock", model: "mock-1", override: false }, providers: [] };
    case "agentPreset.list":
      return { presets: [{ id: "butler", trust: "system", name: "butler" }], authorable: false };
    case "settings.describe":
      return {
        namespaces: [{
          ns: "maid",
          schema: { type: "object", properties: {
            dispatch_session_mode: { description: "聊天任务执行环境", hint: "控制台任务始终使用隔离 sandbox。", type: "string", options: ["foreground", "background"] },
          } },
          value: { dispatch_session_mode: "background" }, applies: "live", secrets: [], revision: 1,
        }],
      };
    default:
      return {};
  }
}

async function runMockTurn(session, prompt = "你好，女仆", withTool = true) {
  if (!session) return;
  const turn = (session.turn ?? 0) + 1;
  session.turn = turn;
  session.running = true;
  session.blank = false;
  broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: true });
  append(session, "turn/start", { turn });
  append(session, "user/message", {
    id: `u${turn}`, role: "user",
    content: [{ type: "text", text: prompt }],
    source: { kind: "user" },
  });
  append(session, "step/start", { turn, step: 1 });
  const answer = "收到，**正在处理**。\n\n- 项目一\n- 项目二\n\n```python\nprint('ok')\n```";
  for (const char of answer) {
    append(session, "assistant/chunk", { turn, step: 1, chunk: { type: "text-delta", index: 0, text: char } });
    await delay(4);
  }
  append(session, "assistant/message", {
    turn, step: 1,
    message: { id: `a${turn}`, role: "assistant", content: [{ type: "text", text: answer }], source: { kind: "model" } },
    usage: { inputTokens: 120, outputTokens: answer.length, cacheReadTokens: 2048 },
  });
  append(session, "step/end", { turn, step: 1 });
  // 纯文本轮：正文定稿后不再有任何入列，节点数自 partial 建立起就没变过——
  // 这正是「只按节点数当版本号」会让导航轨预览停在空正文的场景。
  if (!withTool) {
    append(session, "turn/end", { turn, reason: { kind: "completed" } });
    session.running = false;
    broadcast("host", { type: "host/session-status", sessionId: session.sessionId, running: false });
    return;
  }
  // 工具调用一轮（真实后端形状：结果正文嵌在 tool-result 块内层）
  append(session, "step/start", { turn, step: 2 });
  append(session, "tool/call", {
    turn, step: 2, callId: `call_${turn}`, name: "web_search",
    arguments: JSON.stringify({ query: "今天天气" }),
  });
  // 工具执行耗时：真实工具不会瞬时返回，这段时间正是「正在工作」状态行可见的窗口
  await delay(400);
  append(session, "tool/result", {
    turn, step: 2,
    message: {
      id: `t${turn}`, role: "user",
      content: [{
        type: "tool-result", toolCallId: `call_${turn}`, isError: false,
        content: [{ type: "text", text: "晴，26 度，适合出门。" }],
      }],
      source: { kind: "tool", callId: `call_${turn}` },
    },
  });
  session.deliveryStatus = "sent";
  append(session, "maid/delivery", {
    turn, status: session.deliveryStatus, agentId: session.agentId, taskId: session.taskId,
  });
  append(session, "step/end", { turn, step: 2 });
  append(session, "turn/end", { turn, reason: { kind: "completed" } });
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

const buildDir = resolve(process.env.MAID_CONSOLE_BUILD_DIR ?? "../pages/console");
await import(pathToFileURL(resolve(buildDir, "assets/console.js")).href);
await delay(800);

const rootText = document.getElementById("root").textContent;
console.log("== 首屏（hero）文本 ==");
console.log(rootText.slice(0, 200));
const heroOk = rootText.includes("派一个新任务");
console.log(heroOk ? "PASS hero 渲染" : "FAIL hero 未渲染");

// 配置对象直接以 key -> metadata 形式下发：字符串 options 必须成为原生 select，hint 必须可见。
const settingsButton = document.querySelector('button[aria-label="设置"]');
settingsButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(150);
const settingsSelect = document.querySelector("select#setting-input-dispatch_session_mode");
const settingsOk =
  settingsSelect?.getAttribute("aria-describedby") === "setting-hint-dispatch_session_mode" &&
  settingsSelect.querySelectorAll("option").length === 2 &&
  document.getElementById("root").textContent.includes("控制台任务始终使用隔离 sandbox。");
console.log(`${settingsOk ? "PASS" : "FAIL"} 配置选项与提示`);
window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
await delay(50);

// Console tasks must be explicitly assigned to an Agent before sending.
const presetButton = document.querySelector('button[aria-label="选择代理预设"]');
presetButton?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(50);
const butlerPreset = [...document.querySelectorAll('button[role="menuitem"]')]
  .find((button) => button.textContent?.includes("butler"));
butlerPreset?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(50);

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

// 折叠条行为：完成后过程行默认收起；点击折叠条在原位展开；再点工具行可见输出卡
const toolRow = document.querySelector(".tool-row");
const collapsedOk = toolRow !== null && toolRow.hasAttribute("hidden");
console.log(`${collapsedOk ? "PASS" : "FAIL"} 完成后过程行收起`);

let expandOk = false;
let outputOk = false;
const barBtn = document.querySelector(".turn-process");
if (barBtn && toolRow) {
  barBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await delay(200);
  expandOk = !toolRow.hasAttribute("hidden");
  const toolRowBtn = toolRow.querySelector(".disclosure-row");
  if (toolRowBtn) {
    toolRowBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await delay(300);
    outputOk = document.getElementById("root").textContent.includes("晴，26 度，适合出门。");
  }
}
console.log(`${expandOk ? "PASS" : "FAIL"} 折叠条展开过程行`);
console.log(`${outputOk ? "PASS" : "FAIL"} 工具输出正文`);

// 轮次导航轨回归：folder 是原地增长 nodes 的，引用恒定。TurnRail 若只按 nodes
// 引用做 memo 比较就会永久 bail out——首次挂载不足 2 轮返回 null，之后再也不出现。
// 跑第二轮后梯子必须出现，且刻度数跟上轮次数。
setter.call(textarea, "第二个问题");
textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
textarea.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

// 轮次运行期间采样工作状态行：应带计时，并在拿到首个 usage 后带上下文占用
let statusSample = "";
for (let i = 0; i < 60; i++) {
  await delay(50);
  const el = document.querySelector(".turn-status");
  if (el) statusSample = el.textContent ?? "";
  if (/\d+秒/.test(statusSample) && /K|\d{3}/.test(statusSample)) break;
}
const statusClockOk = /正在工作/.test(statusSample) && /\d+秒/.test(statusSample);
console.log(`${statusClockOk ? "PASS" : "FAIL"} 工作中显示计时（"${statusSample}"）`);
const statusCtxOk = /\d/.test(statusSample.replace(/\d+秒/, ""));
console.log(`${statusCtxOk ? "PASS" : "FAIL"} 工作中显示上下文占用`);

await delay(2500);
const railMarks = document.querySelectorAll(".turn-rail-mark");
const railOk = railMarks.length >= 2;
console.log(`${railOk ? "PASS" : "FAIL"} 轮次导航轨随新轮次更新（${railMarks.length} 个刻度）`);

const secondTurnOk = document.getElementById("root").textContent.includes("第二个问题");
console.log(`${secondTurnOk ? "PASS" : "FAIL"} 第二轮用户消息上屏`);

// 导航轨预览的回复正文。跑一轮纯文本（无工具）：正文定稿走等长替换，
// turn/end 也不入列，节点数自 partial 建立起就没再变过。若只按节点数当版本号，
// 这一轮的预览会永远停在空正文——带工具的轮次会因随后入列的工具行而被掩盖。
setter.call(textarea, "纯文本轮，不要用工具");
textarea.dispatchEvent(new window.Event("input", { bubbles: true }));
textarea.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
await delay(2000);

let railPreviewOk = false;
const marks = [...document.querySelectorAll(".turn-rail-mark")];
const lastMark = marks[marks.length - 1];
if (lastMark) {
  lastMark.focus();
  lastMark.dispatchEvent(new window.FocusEvent("focus", { bubbles: false }));
  await delay(200);
  const preview = document.querySelector(".turn-rail-preview")?.textContent ?? "";
  railPreviewOk = preview.includes("收到");
  console.log(`${railPreviewOk ? "PASS" : "FAIL"} 导航轨预览含回复正文（"${preview.slice(0, 40)}"）`);
} else {
  console.log("FAIL 导航轨预览：找不到刻度");
}

// 用时 pill：turn/end 时把 runMs/TTFT/TPS 回填到本轮最后一条助手消息
const timePill = [...document.querySelectorAll(".stat-pill")]
  .find((b) => (b.textContent ?? "").includes("用时"));
const timePillOk = timePill !== undefined;
console.log(`${timePillOk ? "PASS" : "FAIL"} 本轮用时 pill（"${timePill?.textContent ?? "缺失"}"）`);

let timeDialogOk = false;
if (timePill) {
  timePill.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await delay(200);
  const dlg = document.querySelector('[role="dialog"][aria-label="本轮用时和速度"]');
  const txt = dlg?.textContent ?? "";
  timeDialogOk = txt.includes("本轮总用时") && (txt.includes("TPS") || txt.includes("TTFT"));
  console.log(`${timeDialogOk ? "PASS" : "FAIL"} 用时弹窗明细（${txt.replace(/\s+/g, " ").slice(0, 60)}）`);
}

// 产物是零 chunk 单文件（宿主约束，见 vite.config.ts），正常不该有 fetch failed；
// 保留归类是为了让偶发的环境噪音不至于伪装成应用缺陷。
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

const allOk =
  heroOk &&
  settingsOk &&
  checks.every(([, ok]) => ok) &&
  collapsedOk &&
  expandOk &&
  outputOk &&
  railOk &&
  secondTurnOk &&
  statusClockOk &&
  statusCtxOk &&
  railPreviewOk &&
  timePillOk &&
  timeDialogOk &&
  runtimeErrors.length === 0;
console.log(allOk ? "\nSMOKE OK" : "\nSMOKE FAILED");
process.exit(allOk ? 0 : 1);
