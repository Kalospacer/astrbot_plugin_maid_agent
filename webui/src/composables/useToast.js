import { reactive } from "vue";

/**
 * 全局 toast。单条、后来居上，2.6s 自动消失 —— 与 1.4.1 行为一致。
 */
export const toastState = reactive({
  message: "",
  visible: false,
  tone: "info",
});

let timer = 0;

export function toast(message, tone = "info") {
  const text = String(message || "").trim();
  if (!text) return;
  toastState.message = text;
  toastState.tone = tone;
  toastState.visible = true;
  window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    toastState.visible = false;
  }, 2600);
}

export function toastError(err, fallback = "操作失败") {
  toast(err?.message || fallback, "error");
}
