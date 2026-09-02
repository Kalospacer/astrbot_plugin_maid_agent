
import type { ContentBlock, SessionEvent, ToolEventView } from "@/types";

export interface AssistantPartial {
  kind: "assistant-partial";
  key: string;
  turn: number;
  step: number;
  blocks: { kind: "text" | "reasoning"; text: string }[];
  startedAt: number;
}

export interface UserNode {
  kind: "user";
  key: string;
  seq: number;
  message: { content: ContentBlock[]; source: any };
  time: number;
}

export interface AssistantNode {
  kind: "assistant";
  key: string;
  seq: number;
  turn: number;
  step: number;
  blocks: ContentBlock[];
  usage?: any;
  time: number;
}

export interface ToolNode {
  kind: "tool";
  key: string;
  callId: string;
  name: string;
  arguments: string;
  callView?: any;
  resultSeq?: number;
  resultText?: string;
  isError?: boolean;
  resultView?: any;
  callTime: number;
  resultTime?: number;
  /** 所属轮次（过程折叠归属）。 */
  turn: number;
}

export interface TurnTailNode {
  kind: "turn-tail";
  key: string;
  turn: number;
  reason: any;
  time: number;
  /** 本轮过程统计（DSH turn-process 折叠条标签）：工具调用数 / 助手消息数。 */
  toolCallCount: number;
  messageCount: number;
  /** 本轮首个 user 节点 seq（折叠条展开定位锚）。 */
  startSeq: number | null;
}

export interface UsageNode {
  kind: "usage";
  key: string;
  turn: number;
  step: number;
  usage: any;
  time: number;
}

export type ChatNode = UserNode | AssistantNode | ToolNode | TurnTailNode | AssistantPartial | UsageNode;

export interface FoldResult {
  nodes: ChatNode[];
  running: boolean;
}

export const EMPTY_FOLD: FoldResult = { nodes: [], running: false };

/**
 * 增量会话折叠器。
 *
 * 旧实现每个流式 chunk 都对全部事件重新 fold（O(n)/token），长会话下抖动明显。
 * 这里的 folder 常驻在 SessionState 上，事件按 seq 单调追加时只处理新增事件；
 * 节点对象在未被替换时保持引用稳定，配合 React.memo 避免整列重渲染。
 * 检测到历史前置加载（出现比已处理窗口更早的 seq）、事件收缩或追溯性 view
 * 变化时，自动退化为全量重折。
 */
export class ConversationFolder {
  private nodes: ChatNode[] = [];
  private openTools = new Map<string, ToolNode>();
  private partial: AssistantPartial | null = null;
  private running = false;
  private lastTurn = 0;
  private lastStep = 0;
  private minSeq = Number.POSITIVE_INFINITY;
  private maxSeq = -1;
  private processed = 0;
  private viewsCount = 0;
  private result: FoldResult = EMPTY_FOLD;
  /** 本轮过程统计（turn-tail 标签用）。 */
  private turnToolCount = 0;
  private turnMessageCount = 0;
  private turnStartSeq: number | null = null;

  ingest(events: Map<number, SessionEvent>, views: Map<number, ToolEventView>): FoldResult {
    let fresh: SessionEvent[] | null = [];
    if (events.size < this.processed) {
      fresh = null; // 事件收缩（重建/清空）→ 全量重折
    } else {
      for (const event of events.values()) {
        if (event.seq > this.maxSeq) {
          fresh.push(event);
        } else if (event.seq < this.minSeq) {
          fresh = null; // 历史前置加载 → 全量重折
          break;
        }
      }
      // view 追溯性变化但没有新事件（理论上不发生，防御性处理）
      if (fresh !== null && fresh.length === 0 && views.size !== this.viewsCount) {
        fresh = null;
      }
    }

    if (fresh === null) {
      this.reset();
      fresh = [...events.values()];
    }
    if (fresh.length === 0) return this.result;

    fresh.sort((a, b) => a.seq - b.seq);
    for (const event of fresh) {
      this.process(event, views);
      if (event.seq < this.minSeq) this.minSeq = event.seq;
      if (event.seq > this.maxSeq) this.maxSeq = event.seq;
    }
    this.processed = events.size;
    this.viewsCount = views.size;
    this.result = { nodes: this.nodes, running: this.running };
    return this.result;
  }

  private reset(): void {
    this.nodes = [];
    this.openTools.clear();
    this.partial = null;
    this.running = false;
    this.lastTurn = 0;
    this.lastStep = 0;
    this.minSeq = Number.POSITIVE_INFINITY;
    this.maxSeq = -1;
    this.processed = 0;
    this.viewsCount = 0;
    this.turnToolCount = 0;
    this.turnMessageCount = 0;
    this.turnStartSeq = null;
  }

  private push(node: ChatNode): void {
    this.nodes.push(node);
  }

  private replaceNode(prev: ChatNode, next: ChatNode): void {
    const idx = this.nodes.lastIndexOf(prev);
    if (idx >= 0) this.nodes.splice(idx, 1, next);
    else this.nodes.push(next);
  }

