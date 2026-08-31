
import { hasBridge, rpcPost, subscribeStream, unsubscribeStream } from "./bridge";
import type {
  ClientRequest,
  HostFrame,
  MuxFrame,
  ServerResponse,
} from "@/types";

export class RpcFailure extends Error {
  code: string;
  details: unknown;

  constructor(code: string, message: string, details?: unknown) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

let rpcSeq = 0;

export async function call<T = unknown>(method: string, payload: unknown = {}): Promise<T> {
  if (!hasBridge()) {
    throw new RpcFailure("internal", "页面桥接未加载。");
  }
  const rpcId = `rpc_${Date.now().toString(36)}_${++rpcSeq}`;
  const envelope: ClientRequest = { type: "client-request", rpcId, method, payload };
  const response = (await rpcPost(method, envelope)) as ServerResponse<T> | undefined;
  if (!response || response.type !== "server-response" || response.rpcId !== rpcId) {
    throw new RpcFailure("internal", "响应信封不合法。");
  }
  if (!response.result.ok) {
    throw new RpcFailure(
      response.result.error.code,
      response.result.error.message,
      (response.result.error as { details?: unknown }).details,
    );
  }
  return response.result.value;
}

export type ConnectionState = "connecting" | "connected" | "reconnecting";

export interface StreamHandlers {
  onState: (state: ConnectionState) => void;
  onMuxFrame?: (frame: MuxFrame) => void;
  onHostFrame?: (frame: HostFrame) => void;
  onReconnected?: () => void;
}

export class ConnectionController {
  private handlers: StreamHandlers;
  private muxSub = "";
  private hostSub = "";
  private stopped = false;
  private retryTimer: number | null = null;
  private retryDelay = 500;

  constructor(handlers: StreamHandlers) {
    this.handlers = handlers;
  }

  async start(): Promise<void> {
    this.stopped = false;
    this.handlers.onState("connecting");
    await this.connect();
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    try {
      this.muxSub = await subscribeStream("api/events.mux", {
        onOpen: () => {
          this.retryDelay = 500;
          this.handlers.onState("connected");
        },
        onMessage: (event) => this.dispatch(event, "mux"),
        onError: () => this.scheduleReconnect(),
      });
      this.hostSub = await subscribeStream("api/events.host", {
        onMessage: (event) => this.dispatch(event, "host"),
        onError: () => this.scheduleReconnect(),
      });
      this.handlers.onState("connected");
      this.handlers.onReconnected?.();
    } catch {
      this.scheduleReconnect();
    }
  }

  private dispatch(event: { raw?: string; parsed?: unknown }, stream: "mux" | "host"): void {
    const raw = typeof event.parsed === "object" && event.parsed !== null ? event.parsed : null;
    const envelope = (raw ?? this.tryParse(event.raw)) as {
      type?: string;
      method?: string;
      payload?: unknown;
    } | null;
    if (!envelope || typeof envelope.type !== "string") return;
    let frame = envelope as { type: string } & Record<string, unknown>;
    if (envelope.type === "server-request") {
      if (typeof envelope.method !== "string") return;
      const payload = (envelope.payload ?? {}) as Record<string, unknown>;
      frame = { ...payload, type: typeof payload.type === "string" ? payload.type : envelope.method } as typeof frame;
    }
    if (stream === "mux") {
      this.handlers.onMuxFrame?.(frame as unknown as MuxFrame);
    } else {
      this.handlers.onHostFrame?.(frame as unknown as HostFrame);
    }
  }

  private tryParse(raw: unknown): unknown {
    if (typeof raw !== "string" || !raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    this.handlers.onState("reconnecting");
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    const jitter = 0.75 + Math.random() * 0.5;
    const delay = Math.min(10_000, Math.round(this.retryDelay * jitter));
    this.retryDelay = Math.min(10_000, this.retryDelay * 2);
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      void this.reconnect();
    }, delay);
  }

  private async reconnect(): Promise<void> {
    await this.dropSubs();
    await this.connect();
  }

  private async dropSubs(): Promise<void> {
    const subs = [this.muxSub, this.hostSub];
    this.muxSub = "";
    this.hostSub = "";
    for (const sub of subs) {
      if (sub) await unsubscribeStream(sub).catch(() => undefined);
    }
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    await this.dropSubs();
  }
}
