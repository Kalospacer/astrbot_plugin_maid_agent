import { useEffect, useRef, useState } from "react";
import clsx from "clsx";

import { Menu, Tooltip } from "@/ui/primitives";
import { IconChevronDownOutline14, IconCloseFill14, IconPlusOutline16 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import type { PromptContentPart, SessionId } from "@/types";
import css from "./Composer.module.css";

const EMPTY_QUEUE: never[] = [];

interface PendingAttachment {
  name: string;
  mediaType: string;
  file: File;
  preview?: string;
}

export function Composer(props: {
  variant: "hero" | "composer";
  running: boolean;
  disabled?: boolean;
}) {
  const current = useApp((s) => s.current);
  const busy = useApp((s) => s.busy);
  const queue = useApp((s) => (s.current ? s.byId.get(s.current) : undefined)?.queue) ?? EMPTY_QUEUE;
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const sendingRef = useRef(false);

  const steering = queue.filter((item) => item.placement === "steering");
  const mode: "queue" | "steer" = props.running ? "steer" : "queue";
  const disabled = props.disabled === true;
  const draftDisabled = disabled || sending;
  const empty = !text.trim() && attachments.length === 0;

  async function onSend() {
    if (sendingRef.current || disabled || busy) return;
    const trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;
    const pendingAttachments = attachments;
    sendingRef.current = true;
    setSending(true);
    setError("");
    setText("");
    setAttachments([]);
    try {
      const target = await app.ensurePromptSession(current);
      const parts: PromptContentPart[] = [];
      if (trimmed) parts.push({ type: "text", text: trimmed });
      for (const attachment of pendingAttachments) {
        const ref = await app.uploadAttachment(target, attachment.file);
        parts.push({ type: "file", attachment: ref });
      }
      await app.sendPrompt(target, parts, mode);
    } catch (error) {
      console.error(error);
      setText(trimmed);
      setAttachments(pendingAttachments);
      setError(error instanceof Error ? error.message : "发送失败，请重试。");
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  }

  async function addFiles(files: FileList | File[]) {
    if (!files || sendingRef.current) {
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    const next: PendingAttachment[] = [];
    for (const file of Array.from(files)) {
      next.push({
        name: file.name || "attachment",
        mediaType: file.type || "application/octet-stream",
        file,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined,
      });
    }
    if (sendingRef.current) {
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    if (next.length) setAttachments((prev) => [...prev, ...next]);
    setError("");
    if (fileRef.current) fileRef.current.value = "";
  }

  return (
    <div className={clsx(css.root, props.variant === "hero" && css.hero)}>
      {steering.length > 0 && (
        <div className={css.notice} role="status">
          {steering.length} 条补充要求等待注入…
        </div>
      )}
      {error !== "" && <div className={css.error} role="alert">{error}</div>}
      <div className={css.card} data-composer-card="">
        {attachments.length > 0 && (
          <div className={css.attachments}>
            {attachments.map((attachment, index) => (
              <span key={`${attachment.name}-${index}`} className={css.chip}>
                {attachment.preview ? <img src={attachment.preview} alt={attachment.name} /> : <span aria-hidden>📎</span>}
                <span className={css.chipName}>{attachment.name}</span>
                <button
                  type="button"
                  className={css.chipRemove}
                  aria-label="移除附件"
                  onClick={() => setAttachments((prev) => prev.filter((_, i) => i !== index))}
                >
                  <IconCloseFill14 />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className={css.scroll} data-input-scroll="">
          <div className={css.grow}>
            <textarea
              className={css.input}
              value={text}
              disabled={draftDisabled}
              rows={2}
              placeholder={
                mode === "steer"
                  ? "补充要求，发送后注入运行中的任务…"
                  : "描述任务，Enter 发送 / Shift+Enter 换行"
              }
              onChange={(e) => {
                setText(e.target.value);
                setError("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void onSend();
                }
              }}
              onPaste={(e) => {
                const files = Array.from(e.clipboardData.files);
                if (files.length > 0) {
                  e.preventDefault();
                  void addFiles(files);
                }
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (e.dataTransfer.files.length > 0) void addFiles(e.dataTransfer.files);
              }}
            />
            <div aria-hidden className={css.mirror} data-input-mirror="">{`${text}\n`}</div>
          </div>
        </div>
        <div className={css.row}>
          <div className={css.tools}>
            <Tooltip label="附加文件" side="top" delayMs={500}>
              <button
                type="button"
                className={css.add}
                aria-label="附加文件"
                disabled={draftDisabled}
                onClick={() => fileRef.current?.click()}
              >
                <IconPlusOutline16 size={14} />
              </button>
            </Tooltip>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              disabled={draftDisabled}
              onChange={(e) => {
                if (e.target.files) void addFiles(e.target.files);
              }}
            />
            {current ? <ModelChip sessionId={current} /> : null}
          </div>
          <div className={css.trailing}>
            {props.running && current ? (
              <Tooltip label="停止" side="top" delayMs={500}>
                <button
                  type="button"
                  className={css.primary}
                  aria-label="停止"
                  onClick={() => void app.cancelTurn(current).catch(() => undefined)}
                >
                  <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
                    <rect x="3" y="3" width="10" height="10" rx="3" fill="currentColor" />
                  </svg>
                </button>
              </Tooltip>
            ) : null}
            <Tooltip label="发送" side="top" delayMs={500}>
              <button
                type="button"
                className={css.primary}
                aria-label="发送"
                disabled={disabled || busy || sending || empty}
                onClick={() => void onSend()}
              >
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden>
                  <path d="M8.3125 0.980183C8.66767 1.0531 8.97902 1.20418 9.2627 1.43233C9.48724 1.61297 9.73029 1.85793 9.97949 2.10714L14.707 6.83468L13.293 8.24874L9 3.95577V15.0417H7V3.95577L2.70703 8.24874L1.29297 6.83468L6.02051 2.10714C6.26971 1.85793 6.51277 1.61297 6.7373 1.43233C6.97662 1.23986 7.28445 1.04402 7.6875 0.980183C7.8973 0.947006 8.1031 0.95516 8.3125 0.980183Z" fill="currentColor" />
                </svg>
              </button>
            </Tooltip>
          </div>
        </div>
      </div>
    </div>
  );
}

function ModelChip(props: { sessionId: SessionId }) {
  const models = useApp((s) => s.byId.get(props.sessionId)?.models ?? null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    void app.loadModels(props.sessionId).catch(() => undefined);
  }, [props.sessionId]);

  // 防御式读取：session.models 一旦返回缺字段的响应（旧后端、异常分支），
  // 直接取 .providers.length / .current.model 会在渲染期抛错，而这是渲染树里
  // 相当靠上的一层——整个控制台会白屏，而不是少一个模型选择器。
  const providers = models?.providers;
  if (!providers || providers.length === 0) return null;
  const current = models.current;
  const label = current?.model || current?.provider || "选择模型";

  return (
    <Menu
      open={open}
      onClose={() => setOpen(false)}
      portal
      side="top"
      anchor={
        <Tooltip label="本会话 subagent 模型" side="top" delayMs={500}>
          <button
            type="button"
            className={css.modelChip}
            aria-label="选择模型"
            aria-haspopup="menu"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <span className={css.modelChipLabel}>{label}</span>
            <IconChevronDownOutline14 size={12} />
          </button>
        </Tooltip>
      }
      items={[
        { id: "__default", label: "默认（跟随子代理配置）" },
        { type: "separator" as const, id: "sep" },
        ...providers.map((p) => ({
          id: p.id,
          label: p.model ? `${p.model}（${p.id}）` : p.id,
        })),
      ]}
      selectedId={current?.override ? current.provider : "__default"}
      onSelect={(id) => {
        setOpen(false);
        void app.selectModel(props.sessionId, id === "__default" ? "" : id).catch(() => undefined);
      }}
    />
  );
}
