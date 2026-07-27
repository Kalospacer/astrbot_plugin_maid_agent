/**
 * tool_chain 解析：把后端 append-only 的 entries（assistant / tool_call / tool_result）
 * 配成可展示的步骤序列。从 1.4.1 的 pages/console/app.js 移植。
 */

export const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "stopping"]);
export const FAILED_STATUSES = new Set(["error", "failed", "stopped", "interrupted"]);
export const STOPPABLE_STATUSES = new Set(["starting", "running"]);

const TRACE_ERROR_PATTERN = /^\s*(error|exception|traceback|失败|错误|超时)/i;

export function stringifyStructuredValue(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function clipTraceValue(text, max) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  if (value.length <= max) return value;
  if (/[\\/]/.test(value)) return `…${value.slice(-(max - 1))}`;
  return `${value.slice(0, max - 1)}…`;
}

export function summarizeToolArgs(args) {
  if (args === undefined || args === null) return "";
  if (typeof args !== "object" || Array.isArray(args)) {
    return clipTraceValue(stringifyStructuredValue(args), 60);
  }
  const keys = Object.keys(args);
  const parts = [];
  for (const key of keys) {
    const value = args[key];
    const isText = typeof value === "string";
    const shown = clipTraceValue(isText ? value : stringifyStructuredValue(value), 40);
    parts.push(isText ? `${key}: "${shown}"` : `${key}: ${shown}`);
    if (parts.length >= 2) break;
  }
  const extra = keys.length - parts.length;
  return extra > 0 ? `${parts.join(", ")}, +${extra}` : parts.join(", ");
}

/**
 * 把 entries 配对成步骤。tool_result 按 tool_call_id 回填到对应 tool_call；
 * 找不到归属的 result 作为 orphan 步骤单列。
 */
export function pairToolChainSteps(entries) {
  const steps = [];
  const byCallId = new Map();

  for (const raw of entries || []) {
    const entry = raw && typeof raw === "object" ? raw : {};
    const kind = entry.kind || "";

    if (kind === "tool_call") {
      const step = {
        type: "tool",
        name: String(entry.tool_name || ""),
        callId: String(entry.tool_call_id || ""),
        argsText: stringifyStructuredValue(entry.arguments ?? entry.message),
        argsSummary: summarizeToolArgs(entry.arguments),
        result: null,
        index: entry.index,
      };
      steps.push(step);
      if (step.callId) {
        if (!byCallId.has(step.callId)) byCallId.set(step.callId, []);
        byCallId.get(step.callId).push(step);
      }
      continue;
    }

    if (kind === "tool_result") {
      const callId = String(entry.tool_call_id || "");
      const text = String(entry.message ?? "");
      const siblings = callId ? byCallId.get(callId) || [] : [];
      const pending = siblings.find((step) => step.result === null);
      if (pending) {
        pending.result = text;
        continue;
      }
      const latest = siblings[siblings.length - 1];
      if (latest) {
        latest.result = `${latest.result ?? ""}\n${text}`.trim();
        continue;
      }
      steps.push({
        type: "tool",
        name: String(entry.tool_name || ""),
        callId,
        argsText: "",
        argsSummary: "",
        result: text,
        index: entry.index,
        orphan: true,
      });
      continue;
    }

    if (kind === "assistant") {
      const text = String(entry.message ?? "").trim();
      if (text) steps.push({ type: "text", text, index: entry.index });
    }
  }

  return steps.map((step, position) => ({
    ...step,
    key: step.callId || `i${step.index ?? position}`,
  }));
}

export function traceStepStatus(step, taskActive) {
  if (step.type === "text") return "text";
  if (step.result !== null && step.result !== undefined) {
    return TRACE_ERROR_PATTERN.test(step.result) ? "error" : "ok";
  }
  return taskActive ? "running" : "dead";
}

export function truncateTraceText(text, { lines = 3, chars = 200 } = {}) {
  const value = String(text ?? "").trim();
  if (!value) return { preview: "", truncated: false, remainLines: 0 };
  const allLines = value.split("\n");
  let preview = allLines.slice(0, lines).join("\n");
  let truncated = allLines.length > lines;
  if (preview.length > chars) {
    preview = preview.slice(0, chars);
    truncated = true;
  }
  return { preview, truncated, remainLines: Math.max(0, allLines.length - lines) };
}

export function countToolSteps(steps) {
  return steps.filter((step) => step.type === "tool").length;
}
