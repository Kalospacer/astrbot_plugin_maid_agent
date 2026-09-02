// 工具行模型：变体分类 + 摘要/正文推导，对齐 DSH ui-tool models/tool-call-model.ts。
// 输入是 maid 事件流的 ToolNode（服务端 view 附带 card 字段），输出 ToolRow 所需字段。

import type { ToolNode } from "@/store/conversation";

export type ToolRowVariant = "search" | "read" | "bash" | "write" | "edit" | "code" | "others";
export type ToolRowState = "running" | "ok" | "error" | "stopped";

/** 工具名 → 变体（DSH TOOL_VARIANTS，maid 场景按 AstrBot 工具名映射）。 */
const TOOL_VARIANTS: Record<string, ToolRowVariant> = {
  bash: "bash",
  pwsh: "bash",
  shell: "bash",
  run_command: "bash",
  read: "read",
  read_file: "read",
  view_file: "read",
  get_file_content: "read",
  web_fetch: "read",
  visit_webpage: "read",
  web_search: "search",
  grep: "search",
  glob: "search",
  search: "search",
  find: "search",
  write: "write",
  write_file: "write",
  create_file: "write",
  edit: "edit",
  edit_file: "edit",
  apply_patch: "edit",
  run_code: "code",
};

/** 摘要键优先序（DSH SUMMARY_KEYS）。 */
const SUMMARY_KEYS: Record<ToolRowVariant, readonly string[]> = {
  bash: ["description", "command"],
  read: ["path", "file_path", "url"],
  search: ["query", "pattern", "url"],
  write: ["path", "file_path"],
  edit: ["path", "file_path"],
  code: ["description"],
  others: [],
};

const FILE_PATH_KEYS = ["path", "file_path"] as const;
const FILE_PATH_VARIANTS: ReadonlySet<ToolRowVariant> = new Set(["read", "write", "edit"]);

export function classifyTool(toolName: string): ToolRowVariant {
  const lowered = (toolName || "").toLowerCase();
  for (const [needle, variant] of Object.entries(TOOL_VARIANTS)) {
    if (lowered === needle || lowered.startsWith(`${needle}_`) || lowered.endsWith(`_${needle}`)) {
      return variant;
    }
  }
  return "others";
}

/** 变体标题（DSH tool.title.* zh 模板）。 */
export const VARIANT_TITLES: Record<ToolRowVariant, string> = {
  search: "搜索",
  read: "读取",
  bash: "Bash",
  write: "写入",
  edit: "编辑",
  code: "代码",
  others: "工具调用",
};

function parseArgs(argsRaw: string): unknown {
  if (argsRaw === "") return undefined;
  try {
    return JSON.parse(argsRaw);
  } catch {
    return undefined;
  }
}

function firstLine(text: string): string {
  const nl = text.indexOf("\n");
  return nl === -1 ? text : text.slice(0, nl);
}

function pickString(args: Record<string, unknown>, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const v = args[key];
    if (typeof v === "string" && v !== "") return v;
  }
  return undefined;
}

function deriveSummary(variant: ToolRowVariant, argsRaw: string): string {
  const parsed = parseArgs(argsRaw);
  if (typeof parsed !== "object" || parsed === null) return firstLine(argsRaw);
  const args = parsed as Record<string, unknown>;
  if (variant === "search" && Array.isArray(args.queries)) {
    const queries = (args.queries as unknown[]).filter(
      (q): q is string => typeof q === "string" && q !== "",
    );
    if (queries.length > 0) return queries.map(firstLine).join(", ");
  }
  const picked = pickString(args, SUMMARY_KEYS[variant]);
  if (picked !== undefined) return firstLine(picked);
  for (const v of Object.values(args)) {
    if (typeof v === "string" && v !== "") return firstLine(v);
  }
  return firstLine(argsRaw);
}

function deriveFilePath(variant: ToolRowVariant, argsRaw: string): string | undefined {
  if (!FILE_PATH_VARIANTS.has(variant)) return undefined;
  const parsed = parseArgs(argsRaw);
  if (typeof parsed !== "object" || parsed === null) return undefined;
  const picked = pickString(parsed as Record<string, unknown>, FILE_PATH_KEYS);
  return picked === undefined ? undefined : firstLine(picked);
}

/** 展开时格式化参数正文（DSH formatToolBody）：others 变体收 JSON 包络。 */
export function formatToolBody(argsRaw: string): string | null {
  if (argsRaw === "") return null;
  const parsed = parseArgs(argsRaw);
  if (parsed === undefined) return argsRaw;
  return JSON.stringify(parsed, null, 2);
}

export interface ToolRowModel {
  variant: ToolRowVariant;
  title: string;
  summary: string;
  bodyRaw: string | null;
  output: string | null;
  errorSummary: string | null;
  state: ToolRowState;
  filePath: string | undefined;
}

/** 从 ToolNode 推导完整行模型。 */
export function toolRowModel(node: ToolNode): ToolRowModel {
  const variant = classifyTool(node.name);
  const done = node.resultSeq !== undefined;
  const argsRaw = node.arguments ?? "";
  const state: ToolRowState = !done
    ? "running"
    : node.isError
      ? "error"
      : "ok";
  const base = deriveSummary(variant, argsRaw);
  const summary = variant === "others" && node.name !== "" ? `${node.name} · ${base}` : base;
  const output = done ? node.resultText ?? null : null;
  const errorSummary = state === "error" && output !== null ? firstLine(output) : null;
  return {
    variant,
    title: VARIANT_TITLES[variant],
    summary,
    bodyRaw: argsRaw === "" ? null : argsRaw,
    output,
    errorSummary,
    state,
    filePath: deriveFilePath(variant, argsRaw),
  };
}
