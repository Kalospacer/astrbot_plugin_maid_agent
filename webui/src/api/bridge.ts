
type BridgeHandlers = {
  onOpen?: () => void;
  onMessage: (event: { raw?: string; parsed?: unknown; eventType?: string }) => void;
  onError?: () => void;
};

interface Bridge {
  ready(): Promise<unknown>;
  apiGet(endpoint: string, params?: unknown): Promise<any>;
  apiPost(endpoint: string, body?: unknown): Promise<any>;
  upload(endpoint: string, file: File): Promise<any>;
  download(endpoint: string, params?: unknown, filename?: string): Promise<any>;
  subscribeSSE(endpoint: string, handlers: BridgeHandlers, params?: unknown): Promise<string>;
  unsubscribeSSE(subscriptionId: string): Promise<void>;
}

declare global {
  interface Window {
    AstrBotPluginPage?: Bridge;
  }
}

let bridge: Bridge | null = null;

function resolveBridge(): Bridge {
  bridge = bridge ?? window.AstrBotPluginPage ?? null;
  if (!bridge || typeof bridge.apiGet !== "function" || typeof bridge.apiPost !== "function") {
    throw new Error("AstrBot 页面桥接未加载，请从插件页面重新打开控制台。");
  }
  return bridge;
}

export function hasBridge(): boolean {
  return Boolean(window.AstrBotPluginPage);
}

export async function ready(): Promise<void> {
  await resolveBridge().ready();
}

export async function rpcPost(method: string, payload: unknown) {
  const response = (await resolveBridge().apiPost(`api/${method}`, payload)) as any;
  return response;
}

export async function subscribeStream(
  endpoint: "api/events.mux" | "api/events.host",
  handlers: BridgeHandlers,
): Promise<string> {
  const page = resolveBridge();
  if (typeof page.subscribeSSE !== "function") {
    throw new Error("当前 AstrBot 页面桥不支持 SSE 订阅，请刷新 Dashboard。");
  }
  return page.subscribeSSE(endpoint, handlers);
}

export async function unsubscribeStream(subscriptionId: string): Promise<void> {
  if (!subscriptionId) return;
  const page = window.AstrBotPluginPage;
  if (page && typeof page.unsubscribeSSE === "function") {
    await page.unsubscribeSSE(subscriptionId);
  }
}
