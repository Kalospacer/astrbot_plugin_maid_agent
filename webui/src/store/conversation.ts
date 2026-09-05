
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

/** 本轮的时延/解码吞吐读数（口径对齐 DSH turn-metrics）。 */
export interface TurnMetrics {
  /** turn/start → turn/end 的墙上时间。 */
  runMs: number;
  /** 本轮首个 step 的 step/start → 首个 token delta；无记录时缺省。 */
  ttftMs?: number;
  /** 同时具备解码计时与 provider 用量的 step 上，输出 token / 解码墙上时间。 */
  tokensPerSecond?: number;
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
  /** 中间步骤消息（带工具调用块）：归属轮次过程，随折叠条显隐。 */
  inProcess?: boolean;
  /**
   * 本轮指标，turn/end 时回填到该轮最后一条助手消息上（尾部用时 pill 的数据源）。
   * 之所以回填而不是让消费方按轮号查表：节点是 memo 化的，只有换新对象才会重渲染。
   */
  metrics?: TurnMetrics;
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
  /** turn/end 时填充；运行中为 undefined（折叠条先于过程行创建）。 */
  reason?: any;
  time: number;
  /** 本轮过程统计（折叠条标签）：工具调用数 / 过程内助手消息数。 */
  toolCallCount: number;
  messageCount: number;
  deliveryCount: number;
}

export type ChatNode = UserNode | AssistantNode | ToolNode | TurnTailNode | AssistantPartial;

export interface FoldResult {
  nodes: ChatNode[];
  running: boolean;
  /**
   * nodes 的长度。nodes 是原地增长的（见下方 folder 说明），引用恒定，
   * 消费方无法靠引用比较感知“多了一行”——memo 化的组件会一直 bail out。
   * 流式 chunk 走的是 replaceNode（长度不变），所以用长度当版本号既能
   * 让新增行触发重算，又不会让每个 token 都触发。
   */
  size: number;
  /**
   * 内容版本号：助手消息定稿（assistant/message）时 +1。
   *
   * 光靠 size 不够——流式的最后一步是用定稿节点 replaceNode 掉 partial，长度
   * 不变。只订阅 size 的消费方会停在「partial 刚建、正文还是空」的那一帧，
   * 例如轮次导航轨的预览会一直显示空回复。
   * 按步递增而不是按 token 递增：正文在流式期间不实时跟，但每步定稿即正确。
   */
  contentRevision: number;
  /** 运行中轮次的 turn/start 时刻（工作状态行计时的锚点）；未运行为 undefined。 */
  runningSince?: number;
  /**
   * 当前上下文占用：最近一条带 usage 的助手消息的计费输入
   * （未缓存输入 + 缓存读取 + 缓存写入），即那次请求的 prompt 规模。
   */
  contextTokens?: number;
}

