import type { DeliveryStatus, SessionRuntimeMetadata } from "@/types";

type RuntimeSummary = SessionRuntimeMetadata & { umo?: string };

function isDashboardSession(summary: RuntimeSummary): boolean {
  return summary.sourceKind === "dashboard" ||
    (summary.sourceKind === undefined && summary.umo?.startsWith("dashboard:") === true);
}

export function sessionRuntimeBadge(summary: RuntimeSummary): string | undefined {
  if (isDashboardSession(summary)) return "独立沙箱";
  if (summary.sourceKind === "chat") return summary.executionMode === "background" ? "后台" : "前台";
  return undefined;
}

export function sessionRuntimeDescription(summary: RuntimeSummary): string | undefined {
  if (isDashboardSession(summary)) return "控制台会话 · 独立沙箱";
  if (summary.sourceKind !== "chat") return undefined;
  return summary.executionMode === "background" ? "聊天会话 · 后台运行" : "聊天会话 · 前台运行";
}

export function deliveryStatusLabel(status: DeliveryStatus | undefined): string | undefined {
  if (!status) return undefined;
  const labels: Record<string, string> = {
    pending: "待投递",
    sending: "正在投递",
    sent: "已发送",
    failed: "投递失败",
    skipped: "无需投递",
  };
  return labels[status] ?? status;
}
