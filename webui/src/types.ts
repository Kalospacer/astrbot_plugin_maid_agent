
export type RpcId = string;
export type SessionId = string;

export interface ClientRequest {
  type: "client-request";
  rpcId: RpcId;
  method: string;
  payload: unknown;
}

export interface ServerResponse<T = unknown> {
  type: "server-response";
  rpcId: RpcId;
  result: { ok: true; value: T } | { ok: false; error: { code: string; message: string; details: unknown } };
}

export interface ServerRequest {
  type: "server-request";
  rpcId: RpcId;
  method: string;
  payload: unknown;
}

export interface TextBlock { type: "text"; text: string }
export interface ReasoningBlock { type: "reasoning"; text: string }
export interface ImageAttachmentRef {
  attachmentId: string;
  mediaType: string;
  byteLength: number;
  name?: string;
}
export interface ImageBlock { type: "image"; attachment: ImageAttachmentRef }
export interface ToolCallBlock { type: "tool-call"; id: string; name: string; arguments: string }
export interface ToolResultBlock { type: "tool-result"; toolCallId: string; content: ContentBlock[]; isError?: boolean }

export type ContentBlock = TextBlock | ReasoningBlock | ImageBlock | ToolCallBlock | ToolResultBlock;

export type MessageSource =
  | { kind: "user"; rpcId?: string; clientTimeZone?: string }
  | { kind: "model"; provider: string; model: string }
  | { kind: "tool"; callId: string };

export interface Message {
  id: string;
  role: "system" | "user" | "assistant";
  content: ContentBlock[];
  source: MessageSource;
}

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
}

export type StreamChunk =
  | { type: "block-start"; index: number; blockType: string }
  | { type: "text-delta"; index: number; text: string }
  | { type: "reasoning-delta"; index: number; text: string }
  | { type: "tool-call-delta"; index: number; id: string; name?: string; argumentsDelta: string }
  | { type: "block-end"; index: number; block: ContentBlock }
  | { type: "usage"; usage: TokenUsage }
  | { type: "finish"; reason: { kind: string } };

export type TurnEndReason =
  | { kind: "completed" | "blocked" | "max-tokens" | "interrupted" }
  | { kind: "aborted"; reason: { kind: string } }
  | { kind: "error"; error: { message: string; code?: string } };

export interface SessionEvent {
  type: string;
  seq: number;
  time: number;
  data: any;
  ignorable?: true;
  surfaceOp?: "append" | { op: "replace"; start: number; end: number };
  sourceEventSeqs?: number[];
}

export type ToolCallView =
  | { card: "generic"; title: string; kind?: string; rawInput?: unknown }
  | { card: "terminal"; title: string; description?: string; cwd?: string }
  | { card: "diff"; title: string; diffs: { path: string; oldText: string | null; newText: string }[] };

export type ToolResultView =
  | { card: "generic"; title?: string; content?: ContentBlock[] }
  | { card: "terminal"; title?: string; output?: string; exitCode?: number }
  | { card: "read"; title?: string; path: string; offset: number; lines: { number: number; text: string }[]; totalLines: number; lang?: string }
  | { card: "diff"; title?: string; diffs: { path: string; oldText: string | null; newText: string }[] };

export type ToolEventView =
  | { for: "call"; view: ToolCallView }
  | { for: "result"; view: ToolResultView };

export type MuxFrame =
  | { type: "session/event"; sessionId: SessionId; event: SessionEvent; view?: ToolEventView }
  | { type: "session/subscribed"; sessionId: SessionId; lastSeq: number }
  | { type: "session/queue"; sessionId: SessionId; items: QueuedInboxItem[] }
  | { type: "session/projection"; sessionId: SessionId; key: string; value: unknown; seq: number }
  | { type: "stream/error"; error: { code: string; message: string } };

export interface QueuedInboxItem {
  id: string;
  placement: "queued" | "steering" | "context";
  message: Message;
}

export type SessionSourceKind = "chat" | "dashboard";
export type DeliveryStatus = "pending" | "sending" | "sent" | "failed" | "skipped" | (string & {});

/** Runtime metadata shared by session snapshots and host lifecycle frames. */
export interface SessionRuntimeMetadata {
  sourceKind?: SessionSourceKind;
  dispatchId?: string;
  agentId?: string;
  taskId?: string;
  deliveryStatus?: DeliveryStatus;
}

export type HostFrame =
  | ({ type: "host/session-added"; sessionId: SessionId; blank: boolean; agentPreset?: string } & SessionRuntimeMetadata)
  | { type: "host/session-removed"; sessionId: SessionId }
  | ({ type: "host/session-status"; sessionId: SessionId; running: boolean } & SessionRuntimeMetadata)
  | { type: "host/agent-error"; sessionId: SessionId; message: string }
  | { type: "stream/error"; error: { code: string; message: string } };

export interface SessionSummary extends SessionRuntimeMetadata {
  sessionId: SessionId;
  updatedAt: number;
  running: boolean;
  blank: boolean;
  agentPreset?: string;
  umo?: string;
  projections?: { asOfSeq: number; values: Record<string, unknown> };
}

export interface HistoryEntry {
  event: SessionEvent;
  view?: ToolEventView;
}

export interface AgentPresetEntry {
  id: string;
  trust: "system" | "user";
  name?: string;
  description?: string;
}

export type PromptContentPart =
  | { type: "text"; text: string }
  | { type: "image"; mediaType: string; data: string; name?: string };

export interface SettingsFieldSchema {
  description?: string;
  hint?: string;
  type?: string;
  options?: string[];
}

export interface SettingsNamespaceView {
  ns: string;
  schema: {
    type?: string;
    properties?: Record<string, SettingsFieldSchema>;
  };
  value: Record<string, unknown>;
  applies: "live" | "restart";
  revision: number;
}
