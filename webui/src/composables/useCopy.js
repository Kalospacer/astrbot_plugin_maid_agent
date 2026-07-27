import { toast } from "@/composables/useToast";

/**
 * 复制到剪贴板。
 *
 * 页面跑在 dashboard 的 iframe 里，navigator.clipboard 可能因为
 * permissions-policy 不可用，所以保留 execCommand 兜底。
 */
export async function copyText(text, label = "已复制") {
  const value = String(text ?? "");
  if (!value) return false;

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      toast(label);
      return true;
    }
  } catch {
    /* 落到下面的兜底 */
  }

  try {
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    toast(ok ? label : "复制失败，请手动选中", ok ? "info" : "error");
    return ok;
  } catch {
    toast("复制失败，请手动选中", "error");
    return false;
  }
}