export const EMPTY_FOLD: FoldResult = { nodes: [], running: false, size: 0, contentRevision: 0 };

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
  /** 本轮过程统计（折叠条标签用）。 */
  private turnToolCount = 0;
  private turnMessageCount = 0;
  private turnDeliveryCount = 0;
  /** 本轮折叠条节点：首个过程行出现时创建在行之前（DSH TurnProcess 位置）。 */
  private turnBar: TurnTailNode | null = null;
  /** 助手消息定稿计数（见 FoldResult.contentRevision）。 */
  private contentRevision = 0;
  /** ---- 本轮计时（口径见 TurnMetrics）---- */
  private turnStartedAt: number | null = null;
  private stepStartedAt: number | null = null;
  private stepFirstTokenAt: number | null = null;
  private turnTtftMs: number | undefined = undefined;
  private turnDecodeMs = 0;
  private turnDecodeTokens = 0;
  /** 本轮最后一条“非过程”助手消息：turn/end 时把指标回填到它身上。 */
  private turnLastAssistant: AssistantNode | null = null;
  /** 最近一次 usage 的计费输入，即当前上下文占用。 */
  private contextTokens: number | undefined = undefined;

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
    this.result = {
      nodes: this.nodes,
      running: this.running,
      size: this.nodes.length,
      contentRevision: this.contentRevision,
      runningSince: this.running && this.turnStartedAt !== null ? this.turnStartedAt : undefined,
      contextTokens: this.contextTokens,
    };
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
    this.turnDeliveryCount = 0;
    this.turnBar = null;
    this.contentRevision = 0;
    this.resetTurnTiming();
    this.contextTokens = undefined;
  }

  private resetTurnTiming(): void {
    this.turnStartedAt = null;
    this.stepStartedAt = null;
    this.stepFirstTokenAt = null;
    this.turnTtftMs = undefined;
    this.turnDecodeMs = 0;
    this.turnDecodeTokens = 0;
    this.turnLastAssistant = null;
  }

  private push(node: ChatNode): void {
    this.nodes.push(node);
  }

  private replaceNode(prev: ChatNode, next: ChatNode): void {
    const idx = this.nodes.lastIndexOf(prev);
    if (idx >= 0) this.nodes.splice(idx, 1, next);
    else this.nodes.push(next);
  }

  /**
   * turn/end 时把本轮指标回填到该轮最后一条助手消息上。
   * 换新对象而非原地改：节点视图是 memo 化的，原地改不会重渲染。
   */
  private backfillMetrics(endTime: number): void {
    const target = this.turnLastAssistant;
    this.turnLastAssistant = null;
    if (target === null || this.turnStartedAt === null) return;
    const metrics: TurnMetrics = { runMs: Math.max(0, endTime - this.turnStartedAt) };
    if (this.turnTtftMs !== undefined) metrics.ttftMs = this.turnTtftMs;
    if (this.turnDecodeMs > 0 && this.turnDecodeTokens > 0) {
      metrics.tokensPerSecond = (this.turnDecodeTokens * 1000) / this.turnDecodeMs;
    }
    this.replaceNode(target, { ...target, metrics });
  }

  /** 首个过程行入列前调用：把折叠条插到它前面（用户消息下方）。 */
  private ensureTurnBar(time: number): void {
    if (this.turnBar !== null) return;
    this.turnBar = {
      kind: "turn-tail",
      key: `tb${this.lastTurn}`,
      turn: this.lastTurn,
      time,
      toolCallCount: 0,
      messageCount: 0,
      deliveryCount: 0,
    };
    this.push(this.turnBar);
  }

  private process(event: SessionEvent, views: Map<number, ToolEventView>): void {
    const data = (event.data ?? {}) as Record<string, any>;
    switch (event.type) {
      case "turn/start": {
        this.lastTurn = data.turn ?? this.lastTurn;
        this.running = true;
        this.turnToolCount = 0;
        this.turnMessageCount = 0;
        this.turnDeliveryCount = 0;
        this.turnBar = null;
        this.resetTurnTiming();
        this.turnStartedAt = event.time;
        break;
      }
      case "step/start": {
        this.lastStep = data.step ?? this.lastStep;
        this.stepStartedAt = event.time;
        this.stepFirstTokenAt = null;
        break;
      }
      case "turn/end": {
        this.running = false;
        this.partial = null;
        const reason = data.reason;
        const turn = data.turn ?? this.lastTurn;
        this.backfillMetrics(event.time);
        if (this.turnBar !== null) {
          // 折叠条在轮首已就位，回填计数与终态
          const bar = this.turnBar;
          this.turnBar = null;
          this.replaceNode(bar, {
            ...bar,
            reason,
            time: event.time,
            toolCallCount: this.turnToolCount,
            messageCount: this.turnMessageCount,
            deliveryCount: this.turnDeliveryCount,
          });
        } else if (reason?.kind !== undefined && reason?.kind !== "completed") {
          // 纯文本轮的失败/中断也要有终态行
          this.push({
            kind: "turn-tail",
            key: `t${event.seq}`,
            turn,
            reason,
            time: event.time,
            toolCallCount: 0,
            messageCount: 0,
            deliveryCount: 0,
          });
        }
        break;
      }
      case "user/message": {
        this.partial = null;
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
        // 首个 token delta 落地：本 step 的 TTFT 与解码起点
        if (this.stepFirstTokenAt === null && isTokenDelta(chunk)) {
          this.stepFirstTokenAt = event.time;
          if (this.turnTtftMs === undefined && this.stepStartedAt !== null) {
            this.turnTtftMs = Math.max(0, event.time - this.stepStartedAt);
          }
        }
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
        // 定稿即内容变化：partial 被同长度替换，size 感知不到
        this.contentRevision += 1;
        const turn = data.turn ?? this.lastTurn;
        const step = data.step ?? this.lastStep;
        // 只统计同时具备解码计时与 provider 用量的 step（DSH 口径）
        const outputTokens = Number(data.usage?.outputTokens ?? 0);
        if (this.stepFirstTokenAt !== null && outputTokens > 0) {
          this.turnDecodeMs += Math.max(0, event.time - this.stepFirstTokenAt);
          this.turnDecodeTokens += outputTokens;
        }
        this.stepFirstTokenAt = null;
        const billedInput = billedInputTokens(data.usage);
        if (billedInput !== undefined) this.contextTokens = billedInput;
        const blocks: ContentBlock[] = data.message?.content ?? [];
        if (blocks.length === 0) blocks.push({ type: "text", text: "" });
        // 带工具调用块的是中间步骤消息：归入轮次过程（计一条），受折叠条控制
        const inProcess = blocks.some((b) => b.type === "tool-call");
        if (inProcess) {
          this.ensureTurnBar(event.time);
          this.turnMessageCount += 1;
        }
        const node: AssistantNode = {
          kind: "assistant",
          key: `a${event.seq}`,
          seq: event.seq,
          turn,
          step,
          blocks,
          usage: data.usage,
          time: event.time,
          inProcess: inProcess || undefined,
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
        // 尾部 pill 挂在本轮最后一条“非过程”助手消息上；指标要等 turn/end
        // 才齐（runMs 需要轮尾时间），先记住这条节点，届时回填。
        if (!inProcess) this.turnLastAssistant = node;
        // usage 不单独建行：它挂在本轮最后一条助手消息尾部的 StatPanel 上。
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
        this.ensureTurnBar(event.time);
        this.turnToolCount += 1;
        const view = views.get(event.seq);
        const callView = view && view.for === "call" ? view.view : undefined;
        // migrate 路径 assistant/message 已按 tool-call 块建行：只补视图，不重复建行
        const existing = this.openTools.get(data.callId);
        if (existing) {
          const merged: ToolNode = { ...existing, callView: callView ?? existing.callView };
          this.openTools.set(data.callId, merged);
          this.replaceNode(existing, merged);
          break;
        }
        const toolNode: ToolNode = {
          kind: "tool",
          key: `tc${event.seq}`,
          callId: data.callId,
          name: data.name,
          arguments: data.arguments ?? "",
          callView,
          callTime: event.time,
          turn: data.turn ?? this.lastTurn,
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
            turn: data.turn ?? this.lastTurn,
          });
        }
        break;
      }
      case "maid/delivery": {
        // 投递本身不建行，只计入本轮过程统计（折叠条标签的“N 次投递”）。
        if (this.running || this.turnBar !== null) {
          this.ensureTurnBar(event.time);
          this.turnDeliveryCount += 1;
        }
        break;
      }
      default:
        break;
    }
  }
}

/** token 增量 chunk（text/reasoning delta）；usage 之类的控制 chunk 不算。 */
function isTokenDelta(chunk: any): boolean {
  return chunk?.type === "text-delta" || chunk?.type === "reasoning-delta";
}

/**
 * 三个不相交的计费输入桶之和 = 那次请求的 prompt 规模 = 当前上下文占用。
 * usage 缺失或全为 0 时返回 undefined，避免把“没数据”显示成 0。
 */
function billedInputTokens(usage: any): number | undefined {
  if (usage === undefined || usage === null) return undefined;
  const total =
    Number(usage.inputTokens ?? 0) +
    Number(usage.cacheReadTokens ?? 0) +
    Number(usage.cacheWriteTokens ?? 0);
  return total > 0 ? total : undefined;
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
