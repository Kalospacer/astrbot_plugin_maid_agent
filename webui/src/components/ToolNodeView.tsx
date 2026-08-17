import { useState } from "react";

import {
  DiffBlock,
  DisclosureRow,
  ReadBlock,
  TerminalBlock,
} from "@/ui/primitives";
import type { ToolNode } from "@/store/conversation";

const PARAMS_MAX_CHARS = 20_000;

/** 工具节点：按视图词表（terminal/read/diff/generic）渲染卡片。 */
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
        <ReadBlock
          label={resultView.path}
          lines={resultView.lines}
          totalLines={resultView.totalLines}
          lang={resultView.lang}
        />
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

/** 参数内联展示：外层工具卡本身已是一层折叠，参数不再套第二层折叠块。 */
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
