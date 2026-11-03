import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";

import { call } from "@/api/client";
import { Button, DisclosureRow, Modal } from "@/ui/primitives";
import { IconListPenOutline16 } from "@/ui/primitives/icons";
import * as app from "@/store/app";
import type { SessionId } from "@/types";
import css from "./DecisionsPanel.module.css";

/** 路由判断记录（harness/admission.py DecisionStore 写入，api 补上任务名与规则名）。 */
interface Decision {
  id: string;
  at: number;
  kind: "accept" | "reopen" | "stop" | "ignore";
  reason: string;
  taskId: string;
  trigger: string;
  matched: string[];
  matchedNames: string[];
  taskDescription?: string;
  sessionId?: string;
  form?: Record<string, unknown>;
}

type Filter = "all" | "work" | "stop" | "ignore";

const FILTERS: { id: Filter; label: string; kinds: string[] }[] = [
  { id: "all", label: "全部", kinds: [] },
  { id: "work", label: "派发", kinds: ["accept", "reopen"] },
  { id: "stop", label: "停止", kinds: ["stop"] },
  { id: "ignore", label: "忽略", kinds: ["ignore"] },
];

const KIND_LABELS: Record<Decision["kind"], string> = {
  accept: "派发",
  reopen: "恢复",
  stop: "停止",
  ignore: "忽略",
};

const REASON_LABELS: Record<string, string> = {
  explicitRouted: "有人发消息派发",
  humanStop: "有人叫停",
  directionUnsettled: "方向未定",
  noRuleMatched: "没有命中规则",
};

const PAGE = 30;

function formatTime(at: number): string {
  return new Date(at).toLocaleString();
}

function DecisionLine(props: { decision: Decision; onOpen?: (sessionId: SessionId) => void }) {
  const { decision } = props;
  const title = decision.taskDescription || decision.trigger || "（无内容）";
  const reason = REASON_LABELS[decision.reason] ?? decision.reason;
  const target = decision.sessionId;
  return (
    <li className={css.row}>
      <span className={clsx(css.mark, css[decision.kind])}>{KIND_LABELS[decision.kind]}</span>
      <div className={css.body}>
        {target && props.onOpen ? (
          <button type="button" className={css.title} onClick={() => props.onOpen?.(target as SessionId)}>
            {title}
          </button>
        ) : (
          <span className={css.title}>{title}</span>
        )}
        {decision.trigger && decision.trigger !== title ? <div className={css.quote}>{decision.trigger}</div> : null}
        <div className={css.meta}>
          {reason ? <span>{reason}</span> : null}
          {decision.matchedNames.length > 0 ? <span>规则：{decision.matchedNames.join("、")}</span> : null}
          <span>{formatTime(decision.at)}</span>
        </div>
        {decision.form ? <pre className={css.form}>{JSON.stringify(decision.form, null, 2)}</pre> : null}
      </div>
    </li>
  );
}

/** 判断页：按种类筛选、可限最近 24 小时，每次多加载 30 条（对应 superagent client/eval.tsx）。 */
export function DecisionsDialog(props: { open: boolean; onClose: () => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [recent, setRecent] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  const [items, setItems] = useState<Decision[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!props.open) return;
    const kinds = FILTERS.find((row) => row.id === filter)?.kinds ?? [];
    call<{ items: Decision[]; nextOffset: number | null }>("decision.list", {
      kinds,
      limit,
      ...(recent ? { window: "24h" } : {}),
    })
      .then((page) => {
        setItems(page.items);
        setMore(page.nextOffset !== null);
        setError("");
      })
      .catch((exc: any) => setError(exc?.message ?? String(exc)));
  }, [props.open, filter, recent, limit]);

  const open = useCallback(
    (sessionId: SessionId) => {
      props.onClose();
      void app.selectSession(sessionId);
    },
    [props.onClose],
  );

  return (
    <Modal open={props.open} onClose={props.onClose} title="判断记录" closeLabel="关闭" className={css.dialog}>
      <div className={css.toolbar}>
        {FILTERS.map((row) => (
          <Button
            key={row.id}
            variant={row.id === filter ? "primary" : "outline"}
            onClick={() => {
              setFilter(row.id);
              setLimit(PAGE);
            }}
          >
            {row.label}
          </Button>
        ))}
        <label className={css.recent}>
          <input type="checkbox" checked={recent} onChange={(e) => setRecent(e.target.checked)} />
          最近 24 小时
        </label>
      </div>
      {error ? <p className={css.empty}>{error}</p> : null}
      {!error && items.length === 0 ? <p className={css.empty}>没有记录。</p> : null}
      <ul className={css.list}>
        {items.map((decision) => (
          <DecisionLine key={decision.id} decision={decision} onOpen={open} />
        ))}
      </ul>
      {more ? (
        <Button variant="outline" onClick={() => setLimit((value) => value + PAGE)}>
          加载更多
        </Button>
      ) : null}
    </Modal>
  );
}

/** 会话视图里可展开的判断记录：这个管家会话上任务的派发、恢复与停止。 */
export function SessionDecisions(props: { sessionId: SessionId }) {
  const [items, setItems] = useState<Decision[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setItems([]);
    setOpen(false);
    call<{ items: Decision[] }>("session.decisions", { sessionId: props.sessionId })
      .then((page) => setItems(page.items))
      .catch(() => setItems([]));
  }, [props.sessionId]);

  if (items.length === 0) return null;
  return (
    <div className={css.session}>
      <DisclosureRow
        icon={<IconListPenOutline16 size={16} />}
        title={`判断记录（${items.length}）`}
        open={open}
        expandable
        expandOnRowClick
        onToggle={() => setOpen((value) => !value)}
      >
        <ul className={css.list}>
          {items.map((decision) => (
            <DecisionLine key={decision.id} decision={decision} />
          ))}
        </ul>
      </DisclosureRow>
    </div>
  );
}
