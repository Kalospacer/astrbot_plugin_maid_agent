import { useState, lazy, Suspense } from "react";

import {
  DiffBlock,
  DisclosureRow,
  TerminalBlock,
} from "@/ui/primitives";
import type { ToolNode } from "@/store/conversation";

// ReadBlock 依赖 shiki 高亮栈，懒加载以保持入口 chunk 精简
const ReadBlock = lazy(() =>
  import("@/ui/primitives/ReadBlock.tsx").then((m) => ({ default: m.ReadBlock })),
);

const PARAMS_MAX_CHARS = 20_000;

export function ToolNodeView(props: { node: ToolNode }) {
  const node = props.node;
  const [open, setOpen] = useState(false);
  const done = node.resultSeq !== undefined;
  const title = node.callView?.title ?? node.resultView?.title ?? (node.name || "工具调用");

  return (
    <div className="tool-card">
      <div className="tool-card-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <DisclosureRow title={String(title)} icon={null} expandable open={open} onToggle={() => setOpen((v) => !v)}>
            {open ? <ToolBody node={node} /> : null}
          </DisclosureRow>
        </div>
        {done ? (
          node.isError ? <span className="tool-status error-text">失败</span> : <span className="tool-status muted">完成</span>
        ) : (
          <span className="tool-status muted">运行中…</span>
        )}
      </div>
    </div>
  );
}

function ToolBody(props: { node: ToolNode }) {
  const node = props.node;

  const resultView = node.resultView;
  if (resultView?.card === "terminal") {
    return (
      <div className="tool-card-body">
        <TerminalBlock
          command={resultView.title ?? node.name}
          output={resultView.output ?? node.resultText ?? ""}
          exitCode={resultView.exitCode}
        />
      </div>
    );
  }
  if (resultView?.card === "read") {
    return (
      <div className="tool-card-body">
        <Suspense
          fallback={
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12 }}>
              {(resultView.lines ?? []).map((l: any) => l.text ?? "").join("\n")}
            </pre>
          }
        >
          <ReadBlock
            label={resultView.path}
            lines={resultView.lines}
            totalLines={resultView.totalLines}
            lang={resultView.lang}
          />
        </Suspense>
      </div>
    );
  }
  const diffView =
    resultView?.card === "diff"
      ? resultView
      : node.callView?.card === "diff"
        ? node.callView
        : undefined;
  if (diffView) {
    return (
      <div className="tool-card-body">
        <DiffBlock
          diffs={diffView.diffs.map((d: any) => ({ path: d.path, oldText: d.oldText ?? "", newText: d.newText }))}
        />
      </div>
    );
  }
  return (
    <div className="tool-card-body">
      {node.arguments ? (
        <>
          <div className="muted" style={{ fontSize: 12 }}>参数</div>
          <pre style={{ margin: "2px 0 0", whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12 }}>
            {formatArguments(node.arguments)}
          </pre>
        </>
      ) : null}
      {node.resultText !== undefined ? (
        <pre style={{ margin: "6px 0 0", whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12 }}>
          {node.resultText}
        </pre>
      ) : null}
    </div>
  );
}

function formatArguments(raw: string): string {
  let text = raw;
  try {
    text = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    /* 非 JSON 原样展示 */
  }
  return text.length > PARAMS_MAX_CHARS
    ? `${text.slice(0, PARAMS_MAX_CHARS)}\n… 已截断，共 ${text.length} 字符`
    : text;
}
