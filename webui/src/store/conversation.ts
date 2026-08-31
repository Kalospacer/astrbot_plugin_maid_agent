
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
}

export interface TurnTailNode {
  kind: "turn-tail";
  key: string;
  turn: number;
  reason: any;
  time: number;
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

interface OpenTool {
  node: ToolNode;
}

export function foldConversation(
  events: SessionEvent[],
  views: Map<number, ToolEventView>,
): { nodes: ChatNode[]; running: boolean } {
  const nodes: ChatNode[] = [];
  const openTools = new Map<string, OpenTool>();
  let partial: AssistantPartial | null = null;
  let running = false;
  let lastTurn = 0;
  let lastStep = 0;

  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  for (const event of ordered) {
    const data = event.data ?? {};
    switch (event.type) {
      case "turn/start": {
        lastTurn = data.turn ?? lastTurn;
        running = true;
        break;
      }
      case "turn/end": {
        running = false;
        partial = null;
        nodes.push({
          kind: "turn-tail",
          key: `t${event.seq}`,
          turn: data.turn ?? lastTurn,
          reason: data.reason,
          time: event.time,
        });
        break;
      }
      case "user/message": {
        partial = null;
        nodes.push({
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
        const turn = data.turn ?? lastTurn;
        const step = data.step ?? lastStep;
        if (!partial || partial.turn !== turn || partial.step !== step) {
          partial = { kind: "assistant-partial", key: `p${event.seq}`, turn, step, blocks: [], startedAt: event.time };
          nodes.push(partial);
        }
        applyChunk(partial, chunk);
        break;
      }
      case "assistant/message": {
        const turn = data.turn ?? lastTurn;
        const step = data.step ?? lastStep;
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
        if (partial) {
          const idx = nodes.lastIndexOf(partial);
          const sameStep = partial.turn === turn && partial.step === step;
          partial = null;
          if (idx >= 0 && sameStep) nodes.splice(idx, 1, node);
          else {
            if (idx >= 0) nodes.splice(idx, 1);
            nodes.push(node);
          }
        } else {
          nodes.push(node);
        }
        if (data.usage) {
          nodes.push({
            kind: "usage",
            key: `s${event.seq}`,
            turn,
            step,
            usage: data.usage,
            time: event.time,
          });
        }
        for (const block of blocks) {
          if (block.type === "tool-call" && !openTools.has(block.id)) {
            const node: ToolNode = {
              kind: "tool",
              key: `tc${event.seq}-${block.id}`,
              callId: block.id,
              name: block.name,
              arguments: block.arguments,
              callTime: event.time,
            };
            openTools.set(block.id, { node });
            nodes.push(node);
          }
        }
        break;
      }
      case "tool/call": {
        lastStep = data.step ?? lastStep;
        const view = views.get(event.seq);
        const node: ToolNode = {
          kind: "tool",
          key: `tc${event.seq}`,
          callId: data.callId,
          name: data.name,
          arguments: data.arguments ?? "",
          callView: view && view.for === "call" ? view.view : undefined,
          callTime: event.time,
        };
        openTools.set(data.callId, { node });
        nodes.push(node);
        break;
      }
      case "tool/result": {
        const open = openTools.get(data.message?.source?.callId ?? "");
        const text = (data.message?.content ?? [])
          .filter((b: any) => b.type === "text")
          .map((b: any) => b.text)
          .join("\n");
        const view = views.get(event.seq);
        if (open) {
          open.node.resultSeq = event.seq;
          open.node.resultText = text;
          open.node.isError = Boolean(data.error);
          open.node.resultView = view && view.for === "result" ? view.view : undefined;
          open.node.resultTime = event.time;
        } else {
          nodes.push({
            kind: "tool",
            key: `tr${event.seq}`,
            callId: data.message?.source?.callId ?? "",
            name: data.error?.name ?? "",
            arguments: "",
            resultText: text,
            isError: Boolean(data.error),
            resultView: view && view.for === "result" ? view.view : undefined,
            callTime: event.time,
          });
        }
        break;
      }
      default:
        break;
    }
  }
  return { nodes, running };
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
