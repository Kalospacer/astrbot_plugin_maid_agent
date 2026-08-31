import { useEffect, useState } from "react";

import { Button, Input, Modal } from "@/ui/primitives";
import { useApp } from "@/hooks";
import * as app from "@/store/app";
import css from "./SettingsDialog.module.css";

export function SettingsDialog(props: { open: boolean; onClose: () => void }) {
  const settings = useApp((s) => s.settings);
  const theme = useApp((s) => s.theme);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    if (!props.open) return;
    setDraft(null);
    setError("");
    setLoading(true);
    app
      .refreshSettings()
      .catch((exc: any) => setError(exc?.message ?? String(exc)))
      .finally(() => setLoading(false));
  }, [props.open]);

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
    <Modal
      open={props.open}
      title="插件设置"
      onClose={props.onClose}
      className={css.dialog}
      contentClassName={css.scroll}
      footer={
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={props.onClose}>
            取消
          </Button>
          <Button variant="primary" onClick={() => void onSave()} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </div>
      }
    >
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
        {settings !== null && Object.entries(value).map(([key, current]) => {
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
    </Modal>
  );
}