  private process(event: SessionEvent, views: Map<number, ToolEventView>): void {
    const data = (event.data ?? {}) as Record<string, any>;
    switch (event.type) {
      case "turn/start": {
        this.lastTurn = data.turn ?? this.lastTurn;
        this.running = true;
        this.turnToolCount = 0;
        this.turnMessageCount = 0;
        this.turnStartSeq = null;
        break;
      }
      case "turn/end": {
        this.running = false;
        this.partial = null;
        this.push({
          kind: "turn-tail",
          key: `t${event.seq}`,
          turn: data.turn ?? this.lastTurn,
          reason: data.reason,
          time: event.time,
          toolCallCount: this.turnToolCount,
          messageCount: this.turnMessageCount,
          startSeq: this.turnStartSeq,
        });
        break;
      }
      case "user/message": {
        this.partial = null;
        if (this.turnStartSeq === null) this.turnStartSeq = event.seq;
        this.push({
          kind: "user",
          key: `u${event.seq}`,
          seq: event.seq,
          message: { content: data.content ?? [], source: data.source ?? { kind: "user" } },
          time: event.time,
        });
        break;
      }
      case "assistant/chunk": {
        const chunk = data.chunk;
        if (!chunk) break;
        const turn = data.turn ?? this.lastTurn;
        const step = data.step ?? this.lastStep;
        let partial = this.partial;
        if (!partial || partial.turn !== turn || partial.step !== step) {
          partial = { kind: "assistant-partial", key: `p${event.seq}`, turn, step, blocks: [], startedAt: event.time };
          this.partial = partial;
          this.push(partial);
        }
        // 复制一份再写入，保证 memo 化的节点视图能感知文本增长
        const next: AssistantPartial = { ...partial, blocks: partial.blocks.map((b) => ({ ...b })) };
        applyChunk(next, chunk);
        this.partial = next;
        this.replaceNode(partial, next);
        break;
      }
      case "assistant/message": {
        this.turnMessageCount += 1;
        const turn = data.turn ?? this.lastTurn;
        const step = data.step ?? this.lastStep;
        const blocks: ContentBlock[] = data.message?.content ?? [];
        if (blocks.length === 0) blocks.push({ type: "text", text: "" });
        const node: AssistantNode = {
          kind: "assistant",
          key: `a${event.seq}`,
          seq: event.seq,
          turn,
          step,
          blocks,
          usage: data.usage,
          time: event.time,
        };
        const partial = this.partial;
        if (partial) {
          this.partial = null;
          const sameStep = partial.turn === turn && partial.step === step;
          if (sameStep) {
            this.replaceNode(partial, node);
          } else {
            const idx = this.nodes.lastIndexOf(partial);
            if (idx >= 0) this.nodes.splice(idx, 1);
            this.push(node);
          }
        } else {
          this.push(node);
        }
        if (data.usage) {
          this.push({
            kind: "usage",
            key: `s${event.seq}`,
            turn,
            step,
            usage: data.usage,
            time: event.time,
          });
        }
        for (const block of blocks) {
          if (block.type === "tool-call" && !this.openTools.has(block.id)) {
            const toolNode: ToolNode = {
              kind: "tool",
              key: `tc${event.seq}-${block.id}`,
              callId: block.id,
              name: block.name,
              arguments: block.arguments,
              callTime: event.time,
              turn,
            };
            this.openTools.set(block.id, toolNode);
            this.push(toolNode);
          }
        }
        break;
      }
      case "tool/call": {
        this.lastStep = data.step ?? this.lastStep;
        this.turnToolCount += 1;
        const view = views.get(event.seq);
        const toolNode: ToolNode = {
          kind: "tool",
          key: `tc${event.seq}`,
          callId: data.callId,
          name: data.name,
          arguments: data.arguments ?? "",
          callView: view && view.for === "call" ? view.view : undefined,
          callTime: event.time,
          turn: this.lastTurn,
        };
        this.openTools.set(data.callId, toolNode);
        this.push(toolNode);
        break;
      }
      case "tool/result": {
        const callId = data.message?.source?.callId ?? "";
        // contracts.tool_result_message 的正文在 tool-result 块内层：
        // content = [{ type: "tool-result", toolCallId, content: [textBlock] }]
        const blocks = (data.message?.content ?? []) as any[];
        const text = blocks
          .flatMap((b: any) => (b?.type === "tool-result" ? b.content ?? [] : [b]))
          .filter((b: any) => b?.type === "text")
          .map((b: any) => b.text ?? "")
          .join("\n");
        const view = views.get(event.seq);
        const open = this.openTools.get(callId);
        if (open) {
          // 替换而非原地修改，保证 memo 化的工具行重新渲染
          const updated: ToolNode = {
            ...open,
            resultSeq: event.seq,
            resultText: text,
            isError: Boolean(data.error),
            resultView: view && view.for === "result" ? view.view : undefined,
            resultTime: event.time,
          };
          this.openTools.set(callId, updated);
          this.replaceNode(open, updated);
        } else {
          this.push({
            kind: "tool",
            key: `tr${event.seq}`,
            callId,
            name: data.error?.name ?? "",
            arguments: "",
            resultText: text,
            isError: Boolean(data.error),
            resultView: view && view.for === "result" ? view.view : undefined,
            callTime: event.time,
            turn: this.lastTurn,
          });
        }
        break;
      }
      default:
        break;
    }
  }
}

/** 兼容入口：一次性全量折叠。 */
export function foldConversation(
  events: SessionEvent[],
  views: Map<number, ToolEventView>,
): FoldResult {
  const folder = new ConversationFolder();
  return folder.ingest(new Map(events.map((event) => [event.seq, event])), views);
}

function applyChunk(partial: AssistantPartial, chunk: any): void {
  if (chunk.type === "text-delta" || chunk.type === "reasoning-delta") {
    const kind = chunk.type === "text-delta" ? "text" : "reasoning";
    let block = partial.blocks.find((b) => b.kind === kind);
    if (!block) {
      block = { kind, text: "" };
      partial.blocks.push(block);
    }
    block.text += chunk.text ?? "";
  }
}
