import { Button, JsonTree } from "@/ui/primitives";
import { IconBranchOutline16 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import { getSnapshot } from "@/store/app";

export function DetailsPanel(props: { onClose?: () => void }) {
  // session 数据原地修改，订阅 stamp；stamp 不变时面板完全不重渲染
  useApp((s) => {
    if (!s.current) return "";
    return `${s.current}:${s.byId.get(s.current)?.stamp ?? -1}`;
  });
  const state = getSnapshot();
  const session = state.current ? state.byId.get(state.current) : undefined;

  if (!session) {
    return (
      <div className="details">
        <p className="muted">选择一个会话查看详情。</p>
      </div>
    );
  }

  const projections = session.projections as Record<string, any>;
  const stats = projections.sessionStats ?? {};
  const usage = projections.tokenUsage ?? {};
  const events = [...session.events.values()].sort((a, b) => b.seq - a.seq);

  return (
    <div className="details">
      <div className="details-head">
        <span className="details-title">详情</span>
        {props.onClose ? (
          <button type="button" className="details-close" aria-label="关闭详情" onClick={props.onClose}>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
              <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        ) : null}
      </div>
      <div className="details-body">
      <div className="details-section">
        <h3>会话</h3>
        <dl className="details-kv">
          <dt>ID</dt>
          <dd>{session.sessionId.slice(0, 16)}…</dd>
          <dt>Agent</dt>
          <dd>{session.summary.agentPreset ?? "—"}</dd>
          <dt>状态</dt>
          <dd>{session.summary.running ? "运行中" : "空闲"}</dd>
        </dl>
        <div className="row" style={{ marginTop: 8 }}>
          <Button variant="ghost" onClick={() => void app.forkSession(session.sessionId).catch(() => undefined)}>
            <IconBranchOutline16 />
            <span>Fork</span>
          </Button>
        </div>
      </div>

      <div className="details-section">
        <h3>统计</h3>
        <dl className="details-kv">
          <dt>回合 / 步骤</dt>
          <dd>
            {stats.turns ?? 0} / {stats.steps ?? 0}
          </dd>
          <dt>LLM / 工具耗时</dt>
          <dd>
            {fmtMs(stats.llmMs)} / {fmtMs(stats.toolMs)}
          </dd>
          <dt>首 token</dt>
          <dd>{fmtMs(stats.ttftMs)}</dd>
          <dt>tokens</dt>
          <dd>
            ↑{usage.uncachedInputTokens ?? 0} ↓{usage.outputTokens ?? 0}
            {usage.cacheReadTokens ? ` 缓存${usage.cacheReadTokens}` : ""}
          </dd>
        </dl>
      </div>

      <div className="details-section">
        <h3>事件流（{events.length}）</h3>
        <JsonTree data={events.slice(0, 30)} label="events" expandTopLevel={false} />
      </div>
      </div>
    </div>
  );
}

function fmtMs(ms: unknown): string {
  const value = Number(ms) || 0;
  if (value >= 60_000) return `${Math.round(value / 600) / 10}m`;
  if (value >= 1_000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}
