/**
 * AstrBot 插件页桥接封装。
 *
 * 页面跑在 dashboard 的 iframe 里，所有请求都经 window.AstrBotPluginPage 走
 * postMessage 转发给父窗口，由父窗口带鉴权发出。这里只做"桥是否可用"的收口，
 * 请求/响应形状与 1.4.1 保持一致（resolve 出来的就是后端 _console_ok 的 data）。
 */

let bridge = null;

function resolveBridge() {
  bridge = bridge || window.AstrBotPluginPage || null;
  if (!bridge || typeof bridge.apiGet !== "function" || typeof bridge.apiPost !== "function") {
    throw new Error("AstrBot 页面桥接未加载，请从插件页面重新打开控制台。");
  }
  return bridge;
}

export function hasBridge() {
  return Boolean(window.AstrBotPluginPage);
}

export async function ready() {
  return resolveBridge().ready();
}

export async function apiGet(endpoint, params) {
  return resolveBridge().apiGet(endpoint, params);
}

export async function apiPost(endpoint, body) {
  return resolveBridge().apiPost(endpoint, body);
}

export async function uploadFile(endpoint, file) {
  const page = resolveBridge();
  if (typeof page.upload !== "function") {
    throw new Error("当前 AstrBot 页面桥不支持文件上传，请刷新 Dashboard。");
  }
  return page.upload(endpoint, file);
}

export async function download(endpoint, params, filename) {
  const page = resolveBridge();
  if (typeof page.download !== "function") {
    throw new Error("当前 AstrBot 页面桥不支持下载，请刷新 Dashboard。");
  }
  return page.download(endpoint, params, filename);
}

export async function subscribeSSE(endpoint, handlers) {
  const page = resolveBridge();
  if (typeof page.subscribeSSE !== "function") return "";
  return page.subscribeSSE(endpoint, handlers);
}

export async function unsubscribeSSE(subscriptionId) {
  if (!subscriptionId) return;
  const page = window.AstrBotPluginPage;
  if (page && typeof page.unsubscribeSSE === "function") {
    await page.unsubscribeSSE(subscriptionId);
  }
}

/** SSE 消息在不同 bridge 版本下可能是 {parsed} / {raw} / 字符串，统一成对象。 */
export function parseSsePayload(event) {
  if (event?.parsed && typeof event.parsed === "object") return event.parsed;
  const raw = typeof event === "string" ? event : event?.raw || event?.data || "";
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
