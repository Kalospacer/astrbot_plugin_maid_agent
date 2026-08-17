import { useRef, useState } from "react";
import clsx from "clsx";

import { Tooltip } from "@/ui/primitives";
import { IconCloseFill14, IconPanelLeftOutline16, IconPlusOutline16 } from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import type { PromptContentPart } from "@/types";
import css from "./Composer.module.css";

const EMPTY_QUEUE: never[] = [];

interface PendingImage {
  name: string;
  mediaType: string;
  data: string; // base64
  preview: string;
}

/** 输入条：附件轨 + 文本 + 工具行（+ 附件，发送/停止）。
 *  常驻同一树位：hero（空会话居中）与 composer（吸附底部）只是 variant 之差。 */
export function Composer(props: {
  variant: "hero" | "composer";
  running: boolean;
  disabled?: boolean;
  detailsOpen?: boolean;
  onToggleDetails?: () => void;
}) {
  const current = useApp((s) => s.current);
  const busy = useApp((s) => s.busy);
  const queue = useApp((s) => (s.current ? s.byId.get(s.current) : undefined)?.queue) ?? EMPTY_QUEUE;
  const [text, setText] = useState("");
  const [images, setImages] = useState<PendingImage[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const steering = queue.filter((item) => item.placement === "steering");
  const mode: "queue" | "steer" = props.running ? "steer" : "queue";
  const disabled = props.disabled === true;
  const empty = !text.trim() && images.length === 0;

  async function onSend() {
    const trimmed = text.trim();
    if (!trimmed && images.length === 0) return;
    const parts: PromptContentPart[] = [];
    if (trimmed) parts.push({ type: "text", text: trimmed });
    for (const image of images) {
      parts.push({ type: "image", mediaType: image.mediaType, data: image.data, name: image.name });
    }
    setText("");
    setImages([]);
    try {
      await app.sendPrompt(current, parts, mode);
    } catch (error) {
      console.error(error);
      setText(trimmed); // 发送失败退回输入框
    }
  }

  async function onPickImages(files: FileList | null) {
    if (!files) return;
    const next: PendingImage[] = [];
    for (const file of Array.from(files).slice(0, 5)) {
      if (!file.type.startsWith("image/")) continue;
      const data = await fileToBase64(file);
      next.push({ name: file.name, mediaType: file.type, data, preview: `data:${file.type};base64,${data}` });
    }
    if (next.length) setImages((prev) => [...prev, ...next].slice(0, 5));
    if (fileRef.current) fileRef.current.value = "";
  }

  return (
    <div className={clsx(css.root, props.variant === "hero" && css.hero)}>
      {steering.length > 0 && (
        <div className={css.notice} role="status">
          {steering.length} 条补充要求等待注入…
        </div>
      )}
      <div className={css.card} data-composer-card="">
        {images.length > 0 && (
          <div className={css.attachments}>
            {images.map((image, index) => (
              <span key={`${image.name}-${index}`} className={css.chip}>
                <img src={image.preview} alt={image.name} />
                <span className={css.chipName}>{image.name}</span>
                <button
                  type="button"
                  className={css.chipRemove}
                  aria-label="移除附件"
                  onClick={() => setImages((prev) => prev.filter((_, i) => i !== index))}
                >
                  <IconCloseFill14 />
                </button>
              </span>
            ))}
          </div>
        )}
        {/* 一个滚动口、两层文本：隐藏镜像渲染 draft+'\n' 把栈撑到草稿全高，
            绝对定位的 textarea 骑在这个高度上；.scroll 是唯一滚动的盒子。 */}
        <div className={css.scroll} data-input-scroll="">
          <div className={css.grow}>
            <textarea
              className={css.input}
              value={text}
              disabled={disabled}
              rows={2}
              placeholder={
                mode === "steer"
                  ? "补充要求，发送后注入运行中的任务…"
                  : "描述任务，Enter 发送 / Shift+Enter 换行"
              }
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void onSend();
                }
              }}
            />
            <div aria-hidden className={css.mirror} data-input-mirror="">{`${text}\n`}</div>
          </div>
        </div>
        <div className={css.row}>
          <div className={css.tools}>
            <Tooltip label="附加图片" side="top" delayMs={500}>
              <button
                type="button"
                className={css.add}
                aria-label="附加图片"
                disabled={disabled}
                onClick={() => fileRef.current?.click()}
              >
                <IconPlusOutline16 size={14} />
              </button>
            </Tooltip>
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              multiple
              hidden
              onChange={(e) => void onPickImages(e.target.files)}
            />
          </div>
          <div className={css.trailing}>
            {props.onToggleDetails ? (
              <Tooltip label={props.detailsOpen ? "收起详情" : "会话详情"} side="top" delayMs={500}>
                <button
                  type="button"
                  className={css.add}
                  aria-label={props.detailsOpen ? "收起详情" : "会话详情"}
                  aria-pressed={props.detailsOpen}
                  onClick={props.onToggleDetails}
                >
                  {/* 面板图标镜像朝右（详情列在右） */}
                  <span style={{ display: "inline-flex", transform: "scaleX(-1)" }}>
                    <IconPanelLeftOutline16 size={16} />
                  </span>
                </button>
              </Tooltip>
            ) : null}
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
                disabled={disabled || busy || empty}
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

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result ?? "");
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
