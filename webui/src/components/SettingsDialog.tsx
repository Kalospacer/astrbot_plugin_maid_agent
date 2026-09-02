import { useEffect, useId, useRef, useState } from "react";

import { Button, Input, Modal } from "@/ui/primitives";
import {
  IconCloseOutline16,
  IconDataOutline16,
  IconSettingsOutline16,
  IconThinkOutline16,
} from "@/ui/primitives/icons";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import css from "./SettingsDialog.module.css";

/** 设置分区（DSH SettingsRoot nav 行）：代理女仆只有两个分区。 */
type SectionId = "general" | "about";

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "general", label: "通用" },
  { id: "about", label: "关于" },
];

function navIcon(id: SectionId) {
  if (id === "about") return <IconDataOutline16 size={16} />;
  return <IconSettingsOutline16 size={16} />;
}

export function SettingsDialog(props: { open: boolean; onClose: () => void }) {
  const settings = useApp((s) => s.settings);
  const theme = useApp((s) => s.theme);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [activeId, setActiveId] = useState<SectionId>("general");
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!props.open) return;
    setDraft(null);
    setError("");
    setActiveId("general");
    setLoading(true);
    app
      .refreshSettings()
      .catch((exc: any) => setError(exc?.message ?? String(exc)))
      .finally(() => setLoading(false));
    // 打开时焦点给关闭按钮（DSH SettingsPanel）；挂载后再取
    const timer = window.setTimeout(() => closeRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [props.open]);

  useEffect(() => {
    if (!props.open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [props.open, props.onClose]);

  if (!props.open) return null;
  const value = draft ?? { ...(settings?.value ?? {}) };
  const schema = (settings?.schema ?? {}) as {
    properties?: Record<string, { description?: string; type?: string; hint?: string }>;
  };
  const properties = schema?.properties ?? {};

  async function onSave() {
    setSaving(true);
    setError("");
    try {
      await app.saveSettings(value as Record<string, unknown>);
      props.onClose();
    } catch (exc: any) {
      setError(exc?.message ?? String(exc));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={css.overlay} role="presentation">
      <div className={css.mask} aria-hidden onClick={props.onClose} />
      <div className={css.panel} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <nav className={css.nav}>
          <div className={css.navTitle} id={titleId}>
            设置
          </div>
          <div className={css.navList}>
            {SECTIONS.map((row) => (
              <button
                key={row.id}
                type="button"
                className={`${css.navCell}${row.id === activeId ? ` ${css.active}` : ""}`}
                aria-current={row.id === activeId ? "true" : undefined}
                onClick={() => setActiveId(row.id)}
              >
                {navIcon(row.id)}
                <span className={css.navLabel}>{row.label}</span>
              </button>
            ))}
          </div>
        </nav>
        <div className={css.content}>
          <div className={css.header}>
            <div className={css.headerSpacer} />
            <button ref={closeRef} type="button" className={css.close} onClick={props.onClose}>
              <IconCloseOutline16 size={14} />
              <span className={css.hiddenLabel}>关闭</span>
            </button>
          </div>
          <div className={css.options}>
            {activeId === "general" ? (
              <div className={css.form}>
                <div className={css.field}>
                  <span className={css.label}>主题</span>
                  <div className={css.themeRow}>
                    <Button
                      variant={theme === "light" ? "primary" : "outline"}
                      onClick={() => app.setTheme("light")}
                    >
                      浅色
                    </Button>
                    <Button
                      variant={theme === "dark" ? "primary" : "outline"}
                      onClick={() => app.setTheme("dark")}
                    >
                      深色
                    </Button>
                  </div>
                </div>
                {settings === null ? (
                  <p className="muted" style={{ fontSize: 12.5 }}>
                    {loading ? "正在获取插件配置…" : "插件配置不可用（settings.describe 失败）。"}
                  </p>
                ) : null}
                {settings !== null &&
                  Object.entries(value).map(([key, current]) => {
                    const meta = properties[key] ?? {};
                    const label = meta.description ?? key;
                    if (typeof current === "boolean") {
                      return (
                        <label key={key} className={css.checkRow}>
                          <span>{label}</span>
                          <input
                            type="checkbox"
                            className={css.checkbox}
                            checked={current}
                            onChange={(e) => setDraft({ ...value, [key]: e.target.checked })}
                          />
                        </label>
                      );
                    }
                    return (
                      <label key={key} className={css.field}>
                        <span className={css.label}>{label}</span>
                        {Array.isArray(current) ? (
                          <Input
                            value={(current as string[]).join(", ")}
                            placeholder="逗号分隔"
                            onChange={(e) =>
                              setDraft({
                                ...value,
                                [key]: String(e.target.value)
                                  .split(",")
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              })
                            }
                          />
                        ) : typeof current === "number" ? (
                          <Input
                            type="number"
                            value={String(current)}
                            onChange={(e) => setDraft({ ...value, [key]: Number(e.target.value) })}
                          />
                        ) : key === "dispatch_prompt_template" ? (
                          <textarea
                            className={css.textarea}
                            value={String(current ?? "")}
                            rows={4}
                            onChange={(e) => setDraft({ ...value, [key]: e.target.value })}
                          />
                        ) : (
                          <Input
                            value={String(current ?? "")}
                            onChange={(e) => setDraft({ ...value, [key]: e.target.value })}
                          />
                        )}
                      </label>
                    );
                  })}
                {error ? <p className="error-text">{error}</p> : null}
              </div>
            ) : (
              <div className={css.form}>
                <div className={css.aboutRow}>
                  <IconThinkOutline16 size={16} />
                  <span>代理女仆 · AstrBot 插件</span>
                </div>
                <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
                  前端界面基于 Kimi 设计语言。
                </p>
              </div>
            )}
          </div>
          {activeId === "general" ? (
            <div className={css.footer}>
              <Button variant="ghost" onClick={props.onClose}>
                取消
              </Button>
              <Button variant="primary" onClick={() => void onSave()} disabled={saving}>
                {saving ? "保存中…" : "保存"}
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
